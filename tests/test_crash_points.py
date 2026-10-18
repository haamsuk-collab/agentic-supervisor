"""SPEC-H section 7: kill the run right after each kind of transition, resume, and get the uninterrupted result."""

import re
from unittest import mock

from agentic import faults, loop
from helpers import Crash, PlannedTestCase, fix, make_mission
from test_mutation import GOOD, ONE_STEP, STRONG, WEAK, write_calc
from test_ratchet import HIGH_ONLY
from test_review import FINDING, cheating_fix

# One planned mission that passes through every transition: plan steps, worker records, check results, the canary,
# the mutation score (solution.py is Python), the cheat review and its veto, and the finish.
TRANSITIONS = ("plan_step", "iteration_worker", "check_result", "canary", "mutation", "review", "review_veto",
               "mission_finished")
VOLATILE = {"ts", "hash", "prev", "duration_seconds", "elapsed_seconds"}
# A check's traceback names its snapshot: the test's temporary folder and 128 random bits change every run. The
# sha256 of the full output covers those names too, so it changes with them.
RANDOM_PATHS = re.compile(r"/tmp/agentic-test-[^/]+|-[0-9a-f]{32}")
OUTPUT_HASHES = {"stdout_sha256", "stderr_sha256"}


def stable(value):
    """A ledger record or its data without timestamps, hashes, measured durations and random snapshot names."""
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in VOLATILE | OUTPUT_HASHES}
    if isinstance(value, list):
        return [stable(v) for v in value]
    if isinstance(value, str):
        return RANDOM_PATHS.sub("<random>", value)
    return value


class CrashPointTests(PlannedTestCase):
    TIMEOUT = 300.0

    def scenario(self) -> None:
        self.new_world()
        self.plan_script(script={"review": [{"findings": [FINDING]}, {}]})
        self.play(cheating_fix("c1", "c2"), fix("c1", "c2"))

    def stable_report(self) -> str:
        text = re.sub(r"걸린 시간: \S+ \S+", "", self.report_text())
        return re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST", "TS", text)

    def uninterrupted(self) -> tuple[list[dict], str]:
        self.scenario()
        self.assertEqual(self.new(), 0, self.err)
        return [stable(r) for r in self.records()], self.stable_report()

    def killer(self, where: str, kind: str):
        def hook(point: str, record: dict) -> None:
            if point == where and record["type"] == kind:
                faults.HOOK = None  # the process dies once; the resumed run goes on
                raise Crash
        return hook

    def crash_at(self, where: str, kind: str) -> None:
        with mock.patch.object(faults, "HOOK", self.killer(where, kind)):
            with self.assertRaises(Crash):
                self.new()

    def test_every_transition_survives_a_kill_right_after_it(self):
        expected_records, expected_report = self.uninterrupted()
        kinds = {r["type"] for r in expected_records}
        self.assertLessEqual(set(TRANSITIONS), kinds, "the scenario passes through every transition")
        for where in (faults.APPENDED, faults.APPLIED):
            for kind in TRANSITIONS:
                with self.subTest(where=where, kind=kind):
                    self.scenario()
                    self.crash_at(where, kind)
                    self.assertEqual(self.types()[-1], kind, "the run died right after that record")
                    self.assertEqual(loop.run("demo", self.cfg), "complete")
                    self.assertEqual([stable(r) for r in self.records()], expected_records)
                    self.assertEqual(self.stable_report(), expected_report)
                    self.assertEqual(len(self.calls()), 2, "a recorded worker iteration is not repeated")

    def test_the_ratchet_records_survive_a_kill(self):
        """SPEC-H section 2's records: a resumed strengthening neither repeats nor loses them."""
        for stronger, kinds in ((STRONG, ("plan_filter", "checks_update", "checks_strengthened")),
                                (HIGH_ONLY, ("checks_update_rejected",))):
            def weak_mission():
                self.new_world()
                self.plan_script(script={"checks": [{"checks": {"c1": stronger}}]})
                self.play(write_calc(GOOD))
                self.install(make_mission(("c1",), checks={"c1": WEAK}, ladder=ONE_STEP))

            weak_mission()
            self.assertEqual(loop.run("demo", self.cfg), "complete")
            expected = [stable(r) for r in self.records()]
            for kind in kinds:
                with self.subTest(kind=kind):
                    weak_mission()
                    with mock.patch.object(faults, "HOOK", self.killer(faults.APPENDED, kind)):
                        with self.assertRaises(Crash):
                            loop.run("demo", self.cfg)
                    self.assertEqual(loop.run("demo", self.cfg), "complete")
                    self.assertEqual([stable(r) for r in self.records()], expected)
