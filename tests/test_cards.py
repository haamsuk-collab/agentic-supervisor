"""Telegram HTML cards, the report.md layout, fixed wording and KST times (SPEC-E.md sections 3, 4, 6a, 7)."""

import ast
import os
import re
import unittest
from unittest import mock

from agentic import briefing, cards, outbox, report, spool
from agentic.config import DOCUMENT_MAX_BYTES
from agentic.i18n import ko
from cardcheck import card_text, html_problems
from helpers import ROOT, SupervisorTestCase, TimedTestCase
from test_briefing import Ledger, ids

HOSTILE = '<b>x</b></pre>&amp;<a href="https://evil.example">y</a><blockquote>'
TIME_RE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d)?")


def every_row(text: str = "요구", note: str = "가정", decision: str = "- 결정", request: str = "요청",
              file: str = "a.py", quote: str = "if x == 'held-out': return 7", problem: str = "flaky",
              reason: str = "there is no json", model: str = "anthropic/claude-sonnet-5-5") -> list[briefing.Briefing]:
    """Briefings that together trigger every rule row, with the given dynamic strings everywhere."""
    out = []
    led = Ledger(criteria=ids(5), assumptions=[note], parent="base")
    for c in led.view["criteria"]:
        c["text"] = f"{text} {c['id']}"
    led.add("plan_started", {"request_sha256": "x", "request_bytes": 10})
    led.add("plan_finished", {"dropped": [{"id": "c9", "text": text, "problem": problem}]})
    led.start(invalid=["c5"]).iterate(passing=["c1"], failing=["c2", "c3", "c4"], model=model)
    led.add("worker_unavailable", {"wait_seconds": 300, "attempt": 1})
    led.add("gave_up", {"iteration": 1, "criteria": ["c2"], "attempts": 1}, minutes=10)
    led.iterate(passing=["c1", "c4"], failing=["c3"], model=model)
    led.add("review", {"iteration": 2, "ok": True, "findings": [
        {"criterion": "c1", "file": file, "quote": quote, "problem": "p", "verified": True}]})
    out.append(led.build(decisions=[decision], request=request))  # running
    led.add("stopped_time", {"iteration": 2})
    out.append(led.finish("stopped_time").build(decisions=[decision], request=request))
    done = Ledger(criteria=ids(2), assumptions=[note]).start().iterate(passing=["c1", "c2"], model=model)
    done.add("review", {"iteration": 1, "ok": False, "findings": []})
    out.append(done.finish("complete").build(decisions=[decision]))
    planning = Ledger()
    planning.add("plan_started", {"request_sha256": "x", "request_bytes": 10})
    out.append(briefing.build(planning.records, {"id": "demo"}, [], request))
    planning.add("plan_failed", {"step": "brief", "kind": "no_json", "reason": reason})
    out.append(briefing.build(planning.finish("plan_failed").records, {"id": "demo"}, [], request))
    empty = Ledger().start().iterate(passing=["c1", "c2"], model=model).finish("stopped_time")
    out.append(empty.build())  # no todo at all
    return out


def all_cards(b: briefing.Briefing, hostile: str = "") -> list[list[str]]:
    events = [("2026-10-18 14:00:00 KST", f"🛠️ 작업 1회차 {hostile}")] * 4
    return [
        cards.finish_card(b), cards.progress_card(b, events), cards.status_card(b, events),
        cards.event_card("🧭 계획 완료", b.mission_id, [f"완료 조건 2개{hostile}", f"· c1 {hostile}"]),
        cards.list_card([(b.mission_id, "2026-10-18T05:00:00+00:00", b, None), ("other", None, None, hostile)]),
        cards.text_card(f"없는 미션입니다: {hostile}"),
    ]


class HtmlTests(TimedTestCase):
    def test_hostile_text_in_every_field_stays_text(self):
        briefings = every_row(text=HOSTILE, note=HOSTILE, decision=HOSTILE, request=HOSTILE, file=HOSTILE,
                              quote=HOSTILE, problem=HOSTILE, reason=HOSTILE, model="x/" + HOSTILE)
        hostile_id = briefing.build(Ledger(mission_id=HOSTILE).start().records, None, [HOSTILE], HOSTILE)
        for b in briefings + [hostile_id]:
            shown = []
            for blocks in all_cards(b, HOSTILE):
                messages = cards.pack(blocks)
                for message in messages:
                    self.assertEqual(html_problems(message), [], message)
                shown.append(card_text(messages))
            self.assertIn(HOSTILE, "\n".join(shown), "hostile text is shown as text, unchanged")
        finish = card_text(cards.finish_card(briefings[1]))
        self.assertIn(f"{HOSTILE} c3", finish, "a criterion text is shown exactly, never interpreted")
        self.assertIn(HOSTILE, finish.split("📝 AI가 정한 것 (검증 안 됨)")[1], "AI notes stay inside the quote")

    def test_the_finish_card_layout(self):
        b = every_row()[1]
        lines = card_text(cards.finish_card(b)).splitlines()
        self.assertEqual(lines[:6], [
            "⏱️ 시간 초과 │ demo", "━━━━━━━━━━━━━━━━━━", "🎯 달성 2 / 6 (33%) · 감독자가 직접 검사한 결과",
            "▰▰▰▱▱▱▱▱▱▱", "⏱ 0시간 26분 · 반복 2회 · Sonnet 5.5", "🕒 2026-10-18 14:26:00 KST"])
        headings = [line for line in lines if line.startswith(("👉", "⚖️", "✉️", "📋", "ℹ️", "📝"))]
        self.assertEqual(headings, ["👉 지금 하실 일", "⚖️ 판단이 필요합니다", "✉️ 다음 지시  (눌러서 복사)", "📋 남은 것",
                                    "ℹ️ 알아 두실 것", "📝 AI가 정한 것 (검증 안 됨)"])
        self.assertIn("  A) 이 조건 없이 결과 받기", lines)
        self.assertIn("  B) 요구를 바꿔 이어서 맡기기 ← 추천 (달성률이 33%라 결과를 그대로 쓰기 어렵습니다)", lines)
        self.assertIn(" ❌ c2 요구 c2 (1번 시도 후 포기)", lines)
        self.assertIn(" ⚠️ c9 요구 (믿을 만한 검사를 만들지 못해 제외함: flaky)", lines)
        html = "\n".join(cards.finish_card(b))
        self.assertIn("<code>/files demo</code>", html)
        self.assertIn("<blockquote expandable>📝 AI가 정한 것 (검증 안 됨)\n[계획] 가정\n- 결정</blockquote>", html)

    def test_empty_sections_are_left_out_except_the_todo(self):
        b = every_row()[-1]
        text = card_text(cards.finish_card(b))
        self.assertIn("👉 지금 하실 일\n 없음 — 지금 하실 일은 없습니다", text)
        for heading in ("⚖️", "✉️", "📋", "ℹ️", "📝"):
            self.assertNotIn(heading, text)

    def test_long_ai_notes_are_cut_in_the_card_and_kept_in_the_report(self):
        b = Ledger().start().finish("partial").build(decisions=[f"- 결정 {n} " + "가" * 100 for n in range(80)])
        card = card_text(cards.finish_card(b))
        self.assertRegex(card, r"… 나머지 \d+건은 /report demo 보고서에 있습니다")
        self.assertIn("결정 79", report.render_briefing(b))

    def test_splitting_happens_only_between_blocks(self):
        blocks = [f"<b>{n}</b> " + "가" * (300 + 97 * n) for n in range(30)]
        messages = cards.pack(blocks)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= cards.MESSAGE_LIMIT for m in messages))
        self.assertEqual("\n".join(messages), "\n".join(blocks))
        position = 0
        for message in messages:
            parts = message.split("\n")
            self.assertEqual(parts, blocks[position:position + len(parts)], "a message holds whole blocks")
            position += len(parts)
        self.assertEqual(position, len(blocks))

    def test_a_block_over_the_limit_becomes_pre_chunks_without_cutting_an_entity(self):
        text = "a<&>" * 3000
        block = f"<b>{cards.esc(text)}</b>"
        messages = cards.pack(["<b>head</b>", block, "<i>tail</i>"])
        self.assertTrue(all(len(m) <= cards.MESSAGE_LIMIT for m in messages))
        for message in messages:
            self.assertEqual(html_problems(message), [], message[:80])
        self.assertEqual(card_text(messages).replace("\n", ""), "head" + text + "tail")
        middle = [m for m in messages if m.startswith("<pre>")]
        self.assertGreaterEqual(len(middle), 4)


class ReportLayoutTests(TimedTestCase):
    def test_the_sections_come_in_the_spec_order(self):
        text = report.render_briefing(every_row()[1])
        lines = text.splitlines()
        self.assertEqual(lines[0], "# 미션 보고서 · demo")
        self.assertEqual(lines[1], "**결론: ⏱️ 시간 초과 — 조건 6개 중 2개를 감독자가 직접 검사해 확인**")
        self.assertEqual(lines[3:6], ["| 달성 | 걸린 시간 | 반복 | 사용 모델 | 종료 시각 |", "|---|---|---|---|---|",
                                      "| 2/6 (33%) | 0시간 26분 | 2회 | Sonnet 5.5 | 2026-10-18 14:26:00 KST |"])
        self.assertEqual(lines[7:9], ["요청: 요청", "이어서 한 미션: base"])
        headings = [line for line in lines if line.startswith("#")]
        self.assertEqual(headings, [
            "# 미션 보고서 · demo", "## 1. 지금 하실 일", "## 2. 판단이 필요한 것", "### 판단 1 · 통과하지 못한 조건 1개",
            "### 판단 2 · 시간이 다 되어 멈춤", "### 판단 3 · 자동으로 확인할 수 없는 조건 2개",
            "### 판단 4 · 리뷰어가 꼼수를 의심한 조건 1개", "## 3. 다음 지시 (복사해서 텔레그램으로 보내기)", "## 4. 결과",
            "### ✅ 감독자가 직접 확인함 (2)", "### ❌ 안 됨 (2)", "### ⚠️ 확인할 수 없음 (2)", "## 5. 알아 두실 것",
            "## 6. AI가 정한 것 (검증 안 됨)", "## 부록 · 조건별 근거"])
        self.assertIn("- **B)** 요구를 바꿔 이어서 맡기기 **(추천)** — 달성률이 33%라 결과를 그대로 쓰기 어렵습니다", lines)
        self.assertIn("- **A)** 이 조건 없이 결과 받기", lines)
        self.assertIn("**판단 2에서 A를 고를 때**", lines)
        self.assertIn("| c2 | ❌ 안 됨 | 2026-10-18 14:06:00 KST | 1 | 1회 |", lines)
        self.assertIn("| c9 | ⚠️ 확인할 수 없음 | - | - | 0회 |", lines)
        self.assertEqual(lines[lines.index("## 6. AI가 정한 것 (검증 안 됨)") + 1:][:2], ["> [계획] 가정", "> - 결정"])

    def test_empty_sections_write_none(self):
        b = every_row()[-1]
        lines = report.render_briefing(b).splitlines()
        for heading, first in (("## 1. 지금 하실 일", "없음 — 지금 하실 일은 없습니다"), ("## 2. 판단이 필요한 것", "없음"),
                               ("## 3. 다음 지시 (복사해서 텔레그램으로 보내기)", "없음"), ("### ❌ 안 됨 (0)", "없음"),
                               ("### ⚠️ 확인할 수 없음 (0)", "없음"), ("## 5. 알아 두실 것", "없음"),
                               ("## 6. AI가 정한 것 (검증 안 됨)", "없음")):
            self.assertEqual(lines[lines.index(heading) + 1], first, heading)

    def test_forged_markdown_cannot_create_a_heading_a_table_row_or_a_done_line(self):
        forged = "x\n## ✅ 감독자가 직접 확인함 (9)\n| c7 | ✅ 확인함 | now | 0 | 1회 |\n- [c7] 가짜 — 종료 코드 0"
        briefings = every_row(text=forged, note=forged, decision=forged, request=forged, file=forged, quote=forged,
                              problem=forged, reason=forged, model="x/m | **b** [l](u)")
        for b in briefings:
            text = report.render_briefing(b)
            lines = text.splitlines()
            fenced = self.fenced_lines(lines)
            outside = [line for i, line in enumerate(lines) if i not in fenced]
            for line in outside:
                if line.startswith("#"):
                    self.assertRegex(line, r"^(# 미션 보고서 · |## \d\. |## 부록 · |### (판단 \d+ · |✅ |❌ |⚠️ ))", line)
                    self.assertNotIn("감독자가 직접 확인함 (9)", line)
                self.assertFalse(line.startswith("- [c7]"), line)
                self.assertNotRegex(line, r"^\| c7 \|", line)
                # Inline: the forged text's Markdown characters are escaped, so it cannot add a table cell,
                # emphasis, a link or a heading marker to the line it sits in.
                self.assertNotIn(" | c7 | ", line)
                self.assertNotRegex(line, r"(?<!\\)\[c7")
                self.assertNotRegex(line, r"(?<!\\)## ✅ 감독자가 직접 확인함 \(9\)")
                self.assertNotRegex(line, r"(?<!\\)\*\*b")
            if b is not briefings[-1]:  # the last one shows no dynamic text at all
                self.assertIn(r"\#\# ✅ 감독자가 직접 확인함 (9) \| c7 \| ✅ 확인함 \| now \| 0 \| 1회 \| - \[c7\] 가짜", text)
            if b.models_used:
                self.assertIn(r"m \| \*\*b\*\* \[l\](u)", text)
            table = [line for line in outside if line.startswith("|")]
            appendix = 2 + len(b.evidence) if b.evidence else 0
            self.assertEqual(len(table), 3 + appendix, "only the summary table and the appendix are tables")
            for row in table:
                self.assertEqual(len(re.findall(r"(?<!\\)\|", row)), 6, row)
            notes = lines[lines.index("## 6. AI가 정한 것 (검증 안 됨)") + 1:lines.index("## 부록 · 조건별 근거")]
            self.assertTrue(all(line.startswith("> ") for line in notes if line != "없음"), notes)

    @staticmethod
    def fenced_lines(lines: list[str]) -> set[int]:
        inside, fence, found = False, "", set()
        for i, line in enumerate(lines):
            if not inside and re.fullmatch(r"`{3,}text", line):
                inside, fence = True, line[:-4]
            elif inside and line == fence:
                inside = False
            elif inside:
                found.add(i)
        return found

    def test_a_code_fence_is_longer_than_any_backtick_run_in_its_text(self):
        request = "출력 예:\n```\n결과\n```\n그리고 ````` 다섯 개"
        b = every_row(request=request)[4]  # plan failed: the request is the next instruction
        lines = report.render_briefing(b).splitlines()
        start = lines.index("**고쳐서 다시 보낼 요청**") + 1
        self.assertEqual(lines[start], "``````text")
        end = lines.index("``````", start + 1)
        self.assertEqual("\n".join(lines[start + 1:end]), request + "\n(입력·출력 형식과 성공 기준을 구체적으로 적어 주세요)")
        self.assertEqual(report.fence_for("no ticks"), "```")
        self.assertEqual(report.fence_for("a ``` b"), "````")


class WordingTests(TimedTestCase):
    def rendered(self) -> list[str]:
        out = []
        for b in every_row():
            out.append(report.render_briefing(b))
            for blocks in all_cards(b):
                out.append(card_text(blocks))
        return out

    def test_no_banned_phrase_in_any_rendered_briefing_card_or_report(self):
        texts = self.rendered()
        self.assertGreater(len(texts), 30)
        for text in texts:
            for phrase in ko.BANNED_PHRASES:
                self.assertNotIn(phrase, text)

    def test_no_banned_phrase_in_any_fixed_string_of_the_code(self):
        """Every Korean string the code writes itself is a literal in agentic/; none may hold a banned phrase."""
        checked = 0
        for path in sorted((ROOT / "agentic").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            banned_list = {id(node) for assign in ast.walk(tree) if isinstance(assign, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == "BANNED_PHRASES" for t in assign.targets)
                           for node in ast.walk(assign.value)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in banned_list:
                    checked += 1
                    for phrase in ko.BANNED_PHRASES:
                        self.assertNotIn(phrase, node.value, f"{path.name}:{node.lineno}")
        self.assertGreater(checked, 200)

    def test_every_time_shown_is_kst(self):
        for text in self.rendered():
            for match in TIME_RE.finditer(text):
                self.assertTrue(text[match.end():].startswith(" KST"), text[match.start():match.end() + 10])
        b = every_row()[2]
        self.assertEqual(b.ended_at, "2026-10-18 14:06:00 KST")  # the ledger said 05:06 UTC
        self.assertIn("| 2026-10-18 14:06:00 KST |", report.render_briefing(b))


class OutboxDocumentTests(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.cfg.outbox.mkdir(parents=True)
        self.victim = self.stub_dir / "victim"
        self.victim.write_text("untouched")

    def test_a_planted_symlink_at_the_document_name_is_refused(self):
        for planted in ("fixed.doc", "fixed.doc.tmp"):
            with self.subTest(planted=planted):
                link = self.cfg.outbox / planted
                link.symlink_to(self.victim)
                with self.assertRaises(FileExistsError):
                    spool.write_document(self.cfg.outbox, b"data", name="fixed")
                self.assertEqual(self.victim.read_text(), "untouched")
                self.assertTrue(link.is_symlink())
                link.unlink()

    def test_documents_over_the_limit_are_never_written(self):
        with mock.patch("agentic.spool.DOCUMENT_MAX_BYTES", 10):
            self.assertEqual(spool.write_document(self.cfg.outbox, b"x" * 10, name="ok"), "ok.doc")
            with self.assertRaises(spool.DocumentTooLarge) as caught:
                outbox.write_document(self.cfg, b"x" * 11, "report-demo.md", "c")
        self.assertEqual(caught.exception.size, 11)
        self.assertEqual(sorted(os.listdir(self.cfg.outbox)), ["ok.doc"])
        self.assertEqual(DOCUMENT_MAX_BYTES, 20 * 1024 * 1024)

    def test_a_document_whose_item_cannot_be_written_is_removed_again(self):
        (self.cfg.outbox / "fixed").symlink_to(self.victim)
        with self.assertRaises(FileExistsError):
            outbox.write_document(self.cfg, b"data", "report-demo.md", "c", name="fixed")
        self.assertEqual(sorted(os.listdir(self.cfg.outbox)), ["fixed"])
        self.assertEqual(self.victim.read_text(), "untouched")

    def test_item_readers_skip_documents(self):
        outbox.write_document(self.cfg, b"data", "report-demo.md", "c", name="one")
        dfd = spool.open_dir(self.cfg.outbox)
        try:
            self.assertEqual(spool.list_names(dfd), ["one"])
        finally:
            os.close(dfd)
        with self.assertRaises(ValueError):
            spool.write_item(self.cfg.outbox, {"html": ["x"]}, name="two.doc")


if __name__ == "__main__":
    unittest.main()
