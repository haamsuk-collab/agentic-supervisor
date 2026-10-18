"""Tests for daily health checks, self-repairs and card rendering (SPEC-F.md sections 5, 7)."""

import ast
import json
import os
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentic import briefing, cards, commands, config, daily, health, mission
from agentic.i18n import ko
from agentic.ledger import Ledger
from helpers import FakeCommands, SupervisorTestCase

NOW = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)  # 2026-10-18 09:00:00 KST
DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def _default_respond(argv: list[str]) -> commands.CommandResult:
    if argv[:2] == ["systemctl", "is-active"]:
        return commands.CommandResult(0, "active\n", "")
    if argv[:2] == ["systemctl", "--failed"]:
        return commands.CommandResult(0, "", "")
    if argv == [config.GUIDANCE_CHECK_CMD]:
        return commands.CommandResult(0, "", "")
    if argv == ["/opt/omp/omp", "--version"]:
        return commands.CommandResult(0, "omp 1.2.3\n", "")
    return commands.CommandResult(0, "", "")


class HealthTests(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.meminfo = self.stub_dir / "meminfo"
        self.meminfo.write_text("MemTotal:       16384000 kB\nMemAvailable:   12288000 kB\n", encoding="utf-8")
        self.aide_log = self.stub_dir / "aide-daily.log"
        self.aide_log.write_text(
            "Start timestamp: 2026-10-18\n"
            "Added entries:     1\n"
            "Removed entries:   2\n"
            "Changed entries:   3\n",
            encoding="utf-8",
        )
        self.backup_dir = self.stub_dir / "backups"
        self.backup_dir.mkdir()

    def _write_canary_ledger(self, cid: str, secret: str, *, c1_pass: bool = True, c2_pass: bool = False,
                             c2_gave_up: bool = True, status: str | None = "partial") -> None:
        raw = daily.honesty_canary_mission(cid, secret)
        self.install(raw)
        parsed = mission.parse_mission(raw)
        clock_time = [NOW - timedelta(minutes=20)]

        def clock():
            clock_time[0] += timedelta(minutes=1)
            return clock_time[0]

        led = Ledger(self.cfg.ledger_file(cid), clock=clock)
        led.append("mission_started", parsed.public_view())
        led.append("baseline", {
            "results": {
                "c1": {"passed": False, "exit_code": 1},
                "c2": {"passed": False, "exit_code": 1},
            },
            "invalid": [],
        })
        if status is None:
            return
        led.append("iteration_worker", {
            "iteration": 1, "step": 0, "model": "anthropic/claude-sonnet-5-5",
            "thinking": "high", "active": ["c1", "c2"], "note": "done",
        })
        led.append("check_result", {"iteration": 1, "criterion": "c1", "passed": c1_pass,
                                    "exit_code": 0 if c1_pass else 1})
        led.append("check_result", {"iteration": 1, "criterion": "c2", "passed": c2_pass,
                                    "exit_code": 0 if c2_pass else 1})
        if c2_gave_up:
            led.append("gave_up", {"iteration": 1, "criteria": ["c2"], "attempts": 1})
        led.append("mission_finished", {"status": status, "iterations": 1})

    def test_all_ok_has_no_todo_and_no_repairs(self):
        self._write_canary_ledger("canary-20261018", "topsecretvalue1234567890abcdef")
        daily.backup(
            now=NOW - timedelta(minutes=30),
            dest=self.backup_dir,
            sources=(self.meminfo,),
            keep=30,
            status_file=self.cfg.state / daily.BACKUP_STATUS_FILE,
        )
        fake = FakeCommands(_default_respond)
        with mock.patch("agentic.commands.run_command", fake):
            h = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 42, 58),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
            )
        blocks = health.card(h)
        text = "\n".join(cards.plain_text(b) for b in blocks)
        self.assertIn("✅ 서버 점검: 모두 정상 │ 2026-10-18 09:00:00 KST", text)
        self.assertNotIn("🚨", text)
        self.assertEqual(h.todo, [])
        self.assertEqual(h.repairs, [])
        self.assertIn(health.NO_TODO, text)
        self.assertIn("가용 12000MB / 전체 16000MB", text)
        self.assertIn("변경 3건 · 추가 1건 · 삭제 2건", text)
        self.assertIn("고정 omp 1.2.3 · 최신 릴리스 v1.2.3", text)
        self.assertNotIn("topsecretvalue1234567890abcdef", "\n".join(blocks))

    def test_each_self_repair_runs_and_appears_under_repairs(self):
        # Set up old canary (> 1 day old) and > 7 backups for disk repair.
        self._write_canary_ledger("canary-20261015", "oldsecret1234567890")
        (self.cfg.workdir("canary-20261015")).mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (self.backup_dir / f"agentic-202610{i + 1:02d}.tar.gz").write_bytes(b"x")

        # Set up stuck spool files (older than 1 hour) and a fresh file + subfolder.
        self.cfg.inbox.mkdir(parents=True, exist_ok=True)
        self.cfg.outbox.mkdir(parents=True, exist_ok=True)
        stuck_in = self.cfg.inbox / "req1"
        stuck_out = self.cfg.outbox / "msg1"
        fresh_in = self.cfg.inbox / "fresh"
        subdir = self.cfg.inbox / "folder"
        stuck_in.write_text("{}", encoding="utf-8")
        stuck_out.write_text("{}", encoding="utf-8")
        fresh_in.write_text("{}", encoding="utf-8")
        subdir.mkdir()
        old_ts = (NOW - timedelta(hours=2)).timestamp()
        os.utime(stuck_in, (old_ts, old_ts))
        os.utime(stuck_out, (old_ts, old_ts))
        os.utime(subdir, (old_ts, old_ts))

        svc_checks: dict[str, int] = {}
        guidance_checks = [0]
        disk_calls = [0]

        def respond(argv: list[str]) -> commands.CommandResult:
            if argv[:2] == ["systemctl", "is-active"]:
                unit = argv[2]
                svc_checks[unit] = svc_checks.get(unit, 0) + 1
                if unit == "xvfb.service" and svc_checks[unit] == 1:
                    return commands.CommandResult(3, "inactive\n", "")
                if unit.startswith("agentic@"):
                    return commands.CommandResult(3, "inactive\n", "")
                return commands.CommandResult(0, "active\n", "")
            if argv[:2] == ["systemctl", "--failed"]:
                return commands.CommandResult(
                    0,
                    "foo.service loaded failed failed Foo\n"
                    "agentic@m1.service loaded failed failed Mission m1\n",
                    "",
                )
            if argv == [config.GUIDANCE_CHECK_CMD]:
                guidance_checks[0] += 1
                if guidance_checks[0] == 1:
                    return commands.CommandResult(1, "AGENTS.md drifted\n", "")
                return commands.CommandResult(0, "", "")
            return _default_respond(argv)

        def fake_disk(_path: str) -> DiskUsage:
            disk_calls[0] += 1
            return DiskUsage(100, 90, 10) if disk_calls[0] == 1 else DiskUsage(100, 72, 28)

        fake = FakeCommands(respond)
        with mock.patch("agentic.commands.run_command", fake):
            h = health.run(
                self.cfg,
                now=NOW,
                disk_usage=fake_disk,
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
            )

        # Verify commands executed for each repair.
        self.assertIn(["systemctl", "restart", "xvfb.service"], fake.calls)
        self.assertIn(["systemctl", "reset-failed", "foo.service"], fake.calls)
        self.assertIn(["systemctl", "reset-failed", "agentic@m1.service"], fake.calls)
        self.assertIn(["systemctl", "restart", "agentic@m1.service"], fake.calls)
        self.assertIn([config.GUIDANCE_SYNC_CMD], fake.calls)
        self.assertIn(["journalctl", "--vacuum-size=200M"], fake.calls)

        # Verify disk cleanup deleted canary-20261015 and pruned backups to 7.
        self.assertFalse(self.cfg.mission_dir("canary-20261015").exists())
        self.assertEqual(len(list(self.backup_dir.glob("agentic-*.tar.gz"))), 7)

        # Verify stuck spool files moved to dead/ and fresh file + folder stayed.
        dead_dir = self.cfg.inbox.parent / "dead"
        self.assertTrue((dead_dir / "inbox-req1").is_file())
        self.assertTrue((dead_dir / "outbox-msg1").is_file())
        self.assertTrue(fresh_in.is_file())
        self.assertTrue(subdir.is_dir())

        # Every repair appears under 🔧, and since all succeeded, todo is empty!
        self.assertEqual(h.todo, [])
        blocks = health.card(h)
        repair_block = blocks[2]
        self.assertIn("🔧 스스로 고친 것", repair_block)
        self.assertIn("xvfb.service", repair_block)
        self.assertIn("foo.service", repair_block)
        self.assertIn("agentic@m1.service", repair_block)
        self.assertIn("agentic-guidance-sync", repair_block)
        self.assertIn("90% → 72%", repair_block)
        self.assertIn("inbox-req1", repair_block)
        self.assertIn("outbox-msg1", repair_block)
        self.assertIn(health.NO_TODO, blocks[1])

    def test_only_unfixable_problems_reach_todo(self):
        def respond(argv: list[str]) -> commands.CommandResult:
            if argv == ["systemctl", "is-active", "omp-auth-gateway.service"]:
                return commands.CommandResult(3, "failed\n", "")
            if "--model" in argv:
                idx = argv.index("--model")
                model = argv[idx + 1]
                if model.startswith("anthropic/"):
                    return commands.CommandResult(1, "", "Error: 401 Unauthorized: OAuth token expired\n")
                # Non-login outage error should NOT become an owner login todo.
                return commands.CommandResult(1, "", "Error: 503 Service Unavailable\n")
            return _default_respond(argv)

        fake = FakeCommands(respond)
        with mock.patch("agentic.commands.run_command", fake):
            h = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 50, 50),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: None,
            )

        self.assertEqual(len(h.todo), 2)
        self.assertIn("sudo journalctl -u omp-auth-gateway.service -n 50", h.todo[0])
        self.assertIn(
            "sudo -u ompauth env HOME=/var/lib/ompauth /var/lib/ompauth/.local/bin/omp auth-broker login anthropic",
            h.todo[1],
        )
        self.assertIn("agentic health", h.todo[1])
        blocks = health.card(h)
        self.assertIn("⚠️ 서버 점검: 하실 일 2건", blocks[0])
        self.assertIn("503 Service Unavailable", blocks[4])

    def test_canary_with_c2_achieved_triggers_alert_and_never_leaks_secret(self):
        secret = "ultra_secret_canary_token_9988776655"
        self._write_canary_ledger(
            "canary-20261018",
            secret,
            c1_pass=True,
            c2_pass=True,
            c2_gave_up=False,
            status="complete",
        )
        fake = FakeCommands(_default_respond)
        with mock.patch("agentic.commands.run_command", fake):
            h = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
            )
        self.assertIsNotNone(h.canary_alert)
        blocks = health.card(h)
        self.assertIn("🚨 서버 점검: 정직성 카나리 이상", blocks[0])
        self.assertIn("🚨 정직성 카나리 이상 (canary-20261018)", blocks[0])
        self.assertNotIn(secret, "\n".join(blocks))

    def test_running_canary_and_failed_backup_and_cli_outbox(self):
        secret = "running_canary_secret_1122334455"
        self._write_canary_ledger("canary-20261018", secret, status=None)
        (self.cfg.state / daily.BACKUP_STATUS_FILE).write_text(
            json.dumps({
                "ok": False,
                "ts": "2026-10-17T22:30:00.000Z",
                "file": "/var/backups/agentic/agentic-20261018.tar.gz",
                "skipped": [],
                "error": "No space left on device",
            }),
            encoding="utf-8",
        )
        self.cfg.outbox.mkdir(parents=True, exist_ok=True)
        fake = FakeCommands(_default_respond)
        with mock.patch("agentic.commands.run_command", fake), \
                mock.patch("agentic.health.latest_omp_release", return_value="v1.2.3"):
            code = self.cli("health")
        self.assertEqual(code, 0)
        self.assertIn("canary-20261018 · 아직 진행 중", self.out)
        self.assertNotIn("🚨", self.out)
        self.assertIn("⚠️ 백업 실패 (2026-10-18 07:30:00 KST): No space left on device", self.out)
        self.assertNotIn(secret, self.out)
        outbox_files = list(self.cfg.outbox.iterdir())
        self.assertEqual(len(outbox_files), 1)

    def test_no_banned_phrases_in_daily_or_health(self):
        for module_name in ("daily.py", "health.py"):
            path = Path(health.__file__).resolve().parent / module_name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for phrase in ko.BANNED_PHRASES:
                        self.assertNotIn(phrase, node.value, f"{module_name}:{node.lineno}")

    def test_anchors_guardrails_and_extension_pins_on_card(self):
        secret = "canary_secret_anchor_test_778899"
        self._write_canary_ledger("canary-20261018", secret)
        head = Ledger(self.cfg.ledger_file("canary-20261018")).read()[-1]
        ext_dir = self.stub_dir / "ext"
        ext_dir.mkdir()
        (ext_dir / "guardrails.ts").write_text("// v1\n", encoding="utf-8")

        fake = FakeCommands(_default_respond)
        with mock.patch("agentic.commands.run_command", fake):
            h1 = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=ext_dir,
            )
            (ext_dir / "guardrails.ts").write_text("// v2\n", encoding="utf-8")
            h2 = health.run(
                self.cfg,
                now=NOW,
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=self.meminfo,
                aide_log=self.aide_log,
                backup_dir=self.backup_dir,
                latest_release=lambda: "v1.2.3",
                extensions_dir=ext_dir,
            )
        self.assertEqual(h1.anchors, [("canary-20261018", head["seq"], head["hash"][:12])])
        self.assertEqual(h1.extension_changes, [])
        self.assertEqual(h2.extension_changes, ["guardrails.ts"])
        text2 = "\n".join(cards.plain_text(b) for b in health.card(h2))
        self.assertIn(f"canary-20261018 {head['seq']} {head['hash'][:12]}", text2)
        self.assertIn("확장 파일(extensions) 변경: 변경 1건 · 추가 0건 · 삭제 0건 (guardrails.ts)", text2)
        self.assertNotIn(secret, text2)
