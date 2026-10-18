import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from agentic import loop
from agentic.ledger import Ledger, LedgerError
from agentic.worker import OSCILLATION_LINE
from helpers import (ALWAYS_PASSES, NOTHING, ROOT, Crash, StepClock, SupervisorTestCase, fix, ledger_summary,
                     make_mission, results, unfix)

THREE_STEPS = [
    {"model": "m-sonnet", "thinking": "high", "iteration_minutes": 45},
    {"model": "m-opus", "thinking": "high", "iteration_minutes": 45},
    {"model": "m-fable", "thinking": "high", "iteration_minutes": 90},
]


class HappyPathTests(SupervisorTestCase):
    def test_everything_fixed_in_one_iteration(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(), "complete")
        report = self.report_text()
        self.assertIn("**결론: ✅ 완료 — 조건 2개 중 2개를 감독자가 직접 검사해 확인**", report)
        self.assertIn("| 1회 |", report)
        self.assertEqual(len(results(report, "✅")), 2)
        self.assertEqual(results(report, "❌"), ["없음"])
        self.assertEqual(self.types(), ["mission_started", "baseline", "iteration_worker",
                                        "check_result", "check_result", "canary", "mission_finished"])

    def test_worker_is_started_with_the_ladder_step_and_in_the_worker_folder(self):
        self.play(fix("c1", "c2"))
        self.run_mission(make_mission(ladder=[{"model": "m-one", "thinking": "low", "iteration_minutes": 7}]))
        call = self.calls()[0]
        self.assertEqual((call["model"], call["thinking"], call["max_time"]), ("m-one", "low", "7m"))
        self.assertEqual(call["cwd"], str(self.cfg.workdir("demo").resolve()))

    def test_worker_stdin_is_dev_null(self):
        self.play(NOTHING, fix("c1", "c2"))
        self.run_mission()
        self.assertEqual(len(self.calls()), 2)
        for call in self.calls():
            self.assertTrue(call["stdin_devnull"])
            self.assertEqual(call["stdin_len"], 0)

    def test_a_criterion_that_passes_and_later_fails_counts_as_failing_again(self):
        self.play(fix("c1", "c2"), {"delete": unfix("c1")}, fix("c1", "c3"))
        self.assertEqual(self.run_mission(make_mission(("c1", "c2", "c3"))), "complete")
        second = [r["data"] for r in self.records() if r["type"] == "check_result" and r["data"]["iteration"] == 2]
        self.assertEqual({d["criterion"]: d["passed"] for d in second}, {"c1": False, "c2": True, "c3": False})
        prompt = self.calls()[2]["prompt"]
        self.assertIn("[c1] Requirement c1 — currently: FAILING", prompt)
        self.assertIn("[c2] Requirement c2 — currently: PASSING", prompt)

    def test_progress_resets_the_stall_counter(self):
        mission = make_mission(("c1", "c2", "c3"), limits={"escalate_after": 2, "give_up_after": 2})
        self.play(fix("c1"), NOTHING, fix("c2"), NOTHING, fix("c3"))
        self.assertEqual(self.run_mission(mission), "complete")
        self.assertNotIn("escalated", self.types())


class RedBaselineTests(SupervisorTestCase):
    def test_check_that_passes_on_the_empty_folder_is_invalid_and_excluded(self):
        mission = make_mission(("c1", "c2"), checks={"c1": ALWAYS_PASSES})
        self.play(fix("c2"))
        self.assertEqual(self.run_mission(mission), "partial")
        baseline = next(r for r in self.records() if r["type"] == "baseline")
        self.assertEqual(baseline["data"]["invalid"], ["c1"])
        checked = {r["data"]["criterion"] for r in self.records() if r["type"] == "check_result"}
        self.assertEqual(checked, {"c2"})
        for call in self.calls():
            self.assertNotIn("[c1]", call["prompt"])
        report = self.report_text()
        self.assertIn("**결론: ⚠️ 일부 완료 — ", report)
        self.assertEqual(len(results(report, "⚠️")), 1)
        self.assertIn("[c1]", results(report, "⚠️")[0])
        self.assertNotIn("[c1]", "".join(results(report, "✅") + results(report, "❌")))

    def test_mission_whose_checks_all_pass_at_baseline_never_calls_the_worker(self):
        self.assertEqual(self.run_mission(make_mission(("c1",), checks={"c1": ALWAYS_PASSES})), "partial")
        self.assertEqual(self.calls(), [])
        self.assertEqual(results(self.report_text(), "✅"), ["없음"])


class FalseCompletionTests(SupervisorTestCase):
    def test_worker_claims_success_but_changes_nothing(self):
        mission = make_mission(limits={"escalate_after": 1, "give_up_after": 1}, ladder=THREE_STEPS[:2])
        self.play(NOTHING)
        self.assertEqual(self.run_mission(mission), "partial")
        report = self.report_text()
        self.assertEqual(results(report, "✅"), ["없음"])
        self.assertEqual(len(results(report, "❌")), 2)
        self.assertGreater(len(self.calls()), 1, "the loop must continue after the false claim")
        for call in self.calls():
            self.assertNotIn("currently: PASSING", call["prompt"])
            self.assertNotIn(OSCILLATION_LINE, call["prompt"], "an unchanged failing set is not oscillation")
        later = self.calls()[1]["prompt"]
        note_line = next(line for line in later.splitlines() if "All done, everything works!" in line)
        self.assertIn("unverified", note_line)


class EscalationTests(SupervisorTestCase):
    def test_default_limits_walk_the_ladder_then_give_up(self):
        self.play(NOTHING)
        self.assertEqual(self.run_mission(make_mission(ladder=THREE_STEPS)), "partial")
        # escalate_after=2 on each non-final step; escalate_after+give_up_after=4 stalls on the last.
        self.assertEqual([c["model"] for c in self.calls()],
                         ["m-sonnet"] * 2 + ["m-opus"] * 2 + ["m-fable"] * 4)
        records = self.records()
        escalations = [r["data"] for r in records if r["type"] == "escalated"]
        self.assertEqual([(e["from_step"], e["to_step"], e["model"]) for e in escalations],
                         [(0, 1, "m-opus"), (1, 2, "m-fable")])
        gave_up = [r["data"] for r in records if r["type"] == "gave_up"]
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(gave_up[0]["criteria"], ["c1", "c2"])
        self.assertEqual([r["type"] for r in records[-2:]], ["gave_up", "mission_finished"])
        report = self.report_text()
        self.assertIn("8번 시도 후 포기", results(report, "❌")[0])
        self.assertIn("m-sonnet → m-opus → m-fable", report)

    def test_single_step_ladder_gives_up_after_the_combined_stalls(self):
        mission = make_mission(limits={"escalate_after": 1, "give_up_after": 1}, ladder=THREE_STEPS[:1])
        self.play(NOTHING)
        self.run_mission(mission)
        self.assertEqual(len(self.calls()), 2)
        self.assertNotIn("escalated", self.types())


class OscillationTests(SupervisorTestCase):
    def test_alternating_failing_sets_trigger_the_different_approach_line(self):
        mission = make_mission(limits={"escalate_after": 1, "give_up_after": 3}, ladder=THREE_STEPS[:1])
        a = {**fix("c1"), "delete": unfix("c2")}
        b = {**fix("c2"), "delete": unfix("c1")}
        # failing after each iteration: {c2}, {c1}, {c2}, {c1}, {c2}
        self.play(a, b, a, b, a)
        self.run_mission(mission)
        prompts = [c["prompt"] for c in self.calls()]
        self.assertEqual(len(prompts), 5)
        has_line = [OSCILLATION_LINE in p for p in prompts]
        self.assertEqual(has_line, [False, False, False, True, True])


class HiddenChecksTests(SupervisorTestCase):
    def test_no_prompt_ever_contains_check_argv_file_names_or_contents(self):
        mission = make_mission()
        self.play(NOTHING, fix("c1"), fix("c2"))
        self.run_mission(mission)
        secrets = []
        for crit in mission["criteria"]:
            check = crit["check"]
            secrets += check["argv"]
            for name, content in check["files"].items():
                secrets += [name, os.path.basename(name), content]
                secrets += [line.strip() for line in content.splitlines() if len(line.strip()) > 8]
        self.assertTrue(any("SECRET_CONTENT_MARKER_c1" in s for s in secrets))
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        for call in calls:
            for secret in secrets:
                self.assertNotIn(secret, call["prompt"])

    def test_check_files_never_land_in_the_worker_folder(self):
        self.play(NOTHING)
        mission = make_mission(limits={"escalate_after": 1, "give_up_after": 1}, ladder=THREE_STEPS[:1])
        self.run_mission(mission)
        names = [n for _, _, files in os.walk(self.cfg.workdir("demo")) for n in files]
        self.assertEqual(names, [])
        self.assertFalse((self.cfg.workdir("demo") / "checks").exists())

    def test_check_data_is_not_in_the_mission_record(self):
        self.play(fix("c1", "c2"))
        self.run_mission()
        started = json.dumps(self.records()[0])
        self.assertNotIn("secret_check", started)
        self.assertNotIn("argv", started)


class TimeLimitTests(SupervisorTestCase):
    def test_time_used_up_stops_the_mission(self):
        mission = make_mission(limits={"mission_hours": 1})
        self.play(NOTHING)
        self.assertEqual(self.run_mission(mission, clock=StepClock(20)), "stopped_time")
        types = self.types()
        self.assertEqual(types[-2:], ["stopped_time", "mission_finished"])
        self.assertGreaterEqual(types.count("iteration_worker"), 1)
        report = self.report_text()
        self.assertIn("**결론: ⏱️ 시간 초과 — ", report)
        self.assertTrue(all(line.endswith("시간이 다 되어 멈춤") for line in results(report, "❌")))
        self.assertEqual(results(report, "✅"), ["없음"])

    def test_no_worker_runs_when_time_is_already_used_up(self):
        self.assertEqual(self.run_mission(make_mission(limits={"mission_hours": 0.001}),
                                          clock=StepClock(20)), "stopped_time")
        self.assertEqual(self.calls(), [])

    def test_finished_mission_is_not_stopped_by_the_clock(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(make_mission(limits={"mission_hours": 1}),
                                          clock=StepClock(20)), "complete")


RESUME_PLAN = [NOTHING, fix("c1"), fix("c2")]


def resume_mission():
    return make_mission(limits={"escalate_after": 1, "give_up_after": 1}, ladder=THREE_STEPS[:2])


class ResumeTests(SupervisorTestCase):
    def uninterrupted(self):
        self.new_world()
        self.play(*RESUME_PLAN)
        self.assertEqual(self.run_mission(resume_mission()), "complete")
        return ledger_summary(self.records()), self.stable_report()

    def stable_report(self):
        text = re.sub(r"걸린 시간: \S+ \S+", "", self.report_text())
        return re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST", "TS", text)

    def test_crash_after_any_record_then_resume_gives_the_identical_ledger(self):
        expected_records, expected_report = self.uninterrupted()
        # The plan exercises escalation, so crash points include "decided but not yet recorded".
        self.assertIn(("escalated", 0, 1), expected_records)
        for crash_after in range(1, len(expected_records) + 1):
            with self.subTest(crash_after=crash_after):
                self.new_world()
                self.play(*RESUME_PLAN)
                real_append, count = Ledger.append, [0]

                def append(ledger, record_type, data):
                    record = real_append(ledger, record_type, data)
                    count[0] += 1
                    if count[0] == crash_after:
                        raise Crash
                    return record

                with mock.patch.object(Ledger, "append", append):
                    with self.assertRaises(Crash):
                        self.run_mission(resume_mission())
                self.assertTrue(Ledger(self.cfg.ledger_file("demo")).verify().ok)
                self.assertEqual(loop.run("demo", self.cfg), "complete")
                self.assertEqual(ledger_summary(self.records()), expected_records)
                self.assertEqual(len(self.calls()), 3, "a recorded iteration must not be repeated")
                self.assertEqual(self.stable_report(), expected_report)

    def test_sigkill_of_the_supervisor_after_one_iteration_then_run_again(self):
        mission = make_mission(ladder=THREE_STEPS[:1])
        expected_records, expected_report = self.uninterrupted_two_steps(mission)

        self.new_world()
        self.play(fix("c1"), fix("c2"))
        os.environ["AGENTIC_STUB_KILL_AT"] = "2"
        self.install(mission)
        cmd = [sys.executable, "-m", "agentic", "run", "demo"]
        first = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, -9, first.stderr)
        self.assertEqual([r["type"] for r in self.records()],
                         ["mission_started", "baseline", "iteration_worker", "check_result", "check_result"])
        second = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(ledger_summary(self.records()), expected_records)
        self.assertEqual(self.stable_report(), expected_report)

    def uninterrupted_two_steps(self, mission):
        self.new_world()
        self.play(fix("c1"), fix("c2"))
        self.run_mission(mission)
        return ledger_summary(self.records()), self.stable_report()

    def test_running_a_finished_mission_again_changes_nothing(self):
        self.play(fix("c1", "c2"))
        self.run_mission()
        before = self.cfg.ledger_file("demo").read_bytes()
        self.assertEqual(loop.run("demo", self.cfg), "complete")
        self.assertEqual(self.cfg.ledger_file("demo").read_bytes(), before)
        self.assertEqual(len(self.calls()), 1)

    def test_run_refuses_a_tampered_ledger(self):
        self.play(NOTHING, fix("c1", "c2"))
        self.run_mission()
        path = self.cfg.ledger_file("demo")
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("".join(lines[:1] + lines[2:]))
        before = path.read_bytes()
        with self.assertRaises(LedgerError):
            loop.run("demo", self.cfg)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
