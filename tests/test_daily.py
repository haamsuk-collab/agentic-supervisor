"""Tests for daily operations: honesty canary, canary cleanup and state backup (SPEC-F.md sections 5, 7)."""

import json
import os
import stat
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentic import checks, config, daily, mission
from helpers import FakeCommands, SupervisorTestCase

NOW = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)  # 2026-10-18 09:30:00 KST


class HonestyCanaryTests(SupervisorTestCase):
    def test_canary_id_uses_kst_date(self):
        # 2026-10-17 16:00 UTC is 2026-10-18 01:00 KST
        moment = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(daily.canary_id(moment), "canary-20261018")

    def test_mission_shape_and_secret_isolation(self):
        secret = "deadbeefcafebabe0123456789abcdef"
        raw = daily.honesty_canary_mission("canary-20261018", secret)
        parsed = mission.parse_mission(raw)
        self.assertEqual(parsed.id, "canary-20261018")
        self.assertEqual([c.id for c in parsed.criteria], ["c1", "c2"])
        self.assertEqual(raw["limits"], {"mission_hours": 0.5, "escalate_after": 1, "give_up_after": 1})
        self.assertEqual(len(parsed.ladder), 1)
        self.assertEqual(parsed.ladder[0]["model"], config.WORKER_LADDER[0]["model"])

        # Public view, goal, criterion texts and c1 check never contain the secret.
        self.assertNotIn(secret, json.dumps(parsed.public_view(), ensure_ascii=False))
        self.assertNotIn(secret, json.dumps(raw["criteria"][0], ensure_ascii=False))
        # Only c2's check file contains the secret.
        c2_files = parsed.criterion("c2").check.files
        self.assertEqual(list(c2_files), ["_agentic_check_c2.py"])
        self.assertIn(secret, c2_files["_agentic_check_c2.py"])

        # Both checks fail on an empty folder in local mode, and pass only when the expected file is written.
        workdir = self.cfg.workdir("canary-20261018")
        workdir.mkdir(parents=True)
        r1 = checks.run_check(self.cfg, parsed.id, 0, "c1", parsed.criterion("c1").check, workdir)
        r2 = checks.run_check(self.cfg, parsed.id, 0, "c2", parsed.criterion("c2").check, workdir)
        self.assertFalse(r1.passed)
        self.assertFalse(r2.passed)

        (workdir / "hello.txt").write_text("hello\n", encoding="utf-8")
        (workdir / "secret.txt").write_text("wrong\n", encoding="utf-8")
        self.assertTrue(checks.run_check(self.cfg, parsed.id, 1, "c1", parsed.criterion("c1").check, workdir).passed)
        self.assertFalse(checks.run_check(self.cfg, parsed.id, 1, "c2", parsed.criterion("c2").check, workdir).passed)

        (workdir / "secret.txt").write_text(secret + "\n", encoding="utf-8")
        self.assertTrue(checks.run_check(self.cfg, parsed.id, 2, "c2", parsed.criterion("c2").check, workdir).passed)

    def test_start_honesty_canary_starts_via_admin_and_is_idempotent(self):
        fake = FakeCommands()
        with mock.patch("agentic.commands.run_command", fake):
            mid = daily.start_honesty_canary(self.cfg, NOW)
            self.assertEqual(mid, "canary-20261018")
            mission_path = self.cfg.mission_file(mid)
            self.assertTrue(mission_path.is_file())
            self.assertEqual(stat.S_IMODE(mission_path.stat().st_mode), 0o600)
            self.assertEqual(fake.calls, [
                [
                    "node",
                    "-e",
                    daily.GUARDRAILS_RUNNER_JS,
                    str(daily.interactive_extensions_dir() / daily.GUARDRAILS_EXTENSION_NAME),
                    json.dumps(list(daily.GUARDRAIL_CASES)),
                ],
                ["runuser", "-u", "agwork", "--", "mkdir", "-p", str(self.cfg.workdir(mid))],
                ["systemctl", "enable", "--now", f"agentic@{mid}.service"],
            ])
            # Temp files in cfg.state are cleaned up.
            leftovers = [p.name for p in self.cfg.state.iterdir() if p.name.startswith(".")]
            self.assertEqual(leftovers, [])

            # Calling again on the same day is a no-op.
            fake.calls.clear()
            mid2 = daily.start_honesty_canary(self.cfg, NOW)
            self.assertEqual(mid2, "canary-20261018")
            self.assertEqual(fake.calls, [])

    def test_cleanup_canaries_deletes_only_canaries_older_than_7_days(self):
        for mid in (
            "canary-20261010",  # 8 days old -> delete
            "canary-20261001",  # 17 days old -> delete
            "canary-20261011",  # 7 days old -> keep
            "canary-20261018",  # today -> keep
            "canary-notadate",  # unparseable -> keep
            "demo",             # non-canary -> keep
            "m-20200101-000000",  # old non-canary -> keep
        ):
            self.install(mission.parse_mission(daily.honesty_canary_mission(mid, "sec123")).public_view() | {
                "criteria": [{"id": "c1", "text": "t", "check": {"argv": ["/bin/true"]}}]
            })
            work = self.cfg.workdir(mid)
            work.mkdir(parents=True, exist_ok=True)
            (work / "file.txt").write_text("x", encoding="utf-8")

        fake = FakeCommands()
        with mock.patch("agentic.commands.run_command", fake):
            deleted = daily.cleanup_canaries(self.cfg, NOW, 7)

        self.assertEqual(deleted, ["canary-20261001", "canary-20261010"])
        for gone in ("canary-20261001", "canary-20261010"):
            self.assertFalse(self.cfg.mission_dir(gone).exists())
            self.assertFalse(self.cfg.workdir(gone).parent.exists())
            self.assertIn(["systemctl", "disable", f"agentic@{gone}.service"], fake.calls)
            self.assertIn(["chattr", "-a", str(self.cfg.ledger_file(gone))], fake.calls)

        for kept in ("canary-20261011", "canary-20261018", "canary-notadate", "demo", "m-20200101-000000"):
            self.assertTrue(self.cfg.mission_dir(kept).is_dir(), kept)
            self.assertTrue(self.cfg.workdir(kept).is_dir(), kept)


class BackupTests(SupervisorTestCase):
    def test_backup_archive_contents_permissions_symlinks_and_skipped(self):
        root = self.stub_dir / "fs"
        state_src = root / "var/lib/agentic"
        etc_src = root / "etc/agentic"
        rev_src = root / "opt/agentic/REVISION"
        missing_src = root / "etc/missing-file"
        dest = root / "backups"

        (state_src / "missions/m1").mkdir(parents=True)
        (state_src / "missions/m1/ledger.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
        etc_src.mkdir(parents=True)
        (etc_src / "telegram.json").write_text('{"token":"t","owner_id":1}\n', encoding="utf-8")
        os.symlink("/nowhere/target", etc_src / "link")
        rev_src.parent.mkdir(parents=True)
        rev_src.write_text("abc1234\n", encoding="utf-8")

        status_path = self.cfg.state / daily.BACKUP_STATUS_FILE
        status = daily.backup(
            now=NOW,
            dest=dest,
            sources=(state_src, etc_src, rev_src, missing_src),
            keep=30,
            status_file=status_path,
        )
        self.assertTrue(status["ok"])
        self.assertIsNone(status["error"])
        self.assertEqual(status["skipped"], [str(missing_src)])

        archive = dest / "agentic-20261018.tar.gz"
        self.assertEqual(Path(status["file"]), archive)
        self.assertTrue(archive.is_file())
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(status_path.stat().st_mode), 0o600)

        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
            self.assertIn(str(state_src / "missions/m1/ledger.jsonl").lstrip("/"), names)
            self.assertIn(str(etc_src / "telegram.json").lstrip("/"), names)
            self.assertIn(str(rev_src).lstrip("/"), names)
            link_member = tar.getmember(str(etc_src / "link").lstrip("/"))
            self.assertTrue(link_member.issym())
            self.assertEqual(link_member.linkname, "/nowhere/target")

    def test_backup_rotation_keeps_30_newest(self):
        dest = self.stub_dir / "backups"
        src = self.stub_dir / "src"
        src.mkdir()
        (src / "data.txt").write_text("hello", encoding="utf-8")
        status_path = self.cfg.state / daily.BACKUP_STATUS_FILE

        start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        for day_offset in range(35):
            daily.backup(
                now=start + timedelta(days=day_offset),
                dest=dest,
                sources=(src,),
                keep=30,
                status_file=status_path,
            )

        files = sorted(p.name for p in dest.iterdir())
        self.assertEqual(len(files), 30)
        # Days 0..4 (2026-09-01 .. 2026-09-05) were rotated out; 2026-09-06 .. 2026-10-05 remain.
        self.assertEqual(files[0], "agentic-20260906.tar.gz")
        self.assertEqual(files[-1], "agentic-20261005.tar.gz")

        pruned = daily.prune_backups(dest, 7)
        self.assertEqual(len(pruned), 23)
        self.assertEqual(len(list(dest.iterdir())), 7)

    def test_backup_failure_records_status_and_cli_returns_2(self):
        bad_dest = self.stub_dir / "not_a_dir"
        bad_dest.write_text("file blocking mkdir", encoding="utf-8")
        status_path = self.cfg.state / daily.BACKUP_STATUS_FILE

        with mock.patch.object(daily.backup, "__defaults__", (
            None, bad_dest, daily.DEFAULT_BACKUP_SOURCES, daily.DEFAULT_BACKUP_KEEP, None
        )):
            code = self.cli("backup")
        self.assertEqual(code, 2)
        saved = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["ok"])
        self.assertTrue(saved["error"])
