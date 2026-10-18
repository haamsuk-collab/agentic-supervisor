"""SPEC-F section 2: a pass must depend on the worker's files."""

import hashlib
import sys

from agentic import briefing, canary, loop, report
from agentic.worker import UNPROVEN_LINE
from helpers import NOTHING, SupervisorTestCase, fix, make_mission
from test_followup import NEW_TEXT, PlannedFollowUpCase

# Passes once c1.txt exists, whatever it holds: it does not depend on what the worker wrote.
EXISTS_ONLY = {"argv": ["/usr/bin/python3.12", "-I", "-c",
                        "import os, sys; sys.exit(0 if os.path.exists('c1.txt') else 1)"],
               "timeout": 30}
ONE_STEP = [{"model": "m-one", "thinking": "high", "iteration_minutes": 5}]


def canaries(records: list[dict]) -> list[dict]:
    return [r["data"] for r in records if r["type"] == "canary"]


class CanaryLoopTests(SupervisorTestCase):
    def test_a_check_that_reads_the_workers_file_fails_on_the_emptied_copy_so_the_canary_holds(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(), "complete")
        (data,) = canaries(self.records())
        self.assertEqual(data["iteration"], 1)
        self.assertEqual(data["changed_files"], 2)
        self.assertEqual({cid: r["held"] for cid, r in data["results"].items()}, {"c1": True, "c2": True})
        self.assertTrue(all(r["exit_code"] != 0 for r in data["results"].values()))
        self.assertEqual(briefing.build(self.records(), None, [], None).achieved, 2)

    def test_a_check_that_passes_regardless_of_the_content_is_canary_failed_and_not_achieved(self):
        mission = make_mission(("c1",), checks={"c1": EXISTS_ONLY}, ladder=ONE_STEP,
                               limits={"escalate_after": 1, "give_up_after": 2})
        self.play(fix("c1"), NOTHING, NOTHING, NOTHING)
        self.assertEqual(self.run_mission(mission), "partial")
        first = canaries(self.records())[0]
        self.assertEqual((first["iteration"], first["results"]["c1"]["held"]), (1, False))
        self.assertEqual(first["results"]["c1"]["exit_code"], 0)
        calls = self.calls()
        self.assertGreaterEqual(len(calls), 2)
        line = UNPROVEN_LINE.format(id="c1")
        self.assertEqual(line, "Requirement c1 is not shown to be met by your files; "
                               "make the result come from the files you write.")
        self.assertNotIn(line, calls[0]["prompt"])
        self.assertIn(line, calls[1]["prompt"])
        b = briefing.build(self.records(), None, [], None)
        (c1,) = b.evidence
        self.assertEqual((c1.mark, c1.reason), (briefing.FAILED, "검사가 결과물을 확인하지 않음"))
        self.assertEqual(b.achieved, 0)
        self.assertIn("검사가 결과물을 확인하지 않음", report.build(self.cfg, "demo", self.records()).text)

    def test_the_baseline_manifest_lists_the_files_that_were_there_before_the_mission(self):
        work = self.cfg.workdir("demo")
        work.mkdir(parents=True)
        (work / "given.txt").write_text("from the owner", encoding="utf-8")
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(), "complete")
        baseline = next(r["data"] for r in self.records() if r["type"] == "baseline")
        self.assertEqual(baseline["manifest"],
                         {"given.txt": hashlib.sha256(b"from the owner").hexdigest()})
        (data,) = canaries(self.records())
        self.assertEqual(data["changed_files"], 2, "given.txt was not emptied")


class EmptiedCopyTests(SupervisorTestCase):
    def test_only_new_or_changed_regular_files_are_truncated(self):
        work = self.cfg.workdir("demo")
        work.mkdir(parents=True)
        (work / "kept.txt").write_text("kept", encoding="utf-8")
        (work / "edited.txt").write_text("before", encoding="utf-8")
        baseline = canary.manifest(work)
        self.assertEqual(sorted(baseline), ["edited.txt", "kept.txt"])
        (work / "edited.txt").write_text("after", encoding="utf-8")
        (work / "sub").mkdir()
        (work / "sub" / "new.py").write_text("x = 1\n", encoding="utf-8")
        (work / "link").symlink_to("kept.txt")
        self.assertEqual(sorted(canary.changed_files(canary.manifest(work), baseline)),
                         ["edited.txt", "sub/new.py"], "a symlink is not a regular file")
        target = self.stub_dir / "copy"
        changed = canary.emptied_copy(work, target, baseline)
        self.assertEqual(changed, 2)
        self.assertEqual((target / "kept.txt").read_text(), "kept")
        self.assertEqual((target / "edited.txt").read_bytes(), b"")
        self.assertEqual((target / "sub" / "new.py").read_bytes(), b"")
        self.assertEqual((work / "edited.txt").read_text(), "after", "the worker folder itself is untouched")
        self.assertEqual(canary.changed_python(work, baseline), ["sub/new.py"])


class InheritedPassTests(PlannedFollowUpCase):
    def test_an_inherited_pass_whose_check_ignores_the_content_is_not_achieved(self):
        self.parent_done(worker_plan=[fix("c1", "c2")])
        new_id = self.follow_up(NEW_TEXT)
        # The follow-up's c1 check only asks that c1.txt exists: it passes on the inherited folder (inherited pass)
        # and on the emptied copy, so the canary does not hold.
        self.plan_script(script={"checks": [{"checks": {"c1": EXISTS_ONLY}}]})
        self.play(*[NOTHING] * 20)
        self.assertNotEqual(loop.run(new_id, self.cfg), "complete")
        records = self.records(new_id)
        baseline = next(r["data"] for r in records if r["type"] == "baseline")
        self.assertIn("c1", baseline["inherited_pass"])
        first = canaries(records)[0]
        self.assertEqual((first["iteration"], first["results"]["c1"]["held"]), (0, False))
        self.assertTrue(first["results"]["c2"]["held"])
        marks = {i.id: (i.mark, i.reason) for i in briefing.build(records, None, [], None).evidence}
        self.assertEqual(marks["c1"], (briefing.FAILED, briefing.CANARY_REASON))
        self.assertEqual(marks["c2"][0], briefing.ACHIEVED)
        self.assertIn(UNPROVEN_LINE.format(id="c1"), self.calls()[0]["prompt"])
