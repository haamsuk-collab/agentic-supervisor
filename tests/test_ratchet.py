"""SPEC-H section 2: a mission's checks may never get weaker."""

import json

from agentic import briefing, mutation, ratchet
from agentic.mission import parse_mission
from helpers import PlannedTestCase, SupervisorTestCase, make_mission
from test_mutation import GOOD, ONE_STEP, STRONG, WEAK, check, write_calc

PY = "/usr/bin/python3.12"
OLD_FILE = {"argv": [PY, "-I", "_agentic_check_c1.py"], "timeout": 30,
            "files": {"_agentic_check_c1.py": "import sys\nsys.exit(0 if open('c1.txt').read() == 'ok' else 1)\n"}}
# Kills the return 'high' mutant but not return 'low' -> None, the one WEAK kills.
HIGH_ONLY = check("grade(20) == 'high'")


def mission(c1: dict, c2: dict | None = None):
    checks = {"c1": c1} if c2 is None else {"c1": c1, "c2": c2}
    return parse_mission(make_mission(tuple(checks), checks=checks))


class ViolationTests(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.work = self.cfg.workdir("demo")
        self.work.mkdir(parents=True)

    def violations(self, old, new, reasons=None, mutants=()):
        return [(v["criterion"], v["rule"]) for v in
                ratchet.violations(self.cfg, old, new, reasons or {}, self.work, list(mutants))]

    def test_an_unchanged_or_extended_check_is_accepted(self):
        extended = {**OLD_FILE, "argv": [PY, "-I", "_agentic_check_c1b.py"],
                    "files": {**OLD_FILE["files"], "_agentic_check_c1b.py": "import runpy\nrunpy.run_path("
                              "'_agentic_check_c1.py')\nassert open('c1.txt').read().endswith('ok')\n"}}
        self.assertEqual(self.violations(mission(OLD_FILE), mission(OLD_FILE)), [])
        self.assertEqual(ratchet.changed(mission(OLD_FILE), mission(extended)), ["c1"])
        self.assertEqual(self.violations(mission(OLD_FILE), mission(extended)), [])

    def test_a_changed_old_file_needs_a_named_bug(self):
        rewritten = {**OLD_FILE, "files": {"_agentic_check_c1.py": "assert open('c1.txt').read() == 'ok'\n"}}
        self.assertEqual(self.violations(mission(OLD_FILE), mission(rewritten)), [("c1", ratchet.FILES)])
        self.assertEqual(self.violations(mission(OLD_FILE), mission(rewritten),
                                         {"c1": "the old check crashed on a missing file"}), [])

    def test_a_criterion_without_a_check_is_a_violation(self):
        self.assertEqual(self.violations(mission(OLD_FILE, OLD_FILE), mission(OLD_FILE)),
                         [("c2", ratchet.MISSING)])

    def test_a_new_check_that_passes_on_an_empty_folder_is_a_violation(self):
        empty_ok = {"argv": [PY, "-I", "-c", "assert True"], "timeout": 30}
        self.assertEqual(self.violations(mission(OLD_FILE), mission(empty_ok), {"c1": "bug"}),
                         [("c1", ratchet.EMPTY)])

    def test_a_mutant_the_old_check_killed_must_still_be_killed(self):
        (self.work / "calc.py").write_text(GOOD, encoding="utf-8")
        mutants = mutation.generate(self.work, ["calc.py"], "demo")
        self.assertEqual(self.violations(mission(STRONG), mission(HIGH_ONLY), mutants=mutants),
                         [("c1", ratchet.MUTANT), ("c1", ratchet.MUTANT)],
                         "STRONG kills four mutants, HIGH_ONLY only two of them")
        self.assertEqual(self.violations(mission(WEAK), mission(STRONG), mutants=mutants), [])

    def test_the_sha256_covers_every_part_of_a_check(self):
        base = ratchet.check_sha256(mission(OLD_FILE).criterion("c1").check)
        for change in ({"timeout": 31}, {"network": True}, {"argv": [PY, "-I", "x.py"]},
                       {"files": {"_agentic_check_c1.py": "assert 0\n"}}):
            with self.subTest(change=change):
                self.assertNotEqual(ratchet.check_sha256(mission({**OLD_FILE, **change}).criterion("c1").check),
                                    base)


class StrengtheningRatchetTests(PlannedTestCase):
    def run_weak(self, stronger: dict) -> str:
        self.plan_script(script={"checks": [{"checks": {"c1": stronger}}]})
        self.play(write_calc(GOOD))
        return self.run_mission(make_mission(("c1",), checks={"c1": WEAK}, ladder=ONE_STEP))

    def test_an_accepted_change_is_recorded_per_criterion(self):
        self.assertEqual(self.run_weak(STRONG), "complete")
        (update,) = [r["data"] for r in self.records() if r["type"] == "checks_update"]
        self.assertEqual(update["criterion"], "c1")
        self.assertEqual(update["old_sha256"], ratchet.check_sha256(mission(WEAK).criterion("c1").check))
        self.assertEqual(update["new_sha256"], ratchet.check_sha256(mission(STRONG).criterion("c1").check))
        self.assertEqual(update["reason"], ratchet.DEFAULT_REASON)
        self.assertNotIn("grade(", json.dumps(update), "no check content in the ledger record")

    def test_a_weaker_check_is_rejected_and_the_old_check_stays(self):
        self.assertEqual(self.run_weak(HIGH_ONLY), "complete")
        (rejected,) = [r["data"] for r in self.records() if r["type"] == "checks_update_rejected"]
        self.assertEqual(rejected["criteria"], ["c1"])
        self.assertEqual({(v["criterion"], v["rule"]) for v in rejected["violations"]}, {("c1", ratchet.MUTANT)})
        self.assertNotIn("checks_update", self.types())
        strengthened = next(r["data"] for r in self.records() if r["type"] == "checks_strengthened")
        self.assertFalse(strengthened["ok"])
        stored = json.loads(self.cfg.mission_file("demo").read_text(encoding="utf-8"))
        self.assertEqual(stored["criteria"][0]["check"]["argv"], WEAK["argv"], "the old checks stay")
        self.assertEqual(len([r for r in self.records() if r["type"] == "mutation"]), 1, "no second score")
        b = briefing.build(self.records(), None, [], None)
        self.assertIn("검사 강화: 변형 4개 중 1개 → 새 검사를 만들지 못함", b.notices)
        self.assertEqual(b.decisions, [])
