"""English and Korean rendering of briefings, cards, reports and core supervisor texts (SPEC-G.md section 2)."""

import ast
import json
import re
import unittest
from unittest import mock

from helpers import ROOT, TimedTestCase
from agentic import briefing, cards, config, daily, outage, report, results
from agentic.i18n import en, ko, t
from cardcheck import card_text, html_problems
from test_briefing import Ledger, ids
from test_cards import TIME_RE, every_row

HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]")
CORE_MODULES = (
    "briefing.py", "cards.py", "report.py", "results.py",
    "loop.py", "outage.py", "daily.py", "planner.py", "config.py",
)


_ORIG_LEDGER_INIT = Ledger.__init__


def _neutral_ledger_init(self, mission_id="demo", criteria=("c1", "c2"), **view):
    _ORIG_LEDGER_INIT(self, mission_id=mission_id, criteria=criteria, **view)
    for c in self.view["criteria"]:
        c["text"] = f"Requirement {c['id']}"


def _neutral_briefings() -> list[briefing.Briefing]:
    with mock.patch("test_briefing.Ledger.__init__", _neutral_ledger_init):
        out = every_row(
            text="Requirement",
            note="Assume UTF-8 input",
            decision="- chose sqlite (stdlib)",
            request="Build the command-line tool",
            file="app.py",
            quote="if x == 'held-out': return 7",
            problem="flaky",
            reason="there is no json",
        )
    # Also cover the three check-strengthening notice variants and multi-criterion canary failure.
    for ok, second in ((False, False), (True, False), (True, True)):
        led = Ledger(criteria=ids(2))
        for c in led.view["criteria"]:
            c["text"] = f"Requirement {c['id']}"
        led.start().iterate(passing=["c1", "c2"])
        led.add("canary", {"iteration": 1, "changed_files": 1,
                           "results": {"c1": {"held": False}, "c2": {"held": True}}})
        led.add("mutation", {"iteration": 1, "mutants": 6, "killed": 2, "skipped": 0, "details": []})
        led.add("checks_strengthened", {"iteration": 1, "ok": ok, "replaced": ["c1"], "kept": []})
        if second:
            led.add("mutation", {"iteration": 1, "mutants": 6, "killed": 5, "skipped": 0, "details": []})
        out.append(led.finish("partial").build(request="Build the command-line tool"))
    # Long AI notes to exercise the card truncation footer.
    many_notes = [f"- decision {n}: " + ("x" * 80) for n in range(60)]
    led_notes = Ledger(criteria=ids(2))
    for c in led_notes.view["criteria"]:
        c["text"] = f"Requirement {c['id']}"
    out.append(led_notes.start().finish("partial").build(decisions=many_notes))
    return out


def _all_cards_neutral(b: briefing.Briefing) -> list[list[str]]:
    tz = "UTC" if config.DISPLAY_TIMEZONE == "UTC" else "KST"
    events = [(f"2026-10-18 05:00:00 {tz}", "🛠️ Iteration 1 — Sonnet 5.5, exit code 0")] * 4
    return [
        cards.finish_card(b),
        cards.progress_card(b, events),
        cards.status_card(b, events),
        cards.event_card("🧭 Plan ready", b.mission_id, ["2 criteria", "· c1 Requirement c1"]),
        cards.list_card([(b.mission_id, "2026-10-18T05:00:00+00:00", b, None), ("other", None, None, None)]),
        cards.text_card("Unknown mission: demo"),
    ]


def _extra_module_texts() -> list[str]:
    canary = daily.honesty_canary_mission("canary-20261018", "secret_token_value")
    return [
        *(report.status_text(s) for s in report.STATUS_KEYS),
        results.format_size(22 * 1024 * 1024, at_least=False),
        results.format_size(22 * 1024 * 1024, at_least=True),
        t("loop.append_only_notice"),
        outage.worker_reason(1, False, "Error: 429 rate limit", 5.0, lambda: True),
        outage.worker_reason(2, False, "plain error", 5.0, lambda: True),
        outage.worker_reason(None, True, "", 5.0, lambda: True),
        outage.worker_reason(None, False, "", 5.0, lambda: True),
        canary["goal"],
        *(c["text"] for c in canary["criteria"]),
        t("planner.outage_reason"),
    ]


def _render_all() -> list[str]:
    texts: list[str] = []
    for b in _neutral_briefings():
        texts.append(report.render_briefing(b))
        for blocks in _all_cards_neutral(b):
            for msg in cards.pack(blocks):
                assert not html_problems(msg), msg
            texts.append(card_text(blocks))
    texts.extend(_extra_module_texts())
    return texts


class I18nRenderTests(TimedTestCase):
    def test_catalogs_have_identical_keys_and_valid_banned_patterns(self):
        self.assertEqual(set(en.MESSAGES), set(ko.MESSAGES))
        rule_path = ROOT / "guidance/user/rules/english-writing.md"
        if rule_path.exists():
            line = next(ln for ln in rule_path.read_text(encoding="utf-8").splitlines()
                        if ln.startswith("condition:"))
            expected = tuple(json.loads(line.split("condition:", 1)[1].strip()))
            self.assertEqual(tuple(en.BANNED_PATTERNS), expected)
        for key, value in en.MESSAGES.items():
            self.assertIsNone(HANGUL_RE.search(value), f"Hangul in en.MESSAGES[{key!r}]: {value!r}")
            for pattern in en.BANNED_PATTERNS:
                self.assertIsNone(re.search(pattern, value), f"Banned pattern in en.MESSAGES[{key!r}]: {value!r}")
        for key, value in ko.MESSAGES.items():
            for phrase in ko.BANNED_PHRASES:
                self.assertNotIn(phrase, value, f"Banned phrase in ko.MESSAGES[{key!r}]: {value!r}")

    def test_no_hangul_in_string_constants_of_core_modules(self):
        for name in CORE_MODULES:
            path = ROOT / "agentic" / name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertIsNone(
                        HANGUL_RE.search(node.value),
                        f"{name}:{node.lineno} still contains Hangul: {node.value!r}",
                    )

    def test_english_renders_have_no_hangul_no_banned_patterns_and_utc_times(self):
        compiled = [re.compile(p) for p in en.BANNED_PATTERNS]
        with mock.patch.object(config, "LANGUAGE", "en"), \
                mock.patch.object(config, "DISPLAY_TIMEZONE", "UTC"):
            texts = _render_all()
        self.assertGreater(len(texts), 50)
        for text in texts:
            self.assertIsNone(HANGUL_RE.search(text), f"Hangul found in English render:\n{text}")
            self.assertNotIn("KST", text, f"Hard-coded KST in UTC render:\n{text}")
            for regex in compiled:
                self.assertIsNone(regex.search(text), f"Banned pattern {regex.pattern!r} matched:\n{text}")
            for match in TIME_RE.finditer(text):
                self.assertTrue(
                    text[match.end():].startswith(" UTC"),
                    f"Timestamp not in UTC: {text[match.start():match.end() + 10]!r}",
                )
        joined = "\n".join(texts)
        self.assertIn("Recent events (UTC)", joined)
        self.assertIn("| Criterion | Result | Last check (UTC) | Exit code | Attempts |", joined)
        self.assertIn("at least 22.0MB", joined)

    def test_korean_renders_contain_no_banned_phrases_and_use_kst(self):
        with mock.patch.object(config, "LANGUAGE", "ko"), \
                mock.patch.object(config, "DISPLAY_TIMEZONE", "Asia/Seoul"):
            texts = _render_all()
        self.assertGreater(len(texts), 50)
        for text in texts:
            for phrase in ko.BANNED_PHRASES:
                self.assertNotIn(phrase, text, f"Banned phrase {phrase!r} in:\n{text}")
            for match in TIME_RE.finditer(text):
                self.assertTrue(
                    text[match.end():].startswith(" KST"),
                    f"Timestamp not in KST: {text[match.start():match.end() + 10]!r}",
                )


if __name__ == "__main__":
    unittest.main()
