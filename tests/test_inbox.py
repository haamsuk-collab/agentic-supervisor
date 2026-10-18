import contextlib
import dataclasses
import datetime
import io
import json
import os
import re
import stat
import sys
import zipfile
from unittest import mock

from agentic import commands, inbox, spool
from agentic.config import INBOX_ITEM_MAX_BYTES
from agentic.ledger import Ledger
from cardcheck import card_text, html_problems
from helpers import CURRENT_USER, Crash, FakeCommands, PlannedTestCase, SupervisorTestCase, fix, make_mission

SECRET_MARKER = "SECRET_CONTENT_MARKER"


def is_active(argv):
    if argv[:2] == ["systemctl", "is-active"]:
        return commands.CommandResult(0, "active\n", "")
    return commands.CommandResult(0, "", "")


class InboxCase(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.cfg.inbox.mkdir(parents=True, mode=0o700)
        self.cfg.outbox.mkdir(parents=True, mode=0o700)
        self.fake = FakeCommands(is_active)
        patcher = mock.patch("agentic.commands.run_command", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.count = 0

    def put(self, payload, raw: bytes | None = None) -> str:
        """An inbox file as the bot would write it; later files are newer."""
        self.count += 1
        name = f"item{self.count:03d}"
        path = self.cfg.inbox / name
        path.write_bytes(raw if raw is not None else json.dumps(payload).encode("utf-8"))
        os.utime(path, (1_000_000 + self.count, 1_000_000 + self.count))
        return name

    def process(self, cfg=None) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(inbox.process(cfg or self.cfg), 0)
        return err.getvalue()

    def replies(self) -> list[dict]:
        """Outbox items, oldest first. A card item is shown as {"text": <what the owner reads>, "reply_to"?} after
        its HTML was checked; a document item keeps its fields plus "content" (the document's bytes)."""
        files = sorted((p for p in self.cfg.outbox.iterdir() if not p.name.endswith(".doc")),
                       key=lambda p: spool.order_key(p.name, p.stat().st_mtime_ns))
        found = []
        for path in files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if "html" in payload:
                for message in payload["html"]:
                    self.assertEqual(html_problems(message), [], message)
                    self.assertLessEqual(len(message), 4000)
                payload = {**{k: v for k, v in payload.items() if k != "html"}, "text": card_text(payload["html"])}
            else:
                payload["content"] = (self.cfg.outbox / payload["document"]).read_bytes()
            found.append(payload)
        return found

    def run_quietly(self, mission=None):
        """Run a mission; the supervisor's own notifications are dropped so only inbox replies remain."""
        status = self.run_mission(mission)
        for path in self.cfg.outbox.iterdir():
            path.unlink()
        return status

    def inbox_names(self) -> list[str]:
        return sorted(p.name for p in self.cfg.inbox.iterdir())


class CommandTests(InboxCase):
    def test_new_creates_the_mission_and_starts_it_in_order(self):
        self.put({"type": "new", "text": "날씨를 알려주는 프로그램을 만들어 주세요.", "reply_to": 77})
        self.process()
        (reply,) = self.replies()
        lines = reply["text"].splitlines()
        match = re.fullmatch(r"🚀 접수 │ (m-\d{8}-\d{6})", lines[0])
        self.assertIsNotNone(match, reply)
        self.assertEqual(lines[2:], ["계획을 세우는 중입니다", "👉 지금 하실 일: 없음 — 자동으로 진행 중입니다"])
        self.assertEqual(reply["reply_to"], 77)
        mission_id = match.group(1)
        self.assertEqual(self.fake.calls, [
            ["runuser", "-u", "agwork", "--", "mkdir", "-p", str(self.cfg.workdir(mission_id))],
            ["systemctl", "enable", "--now", f"agentic@{mission_id}.service"],
        ])
        folder = self.cfg.mission_dir(mission_id)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.cfg.request_file(mission_id).stat().st_mode), 0o600)
        self.assertEqual(self.cfg.request_file(mission_id).read_text(encoding="utf-8"),
                         "날씨를 알려주는 프로그램을 만들어 주세요.")
        self.assertEqual(self.inbox_names(), [])

    def test_two_new_requests_in_one_second_get_different_ids(self):
        self.put({"type": "new", "text": "first request"})
        self.put({"type": "new", "text": "second request"})
        with mock.patch("agentic.admin.datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime.datetime(2026, 1, 2, 3, 4, 5)
            self.process()
        self.assertEqual([r["text"].splitlines()[0] for r in self.replies()], [
            "🚀 접수 │ m-20260102-030405",
            "🚀 접수 │ m-20260102-030405-2"])

    def test_new_with_a_bad_request_or_a_failing_command_is_a_korean_error(self):
        self.put({"type": "new", "text": "   "})
        self.process()
        (reply,) = self.replies()
        self.assertTrue(reply["text"].startswith("❌ 처리하지 못했습니다: "))
        self.assertIn("empty", reply["text"])
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.cfg.state / "missions").exists())

        self.fake.respond = lambda argv: commands.CommandResult(1, "", "boom")
        self.put({"type": "new", "text": "a real request", "reply_to": 5})
        self.process()
        last = self.replies()[-1]
        self.assertTrue(last["text"].startswith("❌ 처리하지 못했습니다: "))
        self.assertIn("boom", last["text"])
        self.assertEqual(last["reply_to"], 5)
        self.assertEqual(self.inbox_names(), [])

    def test_list_without_missions(self):
        self.put({"type": "list"})
        self.process()
        self.assertEqual(self.replies(), [{"text": "등록된 미션이 없습니다"}])

    def test_list_has_one_block_per_mission_newest_first(self):
        self.play(fix("c1", "c2"))
        self.run_quietly()
        self.install(make_mission(mission_id="other"))
        self.put({"type": "list", "reply_to": 3})
        self.process()
        (reply,) = self.replies()
        self.assertEqual(reply["reply_to"], 3)
        first, second = reply["text"].splitlines()
        self.assertRegex(first, r"^demo · ✅ 완료 · 2/2 · \d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST$")
        self.assertEqual(second, "other · 기록 없음")

    def test_list_orders_by_the_latest_record_and_shows_at_most_twenty(self):
        moments = {}
        for n in range(22):
            mission_id = f"m{n:02d}"
            self.install(make_mission(mission_id=mission_id))
            moment = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(hours=(n * 7) % 22)
            Ledger(self.cfg.ledger_file(mission_id), clock=lambda m=moment: m).append(
                "mission_started", {"id": mission_id, "goal": "g", "criteria": [{"id": "c1", "text": "t"}],
                                    "limits": {}, "ladder": []})
            moments[mission_id] = moment
        self.put({"type": "list"})
        self.process()
        lines = self.replies()[0]["text"].splitlines()
        newest = sorted(moments, key=moments.get, reverse=True)[:20]
        self.assertEqual([line.split(" · ")[0] for line in lines], newest)
        self.assertTrue(all(" · 🛠️ 진행 중 · 0/1 · " in line for line in lines))

    def test_status_of_a_finished_mission_is_its_finish_card(self):
        self.play(fix("c1", "c2"))
        self.run_quietly()
        self.put({"type": "status", "id": "demo"})
        self.process()
        (reply,) = self.replies()
        lines = reply["text"].splitlines()
        self.assertEqual(lines[0], "✅ 완료 │ demo")
        self.assertIn("🎯 달성 2 / 2 (100%) · 감독자가 직접 검사한 결과", lines)
        self.assertIn(" 1. 결과물 받기 → /files demo", lines)
        self.assertEqual(self.documents(), [], "/status sends no document")

    def test_status_while_running_shows_progress_and_the_last_three_events(self):
        self.play(fix("c1"), fix("c2"))
        real_append, count = Ledger.append, [0]

        def append(ledger, record_type, data):
            record = real_append(ledger, record_type, data)
            count[0] += 1
            if record_type == "check_result" and data["iteration"] == 1 and data["criterion"] == "c2":
                raise Crash
            return record

        with mock.patch.object(Ledger, "append", append), self.assertRaises(Crash):
            self.run_quietly()
        for path in self.cfg.outbox.iterdir():
            path.unlink()
        self.put({"type": "status", "id": "demo"})
        self.process()
        lines = self.replies()[0]["text"].splitlines()
        self.assertEqual(lines[0], "🛠️ 진행 중 │ demo")
        self.assertEqual(lines[2], "🎯 달성 1 / 2 (50%) · 감독자가 직접 검사한 결과")
        self.assertEqual(lines[3], "▰▰▰▰▰▱▱▱▱▱")
        self.assertEqual(lines[4], "모델 Sonnet 5.5 · 반복 1회")
        self.assertRegex(lines[5], r"^⏱ 걸린 시간 \d+시간 \d+분$")
        self.assertRegex(lines[6], r"^🕒 \d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST 기준$")
        self.assertEqual(lines[7:9], ["👉 지금 하실 일", " 1. 없음 — 자동으로 진행 중입니다"])
        self.assertEqual(lines[9], "최근 기록 (KST)")
        events = lines[10:]
        self.assertEqual(len(events), 3)
        self.assertRegex(events[0], r"^ · \d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST 🛠️ 작업 1회차 — ")
        self.assertRegex(events[1], r"^ · \d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST 🧪 점검 1회차 — c1: 통과$")
        self.assertRegex(events[2], r"^ · \d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST 🧪 점검 1회차 — c2: 실패$")

    def documents(self) -> list[dict]:
        return [r for r in self.replies() if "document" in r]

    def test_report_is_the_report_file_exactly_as_a_document(self):
        self.play(fix("c1", "c2"))
        self.run_quietly()
        self.put({"type": "report", "id": "demo", "reply_to": 9})
        self.process()
        (reply,) = self.replies()
        self.assertEqual(reply["filename"], "report-demo.md")
        self.assertEqual(reply["reply_to"], 9)
        self.assertEqual(reply["content"].decode("utf-8"), self.report_text())
        self.assertEqual(html_problems(reply["caption"]), [])
        self.assertTrue(spool.is_document_name(reply["document"]))

    def test_report_before_the_mission_has_one(self):
        self.install(make_mission())
        self.put({"type": "report", "id": "demo"})
        self.process()
        self.assertEqual(self.replies(), [{"text": "아직 보고서가 없습니다"}])

    def test_stop_runs_systemctl_disable(self):
        self.install(make_mission())
        self.put({"type": "stop", "id": "demo", "reply_to": 4})
        self.process()
        self.assertEqual(self.fake.calls, [["systemctl", "disable", "--now", "agentic@demo.service"]])
        self.assertEqual(self.replies(), [{"text": "⏹️ 멈췄습니다: demo", "reply_to": 4}])

    def test_unknown_missions_get_a_korean_error_and_no_command(self):
        for kind in ("stop", "status", "report", "files"):
            self.put({"type": kind, "id": "nope"})
        self.put({"type": "continue", "id": "nope", "text": "더 빠르게 만들어 주세요"})
        self.process()
        self.assertEqual([r["text"] for r in self.replies()], ["없는 미션입니다: nope"] * 5)
        self.assertEqual(self.fake.calls, [])

    def test_items_are_handled_oldest_first_and_every_one_is_answered(self):
        self.install(make_mission())
        self.put({"type": "stop", "id": "gone", "reply_to": 1})
        self.put({"type": "stop", "id": "demo", "reply_to": 2})
        self.put({"type": "list", "reply_to": 3})
        self.process()
        self.assertEqual([r["reply_to"] for r in self.replies()], [1, 2, 3])
        self.assertEqual(self.inbox_names(), [])

    def test_outbox_items_are_private_files_without_reply_to_when_none_was_given(self):
        self.put({"type": "list"})
        self.process()
        (path,) = self.cfg.outbox.iterdir()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertRegex(path.name, r"^\d{20}-[0-9a-f]{32}$")
        self.assertNotIn("reply_to", json.loads(path.read_text(encoding="utf-8")))

    def test_a_missing_inbox_is_quietly_fine(self):
        os.rmdir(self.cfg.inbox)
        self.assertEqual(self.process(), "")
        self.assertTrue(self.cfg.inbox_lock.exists())
        self.assertEqual(stat.S_IMODE(self.cfg.inbox_lock.stat().st_mode), 0o600)
        self.assertEqual(list(self.cfg.outbox.iterdir()), [])

    def test_a_failing_item_does_not_stop_the_next(self):
        self.put({"type": "new", "text": "x" * 30})
        self.put({"type": "list"})
        self.fake.respond = lambda argv: commands.CommandResult(1, "", "boom") if argv[0] == "runuser" else is_active(argv)
        err = self.process()
        self.assertIn("failed", err)
        first, second = self.replies()
        self.assertTrue(first["text"].startswith("❌ 처리하지 못했습니다"))
        self.assertTrue(second["text"].startswith("m-"), second)


class RejectionTests(InboxCase):
    def assert_rejected(self, reply_to=None):
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.inbox_names(), [])
        (reply,) = self.replies()
        self.assertTrue(reply["text"].startswith("⚠️ 받은 요청을 처리할 수 없습니다"), reply)
        self.assertEqual(reply.get("reply_to"), reply_to)
        self.assertFalse((self.cfg.state / "missions").exists())

    def test_a_symlink_is_deleted_and_never_followed(self):
        target = self.stub_dir / "precious.json"
        target.write_text(json.dumps({"type": "new", "text": "must not be read or touched"}))
        os.symlink(target, self.cfg.inbox / "link")
        err = self.process()
        self.assertIn("symlink", err)
        self.assert_rejected()
        self.assertEqual(json.loads(target.read_text())["text"], "must not be read or touched")

    def test_a_dangling_symlink_is_deleted(self):
        os.symlink(self.stub_dir / "missing", self.cfg.inbox / "link")
        self.process()
        self.assert_rejected()

    def test_a_file_not_owned_by_the_bot_is_rejected(self):
        if os.geteuid() == 0:
            self.skipTest("root owns the test files, so another account cannot be the bot")
        self.put({"type": "new", "text": "a request nobody sent"})
        other = dataclasses.replace(self.cfg, bot_user="root")
        self.process(other)
        self.assert_rejected()

    def test_an_oversized_file_is_rejected(self):
        padding = "x" * INBOX_ITEM_MAX_BYTES
        self.put({"type": "new", "text": padding})
        self.process()
        self.assert_rejected()

    def test_a_file_of_exactly_the_limit_is_read(self):
        head = b'{"type": "list", "reply_to": 1}'
        self.put(None, head + b" " * (INBOX_ITEM_MAX_BYTES - len(head)))
        self.process()
        self.assertEqual(self.replies(), [{"text": "등록된 미션이 없습니다", "reply_to": 1}])

    def test_bad_json_and_non_objects_are_rejected(self):
        for raw in (b"{not json", b"[1, 2]", b'"text"', b"\xff\xfe", b'{"type": "list", "type": "stop"}',
                    b'{"type": "list", "reply_to": NaN}', b""):
            with self.subTest(raw=raw):
                self.put(None, raw)
                self.process()
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(self.inbox_names(), [])
        self.assertEqual(len(self.replies()), 7)

    def test_unknown_keys_are_rejected(self):
        self.put({"type": "list", "extra": 1, "reply_to": 6})
        self.process()
        self.assert_rejected(reply_to=6)

    def test_invalid_ids_are_rejected(self):
        for kind in ("status", "report", "stop", "files"):
            for bad in ("../etc", "Bad", "", "a" * 42, "-x", 5, None, ["demo"]):
                with self.subTest(kind=kind, id=bad):
                    self.put({"type": kind, "id": bad})
        for bad in ("../etc", "Bad", "a" * 42, 5, None):
            self.put({"type": "continue", "id": bad, "text": "더 빠르게 만들어 주세요"})
        self.put({"type": "status"})
        self.process()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.inbox_names(), [])
        self.assertEqual(len(self.replies()), 4 * 8 + 5 + 1)

    def test_every_contract_violation_is_rejected(self):
        bad_items = [
            {"type": "nope"}, {}, {"type": 1}, {"type": ["list"]},
            {"type": "new"}, {"type": "new", "text": 5}, {"type": "new", "text": "x", "id": "demo"},
            {"type": "list", "text": "x"}, {"type": "list", "id": "demo"},
            {"type": "stop", "id": "demo", "text": "x"},
            {"type": "files"}, {"type": "files", "id": "demo", "text": "x"},
            {"type": "continue", "id": "demo"}, {"type": "continue", "text": "더 빠르게 만들어 주세요"},
            {"type": "continue", "id": "demo", "text": 5},
            {"type": "list", "reply_to": 0}, {"type": "list", "reply_to": -1}, {"type": "list", "reply_to": True},
            {"type": "list", "reply_to": 1.5}, {"type": "list", "reply_to": "7"},
        ]
        for item in bad_items:
            self.put(item)
        self.process()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.inbox_names(), [])
        replies = self.replies()
        self.assertEqual(len(replies), len(bad_items))
        self.assertTrue(all(r["text"].startswith("⚠️") and "reply_to" not in r for r in replies), replies)
        self.assertFalse((self.cfg.state / "missions").exists())

    def test_a_directory_in_the_inbox_is_removed_too(self):
        (self.cfg.inbox / "folder").mkdir()
        self.process()
        self.assert_rejected()


class OutboxSafetyTests(InboxCase):
    def test_a_planted_symlink_at_the_target_name_is_refused(self):
        target = self.stub_dir / "victim.txt"
        target.write_text("untouched")
        os.symlink(target, self.cfg.outbox / "fixed")
        with self.assertRaises(FileExistsError):
            spool.write_item(self.cfg.outbox, {"text": "hello"}, name="fixed")
        self.assertEqual(target.read_text(), "untouched")
        self.assertTrue((self.cfg.outbox / "fixed").is_symlink())

    def test_a_planted_symlink_at_the_temporary_name_is_refused(self):
        target = self.stub_dir / "victim.txt"
        target.write_text("untouched")
        os.symlink(target, self.cfg.outbox / "fixed.tmp")
        with self.assertRaises(FileExistsError):
            spool.write_item(self.cfg.outbox, {"text": "hello"}, name="fixed")
        self.assertEqual(target.read_text(), "untouched")
        self.assertFalse((self.cfg.outbox / "fixed").exists())

    def test_a_missing_outbox_loses_the_reply_but_still_deletes_the_request(self):
        os.rmdir(self.cfg.outbox)
        self.put({"type": "list"})
        err = self.process()
        self.assertIn("cannot write the reply", err)
        self.assertEqual(self.inbox_names(), [])

    def test_the_inbox_itself_may_not_be_a_symlink(self):
        real = self.stub_dir / "real-inbox"
        real.mkdir()
        (real / "x").write_text(json.dumps({"type": "list"}))
        self.cfg.inbox.rmdir()
        os.symlink(real, self.cfg.inbox)
        with self.assertRaises(OSError):
            inbox.process(self.cfg)
        self.assertTrue((real / "x").exists())


def inactive(argv):
    if argv[:2] == ["systemctl", "is-active"]:
        return commands.CommandResult(3, "inactive\n", "")
    return commands.CommandResult(0, "", "")


class FilesTests(InboxCase):
    def finished_mission(self):
        self.play(fix("c1", "c2"))
        self.run_quietly()
        work = self.cfg.workdir("demo")
        (work / "sub").mkdir()
        (work / "sub" / "a.txt").write_text("nested", encoding="utf-8")
        return work

    def zip_reply(self) -> tuple[dict, zipfile.ZipFile]:
        (reply,) = self.replies()
        self.assertIn("document", reply, reply)
        return reply, zipfile.ZipFile(io.BytesIO(reply["content"]))

    def test_the_zip_holds_the_worker_files_and_the_report_and_nothing_from_the_state_folder(self):
        self.finished_mission()
        self.put({"type": "files", "id": "demo", "reply_to": 8})
        self.process()
        reply, archive = self.zip_reply()
        self.assertEqual(reply["filename"], "results-demo.zip")
        self.assertEqual(reply["reply_to"], 8)
        self.assertEqual(card_text([reply["caption"]]), "📦 demo 결과물 · 파일 4개")
        self.assertEqual(sorted(archive.namelist()), ["c1.txt", "c2.txt", "report-demo.md", "sub/a.txt"])
        self.assertEqual(archive.read("report-demo.md").decode("utf-8"), self.report_text())
        self.assertEqual(archive.read("sub/a.txt"), b"nested")
        for name in archive.namelist():
            self.assertNotIn(SECRET_MARKER.encode(), archive.read(name))

    def test_symlinks_into_the_state_folder_are_not_followed(self):
        work = self.finished_mission()
        (work / "mission-link.json").symlink_to(self.cfg.mission_file("demo"))
        (work / "state-link").symlink_to(self.cfg.mission_dir("demo"))
        self.put({"type": "files", "id": "demo"})
        self.process()
        _, archive = self.zip_reply()
        self.assertEqual(sorted(archive.namelist()), ["c1.txt", "c2.txt", "report-demo.md", "sub/a.txt"])

    def test_over_twenty_megabytes_gives_a_message_instead_of_a_file(self):
        work = self.finished_mission()
        (work / "big.bin").write_bytes(os.urandom(21 * 1024 * 1024))
        self.put({"type": "files", "id": "demo", "reply_to": 2})
        self.process()
        (reply,) = self.replies()
        self.assertRegex(reply["text"], r"^결과물이 20MB를 넘어 보낼 수 없습니다 \(\d+\.\dMB( 이상)?\)$")
        self.assertEqual(reply["reply_to"], 2)
        self.assertEqual([n for n in os.listdir(self.cfg.outbox) if n.endswith(".doc")], [],
                         "no document file was left in the outbox")

    def test_an_empty_worker_folder_gets_a_korean_message(self):
        self.install(make_mission())
        self.cfg.workdir("demo").mkdir(parents=True)
        self.put({"type": "files", "id": "demo"})
        self.process()
        self.assertEqual(self.replies(), [{"text": "결과물이 아직 없습니다: demo"}])


class ContinueTests(InboxCase):
    def test_continue_starts_a_follow_up_and_names_its_parent(self):
        self.play(fix("c1", "c2"))
        self.run_quietly()
        self.fake.respond = inactive
        self.fake.calls.clear()  # the parent run's own `chattr +a` of its ledger (SPEC-F section 4) is not counted
        self.put({"type": "continue", "id": "demo", "text": "출력을 JSON으로도 내 주세요", "reply_to": 12})
        self.process()
        (reply,) = self.replies()
        self.assertEqual(reply["reply_to"], 12)
        self.assertEqual(reply["text"].splitlines(), [
            "🚀 접수 │ demo-c1", "━━━━━━━━━━━━━━━━━━", "계획을 세우는 중입니다", "이어서 한 미션: demo",
            "👉 지금 하실 일: 없음 — 자동으로 진행 중입니다"])
        commands_run = [call[:5] for call in self.fake.calls if call[1] != "is-active"]
        self.assertEqual(commands_run, [["runuser", "-u", "agwork", "--", "mkdir"], ["runuser", "-u", "agwork", "--", "cp"],
                                        ["systemctl", "enable", "--now", "agentic@demo-c1.service"]])
        self.assertTrue(self.cfg.request_file("demo-c1").read_text(encoding="utf-8").startswith(
            "[이어서 하는 미션] 이전 미션: demo\n"))

    def test_a_running_parent_is_refused(self):
        self.play(fix("c1", "c2"))
        self.run_quietly()
        self.put({"type": "continue", "id": "demo", "text": "출력을 JSON으로도 내 주세요"})
        self.process()
        self.assertEqual(self.replies(), [{"text": "아직 진행 중인 미션입니다"}])
        self.assertFalse(self.cfg.mission_dir("demo-c1").exists())



class StatusSecrecyTests(InboxCase, PlannedTestCase):
    def test_status_of_a_planned_mission_never_shows_check_data(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.new(), 0, self.err)
        for path in self.cfg.outbox.iterdir():  # the supervisor's own notifications
            path.unlink()
        self.put({"type": "status", "id": "demo"})
        self.process()
        text = self.replies()[0]["text"]
        self.assertIn("demo", text)
        self.assert_no_check_data(text)

    def test_a_long_ledger_status_and_list_hide_checks(self):
        self.play(fix("c1"), fix("c2"))
        self.run_quietly(make_mission(ids=("c1", "c2")))
        self.put({"type": "status", "id": "demo"})
        self.put({"type": "list"})
        self.process()
        for reply in self.replies():
            self.assert_no_check_data(reply["text"])

    def assert_no_check_data(self, text: str):
        for secret in (SECRET_MARKER, "secret_check", "checks/", sys.executable, "ALL TESTS PASSED", "argv"):
            self.assertNotIn(secret, text)
        self.assertNotIn(CURRENT_USER + "/", text)
