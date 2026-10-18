"""Evidence bound to what was checked: the contract, the snapshot, the check and the output of every check_result,
and proofs in mission_finished that verify() re-checks."""

import hashlib
import json
import shutil
import sys
from unittest import mock

from agentic import checks, faults, loop, report
from agentic.ledger import Ledger, LedgerError, canonical, compute_hash, manifest_digest
from agentic.mission import contract_sha256, load_mission
from helpers import Crash, SupervisorTestCase, fix, make_mission, results
from test_mutation import GOOD, ONE_STEP, STRONG, WEAK, write_calc

LOUD_C1 = {"argv": [sys.executable, "-c", "import pathlib, sys\nprint('x' * 10000)\n"
                    "sys.exit(0 if pathlib.Path('c1.txt').read_text() == 'ok' else 1)"], "timeout": 30}


def forge(path, mutate) -> None:
    """Rewrite the ledger at `path` with `mutate(records)` applied, as a forger would: every hash and prev is
    recomputed, and each proof names its check_result by the new hash (unless the proof's hash is "keep"), so only
    the binding can tell."""
    records = Ledger(path).read()
    mutate(records)
    prev = "0" * 64
    for rec in records:
        rec["prev"] = prev
        if rec["type"] == "mission_finished":
            for proof in rec["data"].get("proofs", []):
                if 1 <= proof["seq"] < rec["seq"] and proof["hash"] != "keep":
                    proof["hash"] = records[proof["seq"] - 1]["hash"]
        rec["hash"] = compute_hash(rec)
        prev = rec["hash"]
    path.write_bytes(b"".join((canonical(r) + "\n").encode("utf-8") for r in records))


def find(records, record_type, **match):
    return [r for r in records if r["type"] == record_type and all(r["data"].get(k) == v for k, v in match.items())]


class CheckResultBindingTests(SupervisorTestCase):
    def test_check_results_carry_snapshot_check_contract_and_full_output_hashes(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(make_mission(checks={"c1": LOUD_C1})), "complete")
        recs = self.records()
        mission = load_mission(self.cfg.mission_file("demo"))
        started = recs[0]["data"]
        self.assertEqual(started["contract_sha256"], contract_sha256(mission))
        c1, c2 = find(recs, "check_result", iteration=1)
        for rec in (c1, c2):
            self.assertEqual(rec["data"]["contract_sha256"], started["contract_sha256"])
            self.assertEqual(rec["data"]["check_sha256"], started["check_sha256"][rec["data"]["criterion"]])
        full = ("x" * 10000 + "\n").encode()
        self.assertEqual((c1["data"]["stdout_sha256"], c1["data"]["stdout_bytes"]),
                         (hashlib.sha256(full).hexdigest(), len(full)))
        self.assertLess(len(c1["data"]["stdout"]), len(full), "the ledger keeps a tail; the hash covers it all")
        self.assertEqual(c1["data"]["stderr_bytes"], 0)
        # One iteration, one snapshot: the worker folder's manifest before any check file was written.
        work_manifest = checks.tree_manifest(self.cfg.workdir("demo"))
        self.assertEqual(c1["data"]["snapshot_sha256"], c2["data"]["snapshot_sha256"])
        self.assertEqual(c1["data"]["snapshot_sha256"], manifest_digest(work_manifest))
        finished = recs[-1]["data"]
        self.assertEqual(finished["proofs"], [{"criterion": r["data"]["criterion"], "seq": r["seq"],
                                               "hash": r["hash"]} for r in (c1, c2)])
        self.assertEqual(finished["final_snapshot_sha256"], c1["data"]["snapshot_sha256"])
        self.assertEqual(finished["final_manifest"], work_manifest)
        verdict = Ledger(self.cfg.ledger_file("demo")).verify()
        self.assertEqual((verdict.ok, verdict.bound), (True, True))

    def test_a_folder_that_changes_between_checks_reruns_once_then_stops_unstable(self):
        real = checks.run_check
        calls = []

        def drifting(cfg, mission_id, iteration, cid, check, workdir):
            result = real(cfg, mission_id, iteration, cid, check, workdir)
            if isinstance(iteration, int) and iteration >= 1:
                calls.append(cid)
                (workdir / "drift.txt").write_text(str(len(calls)), encoding="utf-8")
            return result

        self.play(fix("c1", "c2"))
        with mock.patch.object(checks, "run_check", drifting):
            self.assertEqual(self.run_mission(), "stopped_unstable")
        recs = self.records()
        notices = find(recs, "notice", kind=loop.CHECKS_UNSTABLE)
        self.assertEqual([n["data"]["attempt"] for n in notices], [1, 2])
        self.assertEqual(calls, ["c1", "c2", "c1", "c2"], "the checks ran once more, then the mission stopped")
        self.assertEqual(find(recs, "check_result"), [], "no check_result from an unstable iteration")
        self.assertEqual(recs[-1]["data"]["status"], "stopped_unstable")
        self.assertEqual(recs[-1]["data"]["proofs"], [])
        self.assertTrue(Ledger(self.cfg.ledger_file("demo")).verify().ok)
        text = self.report_text()
        self.assertIn("검사 불안정으로 중단", text)
        self.assertEqual(results(text, "✅"), ["없음"])

    def test_a_single_drift_is_cured_by_the_rerun(self):
        real = checks.run_check
        drifted = []

        def drift_once(cfg, mission_id, iteration, cid, check, workdir):
            result = real(cfg, mission_id, iteration, cid, check, workdir)
            if iteration == 1 and not drifted:
                drifted.append(cid)
                (workdir / "drift.txt").write_text("late write", encoding="utf-8")
            return result

        self.play(fix("c1", "c2"))
        with mock.patch.object(checks, "run_check", drift_once):
            self.assertEqual(self.run_mission(), "complete")
        recs = self.records()
        self.assertEqual([n["data"]["attempt"] for n in find(recs, "notice", kind=loop.CHECKS_UNSTABLE)], [1])
        snapshots = {r["data"]["snapshot_sha256"] for r in find(recs, "check_result", iteration=1)}
        self.assertEqual(len(snapshots), 1)


class ContractTests(SupervisorTestCase):
    def crash_after_first_iteration(self) -> None:
        def hook(point, record):
            if point == faults.APPENDED and record["type"] == "iteration_worker":
                faults.HOOK = None
                raise Crash
        self.play(fix("c1"), fix("c2"))
        with mock.patch.object(faults, "HOOK", hook):
            with self.assertRaises(Crash):
                self.run_mission()

    def test_resume_refuses_an_edited_mission_file(self):
        self.crash_after_first_iteration()
        path = self.cfg.mission_file("demo")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["criteria"][0]["check"]["timeout"] = 31
        path.write_text(json.dumps(raw), encoding="utf-8")
        before = path.parent.joinpath("ledger.jsonl").read_bytes()
        with self.assertRaisesRegex(LedgerError, "mission file changed"):
            loop.run("demo", self.cfg)
        self.assertEqual(self.cfg.ledger_file("demo").read_bytes(), before, "nothing was appended")
        self.assertNotEqual(self.cli("run", "demo"), 0)
        self.assertIn("mission file changed", self.err)

    def test_resume_accepts_the_same_contract_written_differently(self):
        self.crash_after_first_iteration()
        path = self.cfg.mission_file("demo")
        path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=4), encoding="utf-8")
        self.assertEqual(loop.run("demo", self.cfg), "complete")

    def test_checks_update_records_the_old_and_new_contract(self):
        self.plan_script(script={"checks": [{"checks": {"c1": STRONG}}]})
        self.play(write_calc(GOOD))
        self.assertEqual(self.run_mission(make_mission(("c1",), checks={"c1": WEAK}, ladder=ONE_STEP)), "complete")
        recs = self.records()
        (update,) = find(recs, "checks_update")
        (done,) = find(recs, "checks_strengthened")
        self.assertEqual(update["data"]["old_contract_sha256"], recs[0]["data"]["contract_sha256"])
        new = contract_sha256(load_mission(self.cfg.mission_file("demo")))
        self.assertEqual((update["data"]["new_contract_sha256"], done["data"]["contract_sha256"]), (new, new))
        last = find(recs, "check_result")[-1]
        self.assertEqual((last["data"]["contract_sha256"], last["data"]["check_sha256"]),
                         (new, update["data"]["new_sha256"]))
        self.assertTrue(Ledger(self.cfg.ledger_file("demo")).verify().ok)


class ProofTests(SupervisorTestCase):
    """Two iterations: c1 passes in both, c2 fails in the first and passes in the second."""

    def setUp(self):
        super().setUp()
        self.play(fix("c1"), fix("c2"))
        self.assertEqual(self.run_mission(), "complete")
        self.path = self.cfg.ledger_file("demo")
        self.recs = self.records()
        self.finished_seq = self.recs[-1]["seq"]

    def seq_of(self, iteration, cid):
        (rec,) = find(self.recs, "check_result", iteration=iteration, criterion=cid)
        return rec["seq"]

    def set_proof(self, cid, seq):
        def mutate(records):
            for proof in records[-1]["data"]["proofs"]:
                if proof["criterion"] == cid:
                    proof["seq"] = seq
        forge(self.path, mutate)

    def assert_broken(self, reason):
        verdict = Ledger(self.path).verify()
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.bad_seq, self.finished_seq)
        self.assertIn(reason, verdict.reason)
        with self.assertRaises(LedgerError):
            Ledger(self.path).read()
        self.assertEqual(self.cli("verify", "demo"), 2)

    def test_the_genuine_ledger_verifies(self):
        self.assertEqual([p["seq"] for p in self.recs[-1]["data"]["proofs"]],
                         [self.seq_of(2, "c1"), self.seq_of(2, "c2")])
        self.assertTrue(Ledger(self.path).verify().ok)

    def test_a_proof_naming_another_criterions_result_is_rejected(self):
        self.set_proof("c1", self.seq_of(2, "c2"))
        self.assert_broken("names a check_result of c2")

    def test_a_proof_naming_a_failed_result_is_rejected(self):
        self.set_proof("c2", self.seq_of(1, "c2"))
        self.assert_broken("did not pass")

    def test_a_proof_from_an_earlier_iteration_is_rejected(self):
        self.set_proof("c1", self.seq_of(1, "c1"))
        self.assert_broken("from iteration 1")

    def test_a_proof_with_another_check_hash_is_rejected(self):
        seq = self.seq_of(2, "c1")

        def mutate(records):
            records[seq - 1]["data"]["check_sha256"] = "f" * 64
        forge(self.path, mutate)
        self.assert_broken("another check")

    def test_a_proof_on_another_snapshot_is_rejected(self):
        def mutate(records):
            records[-1]["data"]["final_snapshot_sha256"] = "e" * 64
            records[-1]["data"].pop("final_manifest")
        forge(self.path, mutate)
        self.assert_broken("another snapshot")

    def test_a_proof_with_a_wrong_hash_is_rejected(self):
        def mutate(records):
            records[-1]["data"]["proofs"][0]["hash"] = "keep"
        forge(self.path, mutate)
        self.assert_broken("by its hash")

    def test_complete_without_every_proof_is_rejected(self):
        def mutate(records):
            records[-1]["data"]["proofs"].pop()
        forge(self.path, mutate)
        self.assert_broken("complete without a proof")

    def test_a_missing_proof_is_never_a_pass_in_the_report(self):
        def mutate(records):
            records[-1]["data"]["status"] = "partial"
            records[-1]["data"]["proofs"] = [p for p in records[-1]["data"]["proofs"] if p["criterion"] == "c1"]
        forge(self.path, mutate)
        self.assertTrue(Ledger(self.path).verify().ok)
        text = report.build(self.cfg, "demo", Ledger(self.path).read()).text
        passed = results(text, "✅")
        self.assertTrue(any("c1" in line for line in passed))
        self.assertFalse(any("c2" in line for line in passed), "c2's latest check passed, but it has no proof")
        self.assertTrue(any("c2" in line and "통과 증거가 없음" in line for line in results(text, "⚠️")))

    def test_an_old_ledger_without_binding_verifies_and_is_labelled_unbound(self):
        def mutate(records):
            for key in ("contract_sha256", "check_sha256"):
                records[0]["data"].pop(key)
            for rec in records:
                for key in ("snapshot_sha256", "check_sha256", "contract_sha256", "stdout_sha256", "stdout_bytes",
                            "stderr_sha256", "stderr_bytes"):
                    rec["data"].pop(key, None)
            for key in ("proofs", "final_snapshot_sha256", "final_manifest"):
                records[-1]["data"].pop(key)
        forge(self.path, mutate)
        verdict = Ledger(self.path).verify()
        self.assertEqual((verdict.ok, verdict.bound), (True, False))
        self.assertEqual(self.cli("verify", "demo"), 0)
        self.assertIn("ledger is unbound", self.out)
        self.assertNotIn("work folder", self.out, "an unbound ledger has no snapshot to compare with")
        text = report.build(self.cfg, "demo", Ledger(self.path).read()).text
        self.assertEqual(results(text, "✅"), ["없음"])
        self.assertEqual(sum("예전 원장" in line for line in results(text, "⚠️")), 2)


class VerifyFolderTests(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(), "complete")
        self.work = self.cfg.workdir("demo")

    def test_unchanged(self):
        self.assertEqual(self.cli("verify", "demo"), 0)
        self.assertIn("work folder: unchanged since the final check snapshot", self.out)

    def test_changed_lists_at_most_ten_paths(self):
        (self.work / "c1.txt").write_text("edited", encoding="utf-8")
        (self.work / "new.txt").write_text("added", encoding="utf-8")
        (self.work / "c2.txt").unlink()
        self.assertEqual(self.cli("verify", "demo"), 0)
        self.assertIn("work folder: changed since the final check snapshot (3 paths): c1.txt, c2.txt, new.txt",
                      self.out)
        for i in range(12):
            (self.work / f"extra{i:02d}.txt").write_text("x", encoding="utf-8")
        self.cli("verify", "demo")
        line = next(x for x in self.out.splitlines() if x.startswith("work folder"))
        self.assertIn("(15 paths)", line)
        self.assertEqual(line.split(": ", 1)[1].split("): ", 1)[1].count(","), 10, "ten paths, then an ellipsis")
        self.assertTrue(line.endswith(", …"))

    def test_missing(self):
        shutil.rmtree(self.work)
        self.assertEqual(self.cli("verify", "demo"), 0)
        self.assertIn("work folder: missing", self.out)
