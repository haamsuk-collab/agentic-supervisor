"""Notifications from the supervisor to the bot's outbox (SPEC-D section 5, SPEC-E section 4 cards)."""

import contextlib
import io
import os
import unittest
from unittest import mock

from agentic import commands, loop, notify, spool
from agentic.ledger import Ledger
from cardcheck import card_text, html_problems, outbox_items
from helpers import (NOTHING, Crash, FakeCommands, PlannedTestCase, StepClock, SupervisorTestCase, fix,
                     ledger_summary, make_mission)
from test_outage import RATE_LIMITED, FakeTime
from test_review import FINDING, cheating_fix

RUNNING_TODO = "👉 지금 하실 일: 없음 — 자동으로 진행 중입니다"


class OutboxMixin:
    def make_outbox(self):
        self.cfg.outbox.mkdir(parents=True)

    def _ordered(self) -> list[tuple[str, dict]]:
        """Outbox items in ledger order (notification files are named <mission id>-<seq>[-report])."""
        def key(entry):
            name = entry[0]
            parts = name.split("-")
            if parts[0] == "demo" and len(parts) > 1 and parts[1].isdigit():
                return (int(parts[1]), len(parts))
            return (0, 0)
        return sorted(outbox_items(self.cfg.outbox), key=key)

    def messages(self) -> list[str]:
        """Plain text of every card in the outbox, in ledger order. Every HTML message is checked."""
        texts = []
        for name, payload in self._ordered():
            if "html" in payload:
                self.assertEqual(set(payload), {"html"}, name)
                for message in payload["html"]:
                    self.assertEqual(html_problems(message), [], message)
                    self.assertLessEqual(len(message), 4000)
                texts.append(card_text(payload["html"]))
        return texts

    def documents(self) -> list[tuple[str, bytes, str]]:
        """(filename, content, caption) of every document item, in ledger order."""
        found = []
        for name, payload in self._ordered():
            if "document" in payload:
                self.assertTrue(spool.is_document_name(payload["document"]))
                content = (self.cfg.outbox / payload["document"]).read_bytes()
                found.append((payload["filename"], content, payload["caption"]))
        return found

    def first_lines(self) -> list[str]:
        return [text.splitlines()[0] for text in self.messages()]


class LoopNotificationTests(OutboxMixin, SupervisorTestCase):
    def test_a_finished_mission_sends_the_finish_card_then_the_whole_report_as_a_document(self):
        self.make_outbox()
        self.play(fix("c1", "c2"))
        self.run_mission()
        (text,) = self.messages()
        lines = text.splitlines()
        self.assertEqual(lines[0], "✅ 완료 │ demo")
        self.assertEqual(lines[2], "🎯 달성 2 / 2 (100%) · 감독자가 직접 검사한 결과")
        self.assertEqual(lines[3], "▰" * 10)
        self.assertRegex(lines[4], r"^⏱ \d+시간 \d+분 · 반복 1회 · Sonnet 5\.5$")
        self.assertRegex(lines[5], r"^🕒 \d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST$")
        self.assertIn("결과물 받기 → /files demo", text)
        ((filename, content, caption),) = self.documents()
        self.assertEqual(filename, "report-demo.md")
        self.assertEqual(content.decode("utf-8"), self.report_text())
        self.assertEqual(html_problems(caption), [])
        names = [name for name, _ in self._ordered()]
        self.assertEqual(names[1], names[0] + "-report", "the report follows its finish card")

    def test_escalation_names_both_models(self):
        self.make_outbox()
        self.play(NOTHING, fix("c1", "c2"))
        self.run_mission(make_mission(limits={"escalate_after": 1, "give_up_after": 1}))
        self.assertEqual(self.first_lines(), ["⬆️ 모델 승격 │ demo", "✅ 완료 │ demo"])
        escalation = self.messages()[0]
        self.assertIn("Sonnet 5.5 → Opus 5.5", escalation)
        self.assertTrue(escalation.endswith(RUNNING_TODO))

    def test_stopped_time_title(self):
        self.make_outbox()
        self.play(NOTHING)
        self.run_mission(make_mission(limits={"mission_hours": 0.001}), clock=StepClock(20))
        (text,) = self.messages()
        self.assertEqual(text.splitlines()[0], "⏱️ 시간 초과 │ demo")
        self.assertIn("🎯 달성 0 / 2 (0%) · 감독자가 직접 검사한 결과", text)
        self.assertIn("시간이 다 되어 멈춤", text)

    def test_a_long_worker_outage_is_announced_once_after_thirty_minutes(self):
        self.make_outbox()
        self.play(*[RATE_LIMITED] * 8, fix("c1", "c2"))  # waits 5, 10, 20, 40 minutes
        time = FakeTime()
        self.run_mission(clock=time.clock, sleep=time.sleep)
        waiting = [text for text in self.messages() if text.startswith("⏸️")]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0].splitlines()[0], "⏸️ 모델 대기 │ demo")
        self.assertIn("35분째 기다리는 중", waiting[0])
        self.assertIn("이유: 오류 메시지: Error: 429 Too Many Requests (rate limit reached)", waiting[0])
        self.assertTrue(waiting[0].endswith(RUNNING_TODO))
        self.assertEqual(self.first_lines()[-1], "✅ 완료 │ demo")

    def test_a_short_outage_is_not_announced(self):
        self.make_outbox()
        self.play(RATE_LIMITED, RATE_LIMITED, fix("c1", "c2"))
        time = FakeTime()
        self.run_mission(clock=time.clock, sleep=time.sleep)
        self.assertEqual(self.first_lines(), ["✅ 완료 │ demo"])

    def test_a_resumed_run_never_repeats_a_notification(self):
        plan = [NOTHING, fix("c1"), fix("c2")]
        mission = make_mission(limits={"escalate_after": 1, "give_up_after": 1})
        self.make_outbox()
        self.play(*plan)
        self.run_mission(mission)
        expected = self.first_lines()
        total = len(self.records())
        for crash_after in range(1, total + 1):
            with self.subTest(crash_after=crash_after):
                self.new_world()
                self.make_outbox()
                self.play(*plan)
                real_append, count = Ledger.append, [0]

                def append(ledger, record_type, data):
                    record = real_append(ledger, record_type, data)
                    count[0] += 1
                    if count[0] == crash_after:
                        raise Crash
                    return record

                with mock.patch.object(Ledger, "append", append):
                    with self.assertRaises(Crash):
                        self.run_mission(mission)
                loop.run("demo", self.cfg)
                loop.run("demo", self.cfg)  # a finished mission run again sends nothing
                got = self.first_lines()  # whole texts differ only in the report's timestamps
                self.assertEqual(len(got), len(set(got)), "no notification is sent twice")
                self.assertLessEqual(set(got), set(expected))
                self.assertLessEqual(len(self.documents()), 1)

    def test_a_missing_outbox_changes_nothing(self):
        self.play(NOTHING, fix("c1", "c2"))
        self.run_mission(make_mission(limits={"escalate_after": 1}))
        with_nothing = ledger_summary(self.records())
        self.assertFalse(self.cfg.outbox.exists())
        self.new_world()
        self.make_outbox()
        self.play(NOTHING, fix("c1", "c2"))
        self.run_mission(make_mission(limits={"escalate_after": 1}))
        self.assertEqual(ledger_summary(self.records()), with_nothing)
        self.assertEqual(len(self.messages()), 2)
        self.assertEqual(len(self.documents()), 1)

    def test_a_broken_outbox_is_logged_and_the_mission_goes_on(self):
        self.cfg.outbox.parent.mkdir(parents=True)
        self.cfg.outbox.write_text("not a folder")
        self.play(fix("c1", "c2"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_mission(), "complete")
        self.assertIn("notification for demo record", err.getvalue())

    def test_a_report_over_the_document_limit_becomes_a_message(self):
        self.make_outbox()
        self.play(fix("c1", "c2"))
        with mock.patch("agentic.spool.DOCUMENT_MAX_BYTES", 100):
            self.run_mission()
        self.assertEqual(self.documents(), [])
        finish, too_large = self.messages()
        self.assertEqual(finish.splitlines()[0], "✅ 완료 │ demo")
        self.assertRegex(too_large, r"^보고서가 20MB를 넘어 보낼 수 없습니다 \(\d+\.\dMB\)$")
        self.assertEqual([name for name in os.listdir(self.cfg.outbox) if name.endswith(".doc")], [])


class PlanNotificationTests(OutboxMixin, PlannedTestCase):
    def test_plan_finished_counts_kept_and_dropped_criteria(self):
        self.make_outbox()
        self.plan_script(script={"critique": [{"veto": {"c2": "trivial: an empty file passes"}}]})
        self.play(fix("c1"))
        self.assertEqual(self.new(), 0, self.err)
        self.assertEqual(self.messages()[0].splitlines(), [
            "🧭 계획 완료 │ demo", "━━━━━━━━━━━━━━━━━━", "완료 조건 1개 · 제외 1개", "· c1 요구 사항 c1", RUNNING_TODO])

    def test_plan_finished_shows_at_most_five_criteria(self):
        self.make_outbox()
        self.plan_script(criteria=[f"c{n}" for n in range(1, 8)])
        self.play(fix(*[f"c{n}" for n in range(1, 8)]))
        self.assertEqual(self.new(), 0, self.err)
        lines = self.messages()[0].splitlines()
        self.assertEqual(lines[2], "완료 조건 7개")
        self.assertEqual(lines[3:8], [f"· c{n} 요구 사항 c{n}" for n in range(1, 6)])
        self.assertEqual(lines[8], RUNNING_TODO)

    def test_plan_finished_without_drops(self):
        self.make_outbox()
        self.play(fix("c1", "c2"))
        self.new()
        self.assertEqual(self.first_lines(), ["🧭 계획 완료 │ demo", "✅ 완료 │ demo"])
        self.assertIn("완료 조건 2개", self.messages()[0].splitlines())

    def test_plan_failed_sends_one_finish_card_with_the_reason(self):
        self.make_outbox()
        self.plan_script(script={"brief": [{"raw": "nope"}]})
        self.assertEqual(self.new(), 1)
        (final,) = self.messages()
        self.assertEqual(final.splitlines()[0], "❌ 계획 실패 │ demo")
        self.assertIn("요청을 더 구체적으로 다시 보내기", final)
        self.assertIn("계획 brief 단계 실패: there is no ```json fenced block and the output is not JSON", final)
        self.assertEqual([filename for filename, _, _ in self.documents()], ["report-demo.md"])

    def test_review_veto_and_no_check_data_in_any_notification(self):
        self.make_outbox()
        veto = {"c2": "trivial: SECRET_CONTENT_MARKER_c2 is easy"}
        self.plan_script(script={"critique": [{"veto": veto}, {}], "review": [{"findings": [FINDING]}, {}]})
        self.play(cheating_fix("c1", "c2"), fix("c1", "c2"))
        self.assertEqual(self.new(), 0, self.err)
        self.assertIn("🔍 꼼수 발견 │ demo", self.first_lines())
        veto_card = next(text for text in self.messages() if text.startswith("🔍"))
        self.assertIn("조건 1개", veto_card)
        self.assertTrue(veto_card.endswith(RUNNING_TODO))
        secrets = ["SECRET_CONTENT", "_agentic_check_", "import sys"]
        for c in self.mission_json()["criteria"]:
            secrets += [arg for arg in c["check"]["argv"] if len(arg) > 3] + list(c["check"].get("files", {}))
        everything = self.messages() + [content.decode("utf-8") for _, content, _ in self.documents()]
        for text in everything:
            for secret in secrets:
                self.assertNotIn(secret, text)


class NotifyFailureTests(OutboxMixin, SupervisorTestCase):
    def failure_message(self, sub_state: str) -> str:
        self.make_outbox()
        fake = FakeCommands(lambda argv: commands.CommandResult(0, sub_state + "\n", ""))
        with mock.patch("agentic.commands.run_command", fake):
            notify.notify_failure(self.cfg, "demo")
        self.assertEqual(fake.calls, [["systemctl", "show", "--property=SubState", "--value",
                                       "agentic@demo.service"]])
        (text,) = self.messages()
        return text

    def test_a_unit_about_to_restart(self):
        self.assertEqual(self.failure_message("auto-restart").splitlines(), [
            "💥 비정상 종료 │ demo", "━━━━━━━━━━━━━━━━━━", "감독자가 비정상 종료됨 — 자동으로 다시 시작합니다",
            RUNNING_TODO])

    def test_a_unit_systemd_gave_up_on(self):
        self.assertEqual(self.failure_message("failed").splitlines(), [
            "💥 비정상 종료 │ demo", "━━━━━━━━━━━━━━━━━━", "감독자가 비정상 종료됨 — 다시 시작하지 않습니다",
            "👉 지금 하실 일: /status demo 로 상태 보기"])


if __name__ == "__main__":
    unittest.main()
