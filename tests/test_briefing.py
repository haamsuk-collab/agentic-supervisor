"""The briefing model (SPEC-E.md sections 0-2): one test per rule row, ordering, numbering, honesty."""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import TimedTestCase
from agentic import briefing
from agentic.briefing import FAILED, NO_TODO, RUNNING_TODO, UNVERIFIABLE, NextInstruction

START = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)  # 14:00 KST
LADDER = [{"model": "anthropic/claude-sonnet-5-5", "thinking": "high", "iteration_minutes": 45},
          {"model": "anthropic/claude-opus-5-5", "thinking": "high", "iteration_minutes": 45}]

from unittest import mock
from agentic import cards, config, report
from cardcheck import card_text

class Ledger:
    """Synthetic ledger records with one minute between records."""

    def __init__(self, mission_id="demo", criteria=("c1", "c2"), **view):
        self.records: list[dict] = []
        self.view = {"id": mission_id, "goal": "g",
                     "criteria": [{"id": c, "text": f"요구 {c}"} for c in criteria],
                     "limits": {}, "ladder": LADDER, **view}
        self.iteration = 0

    def add(self, kind: str, data: dict, minutes: float = 1) -> "Ledger":
        moment = datetime.fromisoformat(self.records[-1]["ts"]) + timedelta(minutes=minutes) if self.records else START
        self.records.append({"seq": len(self.records) + 1, "ts": moment.isoformat(), "type": kind, "data": data})
        return self

    def start(self, invalid=()) -> "Ledger":
        self.add("mission_started", self.view)
        results = {c["id"]: {"passed": c["id"] in invalid, "exit_code": 0 if c["id"] in invalid else 1}
                   for c in self.view["criteria"]}
        return self.add("baseline", {"results": results, "invalid": list(invalid)})

    def iterate(self, passing=(), failing=(), model="anthropic/claude-sonnet-5-5") -> "Ledger":
        self.iteration += 1
        self.add("iteration_worker", {"iteration": self.iteration, "step": 0, "model": model, "thinking": "high",
                                      "active": [*passing, *failing], "note": "n"})
        for cid in passing:
            self.add("check_result", {"iteration": self.iteration, "criterion": cid, "passed": True, "exit_code": 0})
        for cid in failing:
            self.add("check_result", {"iteration": self.iteration, "criterion": cid, "passed": False, "exit_code": 1})
        return self

    def finish(self, status: str) -> "Ledger":
        return self.add("mission_finished", {"status": status, "iterations": self.iteration})

    def build(self, decisions=(), request=None, view=None) -> briefing.Briefing:
        return briefing.build(self.records, view or self.view, list(decisions), request)


def ids(n: int) -> tuple[str, ...]:
    return tuple(f"c{i}" for i in range(1, n + 1))


class RuleRowTests(TimedTestCase):
    def test_running(self):
        b = Ledger().start().iterate(passing=["c1"], failing=["c2"]).build()
        self.assertEqual((b.state, b.headline), ("running", "🛠️ 진행 중"))
        self.assertEqual(b.todo, [RUNNING_TODO])
        self.assertEqual(b.decisions, [])
        self.assertEqual(b.next_instructions, [NextInstruction("진행 상황 보기", "/status demo")])
        self.assertEqual(b.notices, ["지금: 작업 1단계 · 모델 Sonnet 5.5 · 반복 1회"])
        self.assertIsNone(b.ended_at)

    def test_planning(self):
        led = Ledger()
        led.add("plan_started", {"request_sha256": "x", "request_bytes": 10})
        led.add("plan_step", {"step": "brief", "round": 0, "attempt": 1, "model": "google-antigravity/gemini-4-argon",
                              "ok": True, "answer": {"goal": "g", "assumptions": ["가정 하나"]}})
        b = briefing.build(led.records, {"id": "demo"}, [], "요청 본문")
        self.assertEqual((b.state, b.headline), ("planning", "🧭 계획 중"))
        self.assertEqual(b.todo, [RUNNING_TODO])
        self.assertEqual(b.next_instructions, [NextInstruction("진행 상황 보기", "/status demo")])
        self.assertEqual(b.notices, ["지금: 계획 brief 단계 · 모델 Argon · 반복 0회"])
        self.assertEqual(len(b.ai_notes), 1, "notes are kept, but the ai_notes row waits until the mission stops")

    def test_complete(self):
        b = Ledger().start().iterate(passing=["c1", "c2"]).finish("complete").build()
        self.assertEqual((b.state, b.headline), ("complete", "✅ 완료"))
        self.assertEqual((b.achieved, b.total, b.percent), (2, 2, 100))
        self.assertEqual(b.todo, ["결과물 받기 → /files demo"])
        self.assertEqual(b.next_instructions, [NextInstruction("결과물 받기", "/files demo")])
        self.assertEqual((b.decisions, b.notices, b.remaining), ([], [], []))

    def test_ai_notes_when_the_mission_is_not_running(self):
        led = Ledger(assumptions=["섭씨로 표시한다"]).start().iterate(passing=["c1", "c2"])
        running = led.build(decisions=["- sqlite를 골랐다 (단순함)"])
        self.assertNotIn("AI가 정한 것", " ".join(running.todo))
        b = led.finish("complete").build(decisions=["- sqlite를 골랐다 (단순함)"])
        self.assertEqual(b.todo, ["결과물 받기 → /files demo", "AI가 정한 것 2건을 읽고, 틀린 것이 있으면 이어서 맡기기"])
        self.assertEqual(b.next_instructions[1], NextInstruction("AI가 정한 것을 고칠 때", "demo 이어서: (바꿀 점을 쓰세요)"))
        self.assertEqual((b.ai_notes.assumptions, b.ai_notes.decisions), (["섭씨로 표시한다"], ["- sqlite를 골랐다 (단순함)"]))

    def gave_up(self, achieved: int, total: int) -> briefing.Briefing:
        led = Ledger(criteria=ids(total)).start()
        passing, failing = ids(total)[:achieved], ids(total)[achieved:]
        led.iterate(passing=passing, failing=failing).iterate(passing=passing, failing=failing)
        led.add("gave_up", {"iteration": 2, "criteria": list(failing), "attempts": 2})
        return led.finish("partial").build()

    def test_failed_gave_up_at_80_percent_recommends_taking_the_result(self):
        b = self.gave_up(4, 5)
        self.assertEqual(b.percent, 80)
        self.assertEqual(b.todo, ["판단 1건"])
        (d,) = b.decisions
        self.assertEqual(d.subject, "통과하지 못한 조건 1개")
        self.assertEqual(d.facts, ["c5 요구 c5 — 2번 시도 후 포기"])
        self.assertEqual(d.options, [("A", "이 조건 없이 결과 받기"), ("B", "요구를 바꿔 이어서 맡기기")])
        self.assertEqual((d.recommended, d.reason), ("A", "이미 80%를 달성했습니다"))
        self.assertEqual(b.next_instructions, [NextInstruction("판단 1에서 B를 고를 때", "demo 이어서: 요구 c5은(는) …로 바꿔 줘")])

    def test_failed_gave_up_below_80_percent_recommends_changing_the_request(self):
        b = self.gave_up(3, 4)
        self.assertEqual(b.percent, 75)
        (d,) = b.decisions
        self.assertEqual(d.subject, "통과하지 못한 조건 1개")
        self.assertEqual((d.recommended, d.reason), ("B", "달성률이 75%라 결과를 그대로 쓰기 어렵습니다"))

    def test_failed_gave_up_writes_one_line_per_criterion(self):
        b = self.gave_up(1, 3)
        (d,) = b.decisions
        self.assertEqual(d.subject, "통과하지 못한 조건 2개")
        self.assertEqual(d.facts, ["c2 요구 c2 — 2번 시도 후 포기", "c3 요구 c3 — 2번 시도 후 포기"])
        self.assertEqual(b.next_instructions[0].text,
                         "demo 이어서: 요구 c2은(는) …로 바꿔 줘\n요구 c3은(는) …로 바꿔 줘")

    def test_failed_time(self):
        led = Ledger().start().iterate(passing=["c1"], failing=["c2"])
        led.add("stopped_time", {"iteration": 1, "elapsed_seconds": 100})
        b = led.finish("stopped_time").build()
        self.assertEqual((b.state, b.headline), ("stopped_time", "⏱️ 시간 초과"))
        self.assertEqual(b.todo, ["판단 1건"])
        (d,) = b.decisions
        self.assertEqual(d.subject, "시간이 다 되어 멈춤")
        self.assertEqual(d.options, [("A", "남은 조건을 이어서 맡기기"), ("B", "지금 결과로 끝내기")])
        self.assertEqual(d.recommended, "A")
        self.assertEqual(b.next_instructions, [NextInstruction("판단 1에서 A를 고를 때", "demo 이어서: 남은 조건을 마저 해 줘")])
        self.assertEqual([(i.mark, i.id, i.reason) for i in b.remaining], [(FAILED, "c2", "시간이 다 되어 멈춤")])

    def test_dropped_or_invalid(self):
        led = Ledger(criteria=ids(3))
        led.add("plan_started", {"request_sha256": "x", "request_bytes": 10})
        led.add("plan_finished", {"dropped": [{"id": "c9", "text": "요구 c9", "problem": "trivial: 빈 파일도 통과"}]})
        led.start(invalid=["c3"]).iterate(passing=["c1", "c2"]).finish("partial")
        b = led.build()
        self.assertEqual((b.achieved, b.total, b.percent), (2, 4, 50))
        self.assertEqual(b.todo, ["자동 검사를 못 만든 조건 2개를 직접 확인"])
        (d,) = b.decisions
        self.assertEqual(d.subject, "자동으로 확인할 수 없는 조건 2개")
        self.assertEqual(d.facts, ["c9 요구 c9 — 믿을 만한 검사를 만들지 못해 제외함: trivial: 빈 파일도 통과",
                                   "c3 요구 c3 — 작업 전에도 통과하는 검사라서 증거가 되지 못함"])
        self.assertEqual(d.options, [("A", "결과물을 받아 직접 확인"),
                                     ("B", "확인 방법이 드러나게 요구를 구체적으로 바꿔 이어서 맡기기")])
        self.assertEqual(d.recommended, "A")
        self.assertEqual(b.next_instructions, [NextInstruction("결과물 받기", "/files demo")])
        self.assertEqual([(i.mark, i.id) for i in b.remaining], [(UNVERIFIABLE, "c3"), (UNVERIFIABLE, "c9")])

    def test_suspect(self):
        led = Ledger().start().iterate(passing=["c1", "c2"])
        led.add("review", {"iteration": 1, "ok": True, "findings": [
            {"criterion": "c1", "file": "solution.py", "quote": "CASES = {'2+2': '4'}", "problem": "p", "verified": True},
            {"criterion": "c2", "file": "x.py", "quote": "unverified quote here", "problem": "p", "verified": False}]})
        b = led.finish("complete").build()
        self.assertEqual(b.achieved, 2, "a suspicion is reported, the check result still counts")
        self.assertEqual(b.todo, ["결과물 받기 → /files demo", "판단 1건"])
        (d,) = b.decisions
        self.assertEqual(d.subject, "리뷰어가 꼼수를 의심한 조건 1개")
        self.assertEqual(d.facts, ["c1 · solution.py: \"CASES = {'2+2': '4'}\""])
        self.assertEqual(d.options, [("A", "일반적으로 동작하게 이어서 맡기기"), ("B", "그대로 받기")])
        self.assertEqual(d.recommended, "A")
        self.assertEqual(b.next_instructions[-1], NextInstruction(
            "판단 1에서 A를 고를 때", "demo 이어서: 특정 입력에만 맞춘 부분을 일반적으로 동작하게 고쳐 줘"))

    def test_plan_failed(self):
        led = Ledger()
        led.add("plan_started", {"request_sha256": "x", "request_bytes": 10})
        led.add("plan_failed", {"step": "brief", "kind": "no_json", "reason": "there is no json"})
        led.add("mission_finished", {"status": "plan_failed", "iterations": 0})
        b = briefing.build(led.records, {"id": "demo"}, [], "날씨 프로그램을 만들어 주세요\n")
        self.assertEqual((b.state, b.headline, b.total, b.percent), ("plan_failed", "❌ 계획 실패", 0, 0))
        self.assertEqual(b.todo, ["요청을 더 구체적으로 다시 보내기"])
        self.assertEqual(b.next_instructions, [NextInstruction(
            "고쳐서 다시 보낼 요청", "날씨 프로그램을 만들어 주세요\n(입력·출력 형식과 성공 기준을 구체적으로 적어 주세요)")])
        self.assertEqual(b.notices, ["계획 brief 단계 실패: there is no json"])

    def test_a_failed_review(self):
        led = Ledger().start().iterate(passing=["c1", "c2"])
        led.add("review", {"iteration": 1, "ok": False, "findings": []})
        b = led.finish("complete").build()
        self.assertEqual(b.notices, ["꼼수 리뷰를 하지 못했습니다"])
        self.assertEqual(b.decisions, [])

    def test_outage_waiting(self):
        led = Ledger().start()
        led.add("worker_unavailable", {"wait_seconds": 0, "attempt": 1})
        led.add("worker_unavailable", {"wait_seconds": 300, "attempt": 2})
        led.add("worker_unavailable", {"wait_seconds": 600, "attempt": 3}, minutes=5)
        led.add("stopped_time", {"iteration": 0}, minutes=7)  # the deadline cut the 10-minute wait at 7
        b = led.finish("stopped_time").build()
        self.assertEqual(b.notices, ["모델을 쓸 수 없어 기다린 시간: 0시간 12분"])

    def test_no_outage_no_notice(self):
        b = Ledger().start().iterate(passing=["c1", "c2"]).finish("complete").build()
        self.assertEqual(b.notices, [])

    def test_parent(self):
        b = Ledger(parent="wordfreq").start().iterate(passing=["c1", "c2"]).finish("complete").build()
        self.assertEqual((b.parent, b.notices), ("wordfreq", ["이어서 한 미션: wordfreq"]))


class OrderTests(TimedTestCase):
    def everything(self) -> briefing.Briefing:
        """gave_up, dropped/invalid, suspect, review failed later... every finished-mission row at once."""
        led = Ledger(criteria=ids(4), assumptions=["가정 A"], parent="base")
        led.add("plan_started", {"request_sha256": "x", "request_bytes": 10})
        led.add("plan_finished", {"dropped": [{"id": "c9", "text": "요구 c9", "problem": "flaky"}]})
        led.start(invalid=["c4"]).iterate(passing=["c1"], failing=["c2", "c3"])
        led.add("worker_unavailable", {"wait_seconds": 300, "attempt": 1})
        led.add("gave_up", {"iteration": 1, "criteria": ["c2"], "attempts": 1}, minutes=10)
        led.iterate(passing=["c1"], failing=["c3"])
        led.add("review", {"iteration": 2, "ok": True, "findings": [
            {"criterion": "c1", "file": "a.py", "quote": "if x == 'held-out': return 7", "problem": "p",
             "verified": True}]})
        led.add("stopped_time", {"iteration": 2})
        return led.finish("stopped_time").build(decisions=["- 결정 B"])

    def test_lists_follow_the_table_order_and_decisions_are_counted_once(self):
        b = self.everything()
        self.assertEqual(b.todo, ["AI가 정한 것 2건을 읽고, 틀린 것이 있으면 이어서 맡기기", "판단 3건",
                                  "자동 검사를 못 만든 조건 2개를 직접 확인"])
        self.assertEqual([d.subject for d in b.decisions], [
            "통과하지 못한 조건 1개", "시간이 다 되어 멈춤", "자동으로 확인할 수 없는 조건 2개", "리뷰어가 꼼수를 의심한 조건 1개"])
        self.assertEqual([n.label for n in b.next_instructions], [
            "AI가 정한 것을 고칠 때", "판단 1에서 B를 고를 때", "판단 2에서 A를 고를 때", "결과물 받기", "판단 4에서 A를 고를 때"])
        self.assertEqual(b.notices, ["모델을 쓸 수 없어 기다린 시간: 0시간 5분", "이어서 한 미션: base"])
        self.assertEqual([(i.mark, i.id) for i in b.remaining],
                         [(FAILED, "c2"), (FAILED, "c3"), (UNVERIFIABLE, "c4"), (UNVERIFIABLE, "c9")])

    def test_the_same_instruction_is_given_once(self):
        led = Ledger()
        led.add("plan_started", {"request_sha256": "x", "request_bytes": 10})
        led.add("plan_finished", {"dropped": [{"id": "c9", "text": "요구 c9", "problem": "flaky"}]})
        b = led.start().iterate(passing=["c1", "c2"]).finish("complete").build()
        self.assertEqual(b.next_instructions, [NextInstruction("결과물 받기", "/files demo")])
        self.assertEqual(b.todo, ["결과물 받기 → /files demo", "자동 검사를 못 만든 조건 1개를 직접 확인"])

    def test_no_row_leaves_the_todo_list_empty(self):
        led = Ledger().start().iterate(passing=["c1", "c2"])
        b = led.finish("stopped_time").build()
        self.assertEqual((b.todo, b.decisions, b.next_instructions, b.notices), ([], [], [], []))
        self.assertEqual(NO_TODO, "없음 — 지금 하실 일은 없습니다")

    def test_progress_bar(self):
        self.assertEqual(briefing.bar(0), "▱" * 10)
        self.assertEqual(briefing.bar(82), "▰" * 8 + "▱" * 2)
        self.assertEqual(briefing.bar(100), "▰" * 10)
        self.assertEqual(briefing.bar(4), "▱" * 10)


class HonestyTests(TimedTestCase):
    def test_achieved_counts_only_criteria_whose_latest_check_passed(self):
        led = Ledger(criteria=ids(4)).start()
        led.iterate(passing=["c1", "c3"], failing=["c2", "c4"])
        led.iterate(passing=["c2", "c3"], failing=["c1", "c4"])  # c1 passed once, then failed again
        led.add("check_result", {"iteration": 2, "criterion": "c4", "passed": True, "exit_code": 1})  # not exit 0
        b = led.build()
        last = {}
        for rec in led.records:
            if rec["type"] == "check_result":
                last[rec["data"]["criterion"]] = rec["data"]
        expected = sum(1 for d in last.values() if d["passed"] and d["exit_code"] == 0)
        self.assertEqual(b.achieved, expected)
        self.assertEqual(sorted(i.id for i in b.evidence if i.mark == "✅"), ["c2", "c3"])

    def test_a_review_veto_takes_the_criterion_back_until_a_new_check_passes(self):
        led = Ledger().start().iterate(passing=["c1", "c2"])
        led.add("review", {"iteration": 1, "ok": True, "findings": [
            {"criterion": "c1", "file": "a.py", "quote": "q" * 30, "problem": "p", "verified": True}]})
        led.add("review_veto", {"iteration": 1, "findings": [{"criterion": "c1", "file": "a.py", "quote": "q" * 30,
                                                              "problem": "p"}]})
        self.assertEqual(led.build().achieved, 1)

    def test_dropped_and_invalid_criteria_are_never_achieved(self):
        led = Ledger(criteria=ids(3))
        led.add("plan_started", {"request_sha256": "x", "request_bytes": 10})
        led.add("plan_finished", {"dropped": [{"id": "c9", "text": "요구 c9", "problem": "flaky"}]})
        led.start(invalid=["c3"]).iterate(passing=["c1", "c2"])
        led.add("check_result", {"iteration": 1, "criterion": "c3", "passed": True, "exit_code": 0})
        b = led.finish("partial").build()
        self.assertEqual((b.achieved, b.total), (2, 4))
        marks = {i.id: i.mark for i in b.evidence}
        self.assertEqual((marks["c3"], marks["c9"]), (UNVERIFIABLE, UNVERIFIABLE))

    def test_an_inherited_pass_is_a_supervisor_check(self):
        led = Ledger(parent="base")
        led.add("mission_started", led.view)
        led.add("baseline", {"results": {"c1": {"passed": True, "exit_code": 0}, "c2": {"passed": False, "exit_code": 1}},
                             "invalid": [], "inherited_pass": ["c1"]})
        self.assertEqual(led.build().achieved, 0, "SPEC-F section 2: not before the canary held")
        led.add("canary", {"iteration": 0, "results": {"c1": {"held": True, "exit_code": 1}}, "changed_files": 1})
        b = led.build()
        self.assertEqual(b.achieved, 1)
        (c1,) = [i for i in b.evidence if i.id == "c1"]
        self.assertEqual((c1.mark, c1.exit_code, c1.attempts), ("✅", 0, 0))
        self.assertEqual(c1.last_check, "2026-10-18 14:01:00 KST")

    def test_no_todo_decision_or_instruction_carries_ai_written_text(self):
        marker_a, marker_d = "ASSUMPTION_MARKER", "DECISIONS_MARKER"
        led = Ledger(criteria=ids(3), assumptions=[f"{marker_a} 가정"])
        led.start(invalid=["c3"]).iterate(passing=["c1"], failing=["c2"])
        led.add("gave_up", {"iteration": 1, "criteria": ["c2"], "attempts": 1})
        led.add("review", {"iteration": 1, "ok": True, "findings": [
            {"criterion": "c1", "file": "a.py", "quote": "if x == 'held-out': return 7", "problem": "p", "verified": True}]})
        led.add("stopped_time", {"iteration": 1})
        b = led.finish("stopped_time").build(decisions=[f"- {marker_d} 결정"], request="요청")
        texts = list(b.todo)
        for d in b.decisions:
            texts += [d.subject, *d.facts, *(t for _, t in d.options), d.reason]
        for n in b.next_instructions:
            texts += [n.label, n.text]
        self.assertGreater(len(texts), 10)
        for text in texts:
            self.assertNotIn(marker_a, text)
            self.assertNotIn(marker_d, text)
        self.assertEqual(len(b.ai_notes), 2, "the notes are kept apart, not lost")

    def test_build_is_a_pure_function(self):
        led = Ledger().start().iterate(passing=["c1"], failing=["c2"]).finish("partial")
        self.assertEqual(led.build(decisions=["x"]), led.build(decisions=["x"]))

    def test_briefing_states_when_supervisor_check_evidence_is_from(self):
        led = Ledger(criteria=ids(2)).start()
        self.assertIsNone(led.build().checked_at)
        self.assertIsNone(led.build().evidence_line)
        self.assertNotIn("감독자 검사 기준", report.render_briefing(led.build()))

        led.iterate(passing=["c1"], failing=["c2"])  # check_result records at 14:03 and 14:04 KST
        b_iter = led.build()
        self.assertEqual(b_iter.checked_at, "2026-10-18 14:04:00 KST")
        self.assertEqual(b_iter.evidence_line, "감독자 검사 기준 2026-10-18 14:04:00 KST")
        self.assertIn("감독자 검사 기준 2026-10-18 14:04:00 KST", report.render_briefing(b_iter))

        led.add("canary", {"iteration": 1, "results": {"c1": {"held": True, "exit_code": 1}}, "changed_files": 1})
        led.finish("partial")  # mission_finished at 14:06 KST, later than the canary at 14:05 KST
        b = led.build()
        self.assertEqual(b.checked_at, "2026-10-18 14:05:00 KST")
        self.assertEqual(b.evidence_line, "감독자 검사 기준 2026-10-18 14:05:00 KST")
        self.assertIn("감독자 검사 기준 2026-10-18 14:05:00 KST", report.render_briefing(b))
        self.assertIn("감독자 검사 기준 2026-10-18 14:05:00 KST", card_text(cards.finish_card(b)))

        with mock.patch.object(config, "LANGUAGE", "en"):
            b_en = led.build()
            self.assertEqual(b_en.evidence_line, "Supervisor check as of 2026-10-18 14:05:00 KST")
            self.assertIn("Supervisor check as of 2026-10-18 14:05:00 KST", report.render_briefing(b_en))
            self.assertIn("Supervisor check as of 2026-10-18 14:05:00 KST", card_text(cards.finish_card(b_en)))


if __name__ == "__main__":
    unittest.main()
