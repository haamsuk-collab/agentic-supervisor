"""The Telegram bot daemon (SPEC-D.md section 2) against an in-process fake API and a local fake HTTP server."""

import contextlib
import email.parser
import email.policy
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import helpers
from helpers import TimedTestCase

from agentic import spool, telegram
from agentic.telegram import ApiError, Bot, NetworkError, TelegramApi

OWNER = 4242
STALE = "이미 처리했거나 만료된 요청입니다"
REQUEST = "날씨를 알려주는 명령줄 프로그램을 만들어 주세요."


class FakeApi:
    """Stands in for TelegramApi: records every call; `hook(method, params)` may raise or return a result."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.hook = None
        self.next_message_id = 1000

    def call(self, method, params):
        self.calls.append((method, json.loads(json.dumps(params))))
        if self.hook is not None:
            result = self.hook(method, params)
            if result is not None:
                return result
        if method == "sendMessage":
            self.next_message_id += 1
            return {"message_id": self.next_message_id}
        if method == "getUpdates":
            return []
        return True

    def send_document(self, params, filename, content):
        """Records the upload as a "sendDocument" call whose params also carry the file name and the content."""
        recorded = {**params, "filename": filename, "content": content.decode("latin-1")}
        self.calls.append(("sendDocument", json.loads(json.dumps(recorded))))
        if self.hook is not None:
            result = self.hook("sendDocument", recorded)
            if result is not None:
                return result
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    def of(self, method):
        return [params for name, params in self.calls if name == method]

    def texts(self):
        return [params["text"] for params in self.of("sendMessage")]


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def message(text, message_id=7, user=OWNER, chat=OWNER, chat_type="private"):
    return {"update_id": 1, "message": {"message_id": message_id, "from": {"id": user}, "text": text,
                                        "chat": {"id": chat, "type": chat_type}}}


def press(data, message_id=500, user=OWNER, chat=OWNER, chat_type="private"):
    return {"update_id": 2, "callback_query": {
        "id": "cb1", "from": {"id": user}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat, "type": chat_type}}}}


class BotTestCase(TimedTestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="agentic-bot-test-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        self.tmp = Path(tmp)
        self.state, self.inbox, self.outbox = self.tmp / "state", self.tmp / "inbox", self.tmp / "outbox"
        for directory in (self.state, self.inbox, self.outbox):
            directory.mkdir(mode=0o700)
        self.api, self.clock, self.sleeps = FakeApi(), Clock(), []
        self.bot = self.new_bot()

    def new_bot(self):
        return Bot(self.api, OWNER, self.state, self.inbox, self.outbox, clock=self.clock, sleep=self.sleeps.append)

    def inbox_items(self):
        dfd = spool.open_dir(self.inbox)
        try:
            return [spool.read_item(dfd, name, 1 << 20) for name in spool.list_names(dfd)]
        finally:
            os.close(dfd)

    def outbox_names(self):
        return sorted(os.listdir(self.outbox))

    def put_outbox(self, payload):
        return spool.write_item(self.outbox, payload)

    def last_keys(self):
        """The callback_data values of the buttons of the last message sent."""
        markup = self.api.of("sendMessage")[-1]["reply_markup"]
        return [button["callback_data"] for button in markup["inline_keyboard"][0]]


class AuthorizationTests(BotTestCase):
    def assert_ignored(self, update):
        self.bot.handle_update(update)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(self.bot.pending, {})

    def test_other_user_other_chat_group_and_channel_are_ignored(self):
        self.assert_ignored(message(REQUEST, user=999))
        self.assert_ignored(message(REQUEST, chat=999))
        self.assert_ignored(message(REQUEST, chat=-100123, chat_type="group"))
        self.assert_ignored(message(REQUEST, chat=OWNER, chat_type="supergroup"))
        self.assert_ignored(message("/list", chat=OWNER, chat_type="group"))
        self.assert_ignored({"update_id": 3, "channel_post": {"message_id": 1, "text": REQUEST,
                                                              "chat": {"id": OWNER, "type": "channel"}}})
        self.assert_ignored({"update_id": 3, "edited_message": message(REQUEST)["message"]})
        self.assertEqual(self.bot.ignored, 7)

    def test_button_from_other_user_or_chat_is_ignored(self):
        self.bot.handle_update(message(REQUEST))
        key = self.last_keys()[0]
        self.api.calls.clear()
        for update in (press(key, user=999), press(key, chat=999), press(key, chat=-5, chat_type="group")):
            self.bot.handle_update(update)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(len(self.bot.pending), 1)  # the owner can still use it

    def test_booleans_and_strings_are_not_the_owner_id(self):
        owner_bot = Bot(self.api, 1, self.state, self.inbox, self.outbox, clock=self.clock, sleep=self.sleeps.append)
        owner_bot.handle_update(message(REQUEST, user=True, chat=True))
        owner_bot.handle_update(message(REQUEST, user="1", chat="1"))
        self.assertEqual(self.api.calls, [])

    def test_ignored_count_is_logged_without_content(self):
        self.api.hook = lambda method, params: [message(REQUEST + " SECRET-TEXT", user=999)] \
            if method == "getUpdates" else None
        with self.assertLogs("agentic.telegram", level="INFO") as logs:
            self.bot.poll_once()
        joined = "\n".join(logs.output)
        self.assertIn("ignored 1 update", joined)
        self.assertNotIn("SECRET-TEXT", joined)


class ConfirmFlowTests(BotTestCase):
    def start_request(self, text=REQUEST, message_id=7):
        self.bot.handle_update(message(text, message_id=message_id))
        return self.last_keys()

    def test_preview_has_first_300_characters_and_two_buttons(self):
        text = "가" * 400
        ok, no = self.start_request(text)
        sent = self.api.of("sendMessage")[-1]
        self.assertIn("가" * 300, sent["text"])
        self.assertNotIn("가" * 301, sent["text"])
        buttons = sent["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in buttons], ["시작", "취소"])
        self.assertNotIn("parse_mode", sent)
        self.assertEqual(self.inbox_items(), [])
        (key,) = self.bot.pending
        self.assertEqual(len(key), 16)
        self.assertEqual(ok, f"ok:{key}")
        self.assertLessEqual(len(ok.encode()), 64)

    def test_start_creates_exactly_one_new_item_with_the_full_text(self):
        text = "가" * 400
        ok, _ = self.start_request(text, message_id=33)
        self.bot.handle_update(press(ok, message_id=501))
        self.assertEqual(self.inbox_items(), [{"type": "new", "text": text, "reply_to": 33}])
        self.assertEqual(len(self.api.of("answerCallbackQuery")), 1)
        edit = self.api.of("editMessageText")[-1]
        self.assertEqual((edit["text"], edit["message_id"], edit["reply_markup"]), ("접수했습니다", 501, {"inline_keyboard": []}))
        self.assertEqual(self.bot.pending, {})

    def test_second_press_creates_nothing(self):
        ok, no = self.start_request()
        self.bot.handle_update(press(ok))
        self.api.calls.clear()
        self.bot.handle_update(press(ok))
        self.bot.handle_update(press(no))
        self.assertEqual(len(self.inbox_items()), 1)
        self.assertEqual(self.api.texts(), [STALE, STALE])
        self.assertEqual(len(self.api.of("answerCallbackQuery")), 2)

    def test_press_after_expiry_creates_nothing(self):
        ok, _ = self.start_request()
        self.clock.now += 3600.0 + 1
        self.bot.handle_update(press(ok))
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(self.api.texts()[-1], STALE)
        self.assertEqual(self.bot.pending, {})

    def test_press_just_before_expiry_works(self):
        ok, _ = self.start_request()
        self.clock.now += 3600.0 - 1
        self.bot.handle_update(press(ok))
        self.assertEqual(len(self.inbox_items()), 1)

    def test_forged_keys_create_nothing(self):
        self.start_request()
        for data in ("ok:AAAAAAAAAAAAAAAA", "ok:short", "ok:", "ok", "", "no:" + "x" * 16, "zz:abc", "ok:../../etc/pass",
                     "ok:" + "A" * 200, None, 12):
            self.api.calls.clear()
            update = press(data)
            self.bot.handle_update(update)
            self.assertEqual(self.api.texts(), [STALE], data)
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(len(self.bot.pending), 1)

    def test_cancel_drops_the_request(self):
        ok, no = self.start_request()
        self.bot.handle_update(press(no, message_id=502))
        self.assertEqual(self.bot.pending, {})
        edit = self.api.of("editMessageText")[-1]
        self.assertEqual((edit["text"], edit["message_id"]), ("취소했습니다", 502))
        self.bot.handle_update(press(ok))
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(self.api.texts()[-1], STALE)

    def test_stop_needs_its_button(self):
        self.bot.handle_update(message("/stop demo", message_id=21))
        self.assertEqual(self.inbox_items(), [])
        sent = self.api.of("sendMessage")[-1]
        self.assertIn("demo", sent["text"])
        self.assertEqual([b["text"] for b in sent["reply_markup"]["inline_keyboard"][0]], ["멈추기", "취소"])
        ok, _ = self.last_keys()
        self.bot.handle_update(press(ok))
        self.bot.handle_update(press(ok))
        self.assertEqual(self.inbox_items(), [{"type": "stop", "id": "demo", "reply_to": 21}])

    def test_stop_cancel_creates_nothing(self):
        self.bot.handle_update(message("/stop demo"))
        _, no = self.last_keys()
        self.bot.handle_update(press(no))
        self.assertEqual(self.inbox_items(), [])

    def test_text_of_wrong_size_gets_an_explanation_and_no_pending(self):
        for text in ("짧음", "x" * 9, "가" * 6667):  # 6 bytes, 9 bytes, 20,001 bytes
            self.api.calls.clear()
            self.bot.handle_update(message(text))
            self.assertEqual(len(self.api.of("sendMessage")), 1)
            self.assertNotIn("reply_markup", self.api.of("sendMessage")[0])
        self.assertEqual(self.bot.pending, {})
        self.assertEqual(self.inbox_items(), [])

    def test_size_limits_are_in_bytes(self):
        self.bot.handle_update(message("x" * 10))
        self.bot.handle_update(message("x" * 20_000))
        self.assertEqual(len(self.bot.pending), 2)

    def test_request_whose_json_would_not_fit_the_inbox_is_refused(self):
        self.bot.handle_update(message('"' * 19_000))  # every quote becomes two bytes in JSON
        self.assertEqual(self.bot.pending, {})
        self.assertNotIn("reply_markup", self.api.of("sendMessage")[-1])

    def test_pending_survives_a_restart_and_expired_ones_are_pruned(self):
        ok, _ = self.start_request()
        restarted = self.new_bot()
        restarted.handle_update(press(ok))
        self.assertEqual(len(self.inbox_items()), 1)
        self.start_request("두 번째 요청입니다. 충분히 길게 씁니다.")
        self.clock.now += 4000
        self.new_bot().handle_update(message("/help"))
        self.assertEqual(json.loads((self.state / "pending.json").read_text()), {})

    def test_failed_inbox_write_keeps_the_pending_item(self):
        ok, _ = self.start_request()
        inbox = self.inbox
        inbox.rename(self.tmp / "moved")
        with self.assertLogs("agentic.telegram", level="ERROR"):
            self.bot.handle_update(press(ok))
        self.assertEqual(len(self.bot.pending), 1)
        (self.tmp / "moved").rename(inbox)
        self.bot.handle_update(press(ok))
        self.assertEqual(len(self.inbox_items()), 1)


class FollowUpTests(BotTestCase):
    TEXT = "결과에 단위 테스트를 더 붙여 주세요."

    def ask(self, text, message_id=7):
        self.bot.handle_update(message(text, message_id=message_id))

    def assert_asked(self, mission_id, text, message_id):
        """The last message is the follow-up preview; pressing 시작 writes exactly one continue item."""
        sent = self.api.of("sendMessage")[-1]
        self.assertTrue(sent["text"].endswith(f"\n\n이 내용으로 미션 {mission_id}을(를) 이어서 맡길까요?"), sent["text"])
        self.assertNotIn("parse_mode", sent)
        self.assertEqual([b["text"] for b in sent["reply_markup"]["inline_keyboard"][0]], ["시작", "취소"])
        ((key, entry),) = self.bot.pending.items()
        self.assertEqual({k: v for k, v in entry.items() if k != "expires"},
                         {"kind": "continue", "id": mission_id, "text": text, "reply_to": message_id})
        self.assertEqual(self.inbox_items(), [])
        ok, _ = self.last_keys()
        self.bot.handle_update(press(ok, message_id=501))
        self.bot.handle_update(press(ok, message_id=501))
        self.assertEqual(self.inbox_items(),
                         [{"type": "continue", "id": mission_id, "text": text, "reply_to": message_id}])
        edit = self.api.of("editMessageText")[-1]
        self.assertEqual((edit["text"], edit["message_id"], edit["reply_markup"]),
                         ("접수했습니다", 501, {"inline_keyboard": []}))
        self.assertEqual(self.api.texts()[-1], STALE)
        self.assertEqual(self.bot.pending, {})

    def test_followup_pattern_goes_through_the_confirm_buttons(self):
        self.ask(f"wordfreq 이어서: {self.TEXT}", message_id=31)
        sent = self.api.of("sendMessage")[-1]
        self.assertTrue(sent["text"].startswith(self.TEXT + "\n\n"))
        self.assert_asked("wordfreq", self.TEXT, 31)

    def test_followup_pattern_variants(self):
        body = f"{self.TEXT}\n둘째 줄\n\n셋째 줄"
        for text, mission_id, expected in ((f"  demo-2이어서：{self.TEXT}  ", "demo-2", self.TEXT),
                                           (f"a1  이어서  :\n  {self.TEXT}", "a1", self.TEXT),
                                           (f"demo 이어서: {body}", "demo", body)):
            with self.subTest(text=text):
                self.setUp()
                self.ask(text)
                self.assert_asked(mission_id, expected, 7)

    def test_followup_preview_is_cut_at_300_characters(self):
        text = "가" * 400
        self.ask(f"demo 이어서: {text}")
        sent = self.api.of("sendMessage")[-1]
        self.assertTrue(sent["text"].startswith("가" * 300 + "…\n\n이 내용으로 미션 demo"))
        self.assertNotIn("가" * 301, sent["text"])
        self.assert_asked("demo", text, 7)

    def test_text_that_only_looks_similar_is_a_new_request(self):
        for text in (f"Demo 이어서: {self.TEXT}", f"여기서 demo 이어서: {self.TEXT}", f"demo 이어서 {self.TEXT}",
                     f"-demo 이어서: {self.TEXT}", f"{'a' * 42} 이어서: {self.TEXT}"):
            self.bot.pending.clear()
            self.ask(text)
            self.assertEqual([entry["kind"] for entry in self.bot.pending.values()], ["new"], text)

    def test_slash_continue_goes_through_the_confirm_buttons(self):
        self.ask(f"/continue demo {self.TEXT}", message_id=32)
        self.assert_asked("demo", self.TEXT, 32)

    def test_slash_continue_keeps_the_raw_multiline_text(self):
        body = "첫 줄입니다 첫 줄입니다\n  들여쓴 둘째 줄\n\n셋째 줄"
        self.ask(f"/CONTINUE@AgenticBot demo-2\n{body}  \n", message_id=33)
        self.assert_asked("demo-2", body, 33)

    def test_slash_continue_with_a_bad_id_or_without_text_gets_usage_and_no_pending(self):
        for text in ("/continue", "/continue demo", "/continue demo   ", "/continue Demo " + self.TEXT,
                     "/continue ../etc " + self.TEXT, "/continue " + "a" * 42 + " " + self.TEXT,
                     "/continue -x " + self.TEXT, "/continue@AgenticBot"):
            self.api.calls.clear()
            self.ask(text)
            self.assertEqual(self.api.texts(), [telegram.USAGE_CONTINUE], text)
            self.assertNotIn("reply_markup", self.api.of("sendMessage")[0])
        self.assertEqual(self.bot.pending, {})
        self.assertEqual(self.inbox_items(), [])

    def test_followup_text_size_limits_match_a_new_request(self):
        for text, reply in (("demo 이어서: 짧음", telegram.TOO_SHORT), ("/continue demo " + "x" * 9, telegram.TOO_SHORT),
                            ("demo 이어서: " + "가" * 6667, telegram.TOO_LONG),
                            ("/continue demo " + "가" * 6667, telegram.TOO_LONG),
                            ("demo 이어서: " + '"' * 19_000, telegram.TOO_LONG)):
            self.api.calls.clear()
            self.ask(text)
            self.assertEqual(self.api.texts(), [reply], text[:20])
        self.assertEqual(self.bot.pending, {})
        self.ask("demo 이어서: " + "x" * 10)
        self.ask("/continue demo " + "x" * 20_000)
        self.assertEqual(len(self.bot.pending), 2)

    def test_cancel_expiry_and_forged_keys_create_nothing(self):
        self.ask(f"demo 이어서: {self.TEXT}")
        ok, no = self.last_keys()
        self.bot.handle_update(press("ok:AAAAAAAAAAAAAAAA"))
        self.bot.handle_update(press(no, message_id=502))
        self.assertEqual(self.api.of("editMessageText")[-1]["text"], "취소했습니다")
        self.bot.handle_update(press(ok))
        self.assertEqual(self.api.texts()[-1], STALE)
        self.ask(f"/continue demo {self.TEXT}")
        ok, _ = self.last_keys()
        self.clock.now += 3600.0 + 1
        self.bot.handle_update(press(ok))
        self.assertEqual(self.api.texts()[-1], STALE)
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(self.bot.pending, {})

    def test_pending_followup_survives_a_restart_and_a_failed_write_keeps_it(self):
        self.ask(f"demo 이어서: {self.TEXT}", message_id=34)
        ok, _ = self.last_keys()
        restarted = self.new_bot()
        self.assertEqual([entry["kind"] for entry in restarted.pending.values()], ["continue"])
        self.inbox.rename(self.tmp / "moved")
        with self.assertLogs("agentic.telegram", level="ERROR"):
            restarted.handle_update(press(ok))
        self.assertEqual(len(restarted.pending), 1)
        (self.tmp / "moved").rename(self.inbox)
        restarted.handle_update(press(ok))
        self.assertEqual(self.inbox_items(),
                         [{"type": "continue", "id": "demo", "text": self.TEXT, "reply_to": 34}])

    def test_a_forged_pending_file_entry_without_a_valid_id_is_dropped(self):
        bad = {"kind": "continue", "id": "Bad Id", "text": "x" * 20, "reply_to": 1, "expires": self.clock.now + 100}
        (self.state / "pending.json").write_text(json.dumps({"A" * 16: bad, "B" * 16: {**bad, "id": "demo"}}))
        self.assertEqual(list(self.new_bot().pending), ["B" * 16])

    def test_usage_mentions_files_and_continue(self):
        self.assertIn("/files <id>", telegram.USAGE)
        self.assertIn("/continue <id> <내용>", telegram.USAGE)
        self.assertIn("<id> 이어서: <내용>", telegram.USAGE)


class CommandTests(BotTestCase):
    def test_list_status_report_make_inbox_items(self):
        self.bot.handle_update(message("/list", message_id=11))
        self.bot.handle_update(message("/status demo", message_id=12))
        self.bot.handle_update(message("/report demo-2", message_id=13))
        self.bot.handle_update(message("/status@AgenticBot demo", message_id=14))
        self.bot.handle_update(message("/LIST@AgenticBot", message_id=15))
        self.assertEqual(self.inbox_items(), [
            {"type": "list", "reply_to": 11},
            {"type": "status", "id": "demo", "reply_to": 12},
            {"type": "report", "id": "demo-2", "reply_to": 13},
            {"type": "status", "id": "demo", "reply_to": 14},
            {"type": "list", "reply_to": 15},
        ])
        self.assertEqual(self.api.calls, [])  # the answers come from root through the outbox

    def test_files_command_makes_a_files_item(self):
        self.bot.handle_update(message("/files demo", message_id=16))
        self.bot.handle_update(message("/Files@AgenticBot demo-2", message_id=17))
        self.assertEqual(self.inbox_items(), [
            {"type": "files", "id": "demo", "reply_to": 16},
            {"type": "files", "id": "demo-2", "reply_to": 17},
        ])
        self.assertEqual(self.api.calls, [])

    def test_bad_or_missing_id_gets_usage_and_no_item(self):
        for text in ("/status", "/report", "/stop", "/status Demo", "/report ../etc", "/status a b",
                     "/status -x", "/report " + "a" * 42, "/stop a;b", "/files", "/files Demo", "/files a b"):
            self.api.calls.clear()
            self.bot.handle_update(message(text))
            self.assertEqual(len(self.api.texts()), 1, text)
            self.assertIn("/", self.api.texts()[0])
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(self.bot.pending, {})

    def test_help_start_and_unknown_commands_get_usage_without_an_item(self):
        for text in ("/help", "/start", "/start@AgenticBot", "/frobnicate", "/list extra", "/"):
            self.api.calls.clear()
            self.bot.handle_update(message(text))
            self.assertEqual(self.api.texts(), [telegram.USAGE], text)
        self.assertEqual(self.inbox_items(), [])

    def test_inbox_write_failure_is_reported(self):
        self.inbox.rmdir()
        with self.assertLogs("agentic.telegram", level="ERROR"):
            self.bot.handle_update(message("/list"))
        self.assertEqual(self.api.texts(), [telegram.UNAVAILABLE])

    def test_non_text_message_gets_a_short_reply(self):
        update = message("x")
        del update["message"]["text"]
        self.bot.handle_update(update)
        self.assertEqual(self.api.texts(), [telegram.NOT_TEXT])


class PollingTests(BotTestCase):
    def queue(self, *batches):
        batches = list(batches)
        self.api.hook = lambda method, params: (batches.pop(0) if batches else []) if method == "getUpdates" \
            else None

    def test_offset_is_persisted_after_each_update_and_used_for_the_next_call(self):
        seen = []

        def hook(method, params):
            if method == "sendMessage":
                seen.append(json.loads((self.state / "offset.json").read_text()))
            if method == "getUpdates":
                return [{**message("/help"), "update_id": 10}, {**message("/help"), "update_id": 11}]

        self.api.hook = hook
        self.assertEqual(self.bot.poll_once(), 2)
        self.assertEqual(seen, [{"offset": 11}])  # the first update was saved when the second was answered
        self.assertEqual(json.loads((self.state / "offset.json").read_text()), {"offset": 12})
        self.assertEqual(oct((self.state / "offset.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.api.of("getUpdates")[0], {"timeout": 50, "allowed_updates": ["message", "callback_query"]})
        self.api.hook = None
        self.bot.poll_once()
        self.assertEqual(self.api.of("getUpdates")[1]["offset"], 12)
        self.assertEqual(self.new_bot().offset, 12)

    def test_offset_is_saved_between_updates(self):
        states = []

        def hook(method, params):
            if method == "getUpdates":
                return [{**message("/help"), "update_id": 10}, {**message("/help"), "update_id": 11}]
            if method == "sendMessage":
                path = self.state / "offset.json"
                states.append(json.loads(path.read_text())["offset"] if path.exists() else None)

        self.api.hook = hook
        self.bot.poll_once()
        self.assertEqual(states, [None, 11])  # the first update was saved before the second was handled

    def test_bad_updates_do_not_crash_and_are_skipped(self):
        bad = [None, 5, "x", [], {}, {"update_id": "a"}, {"update_id": True},
               {"update_id": 20, "message": 5},
               {"update_id": 21, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}}},
               {"update_id": 22, "message": {"message_id": "7", "from": {"id": OWNER}, "text": "/list",
                                             "chat": {"id": OWNER, "type": "private"}}},
               {"update_id": 23, "message": {"message_id": 7, "from": {"id": OWNER}, "text": ["/list"],
                                             "chat": {"id": OWNER, "type": "private"}}},
               {"update_id": 24, "message": {"message_id": 7, "from": 5, "chat": []}},
               {"update_id": 25, "callback_query": 5},
               {"update_id": 26, "callback_query": {"from": {"id": OWNER}, "message": {"chat": {"id": OWNER, "type": "private"}}}},
               {"update_id": 27, "callback_query": {"id": 5, "from": {"id": OWNER}, "data": {"x": 1},
                                                    "message": {"message_id": 1, "chat": {"id": OWNER, "type": "private"}}}},
               {"update_id": 28, "my_chat_member": {}}]
        self.queue(bad)
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.poll_once(), len(bad))
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(json.loads((self.state / "offset.json").read_text()), {"offset": 29})
        self.queue([message("/list")])  # still alive afterwards
        self.bot.poll_once()
        self.assertEqual(len(self.inbox_items()), 1)

    def test_non_list_answer_does_not_crash(self):
        self.api.hook = lambda method, params: {"x": 1} if method == "getUpdates" else None
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.poll_once(), 0)

    def test_telegram_failure_while_answering_does_not_stop_the_poll(self):
        def hook(method, params):
            if method == "getUpdates":
                return [{**message("/help"), "update_id": 5}, {**message("/list"), "update_id": 6}]
            if method == "sendMessage":
                raise NetworkError("down")

        self.api.hook = hook
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.poll_once(), 2)
        self.assertEqual(len(self.inbox_items()), 1)
        self.assertEqual(self.bot.offset, 7)

    def test_backoff_5_10_20_60_stays_60_and_resets_after_success(self):
        outcomes = [NetworkError("x")] * 5 + [None] + [ApiError(502, "bad gateway")] + [ApiError(429, "slow", 90)]

        def hook(method, params):
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome

        self.api.hook = hook
        with self.assertLogs("agentic.telegram", level="WARNING"):
            for _ in range(8):
                self.bot.poll_once()
        self.assertEqual(self.sleeps, [5, 10, 20, 60, 60, 5, 90])

    def test_getupdates_fails_with_4xx_also_backs_off(self):
        self.api.hook = lambda method, params: (_ for _ in ()).throw(ApiError(409, "conflict"))
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.bot.poll_once()
        self.assertEqual(self.sleeps, [5])


def html_item(*parts, **extra):
    return {"html": list(parts), **extra}


class OutboxTests(BotTestCase):
    def test_html_item_is_sent_with_parse_mode_reply_to_and_deleted(self):
        self.put_outbox(html_item("<b>안녕하세요</b> &amp; <code>x</code>", reply_to=77))
        self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(self.api.of("sendMessage"), [{
            "chat_id": OWNER, "text": "<b>안녕하세요</b> &amp; <code>x</code>", "parse_mode": "HTML",
            "reply_to_message_id": 77, "allow_sending_without_reply": True}])
        self.assertEqual(self.outbox_names(), [])

    def test_parts_are_sent_in_order_and_only_the_first_replies(self):
        parts = [f"<b>{i}</b>" + "x" * 4000 for i in range(3)] + ["z" * 4096]
        self.put_outbox(html_item(*parts, reply_to=5))
        self.assertEqual(self.bot.flush_outbox(), 1)
        sent = self.api.of("sendMessage")
        self.assertEqual([p["text"] for p in sent], parts)
        self.assertTrue(all(p["parse_mode"] == "HTML" and p["chat_id"] == OWNER for p in sent))
        self.assertEqual([("reply_to_message_id" in p, "allow_sending_without_reply" in p) for p in sent],
                         [(True, True), (False, False), (False, False), (False, False)])
        self.assertEqual(sent[0]["reply_to_message_id"], 5)

    def test_item_without_reply_to_and_order_is_oldest_first(self):
        for text, age in (("둘째", 2), ("첫째", 1)):
            name = self.put_outbox(html_item(text))
            os.utime(self.outbox / name, (self.clock.now - age * 10, self.clock.now - age * 10))
        self.bot.flush_outbox()
        self.assertEqual(self.api.texts(), ["둘째", "첫째"])  # larger age = older
        self.assertNotIn("reply_to_message_id", self.api.of("sendMessage")[0])

    def test_4xx_deletes_the_file(self):
        self.api.hook = lambda method, params: (_ for _ in ()).throw(ApiError(400, "Bad Request: nope"))
        self.put_outbox(html_item("a"))
        self.put_outbox(html_item("b"))
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.flush_outbox(), 2)
        self.assertEqual(self.outbox_names(), [])

    def test_5xx_and_network_errors_keep_the_file_and_retry_later(self):
        failures = [ApiError(502, "bad gateway"), NetworkError("down"), ApiError(429, "slow", 3)]

        def hook(method, params):
            if failures:
                raise failures.pop(0)

        self.api.hook = hook
        self.put_outbox(html_item("important"))
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.flush_outbox(), 0)
            self.assertEqual(self.bot.flush_outbox(), 0)
            self.assertEqual(self.bot.flush_outbox(), 0)  # 429: also pauses for retry_after
            self.assertEqual(len(self.outbox_names()), 1)
            self.assertEqual(self.bot.flush_outbox(), 0)
            self.assertEqual(len(self.api.calls), 3)  # paused: no call
            self.clock.now += 3
            self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(self.outbox_names(), [])
        self.assertEqual(self.api.texts()[-1], "important")

    def test_a_retry_does_not_resend_parts_that_were_sent(self):
        lines = [f"{i}:" + "x" * 2500 for i in range(5)]
        self.put_outbox(html_item(*lines, reply_to=5))
        sent_count = {"n": 0}

        def hook(method, params):
            sent_count["n"] += 1
            if sent_count["n"] == 3:
                raise ApiError(503, "unavailable")
            if sent_count["n"] == 4:
                raise NetworkError("down")

        self.api.hook = hook
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.flush_outbox(), 0)  # parts 0, 1 sent; 2 failed
            self.assertEqual(self.bot.flush_outbox(), 0)  # part 2 failed again (network)
        self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(self.outbox_names(), [])
        sent = self.api.of("sendMessage")
        self.assertEqual([p["text"] for p in sent if p["text"]].count(lines[0]), 1)
        self.assertEqual([p["text"] for p in sent if "reply_to_message_id" in p], [lines[0]])
        self.assertEqual(sorted(set(p["text"] for p in sent)), sorted(lines))
        self.assertEqual(len(sent), 5 + 2)  # 5 parts, plus the two failed attempts at part 2

    def test_restart_does_not_matter_for_unsent_files_and_order_is_kept_on_failure(self):
        self.api.hook = lambda method, params: (_ for _ in ()).throw(NetworkError("down"))
        self.put_outbox(html_item("one"))
        self.put_outbox(html_item("two"))
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.bot.flush_outbox()
        self.assertEqual(len(self.api.calls), 1)  # stopped at the first failure
        self.assertEqual(len(self.outbox_names()), 2)

    def test_symlink_in_the_outbox_is_not_followed(self):
        target = self.tmp / "secret.json"
        target.write_text(json.dumps(html_item("TOP SECRET")))
        os.symlink(target, self.outbox / "planted")
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.bot.flush_outbox()
        self.assertEqual(self.api.calls, [])
        self.assertTrue(target.exists())
        self.assertEqual(self.outbox_names(), [])  # the link itself was removed

    def test_symlinked_outbox_directory_is_refused(self):
        real = self.tmp / "real"
        real.mkdir()
        spool.write_item(real, html_item("x"))
        self.outbox.rmdir()
        os.symlink(real, self.outbox)
        with self.assertLogs("agentic.telegram", level="ERROR"):
            self.assertEqual(self.bot.flush_outbox(), 0)
        self.assertEqual(self.api.calls, [])

    def test_invalid_items_are_deleted_without_sending(self):
        (self.outbox / "bad-json").write_text("{nope")
        (self.outbox / "a-list").write_text("[]")
        (self.outbox / "no-html").write_text('{"reply_to": 1}')
        (self.outbox / "old-text-shape").write_text('{"text": "x"}')
        (self.outbox / "extra-key").write_text('{"html": ["x"], "parse_mode": "HTML"}')
        (self.outbox / "html-not-list").write_text('{"html": "x"}')
        (self.outbox / "html-empty-list").write_text('{"html": []}')
        (self.outbox / "html-empty-part").write_text('{"html": ["x", ""]}')
        (self.outbox / "html-bad-part").write_text('{"html": ["x", 5]}')
        (self.outbox / "html-too-long").write_text('{"html": ["' + "x" * 4097 + '"]}')
        (self.outbox / "bad-reply").write_text('{"html": ["x"], "reply_to": true}')
        (self.outbox / "neg-reply").write_text('{"html": ["x"], "reply_to": -1}')
        (self.outbox / "huge").write_text('{"html": ["' + "x" * (1 << 20) + '"]}')
        (self.outbox / "dup").write_text('{"html": ["x"], "html": ["y"]}')
        (self.outbox / "both").write_text('{"html": ["x"], "document": "a.doc", "filename": "a.md"}')
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.flush_outbox(), 15)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.outbox_names(), [])

    def test_a_part_of_exactly_4096_characters_is_valid(self):
        self.put_outbox(html_item("x" * 4096))
        self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(len(self.api.of("sendMessage")), 1)

    def test_tmp_and_dot_files_are_left_alone(self):
        (self.outbox / "half.tmp").write_text('{"html": ["x"]}')
        (self.outbox / ".hidden").write_text('{"html": ["x"]}')
        self.assertEqual(self.bot.flush_outbox(), 0)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(len(self.outbox_names()), 2)

    def test_missing_outbox_directory_is_not_an_error(self):
        self.outbox.rmdir()
        self.assertEqual(self.bot.flush_outbox(), 0)

    def test_outbox_thread_loop_stops_and_survives_errors(self):
        stop = threading.Event()
        calls = []

        def broken():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            stop.set()
            return 0

        self.bot.flush_outbox = broken
        with self.assertLogs("agentic.telegram", level="ERROR"):
            with mock.patch.object(telegram, "OUTBOX_INTERVAL", 0.001):
                self.bot.run_outbox(stop)
        self.assertEqual(len(calls), 2)


class DocumentOutboxTests(BotTestCase):
    def put_document(self, data=b"# report\n\xed\x95\x9c\xea\xb8\x80", filename="report-demo.md", **extra):
        doc = spool.write_document(self.outbox, data)
        item = self.put_outbox({"document": doc, "filename": filename, **extra})
        return doc, item

    def test_document_is_sent_through_send_document_and_both_files_are_deleted(self):
        data = bytes(range(256)) * 3
        self.put_document(data, "results-demo.zip", caption="📦 <b>demo</b> 결과물", reply_to=42)
        self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(self.api.of("sendMessage"), [])
        self.assertEqual(self.api.of("sendDocument"), [{
            "chat_id": OWNER, "caption": "📦 <b>demo</b> 결과물", "parse_mode": "HTML",
            "reply_to_message_id": 42, "allow_sending_without_reply": True,
            "filename": "results-demo.zip", "content": data.decode("latin-1")}])
        self.assertEqual(self.outbox_names(), [])

    def test_document_without_caption_and_reply_has_only_the_chat_id(self):
        self.put_document(b"abc")
        self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(self.api.of("sendDocument"), [{"chat_id": OWNER, "filename": "report-demo.md",
                                                        "content": "abc"}])
        self.assertEqual(self.outbox_names(), [])

    def test_documents_keep_their_order_with_messages(self):
        names = [self.put_outbox(html_item("first"))]
        names.append(self.put_document(b"second")[1])
        names.append(self.put_outbox(html_item("third")))
        for index, name in enumerate(names):
            os.utime(self.outbox / name, (self.clock.now + index, self.clock.now + index))
        self.assertEqual(self.bot.flush_outbox(), 3)
        self.assertEqual([name for name, _ in self.api.calls], ["sendMessage", "sendDocument", "sendMessage"])

    def test_4xx_deletes_item_and_document(self):
        self.api.hook = lambda method, params: (_ for _ in ()).throw(ApiError(400, "Bad Request: nope"))
        self.put_document()
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(self.outbox_names(), [])

    def test_retryable_failures_keep_both_files_and_stop_the_pass(self):
        failures = [ApiError(502, "bad gateway"), NetworkError("down"), ApiError(429, "slow", 3)]

        def hook(method, params):
            if failures:
                raise failures.pop(0)

        self.api.hook = hook
        doc, item = self.put_document(b"keep me")
        os.utime(self.outbox / item, (self.clock.now - 10, self.clock.now - 10))
        self.put_outbox(html_item("behind it"))
        with self.assertLogs("agentic.telegram", level="WARNING"):
            for _ in range(3):
                self.assertEqual(self.bot.flush_outbox(), 0)
                self.assertEqual(len(self.outbox_names()), 3)
            self.assertEqual(len(self.api.calls), 3)  # the message behind it was never tried
            self.assertEqual(self.bot.flush_outbox(), 0)  # 429 pause
            self.assertEqual(len(self.api.calls), 3)
            self.clock.now += 3
            self.assertEqual(self.bot.flush_outbox(), 2)
        self.assertEqual(self.outbox_names(), [])
        self.assertEqual(len(self.api.of("sendDocument")), 4)

    def test_symlink_as_the_document_is_never_followed(self):
        secret = self.tmp / "secret.txt"
        secret.write_bytes(b"TOP SECRET")
        os.symlink(secret, self.outbox / "planted.doc")
        self.put_outbox({"document": "planted.doc", "filename": "innocent.txt"})
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(secret.read_bytes(), b"TOP SECRET")
        self.assertEqual(self.outbox_names(), [])  # the link itself is gone, its target is not

    def test_missing_or_special_document_drops_the_item(self):
        os.mkfifo(self.outbox / "pipe.doc")
        self.put_outbox({"document": "pipe.doc", "filename": "a.txt"})
        self.put_outbox({"document": "missing.doc", "filename": "a.txt"})
        (self.outbox / "dir.doc").mkdir()
        self.put_outbox({"document": "dir.doc", "filename": "a.txt"})
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.flush_outbox(), 3)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.outbox_names(), [])

    def test_document_over_the_limit_is_not_sent(self):
        self.put_document(b"x" * 11)
        with mock.patch.object(telegram, "DOCUMENT_MAX_BYTES", 10):
            with self.assertLogs("agentic.telegram", level="WARNING"):
                self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.outbox_names(), [])

    def test_document_of_exactly_the_limit_is_sent(self):
        self.put_document(b"x" * 10)
        with mock.patch.object(telegram, "DOCUMENT_MAX_BYTES", 10):
            self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(len(self.api.of("sendDocument")), 1)

    def test_invalid_document_items_are_deleted_with_their_valid_document_names(self):
        bad = [
            {"filename": "a.txt"},  # no document key: not an html item either
            {"document": "a.doc"},  # no filename
            {"document": "a.doc", "filename": "a.txt", "extra": 1},
            {"document": "a.doc", "filename": "../a.txt"},
            {"document": "a.doc", "filename": ".hidden"},
            {"document": "a.doc", "filename": "a" * 129},
            {"document": "a.doc", "filename": "a b.txt"},
            {"document": "a.doc", "filename": "a.txt", "caption": 5},
            {"document": "a.doc", "filename": "a.txt", "caption": "c" * 1025},
            {"document": "a.doc", "filename": "a.txt", "reply_to": 0},
            {"document": "a.doc", "filename": "a.txt", "html": ["x"]},
            {"document": "../a.doc", "filename": "a.txt"},
            {"document": "a.txt", "filename": "a.txt"},
            {"document": ".doc", "filename": "a.txt"},
            {"document": 5, "filename": "a.txt"},
        ]
        for number, item in enumerate(bad):
            (self.outbox / f"item{number:02d}").write_text(json.dumps(item))
        (self.outbox / "a.doc").write_bytes(b"orphan")
        with self.assertLogs("agentic.telegram", level="WARNING"):
            self.assertEqual(self.bot.flush_outbox(), len(bad))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.outbox_names(), [])  # the .doc file was dropped together with its invalid item

    def test_caption_of_exactly_1024_characters_is_valid_and_filename_of_128(self):
        self.put_document(b"x", filename="a" * 128, caption="c" * 1024)
        self.assertEqual(self.bot.flush_outbox(), 1)
        self.assertEqual(len(self.api.of("sendDocument")), 1)


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        content_type = self.headers.get("Content-Type")
        self.server.seen.append((self.path, content_type, json.loads(body) if content_type == "application/json"
                                 else body))
        route = self.path.rsplit("/", 1)[-1]
        status, payload = {
            "ok": (200, b'{"ok": true, "result": [{"update_id": 1}]}'),
            "sendDocument": (200, b'{"ok": true, "result": {"message_id": 9}}'),
            "bad": (400, '{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}'.encode()),
            "slow": (429, b'{"ok": false, "error_code": 429, "description": "Too Many Requests",'
                          b' "parameters": {"retry_after": 17}}'),
            "boom": (502, b"<html>Bad Gateway</html>"),
            "html": (200, b"<html>hello</html>"),
            "notok": (200, b'{"ok": false, "error_code": 403, "description": "Forbidden"}'),
            "list": (200, b"[1]"),
            "plain404": (404, b"not json"),
        }[route]
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RealClientTests(TimedTestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.seen = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.api = TelegramApi(self.base, "123:SECRET-TOKEN", timeout=5)

    def test_success_returns_the_result_and_posts_json_to_the_token_path(self):
        self.assertEqual(self.api.call("ok", {"a": "한글", "b": [1]}), [{"update_id": 1}])
        ((path, content_type, body),) = self.server.seen
        self.assertEqual(path, "/bot123:SECRET-TOKEN/ok")
        self.assertEqual(content_type, "application/json")
        self.assertEqual(body, {"a": "한글", "b": [1]})

    def test_send_document_posts_multipart_form_data_the_standard_library_parses(self):
        content = bytes(range(256)) * 20 + b"\r\n--boundary-looking text\r\n"
        params = {"chat_id": OWNER, "caption": "<b>제목</b>\n둘째 줄", "parse_mode": "HTML",
                  "reply_to_message_id": 7, "allow_sending_without_reply": True}
        self.assertEqual(self.api.send_document(params, "report-demo.md", content), {"message_id": 9})
        ((path, content_type, body),) = self.server.seen
        self.assertEqual(path, "/bot123:SECRET-TOKEN/sendDocument")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        parsed = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
        self.assertTrue(parsed.is_multipart())
        fields = {}
        for part in parsed.iter_parts():
            name = part.get_param("name", header="content-disposition")
            fields[name] = (part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
        self.assertEqual(list(fields), ["chat_id", "caption", "parse_mode", "reply_to_message_id",
                                        "allow_sending_without_reply", "document"])
        self.assertEqual(fields["chat_id"][2], str(OWNER).encode())
        self.assertEqual(fields["caption"][2].decode("utf-8"), "<b>제목</b>\n둘째 줄")
        self.assertEqual(fields["parse_mode"][2], b"HTML")
        self.assertEqual(fields["reply_to_message_id"][2], b"7")
        self.assertEqual(fields["allow_sending_without_reply"][2], b"true")
        self.assertEqual(fields["document"], ("report-demo.md", "application/octet-stream", content))
        self.assertIsNone(fields["caption"][0])

    def test_multipart_boundary_is_chosen_not_to_occur_in_the_payload(self):
        clash = "0" * 32
        content = b"before --agbot" + clash.encode() + b" after"
        with mock.patch.object(telegram.secrets, "token_hex", side_effect=[clash, "1" * 32]):
            boundary, body = telegram._multipart({"chat_id": 1}, "a.md", content)
        self.assertEqual(boundary, "agbot" + "1" * 32)
        self.assertEqual(body.count(b"--" + boundary.encode()), 3)  # two parts and the closing line

    def test_multipart_refuses_a_file_name_that_could_break_the_header(self):
        for filename in ('a".md', "a\r\nX: y.md", "a\0b"):
            with self.assertRaises(ValueError):
                telegram._multipart({"chat_id": 1}, filename, b"x")

    def test_4xx_is_an_api_error_with_status_and_description(self):
        with self.assertRaises(ApiError) as caught:
            self.api.call("bad", {})
        self.assertEqual((caught.exception.status, caught.exception.description, caught.exception.retry_after),
                         (400, "Bad Request: chat not found", None))
        self.assertFalse(telegram.retryable(caught.exception))

    def test_send_document_maps_errors_like_call_and_hides_the_token(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(NetworkError) as caught:
            TelegramApi(f"http://127.0.0.1:{port}", "123:SECRET-TOKEN", timeout=5).send_document(
                {"chat_id": 1}, "a.md", b"x")
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))
        self.assertTrue(telegram.retryable(caught.exception))

    def test_429_carries_retry_after_and_is_retryable(self):
        with self.assertRaises(ApiError) as caught:
            self.api.call("slow", {})
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, 17))
        self.assertTrue(telegram.retryable(caught.exception))

    def test_5xx_with_a_non_json_body_is_an_api_error_with_the_status(self):
        with self.assertRaises(ApiError) as caught:
            self.api.call("boom", {})
        self.assertEqual((caught.exception.status, caught.exception.description), (502, ""))
        self.assertTrue(telegram.retryable(caught.exception))
        with self.assertRaises(ApiError) as caught:
            self.api.call("plain404", {})
        self.assertEqual(caught.exception.status, 404)

    def test_ok_false_with_http_200_is_an_api_error(self):
        with self.assertRaises(ApiError) as caught:
            self.api.call("notok", {})
        self.assertEqual(caught.exception.status, 403)

    def test_bad_bodies_are_network_errors(self):
        for route in ("html", "list"):
            with self.assertRaises(NetworkError):
                self.api.call(route, {})

    def test_connection_refused_is_a_network_error_and_hides_the_token(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(NetworkError) as caught:
            TelegramApi(f"http://127.0.0.1:{port}", "123:SECRET-TOKEN", timeout=5).call("ok", {})
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))
        self.assertTrue(telegram.retryable(caught.exception))

    def test_timeout_is_a_network_error(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)  # accepts the connection in the kernel, never answers
        self.addCleanup(listener.close)
        api = TelegramApi(f"http://127.0.0.1:{listener.getsockname()[1]}", "123:T", timeout=0.3)
        with self.assertRaises(NetworkError):
            api.call("ok", {})


class ConfigTests(TimedTestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="agentic-bot-config-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        self.tmp = Path(tmp)

    def write(self, value):
        path = self.tmp / "telegram.json"
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def test_valid_file(self):
        self.assertEqual(telegram.load_config(self.write({"token": "123:AB_c-d", "owner_id": 5})), ("123:AB_c-d", 5))

    def test_invalid_files_are_refused(self):
        for value in ("{", "[]", {"token": "1:a"}, {"token": "1:a", "owner_id": 5, "x": 1},
                      {"token": "", "owner_id": 5}, {"token": 5, "owner_id": 5}, {"token": "1:a", "owner_id": True},
                      {"token": "1:a", "owner_id": "5"}, {"token": "1:a", "owner_id": 0},
                      {"token": "1:a/../b", "owner_id": 5}, {"token": "1:a\n", "owner_id": 5}):
            with self.assertRaises(telegram.ConfigError, msg=value):
                telegram.load_config(self.write(value))
        with self.assertRaises(telegram.ConfigError):
            telegram.load_config(self.tmp / "missing.json")

    def test_main_exits_2_on_bad_config_without_printing_the_token(self):
        path = self.write({"token": "999:LEAKY-TOKEN", "owner_id": "nope"})
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTIC_TELEGRAM_CONFIG": str(path)}), contextlib.redirect_stderr(err):
            self.assertEqual(telegram.main(), 2)
        self.assertIn("owner_id", err.getvalue())
        self.assertNotIn("LEAKY-TOKEN", err.getvalue())

    def test_main_exits_2_when_the_file_is_missing(self):
        err = io.StringIO()
        env = {"AGENTIC_TELEGRAM_CONFIG": str(self.tmp / "none.json")}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            self.assertEqual(telegram.main(), 2)


if __name__ == "__main__":
    unittest.main()
