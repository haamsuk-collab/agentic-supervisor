"""Model outages (SPEC-D section 6): worker, planner and reviewer. A fake clock and sleep: nothing really waits."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentic import loop, outage
from agentic.config import PLAN_OUTAGE_MAX_SECONDS
from agentic.i18n import ko
from helpers import (NOTHING, SONNET, Crash, PlannedTestCase, SupervisorTestCase, TimedTestCase, fix, make_mission,
                     notices)

PLAN_OUTAGE_REASON = ko.MESSAGES["planner.outage_reason"]

ALT_SONNET = "google-antigravity/claude-sonnet-5-5"
RATE_LIMITED = {"stderr": "Error: 429 Too Many Requests (rate limit reached)\n", "exit": 1}
PLANNER_DOWN = {"stderr": "provider error: 503 Service Unavailable\n", "exit": 1}


class FakeTime:
    """Clock and sleep for the supervisor: sleeping moves the clock, nothing really waits."""

    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.slept: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class WorkerOutageTests(SupervisorTestCase):
    def run_faked(self, mission=None, time=None):
        time = time or FakeTime()
        self.time = time
        return self.run_mission(mission or make_mission(), clock=time.clock, sleep=time.sleep)

    def unavailable(self):
        return [r["data"] for r in self.records() if r["type"] == "worker_unavailable"]

    def test_rate_limit_tries_the_alternate_provider_then_waits(self):
        self.play(RATE_LIMITED, RATE_LIMITED, fix("c1", "c2"))
        self.assertEqual(self.run_faked(), "complete")
        self.assertEqual(self.types(), ["mission_started", "baseline", "worker_unavailable", "worker_unavailable",
                                        "iteration_worker", "check_result", "check_result", "canary",
                                        "mission_finished"])
        first, second = self.unavailable()
        self.assertEqual((first["model"], first["attempt"], first["wait_seconds"], first["next_model"]),
                         (SONNET, 1, 0, ALT_SONNET))
        self.assertEqual((second["model"], second["attempt"], second["wait_seconds"], second["next_model"]),
                         (ALT_SONNET, 2, 300, None))
        self.assertIn("429 Too Many Requests", first["reason"])
        self.assertEqual([c["model"] for c in self.calls()], [SONNET, ALT_SONNET, SONNET],
                         "every new attempt starts from the step's own model")
        self.assertEqual(self.time.slept, [300])
        worker = next(r["data"] for r in self.records() if r["type"] == "iteration_worker")
        self.assertEqual((worker["iteration"], worker["model"]), (1, SONNET))

    def test_an_outage_counts_no_stall_and_never_escalates(self):
        mission = make_mission(limits={"escalate_after": 1, "give_up_after": 1})
        self.play(*[RATE_LIMITED] * 8, NOTHING, fix("c1", "c2"))
        self.run_faked(mission)
        self.assertNotIn("escalated", self.types()[:self.types().index("iteration_worker")])
        self.assertEqual([d["wait_seconds"] for d in self.unavailable()], [0, 300, 0, 600, 0, 1200, 0, 2400])
        self.assertEqual([d["attempt"] for d in self.unavailable()], list(range(1, 9)))
        self.assertEqual(self.time.slept, [300, 600, 1200, 2400])
        self.assertEqual({c["model"] for c in self.calls()[:8]}, {SONNET, ALT_SONNET})
        first = next(r["data"] for r in self.records() if r["type"] == "iteration_worker")
        self.assertEqual(first["iteration"], 1, "unavailable attempts do not use up iteration numbers")
        # The note of the prompt after the outage is still the initial one: unavailable runs leave no note.
        self.assertIn("(written by the previous agent, unverified): none", self.calls()[8]["prompt"])

    def test_waits_back_off_to_sixty_minutes_and_stay_there(self):
        self.assertEqual([outage.wait_seconds(n) // 60 for n in range(7)], [5, 10, 20, 40, 60, 60, 60])

    def test_a_fast_failure_that_changed_nothing_is_unavailable(self):
        self.play({"exit": 1, "say": "something went wrong"}, fix("c1", "c2"))
        self.run_faked(make_mission(ladder=[{"model": "anthropic/claude-fable-5-1"}]))  # no alternate provider
        (data,) = self.unavailable()
        self.assertIn("작업 폴더가 그대로임", data["reason"])
        self.assertEqual((data["next_model"], data["wait_seconds"]), (None, 300))

    def test_a_failure_that_changed_files_is_a_normal_iteration(self):
        self.play({"exit": 1, "write": {"c1.txt": "ok"}}, fix("c1", "c2"))
        self.assertEqual(self.run_faked(), "complete")
        self.assertEqual(self.unavailable(), [])
        self.assertEqual(self.types().count("iteration_worker"), 2)

    def test_a_slow_failure_is_a_normal_iteration(self):
        self.play({"exit": 1}, fix("c1", "c2"))
        with mock.patch.object(outage, "FAST_EXIT_SECONDS", 0):  # every run counts as slow
            self.assertEqual(self.run_faked(), "complete")
        self.assertEqual(self.unavailable(), [])
        self.assertEqual(self.types().count("iteration_worker"), 2)

    def test_outage_words_in_the_final_text_are_not_an_outage(self):
        # omp reports model errors on stderr; the agent's own text may talk about gateways and quotas.
        self.play({"exit": 1, "say": "Added a 429 retry to the API gateway client and a quota check.",
                   "write": {"c1.txt": "ok"}}, fix("c1", "c2"))
        self.assertEqual(self.run_faked(), "complete")
        self.assertEqual(self.unavailable(), [])
        first = next(r["data"] for r in self.records() if r["type"] == "iteration_worker")
        self.assertEqual((first["iteration"], first["exit_code"]), (1, 1))

    def test_a_resumed_run_sleeps_only_what_is_left_of_the_wait(self):
        self.play(RATE_LIMITED, RATE_LIMITED, fix("c1", "c2"))
        time = FakeTime()

        def killed(seconds):
            raise Crash  # the supervisor dies while it waits

        with self.assertRaises(Crash):
            self.run_mission(clock=time.clock, sleep=killed)
        self.assertEqual(self.types()[-1], "worker_unavailable")
        time.now += timedelta(seconds=120)  # the restart comes two minutes later
        self.assertEqual(loop.run("demo", self.cfg, clock=time.clock, sleep=time.sleep), "complete")
        self.assertEqual(time.slept, [180])
        self.assertEqual(len(self.calls()), 3, "the recorded attempts are not repeated")

    def test_time_running_out_while_waiting_stops_the_mission_with_the_wait_line(self):
        self.play(RATE_LIMITED)
        status = self.run_faked(make_mission(limits={"mission_hours": 0.25}))
        self.assertEqual(status, "stopped_time")
        self.assertEqual(self.types()[-2:], ["stopped_time", "mission_finished"])
        self.assertEqual(self.time.slept, [300, 600], "the second wait is cut at the mission's end")
        self.assertNotIn("iteration_worker", self.types())
        report = self.report_text()
        self.assertIn("**결론: ⏱️ 시간 초과 — ", report)
        self.assertIn("- 모델을 쓸 수 없어 기다린 시간: 0시간 15분", notices(report))


class FakedPlanning(PlannedTestCase):
    def plan_faked(self):
        """Plan and run `demo` from a request, with the fake clock."""
        folder = self.cfg.mission_dir("demo")
        folder.mkdir(parents=True)
        self.cfg.request_file("demo").write_text("날씨를 알려주는 명령줄 프로그램을 만들어 주세요.", encoding="utf-8")
        self.time = FakeTime()
        return loop.run("demo", self.cfg, clock=self.time.clock, sleep=self.time.sleep)

    def unavailable(self):
        return [r["data"] for r in self.records() if r["type"] == "plan_unavailable"]


class PlannerOutageTests(FakedPlanning):
    def test_a_planner_outage_is_retried_after_a_wait(self):
        self.plan_script(script={"brief": [PLANNER_DOWN] * 4 + [{}]})  # 2 models x 2 attempts fail, then fine
        self.play(fix("c1", "c2"))
        self.assertEqual(self.plan_faked(), "complete")
        (data,) = self.unavailable()
        self.assertEqual((data["step"], data["attempt"], data["wait_seconds"]), ("brief", 1, 300))
        self.assertIn("503 Service Unavailable", data["reason"])
        self.assertEqual(self.time.slept, [300])
        self.assertEqual(len(self.planner_calls("brief")), 5)

    def test_planning_fails_after_six_hours_of_waiting(self):
        self.plan_script(script={"brief": [PLANNER_DOWN]})
        self.assertEqual(self.plan_faked(), "plan_failed")
        waits = [d["wait_seconds"] for d in self.unavailable()]
        self.assertEqual(waits, [300, 600, 1200, 2400, 3600, 3600, 3600, 3600, 2700])
        self.assertEqual(sum(waits), PLAN_OUTAGE_MAX_SECONDS)
        self.assertEqual(sum(self.time.slept), PLAN_OUTAGE_MAX_SECONDS)
        failed = next(r["data"] for r in self.records() if r["type"] == "plan_failed")
        self.assertEqual(failed["reason"], PLAN_OUTAGE_REASON)
        self.assertIn(f"- 계획 brief 단계 실패: {PLAN_OUTAGE_REASON}", notices(self.report_text()))

    def test_a_schema_error_is_never_an_outage(self):
        for name, script in (("no json", [{"raw": "rate limit reached, quota exceeded (429)"}]),
                             ("mixed", [PLANNER_DOWN, {"raw": "429 too many requests"}])):
            with self.subTest(name):
                self.new_world()
                self.plan_script(script={"brief": script})
                self.assertEqual(self.plan_faked(), "plan_failed")
                self.assertEqual(self.unavailable(), [])
                self.assertEqual(self.time.slept, [])

    def test_outage_words_on_stdout_are_not_an_outage(self):
        self.plan_script(script={"brief": [{"raw": "Error: 429 rate limit, quota exceeded at the gateway", "exit": 1}]})
        self.assertEqual(self.plan_faked(), "plan_failed")
        self.assertEqual(self.unavailable(), [])
        self.assertEqual(self.time.slept, [])
        self.assertEqual(len(self.planner_calls("brief")), 4, "every model was tried, none waited for")

    def test_a_resumed_planner_wait_sleeps_only_the_remainder(self):
        self.plan_script(script={"brief": [PLANNER_DOWN] * 4 + [{}]})
        self.play(fix("c1", "c2"))
        self.cfg.mission_dir("demo").mkdir(parents=True)
        self.cfg.request_file("demo").write_text("요청", encoding="utf-8")
        time = FakeTime()

        def killed(seconds):
            raise Crash

        with self.assertRaises(Crash):
            loop.run("demo", self.cfg, clock=time.clock, sleep=killed)
        time.now += timedelta(seconds=100)
        self.assertEqual(loop.run("demo", self.cfg, clock=time.clock, sleep=time.sleep), "complete")
        self.assertEqual(time.slept, [200])


class ReviewOutageTests(FakedPlanning):
    def test_a_review_outage_waits_at_most_one_hour_then_the_review_fails(self):
        self.plan_script(script={"review": [PLANNER_DOWN]})
        self.play(fix("c1", "c2"))
        self.assertEqual(self.plan_faked(), "complete")
        waits = [d for d in self.unavailable() if d["step"] == "review"]
        self.assertEqual([d["wait_seconds"] for d in waits], [300, 600, 1200, 1500])
        self.assertEqual(sum(self.time.slept), 3600)
        (review,) = [r["data"] for r in self.records() if r["type"] == "review"]
        self.assertFalse(review["ok"])
        self.assertIn("- 꼼수 리뷰를 하지 못했습니다", notices(self.report_text()))

    def test_a_review_that_works_after_a_wait_is_recorded_once(self):
        self.plan_script(script={"review": [PLANNER_DOWN] * 4 + [{}]})
        self.play(fix("c1", "c2"))
        self.assertEqual(self.plan_faked(), "complete")
        (review,) = [r["data"] for r in self.records() if r["type"] == "review"]
        self.assertTrue(review["ok"])
        self.assertEqual(self.time.slept, [300])


class OutageDetectionTests(TimedTestCase):
    def test_patterns_are_case_insensitive_and_status_codes_are_whole_words(self):
        for text in ("RATE LIMIT", "rate-limited", "Quota exceeded", "usage_limit", "HTTP 529", "Unauthorized",
                     "unauthorised", "ECONNREFUSED", "Bad Gateway", "insufficient credit", "error 401"):
            with self.subTest(text=text):
                self.assertIsNotNone(outage.matching_line(text))
        for text in ("port 4290 open", "id 15031", "all good", ""):
            with self.subTest(text=text):
                self.assertIsNone(outage.matching_line(text))

    def test_a_zero_exit_is_never_unavailable(self):
        self.assertIsNone(outage.worker_reason(0, False, "rate limit", 1.0, lambda: True))

    def test_folder_digest_ignores_skipped_folders_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as elsewhere:
            work = Path(root)
            (work / "a.txt").write_text("a")
            (work / ".git").mkdir()
            (Path(elsewhere) / "target.txt").write_text("1")
            os.symlink(Path(elsewhere) / "target.txt", work / "link")
            before = outage.folder_digest(work)
            (work / ".git" / "HEAD").write_text("x")
            (Path(elsewhere) / "target.txt").write_text("2")
            self.assertEqual(outage.folder_digest(work), before)
            (work / "a.txt").write_text("b")
            self.assertNotEqual(outage.folder_digest(work), before)
            (work / "a.txt").write_text("a")
            self.assertEqual(outage.folder_digest(work), before)
            (work / "empty").mkdir()
            self.assertNotEqual(outage.folder_digest(work), before, "a new folder is a change")


if __name__ == "__main__":
    unittest.main()
