"""Tests for SPEC-H section 6: off-box ledger anchors, guardrail self-test, and extension pinning."""

import hashlib
import json
import shutil
import stat
import subprocess
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentic import cards, commands, config, daily, health, ledger, mission
from agentic.ledger import Ledger, compute_hash
from helpers import ROOT, FakeCommands, SupervisorTestCase

NOW = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)  # 2026-10-18 09:00:00 KST
DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
REAL_GUARDRAILS = ROOT / "guidance" / "user" / "extensions" / "guardrails.ts"


def _default_health_respond(argv: list[str]) -> commands.CommandResult:
    if argv[:2] == ["systemctl", "is-active"]:
        return commands.CommandResult(0, "active\n", "")
    if argv[:2] == ["systemctl", "--failed"]:
        return commands.CommandResult(0, "", "")
    if argv == [config.GUIDANCE_CHECK_CMD]:
        return commands.CommandResult(0, "", "")
    if argv == ["/opt/omp/omp", "--version"]:
        return commands.CommandResult(0, "omp 1.2.3\n", "")
    return commands.CommandResult(0, "", "")


def _real_run_command(argv: list[str], stdin_path: str | None = None) -> commands.CommandResult:
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    return commands.CommandResult(proc.returncode, proc.stdout, proc.stderr)


class AnchorAndSelfTestTests(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.meminfo = self.stub_dir / "meminfo"
        self.meminfo.write_text("MemTotal:       16384000 kB\nMemAvailable:   12288000 kB\n", encoding="utf-8")
        self.aide_log = self.stub_dir / "aide-daily.log"
        self.aide_log.write_text(
            "Added entries:     0\nRemoved entries:   0\nChanged entries:   0\n",
            encoding="utf-8",
        )
        self.backup_dir = self.stub_dir / "backups"
        self.backup_dir.mkdir()
        self.ext_dir = self.stub_dir / "extensions"
        self.ext_dir.mkdir()
        (self.ext_dir / "guardrails.ts").write_text("// guardrails v1\n", encoding="utf-8")

    def _make_mission_with_ledger(self, mid: str, when: datetime, secret_marker: str, records_count: int = 3) -> Ledger:
        raw = {
            "id": mid,
            "goal": f"Goal for {mid}",
            "criteria": [
                {
                    "id": "c1",
                    "text": "Criterion 1",
                    "check": {
                        "argv": ["/usr/bin/python3.12", "-I", "check.py"],
                        "timeout": 30,
                        "files": {"check.py": f"# {secret_marker}\nimport sys\nsys.exit(0)\n"},
                    },
                }
            ],
        }
        self.install(raw)
        parsed = mission.parse_mission(raw)
        ts = [when]

        def clock():
            ts[0] += timedelta(seconds=10)
            return ts[0]

        led = Ledger(self.cfg.ledger_file(mid), clock=clock)
        led.append("mission_started", parsed.public_view())
        if records_count >= 2:
            led.append("baseline", {"results": {"c1": {"passed": False, "exit_code": 1}}, "invalid": []})
        for i in range(3, records_count + 1):
            led.append("check_result", {"iteration": i - 2, "criterion": "c1", "passed": True, "exit_code": 0})
        return led

    def test_anchors_present_only_for_missions_changed_in_last_24h_and_no_check_content_leaks(self):
        secret_recent = "HIDDEN_CHECK_SECRET_RECENT_998877"
        secret_old = "HIDDEN_CHECK_SECRET_OLD_112233"
        led_recent = self._make_mission_with_ledger("m-recent", NOW - timedelta(hours=3), secret_recent, records_count=4)
        self._make_mission_with_ledger("m-old", NOW - timedelta(hours=30), secret_old, records_count=3)
        # Mission directory with no ledger records at all
        self.install({
            "id": "m-empty",
            "goal": "Empty",
            "criteria": [{"id": "c1", "text": "t", "check": {"argv": ["/bin/true"]}}],
        })

        rec_recent = led_recent.read()[-1]
        expected_hash12 = rec_recent["hash"][:12]

        fake = FakeCommands(_default_health_respond)
        with mock.patch("agentic.commands.run_command", fake):
            h = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=self.ext_dir,
            )

        self.assertEqual(h.anchors, [("m-recent", 4, expected_hash12)])
        blocks = health.card(h)
        rendered_html = "\n".join(blocks)
        rendered_text = "\n".join(cards.plain_text(b) for b in blocks)

        self.assertIn(f"m-recent 4 {expected_hash12}", rendered_text)
        self.assertNotIn("m-old", rendered_text)
        self.assertNotIn("m-empty", rendered_text)
        self.assertNotIn(secret_recent, rendered_html)
        self.assertNotIn(secret_old, rendered_html)

    def test_verify_anchors_ok_on_intact_and_extended_ledger_and_fails_on_tamper_rewrite_truncate_missing(self):
        led1 = self._make_mission_with_ledger("m-one", NOW - timedelta(hours=2), "SEC1", records_count=3)
        led2 = self._make_mission_with_ledger("m-two", NOW - timedelta(hours=1), "SEC2", records_count=2)

        fake = FakeCommands(_default_health_respond)
        with mock.patch("agentic.commands.run_command", fake):
            h = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=self.ext_dir,
            )

        # Paste full Telegram card text (including headers, emojis, bullets) into an anchors file.
        card_text = "\n".join(cards.plain_text(b) for b in health.card(h))
        anchors_file = self.stub_dir / "pasted_card.txt"
        anchors_file.write_text(card_text, encoding="utf-8")

        # 1. Intact ledgers -> exit 0, one ok line per anchor.
        code = self.cli("verify", "--anchors", str(anchors_file))
        self.assertEqual(code, 0)
        lines = [line.strip() for line in self.out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.endswith(": ok") for line in lines), lines)

        # 2. Appending new records after the anchor was published still passes (record `seq` is unchanged).
        led1.append("mission_finished", {"status": "complete", "iterations": 1})
        code = self.cli("verify", "--anchors", str(anchors_file))
        self.assertEqual(code, 0)

        # 3. Truncating a ledger (cutting off record 3 of m-one so only 2 records remain) fails!
        m1_path = self.cfg.ledger_file("m-one")
        m1_lines = m1_path.read_text(encoding="utf-8").splitlines()
        m1_path.write_text("\n".join(m1_lines[:2]) + "\n", encoding="utf-8")
        code = self.cli("verify", "--anchors", str(anchors_file))
        self.assertNotEqual(code, 0)
        self.assertIn("m-one", self.out)
        self.assertIn("mismatch", self.out)

        # 4. Rewriting a ledger with recomputed valid hashes fails against the off-box anchor!
        rec2 = json.loads(m1_lines[2])
        rec2["data"]["passed"] = False
        rec2["hash"] = compute_hash(rec2)
        m1_path.write_text("\n".join(m1_lines[:2] + [json.dumps(rec2)]) + "\n", encoding="utf-8")
        self.assertTrue(Ledger(m1_path).verify().ok)  # valid chain on its own, caught only by the anchor
        code = self.cli("verify", "--anchors", str(anchors_file))
        self.assertNotEqual(code, 0)
        self.assertIn("mismatch", self.out)

        # 5. Missing ledger file -> reports `missing ledger` and exits non-zero.
        m1_path.write_text("\n".join(m1_lines) + "\n", encoding="utf-8")
        self.cfg.ledger_file("m-two").unlink()
        code = self.cli("verify", "--anchors", str(anchors_file))
        self.assertNotEqual(code, 0)
        self.assertIn("m-one", self.out)
        self.assertIn(": ok", self.out)
        self.assertIn("m-two", self.out)
        self.assertIn("missing ledger", self.out)

    def test_guardrails_selftest_records_status_and_triggers_alert_when_forbidden_allowed(self):
        all_good_output = json.dumps({
            "results": [
                {"name": "rm_home", "forbidden": True, "blocked": True},
                {"name": "edit_spec", "forbidden": True, "blocked": True},
                {"name": "write_state", "forbidden": True, "blocked": True},
                {"name": "bash_unittest", "forbidden": False, "blocked": False},
                {"name": "edit_work", "forbidden": False, "blocked": False},
            ]
        })

        def respond_ok(argv: list[str]) -> commands.CommandResult:
            if argv and argv[0] == "node":
                return commands.CommandResult(0, all_good_output + "\n", "")
            return _default_health_respond(argv)

        with mock.patch("agentic.commands.run_command", FakeCommands(respond_ok)):
            daily.start_honesty_canary(self.cfg, NOW, guardrails_file=self.ext_dir / "guardrails.ts")
            status_path = self.cfg.state / daily.GUARDRAILS_STATUS_FILE
            self.assertTrue(status_path.is_file())
            self.assertEqual(stat.S_IMODE(status_path.stat().st_mode), 0o600)
            saved = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["ok"])
            self.assertEqual(saved["forbidden_total"], 3)
            self.assertEqual(saved["forbidden_blocked"], 3)
            self.assertEqual(saved["forbidden_allowed"], [])
            self.assertEqual(saved["allowed_total"], 2)
            self.assertEqual(saved["allowed_passed"], 2)

            h_ok = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=self.ext_dir,
            )

        self.assertTrue(h_ok.guardrails_ok)
        self.assertIsNone(h_ok.guardrails_alert)
        card_ok = "\n".join(cards.plain_text(b) for b in health.card(h_ok))
        self.assertNotIn("🚨", card_ok)
        self.assertIn("금지 3건 중 3건 차단, 허용 2건 중 2건 통과", card_ok)

        # Now simulate a forbidden action being allowed (e.g. edit_spec not blocked).
        bad_output = json.dumps({
            "results": [
                {"name": "rm_home", "forbidden": True, "blocked": True},
                {"name": "edit_spec", "forbidden": True, "blocked": False},
                {"name": "write_state", "forbidden": True, "blocked": True},
                {"name": "bash_unittest", "forbidden": False, "blocked": False},
                {"name": "edit_work", "forbidden": False, "blocked": False},
            ]
        })

        def respond_bad(argv: list[str]) -> commands.CommandResult:
            if argv and argv[0] == "node":
                return commands.CommandResult(0, bad_output + "\n", "")
            return _default_health_respond(argv)

        with mock.patch("agentic.commands.run_command", FakeCommands(respond_bad)):
            daily.run_guardrails_selftest(self.cfg, NOW, guardrails_file=self.ext_dir / "guardrails.ts")
            h_bad = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=self.ext_dir,
            )

        self.assertFalse(h_bad.guardrails_ok)
        self.assertIsNotNone(h_bad.guardrails_alert)
        self.assertIn("🚨", h_bad.guardrails_alert)
        blocks_bad = health.card(h_bad)
        self.assertIn("🚨", blocks_bad[0])
        self.assertIn("edit_spec", blocks_bad[0])

        # When the self-test could not run (e.g. node missing/error), health gives a normal line (no 🚨).
        def respond_err(argv: list[str]) -> commands.CommandResult:
            if argv and argv[0] == "node":
                return commands.CommandResult(127, "", "cannot start 'node': No such file")
            return _default_health_respond(argv)

        with mock.patch("agentic.commands.run_command", FakeCommands(respond_err)):
            daily.run_guardrails_selftest(self.cfg, NOW, guardrails_file=self.ext_dir / "guardrails.ts")
            h_err = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=self.ext_dir,
            )

        self.assertFalse(h_err.guardrails_ok)
        self.assertIsNone(h_err.guardrails_alert)
        card_err = "\n".join(cards.plain_text(b) for b in health.card(h_err))
        self.assertNotIn("🚨", card_err)
        self.assertIn("자체 점검을 실행하지 못함", card_err)

    @unittest.skipUnless(
        shutil.which("node") is not None and REAL_GUARDRAILS.is_file(),
        "node or guidance/user/extensions/guardrails.ts is not available",
    )
    def test_real_guardrails_ts_runs_through_real_node_harness(self):
        with mock.patch("agentic.commands.run_command", _real_run_command):
            status = daily.run_guardrails_selftest(self.cfg, NOW, guardrails_file=REAL_GUARDRAILS)

        self.assertTrue(status["ok"], status)
        self.assertEqual(status["forbidden_total"], 3)
        self.assertEqual(status["forbidden_blocked"], 3)
        self.assertEqual(status["forbidden_allowed"], [])
        self.assertEqual(status["allowed_total"], 2)
        self.assertEqual(status["allowed_passed"], 2)
        self.assertEqual(status["allowed_blocked"], [])

    def test_extension_pinning_records_sha256_and_emits_health_line_on_change(self):
        ext_a = self.ext_dir / "guardrails.ts"
        ext_b = self.ext_dir / "workspace-state.ts"
        ext_a.write_text("// guardrails v1\n", encoding="utf-8")
        ext_b.write_text("// workspace v1\n", encoding="utf-8")
        sha_a1 = hashlib.sha256(b"// guardrails v1\n").hexdigest()
        sha_b1 = hashlib.sha256(b"// workspace v1\n").hexdigest()

        fake = FakeCommands(_default_health_respond)
        with mock.patch("agentic.commands.run_command", fake):
            # First run: records pins, no change line.
            h1 = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=self.ext_dir,
            )
            pin_file = self.cfg.state / health.EXTENSION_PINS_FILE
            self.assertTrue(pin_file.is_file())
            self.assertEqual(stat.S_IMODE(pin_file.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(pin_file.read_text(encoding="utf-8")),
                {"guardrails.ts": sha_a1, "workspace-state.ts": sha_b1},
            )
            self.assertEqual(h1.extension_changes, [])
            self.assertIsNone(h1.extension_change_line)
            self.assertNotIn("확장 파일(extensions) 변경", "\n".join(health.card(h1)))

            # Second run with no changes: still no change line.
            h2 = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=self.ext_dir,
            )
            self.assertEqual(h2.extension_changes, [])
            self.assertIsNone(h2.extension_change_line)

            # Modify guardrails.ts, add extra.ts, remove workspace-state.ts.
            ext_a.write_text("// guardrails v2 modified\n", encoding="utf-8")
            (self.ext_dir / "extra.ts").write_text("// extra\n", encoding="utf-8")
            ext_b.unlink()

            h3 = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=self.ext_dir,
            )
            self.assertEqual(h3.extension_changes, ["guardrails.ts", "extra.ts", "workspace-state.ts"])
            self.assertIsNotNone(h3.extension_change_line)
            card3 = "\n".join(cards.plain_text(b) for b in health.card(h3))
            self.assertIn("확장 파일(extensions) 변경: 변경 1건 · 추가 1건 · 삭제 1건", card3)
            self.assertIn("guardrails.ts, extra.ts, workspace-state.ts", card3)


if __name__ == "__main__":
    unittest.main()
