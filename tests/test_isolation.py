"""SPEC-F sections 1 and 4: worker folders belong to agwork, migration, the append-only ledger, silent canaries."""

import io
import os
import tarfile
import zipfile
from pathlib import Path
from unittest import mock

from agentic import admin, commands, config, inbox, ledger, notify
from helpers import FakeCommands, SupervisorTestCase, fix, make_mission


class WorkerUserFakes(SupervisorTestCase):
    """A fake runner that really performs `runuser -u agwork -- mkdir/tar` and root's `tar -cf` (as this user)."""

    def setUp(self):
        super().setUp()
        self.fake = FakeCommands(self.respond)
        patcher = mock.patch("agentic.commands.run_command", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fail_extract = False

    def respond(self, argv):
        index = len(self.fake.calls) - 1
        if argv[:5] == ["runuser", "-u", "agwork", "--", "mkdir"]:
            Path(argv[6]).mkdir(parents=True, exist_ok=True)
        elif argv[:1] == ["tar"] and argv[3] == "-cf":
            with tarfile.open(argv[4], "w") as archive:
                archive.add(argv[2], arcname=".")
        elif argv[:5] == ["runuser", "-u", "agwork", "--", "tar"]:
            if self.fail_extract:
                return commands.CommandResult(2, "", "tar: cannot open")
            with tarfile.open(self.fake.stdin[index]) as archive:
                archive.extractall(argv[6], filter="tar")
        return commands.CommandResult(0, "", "")


class MigrationTests(WorkerUserFakes):
    def old_folder(self, mission_id: str) -> Path:
        work = self.old_root / mission_id / "work"
        (work / "src").mkdir(parents=True)
        (work / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        return work

    def setUp(self):
        super().setUp()
        self.old_root = self.stub_dir / "old-missions"

    def test_folders_move_as_agwork_and_a_second_run_changes_nothing(self):
        old = self.old_folder("weather")
        self.install(make_mission(mission_id="weather"))
        self.install(make_mission(mission_id="lost"))  # a mission whose folder is nowhere
        self.assertEqual(admin.migrate_workspaces(self.cfg, self.old_root), [
            ("lost", f"missing: no folder {self.old_root / 'lost' / 'work'}"), ("weather", "moved")])
        new = self.cfg.workdir("weather")
        self.assertEqual((new / "src" / "app.py").read_text(encoding="utf-8"), "print('hi')\n")
        self.assertFalse(old.exists())
        archive = str(self.cfg.state / "scratch" / "migrate-weather.tar")
        self.assertEqual(self.fake.calls, [
            ["runuser", "-u", "agwork", "--", "mkdir", "-p", str(new)],
            ["tar", "-C", str(old), "-cf", archive, "."],
            ["runuser", "-u", "agwork", "--", "tar", "-C", str(new), "-xf", "-"]])
        self.assertEqual(self.fake.stdin, {2: archive})
        self.assertFalse(Path(archive).exists(), "the archive is deleted")

        self.fake.calls.clear()
        self.assertEqual(admin.migrate_workspaces(self.cfg, self.old_root),
                         [("lost", f"missing: no folder {self.old_root / 'lost' / 'work'}"),
                          ("weather", "already moved")])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((new / "src" / "app.py").read_text(encoding="utf-8"), "print('hi')\n")

    def test_a_failed_unpack_keeps_the_old_folder_and_is_reported(self):
        old = self.old_folder("weather")
        self.fail_extract = True
        ((mission_id, outcome),) = admin.migrate_workspaces(self.cfg, self.old_root)
        self.assertEqual(mission_id, "weather")
        self.assertTrue(outcome.startswith("failed: runuser tar failed (exit 2)"), outcome)
        self.assertTrue((old / "src" / "app.py").exists())

    def test_the_command_prints_one_line_per_mission_and_needs_root(self):
        self.old_folder("weather")
        self.assertEqual(self.cli("migrate-workspaces", str(self.old_root)), 2)
        self.assertIn("must be run as root", self.err)
        with mock.patch("os.geteuid", return_value=0):
            self.assertEqual(self.cli("migrate-workspaces", str(self.old_root)), 0, self.err)
        self.assertEqual(self.out, "weather: moved\n")

    def test_the_default_roots(self):
        self.assertEqual(config.OLD_WORKSPACES, "/home/agent/missions")
        self.assertEqual(config.load({}).workspaces, Path("/var/lib/agwork/missions"))


class WorkerFolderTests(WorkerUserFakes):
    def test_the_supervisor_makes_a_missing_worker_folder_as_agwork(self):
        # The production worker command (no AGENTIC_WORKER_CMD override): the folder is made by agwork.
        cfg = config.load({k: v for k, v in os.environ.items() if k != "AGENTIC_WORKER_CMD"})
        admin.ensure_workdir(cfg, "demo")
        self.assertEqual(self.fake.calls, [["runuser", "-u", "agwork", "--", "mkdir", "-p", str(cfg.workdir("demo"))]])

    def test_files_reads_cfg_workdir_and_keeps_only_files_of_the_worker_account(self):
        self.play(fix("c1", "c2"))
        self.run_mission()
        sent = []
        with mock.patch("os.geteuid", return_value=0), \
                mock.patch("agentic.inbox.user_uid", return_value=os.getuid()) as uid, \
                mock.patch("agentic.inbox._send_document", lambda cfg, data, *rest: sent.append(data)):
            self.assertIsNone(inbox._files(self.cfg, "demo", None))
        uid.assert_called_once_with("agwork")
        names = sorted(zipfile.ZipFile(io.BytesIO(sent[0])).namelist())
        self.assertEqual(names, ["c1.txt", "c2.txt", "report-demo.md"])


class AppendOnlyLedgerTests(SupervisorTestCase):
    def run_with(self, respond) -> FakeCommands:
        fake = FakeCommands(respond)
        with mock.patch("agentic.commands.run_command", fake):
            self.play(fix("c1", "c2"))
            self.assertEqual(self.run_mission(), "complete")
        return fake

    def test_the_new_ledger_is_made_append_only_through_the_runner(self):
        fake = self.run_with(None)
        self.assertEqual(fake.calls, [["chattr", "+a", str(self.cfg.ledger_file("demo"))]])
        self.assertNotIn("notice", self.types())

    def test_a_failed_chattr_becomes_a_notice_and_the_mission_goes_on(self):
        fake = self.run_with(lambda argv: commands.CommandResult(1, "", "chattr: Operation not permitted"))
        self.assertEqual(len(fake.calls), 1)
        notice = next(r for r in self.records() if r["type"] == "notice")
        self.assertEqual(notice["seq"], 2)
        self.assertEqual((notice["data"]["kind"], notice["data"]["exit_code"]), ("append_only", 1))
        self.assertEqual(notice["data"]["text"], "원장을 추가 전용(chattr +a)으로 잠그지 못함")
        self.assertTrue(self.types()[-1] == "mission_finished")
        self.assertTrue(ledger.Ledger(self.cfg.ledger_file("demo")).verify().ok)

    def test_a_resumed_run_does_not_call_chattr_again(self):
        self.run_with(None)
        fake = self.run_with(None)
        self.assertEqual(fake.calls, [])

    def test_verify_reports_a_ledger_without_the_flag(self):
        self.run_with(None)
        self.assertEqual(self.cli("verify", "demo"), 0)
        self.assertIn("lacks the append-only flag", self.err)
        with mock.patch("agentic.ledger.append_only", return_value=True):
            self.assertEqual(self.cli("verify", "demo"), 0)
        self.assertEqual(self.err, "")

    def test_append_only_reads_the_flag(self):
        path = self.stub_dir / "plain"
        path.write_text("x")
        self.assertIs(ledger.append_only(path), False)
        self.assertIsNone(ledger.append_only(self.stub_dir / "missing"))


class SilentCanaryTests(SupervisorTestCase):
    def test_canary_missions_never_notify(self):
        self.cfg.outbox.mkdir(parents=True, mode=0o700)
        self.play(fix("c1", "c2"))
        self.run_mission(make_mission(mission_id="demo"))
        self.assertNotEqual(os.listdir(self.cfg.outbox), [], "an ordinary mission does notify")
        for name in os.listdir(self.cfg.outbox):
            os.unlink(self.cfg.outbox / name)
        self.run_mission(make_mission(mission_id="canary-20261018"))
        self.assertEqual(os.listdir(self.cfg.outbox), [])
        fake = FakeCommands()
        with mock.patch("agentic.commands.run_command", fake):
            notify.notify_failure(self.cfg, "canary-20261018")
        self.assertEqual((fake.calls, os.listdir(self.cfg.outbox)), ([], []))
