"""English i18n coverage for operational modules: telegram, inbox, notify, admin, health, cli, daily."""

import ast
import contextlib
import io
import json
import os
import re
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentic import admin, cards, cli, commands, config, daily, health, inbox, mission, notify, results, spool
from agentic.i18n import en, ko, t
from agentic.ledger import RECORD_TYPES, Ledger
from agentic.mission import MissionError
from agentic.telegram import Bot
from cardcheck import card_text, html_problems
from helpers import FakeCommands, SupervisorTestCase, TimedTestCase, fix, make_mission
from test_telegram import OWNER, Clock, FakeApi, message, press

HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]")
COMPILED_BANNED = tuple(re.compile(p) for p in en.BANNED_PATTERNS)
OPS_MODULES = ("telegram.py", "inbox.py", "notify.py", "admin.py", "health.py", "cli.py", "daily.py")
NOW = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def _default_health_respond(argv: list[str]) -> commands.CommandResult:
    if argv[:2] == ["systemctl", "is-active"]:
        return commands.CommandResult(0, "active\n", "")
    if argv[:2] == ["systemctl", "--failed"]:
        return commands.CommandResult(0, "", "")
    if argv == [config.GUIDANCE_CHECK_CMD]:
        return commands.CommandResult(0, "", "")
    if argv == [config.OMP_BINARY, "--version"]:
        return commands.CommandResult(0, "omp 1.2.3\n", "")
    return commands.CommandResult(0, "", "")


class EnglishAssertionsMixin:
    def assert_clean_english(self, text: str, label: str = "") -> None:
        self.assertIsNone(HANGUL_RE.search(text), f"Hangul found in {label}: {text!r}")
        for pattern in COMPILED_BANNED:
            self.assertIsNone(pattern.search(text), f"Banned pattern {pattern.pattern!r} matched {label}: {text!r}")


class CatalogAndAstTests(TimedTestCase, EnglishAssertionsMixin):
    def test_catalog_keys_match_and_values_obey_writing_rules(self):
        self.assertEqual(list(en.MESSAGES), list(ko.MESSAGES))
        for key, value in en.MESSAGES.items():
            self.assertIsInstance(value, str, key)
            self.assert_clean_english(value, key)
        for key, value in ko.MESSAGES.items():
            self.assertIsInstance(value, str, key)
            for phrase in ko.BANNED_PHRASES:
                self.assertNotIn(phrase, value, f"{key}: {phrase}")

    def test_no_hangul_or_literal_prints_or_argparse_texts_in_ops_modules(self):
        pkg_dir = Path(cli.__file__).resolve().parent
        for mod_name in OPS_MODULES:
            path = pkg_dir / mod_name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertIsNone(
                        HANGUL_RE.search(node.value),
                        f"{mod_name}:{getattr(node, 'lineno', '?')}: {node.value!r}",
                    )
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else (
                        func.attr if isinstance(func, ast.Attribute) else ""
                    )
                    if name == "print":
                        for arg in node.args:
                            for sub in ast.walk(arg):
                                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                                        and sub.func.id == "t"):
                                    break
                            else:
                                for sub in ast.walk(arg):
                                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                        self.assertIsNone(
                                            re.search(r"[A-Za-z]", sub.value),
                                            f"{mod_name}:{node.lineno} literal in print(): {sub.value!r}",
                                        )
                    if name in ("ArgumentParser", "add_parser", "add_argument"):
                        for kw in node.keywords:
                            if kw.arg in ("help", "description"):
                                self.assertTrue(
                                    isinstance(kw.value, ast.Call)
                                    and isinstance(kw.value.func, ast.Name)
                                    and kw.value.func.id == "t",
                                    f"{mod_name}:{node.lineno} {kw.arg}= must call t()",
                                )


class TelegramEnglishTests(TimedTestCase, EnglishAssertionsMixin):
    def setUp(self):
        super().setUp()
        for patcher in (
            mock.patch.object(config, "LANGUAGE", "en"),
            mock.patch.object(config, "DISPLAY_TIMEZONE", "UTC"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="agentic-i18n-bot-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"
        self.inbox = self.tmp / "inbox"
        self.outbox = self.tmp / "outbox"
        for d in (self.state, self.inbox, self.outbox):
            d.mkdir(mode=0o700)
        self.api = FakeApi()
        self.clock = Clock()
        self.bot = Bot(self.api, OWNER, self.state, self.inbox, self.outbox, clock=self.clock, sleep=lambda _: None)

    def last_buttons(self) -> list[str]:
        markup = self.api.of("sendMessage")[-1]["reply_markup"]
        return [b["text"] for b in markup["inline_keyboard"][0]]

    def last_keys(self) -> list[str]:
        markup = self.api.of("sendMessage")[-1]["reply_markup"]
        return [b["callback_data"] for b in markup["inline_keyboard"][0]]

    def test_every_bot_reply_and_button_in_english(self):
        # /help, /start, unknown command
        for cmd in ("/help", "/start", "/unknown"):
            self.api.calls.clear()
            self.bot.handle_update(message(cmd))
            (reply,) = self.api.texts()
            self.assertIn("/continue <id> <text>", reply)
            self.assertIn("<id> continue: <text>", reply)
            self.assert_clean_english(reply, cmd)

        # Missing/bad id for commands
        for cmd in ("/status", "/report", "/files", "/stop", "/status Bad"):
            self.api.calls.clear()
            self.bot.handle_update(message(cmd))
            (reply,) = self.api.texts()
            self.assert_clean_english(reply, cmd)

        # Bad /continue
        self.api.calls.clear()
        self.bot.handle_update(message("/continue demo"))
        (reply,) = self.api.texts()
        self.assert_clean_english(reply, "/continue")

        # Non-text message
        self.api.calls.clear()
        non_text = message("x")
        del non_text["message"]["text"]
        self.bot.handle_update(non_text)
        (reply,) = self.api.texts()
        self.assert_clean_english(reply, "not_text")

        # Too short and too long
        for raw, label in (("short", "too_short"), ("x" * 20_001, "too_long")):
            self.api.calls.clear()
            self.bot.handle_update(message(raw))
            (reply,) = self.api.texts()
            self.assert_clean_english(reply, label)

        # New mission request confirmation, Start, stale, Cancel
        self.api.calls.clear()
        self.bot.handle_update(message("Build a command-line weather tool in Python."))
        (prompt,) = self.api.texts()
        self.assert_clean_english(prompt, "confirm_new")
        self.assertEqual(self.last_buttons(), ["Start", "Cancel"])
        for btn in self.last_buttons():
            self.assert_clean_english(btn, "button")
        ok, _ = self.last_keys()
        self.bot.handle_update(press(ok, message_id=501))
        edited = self.api.of("editMessageText")[-1]["text"]
        self.assertEqual(edited, "Accepted")
        self.assert_clean_english(edited, "accepted")
        self.api.calls.clear()
        self.bot.handle_update(press(ok, message_id=501))
        (stale,) = self.api.texts()
        self.assert_clean_english(stale, "stale")

        # Cancel flow
        self.bot.handle_update(message("Build another command-line tool in Python."))
        _, no = self.last_keys()
        self.bot.handle_update(press(no, message_id=502))
        cancelled = self.api.of("editMessageText")[-1]["text"]
        self.assertEqual(cancelled, "Cancelled")
        self.assert_clean_english(cancelled, "cancelled")

        # Stop confirmation flow
        self.api.calls.clear()
        self.bot.handle_update(message("/stop demo"))
        (stop_prompt,) = self.api.texts()
        self.assert_clean_english(stop_prompt, "confirm_stop")
        self.assertEqual(self.last_buttons(), ["Stop", "Cancel"])
        ok_stop, _ = self.last_keys()
        self.bot.handle_update(press(ok_stop, message_id=503))
        stop_accepted = self.api.of("editMessageText")[-1]["text"]
        self.assertEqual(stop_accepted, "Stop request accepted")
        self.assert_clean_english(stop_accepted, "stop_accepted")

        # Unavailable inbox and retry_button
        self.bot.handle_update(message("Build a third command-line tool in Python."))
        ok_retry, _ = self.last_keys()
        self.inbox.rename(self.tmp / "inbox-moved")
        self.api.calls.clear()
        with self.assertLogs("agentic.telegram", level="ERROR"):
            self.bot.handle_update(message("/list"))
            self.bot.handle_update(press(ok_retry, message_id=504))
        for text in self.api.texts():
            self.assert_clean_english(text, "unavailable/retry")
        (self.tmp / "inbox-moved").rename(self.inbox)

    def test_both_followup_forms_accepted_in_both_languages(self):
        # Under LANGUAGE="en", both `continue:` and `이어서:` work and show English buttons.
        for msg_text in ("demo continue: add unit tests to the CLI", "demo 이어서: add unit tests to the CLI"):
            self.bot.pending.clear()
            self.api.calls.clear()
            self.bot.handle_update(message(msg_text))
            (prompt,) = self.api.texts()
            self.assert_clean_english(prompt, "confirm_continue")
            self.assertEqual(self.last_buttons(), ["Start", "Cancel"])
            ((_key, entry),) = self.bot.pending.items()
            self.assertEqual(entry["kind"], "continue")
            self.assertEqual(entry["id"], "demo")

        # Under LANGUAGE="ko", `continue:` also works and shows Korean buttons.
        with mock.patch.object(config, "LANGUAGE", "ko"):
            self.bot.pending.clear()
            self.api.calls.clear()
            self.bot.handle_update(message("demo continue: add unit tests to the CLI"))
            self.assertEqual(self.last_buttons(), ["시작", "취소"])
            ((_key, entry),) = self.bot.pending.items()
            self.assertEqual(entry["kind"], "continue")
            self.assertEqual(entry["id"], "demo")


class InboxNotifyAdminHealthCliEnglishTests(SupervisorTestCase, EnglishAssertionsMixin):
    def setUp(self):
        super().setUp()
        for patcher in (
            mock.patch.object(config, "LANGUAGE", "en"),
            mock.patch.object(config, "DISPLAY_TIMEZONE", "UTC"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg.inbox.mkdir(parents=True, mode=0o700)
        self.cfg.outbox.mkdir(parents=True, mode=0o700)

    def _drain_outbox_texts_and_docs(self) -> tuple[list[str], list[tuple[str, str]]]:
        files = sorted(
            (p for p in self.cfg.outbox.iterdir() if not p.name.endswith(".doc")),
            key=lambda p: p.stat().st_mtime_ns,
        )
        texts: list[str] = []
        docs: list[tuple[str, str]] = []
        for path in files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if "html" in payload:
                for part in payload["html"]:
                    self.assertEqual(html_problems(part), [], part)
                texts.append(card_text(payload["html"]))
            else:
                docs.append((payload["filename"], payload.get("caption", "")))
                (self.cfg.outbox / payload["document"]).unlink(missing_ok=True)
            path.unlink()
        return texts, docs

    def _put_inbox(self, payload: dict, name: str = "item001") -> None:
        (self.cfg.inbox / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_inbox_replies_in_english(self):
        fake = FakeCommands(lambda argv: commands.CommandResult(3, "inactive\n", "")
                            if argv[:2] == ["systemctl", "is-active"]
                            else commands.CommandResult(0, "", ""))
        with mock.patch("agentic.commands.run_command", fake):
            # 1. Empty list
            self._put_inbox({"type": "list"}, "01")
            inbox.process(self.cfg)
            texts, _ = self._drain_outbox_texts_and_docs()
            self.assertEqual(texts, ["No missions registered"])
            self.assert_clean_english(texts[0], "inbox.list.empty")

            # 2. New mission
            self._put_inbox({"type": "new", "text": "Build a calculator in Python."}, "02")
            inbox.process(self.cfg)
            (new_card,), _ = self._drain_outbox_texts_and_docs()
            self.assertIn("🚀 Accepted", new_card)
            self.assert_clean_english(new_card, "inbox.new")
            parent_id = admin.mission_ids(self.cfg)[0]
            self.cfg.workdir(parent_id).mkdir(parents=True, exist_ok=True)

            # 3. Continue mission
            self._put_inbox({"type": "continue", "id": parent_id, "text": "Add division support."}, "03")
            inbox.process(self.cfg)
            (cont_card,), _ = self._drain_outbox_texts_and_docs()
            self.assertIn("🚀 Accepted", cont_card)
            self.assertIn(f"Continued from mission: {parent_id}", cont_card)
            self.assert_clean_english(cont_card, "inbox.continue")

            # 4. Status and report before any records exist
            self._put_inbox({"type": "status", "id": parent_id}, "04")
            self._put_inbox({"type": "report", "id": parent_id}, "05")
            self._put_inbox({"type": "files", "id": parent_id}, "06")
            inbox.process(self.cfg)
            texts, _ = self._drain_outbox_texts_and_docs()
            self.assertEqual(len(texts), 3)
            for text in texts:
                self.assert_clean_english(text, "inbox.empty_mission")

            # 5. Run a mission to completion, then check list, status, report, files, stop
            self.play(fix("c1", "c2"))
            self.run_mission()
            self._drain_outbox_texts_and_docs()

            self._put_inbox({"type": "list"}, "07")
            self._put_inbox({"type": "status", "id": "demo"}, "08")
            self._put_inbox({"type": "report", "id": "demo"}, "09")
            self._put_inbox({"type": "files", "id": "demo"}, "10")
            self._put_inbox({"type": "stop", "id": "demo"}, "11")
            inbox.process(self.cfg)
            texts, docs = self._drain_outbox_texts_and_docs()
            self.assertEqual(len(texts), 3)  # list, status, stop
            self.assertEqual(len(docs), 2)   # report.md, results.zip
            for text in texts:
                self.assert_clean_english(text, "inbox.completed_mission")
            for filename, caption in docs:
                self.assert_clean_english(caption, f"inbox.doc.{filename}")

            # 6. Error replies: invalid item, unknown mission, corrupted ledger, MissionActive
            self._put_inbox({"type": "nope"}, "12")
            self._put_inbox({"type": "status", "id": "missing"}, "13")
            ledger_path = self.cfg.ledger_file("demo")
            ledger_path.write_text(ledger_path.read_text().replace("mission_started", "corrupted"), encoding="utf-8")
            self._put_inbox({"type": "status", "id": "demo"}, "14")
            with contextlib.redirect_stderr(io.StringIO()):
                inbox.process(self.cfg)
            texts, _ = self._drain_outbox_texts_and_docs()
            self.assertEqual(len(texts), 3)
            for text in texts:
                self.assert_clean_english(text, "inbox.error")

    def test_notifications_in_english(self):
        led = Ledger(self.cfg.ledger_file("demo"))
        notifier = notify.Notifier(self.cfg, "demo", led)
        raw_mission = make_mission()
        self.install(raw_mission)
        state = mock.MagicMock()
        state.ladder = mission.load_mission(self.cfg.mission_file("demo")).ladder
        state.outage_alert_seq = 3
        state.outage_started = NOW - timedelta(minutes=35)

        records = [
            {"seq": 1, "ts": "2026-10-18T00:00:00.000Z", "type": "plan_finished",
             "data": {"dropped": [{"id": "c3"}]}},
            {"seq": 2, "ts": "2026-10-18T00:05:00.000Z", "type": "escalated",
             "data": {"from_step": 0, "to_step": 1, "model": "anthropic/claude-opus-5-5", "stalls": 2}},
            {"seq": 3, "ts": "2026-10-18T00:35:00.000Z", "type": "worker_unavailable",
             "data": {"model": "anthropic/claude-sonnet-5-5", "reason": "429 rate limit"}},
            {"seq": 4, "ts": "2026-10-18T00:40:00.000Z", "type": "review_veto",
             "data": {"findings": [{"criterion": "c1", "problem": "hardcoded"}]}},
        ]
        for rec in records:
            blocks = notifier.card(rec, state)
            self.assertIsNotNone(blocks)
            rendered = card_text(blocks)
            self.assert_clean_english(rendered, f"notify.{rec['type']}")

        # Crash notifications (restarting and stopped)
        for substate in ("auto-restart", "failed"):
            fake = FakeCommands(lambda _argv, s=substate: commands.CommandResult(0, f"{s}\n", ""))
            with mock.patch("agentic.commands.run_command", fake):
                notify.notify_failure(self.cfg, "demo")
            (crash_text,), _ = self._drain_outbox_texts_and_docs()
            self.assertIn("💥 Supervisor Crashed", crash_text)
            self.assert_clean_english(crash_text, f"notify.crash.{substate}")

        # Oversized report fallback
        with mock.patch("agentic.outbox.write_document", side_effect=spool.DocumentTooLarge(25 * 1024 * 1024)):
            self.assertTrue(notify.send_report(self.cfg, "demo", "report body", name="big-report"))
        (oversized_text,), _ = self._drain_outbox_texts_and_docs()
        self.assert_clean_english(oversized_text, "notify.report_too_large")

    def test_admin_errors_and_all_event_lines_in_english(self):
        self.assert_clean_english(str(admin.MissionActive()), "MissionActive")
        fake = FakeCommands(lambda _argv: commands.CommandResult(0, "active\n", ""))
        for mid in ("m1", "m2", "m3"):
            self.cfg.mission_dir(mid).mkdir(parents=True, exist_ok=True)
        with mock.patch("agentic.commands.run_command", fake):
            with self.assertRaises(admin.TooManyMissions) as caught:
                admin.new_mission(self.cfg, "Build a tool.", "m4")
        self.assert_clean_english(str(caught.exception), "TooManyMissions")

        composed = admin.compose_followup_request("m1", None, None, "Add a feature.")
        self.assert_clean_english(composed, "compose_followup_request")
        with self.assertRaises(MissionError) as caught_long:
            admin.compose_followup_request("m1", "prev", None, "x" * (config.REQUEST_MAX_BYTES - 10))
        self.assert_clean_english(str(caught_long.exception), "followup.too_long")

        samples = {
            "mission_started": {"id": "demo", "criteria": [{"id": "c1"}, {"id": "c2"}]},
            "baseline": {"results": {"c1": {}}, "invalid": ["c1"]},
            "iteration_worker": {"iteration": 2, "model": "m/x", "thinking": "high", "exit_code": 0, "timed_out": True},
            "check_result": {"iteration": 2, "criterion": "c1", "passed": False, "exit_code": 1},
            "escalated": {"iteration": 2, "from_step": 0, "to_step": 1, "model": "m/y", "stalls": 2},
            "gave_up": {"iteration": 3, "criteria": ["c1"], "attempts": 3},
            "stopped_time": {"iteration": 3, "elapsed_seconds": 5.0},
            "mission_finished": {"status": "partial", "iterations": 3},
            "plan_started": {"request_sha256": "0" * 64, "request_bytes": 3},
            "plan_step": {"step": "checks", "round": 1, "ok": True},
            "plan_finished": {"dropped": [{"id": "c3"}]},
            "plan_failed": {"step": "design", "kind": "exit", "reason": "Planner timed out"},
            "review": {"ok": True, "findings": [{"verified": True}]},
            "review_veto": {"iteration": 2, "findings": [{"criterion": "c1", "problem": "hardcoded"}]},
            "worker_unavailable": {"iteration": 2, "step": 0, "model": "m/x", "reason": "429",
                                   "attempt": 2, "wait_seconds": 600},
            "plan_unavailable": {"step": "review", "round": 1, "attempt": 1, "reason": "quota", "wait_seconds": 300},
            "canary": {"iteration": 2, "results": {"c1": {"held": True}}, "changed_files": 1},
            "mutation": {"iteration": 2, "mutants": 4, "killed": 1, "skipped": 1, "details": []},
            "checks_strengthened": {"iteration": 2, "ok": True, "replaced": ["c1"], "kept": []},
            # Stage H record types (SPEC-H sections 1-3).
            "hygiene": {"iteration": 0, "files": ["json.py"]},
            "plan_filter": {"step": "critique", "round": 0, "results": {"c1": None}, "checked": 1, "rejected": 0},
            "checks_update": {"iteration": 2, "criterion": "c1", "old_sha256": "0" * 64, "new_sha256": "1" * 64,
                              "reason": "the old check missed the empty input"},
            "checks_update_rejected": {"iteration": 2, "criteria": ["c1"],
                                       "violations": [{"criterion": "c1", "rule": "mutant_survives"}]},
            "notice": {"kind": "append_only", "text": t("loop.append_only_notice"), "exit_code": 1},
        }
        self.assertEqual(set(samples), set(RECORD_TYPES))
        for kind, data in samples.items():
            line = admin.event_text({"type": kind, "data": data})
            self.assert_clean_english(line, f"admin.event.{kind}")

    def test_health_card_in_english(self):
        meminfo = self.stub_dir / "meminfo"
        meminfo.write_text("MemTotal: 16384000 kB\nMemAvailable: 12288000 kB\n", encoding="utf-8")
        aide_log = self.stub_dir / "aide.log"
        aide_log.write_text("Added entries: 1\nRemoved entries: 2\nChanged entries: 3\n", encoding="utf-8")
        backup_dir = self.stub_dir / "backups"
        backup_dir.mkdir()

        # Case 1: healthy with backup and canary
        raw_canary = daily.honesty_canary_mission("canary-20261018", "secret1234567890abcdef")
        self.install(raw_canary)
        parsed = mission.parse_mission(raw_canary)
        led = Ledger(self.cfg.ledger_file("canary-20261018"))
        led.append("mission_started", parsed.public_view())
        led.append("baseline", {"results": {"c1": {"passed": False, "exit_code": 1},
                                            "c2": {"passed": False, "exit_code": 1}}, "invalid": []})
        led.append("iteration_worker", {"iteration": 1, "step": 0, "model": "anthropic/claude-sonnet-5-5",
                                        "thinking": "high", "active": ["c1", "c2"], "note": "done"})
        led.append("check_result", {"iteration": 1, "criterion": "c1", "passed": True, "exit_code": 0})
        led.append("check_result", {"iteration": 1, "criterion": "c2", "passed": False, "exit_code": 1})
        led.append("gave_up", {"iteration": 1, "criteria": ["c2"], "attempts": 1})
        led.append("mission_finished", {"status": "partial", "iterations": 1})
        daily.backup(now=NOW - timedelta(minutes=10), dest=backup_dir, sources=(meminfo,),
                     keep=30, status_file=self.cfg.state / daily.BACKUP_STATUS_FILE)

        fake = FakeCommands(_default_health_respond)
        with mock.patch("agentic.commands.run_command", fake):
            h = health.run(self.cfg, now=NOW, disk_usage=lambda _: DiskUsage(100, 42, 58),
                           meminfo_path=meminfo, aide_log=aide_log, backup_dir=backup_dir,
                           latest_release=lambda: "v1.2.3")
        blocks = health.card(h)
        text = "\n".join(cards.plain_text(b) for b in blocks)
        self.assertIn("✅ Server Check: All Healthy", text)
        self.assert_clean_english(text, "health.ok")

        # Case 2: repairs, todo items, failed backup, and canary alert
        (self.cfg.state / daily.BACKUP_STATUS_FILE).write_text(
            json.dumps({"ok": False, "ts": "2026-10-17T22:30:00.000Z", "file": "x", "skipped": [], "error": "full"}),
            encoding="utf-8",
        )
        disk_calls = [0]

        def respond_problems(argv: list[str]) -> commands.CommandResult:
            if argv == ["systemctl", "is-active", "xvfb.service"]:
                return commands.CommandResult(3, "failed\n", "")
            if argv[:2] == ["systemctl", "--failed"]:
                return commands.CommandResult(0, "foo.service loaded failed failed Foo\n", "")
            if argv == [config.GUIDANCE_CHECK_CMD]:
                return commands.CommandResult(1, "AGENTS.md drifted\n", "")
            if "--model" in argv:
                return commands.CommandResult(1, "", "Error: 401 Unauthorized: OAuth expired\n")
            return _default_health_respond(argv)

        def fake_disk(_path: str) -> DiskUsage:
            disk_calls[0] += 1
            return DiskUsage(100, 90, 10) if disk_calls[0] == 1 else DiskUsage(100, 70, 30)

        with mock.patch("agentic.commands.run_command", FakeCommands(respond_problems)):
            h2 = health.run(self.cfg, now=NOW, disk_usage=fake_disk,
                            meminfo_path=self.stub_dir / "missing", aide_log=self.stub_dir / "missing",
                            backup_dir=backup_dir, latest_release=lambda: None)
        text2 = "\n".join(cards.plain_text(b) for b in health.card(h2))
        self.assertIn("⚠️ Server Check:", text2)
        self.assert_clean_english(text2, "health.problems")

    def test_cli_help_in_english(self):
        for argv in (["--help"], ["mission", "--help"], ["config", "--help"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
                cli.main(argv)
            self.assertEqual(caught.exception.code, 0)
            help_text = out.getvalue()
            self.assert_clean_english(help_text, f"cli {argv}")
