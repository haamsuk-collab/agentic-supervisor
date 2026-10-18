"""The Telegram bot daemon (SPEC-D.md section 2): `python3.12 -m agentic.telegram`, runs as the bot account.

It only relays. A message from the owner becomes one JSON file in the inbox spool; the root inbox processor
does the work and writes the answer into the outbox spool; this daemon sends outbox files to the owner chat
(finished Telegram HTML messages, or a document file). Its own interactive messages are plain text. It never
touches mission state. Messages, tokens and URLs containing the token are never logged.
"""

import http.client
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from agentic import config, spool
from agentic.config import (DOCUMENT_MAX_BYTES, INBOX_ITEM_MAX_BYTES,
                            OUTBOX_ITEM_MAX_BYTES, valid_id)
from agentic.i18n import ko, t

DEFAULT_CONFIG = "/etc/agentic/telegram.json"
DEFAULT_BOT_STATE = "/var/lib/agbot"
DEFAULT_API = "https://api.telegram.org"

POLL_TIMEOUT = 50          # seconds Telegram holds a getUpdates call open
SOCKET_TIMEOUT = 70.0      # must exceed POLL_TIMEOUT
BACKOFF_SECONDS = (5, 10, 20, 60)
OUTBOX_INTERVAL = 2.0
HTML_LIMIT = 4096          # characters per outbox HTML message (Telegram's own limit; root stays below it)
CAPTION_LIMIT = 1024       # characters per document caption (Telegram's own limit)
PREVIEW_CHARS = 300
REQUEST_MIN_BYTES = 10
REQUEST_MAX_BYTES = 20_000
PENDING_TTL = 3600.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ALLOWED_UPDATES = ["message", "callback_query"]

OFFSET_FILE = "offset.json"
PENDING_FILE = "pending.json"

_TOKEN_RE = re.compile(r"[A-Za-z0-9:_-]+")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16}")
_FOLLOWUP_RE = re.compile(
    rf"([a-z0-9][a-z0-9-]{{0,40}})\s*(?:{ko.FOLLOWUP_WORD}|continue)\s*[:：]\s*(.+)", re.DOTALL
)  # same id rule as valid_id
_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

log = logging.getLogger("agentic.telegram")

_LEGACY_KEYS = {
    "USAGE": "telegram.usage",
    "TOO_SHORT": "telegram.too_short",
    "TOO_LONG": "telegram.too_long",
    "NOT_TEXT": "telegram.not_text",
    "USAGE_ID": "telegram.usage_id",
    "USAGE_CONTINUE": "telegram.usage_continue",
    "STALE": "telegram.stale",
    "ACCEPTED": "telegram.accepted",
    "CANCELLED": "telegram.cancelled",
    "STOP_ACCEPTED": "telegram.stop_accepted",
    "UNAVAILABLE": "telegram.unavailable",
    "RETRY_BUTTON": "telegram.retry_button",
}


def __getattr__(name: str):
    if name in _LEGACY_KEYS:
        return t(_LEGACY_KEYS[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

class ConfigError(ValueError):
    pass


class ApiError(Exception):
    """Telegram answered with an HTTP error status."""

    def __init__(self, status: int, description: str = "", retry_after: int | None = None):
        super().__init__(f"HTTP {status}: {description}" if description else f"HTTP {status}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


class NetworkError(Exception):
    """No usable answer: connection failure, timeout, or a body that is not a Telegram JSON answer."""


def retryable(exc: BaseException) -> bool:
    """A failure that says nothing about the message itself: try again later."""
    return isinstance(exc, NetworkError) or (isinstance(exc, ApiError) and (exc.status >= 500 or exc.status == 429))


class TelegramApi:
    """The real client: POST to <base>/bot<token>/<method>. The token never appears in an exception."""

    def __init__(self, base: str, token: str, timeout: float = SOCKET_TIMEOUT):
        self._base = base.rstrip("/")
        self._token = token
        self._timeout = timeout

    def call(self, method: str, params: dict) -> object:
        return self._post(method, json.dumps(params).encode("utf-8"), "application/json")

    def send_document(self, params: dict, filename: str, content: bytes) -> object:
        """sendDocument as multipart/form-data: every param is a text part, the file part is named `document`."""
        boundary, body = _multipart(params, filename, content)
        return self._post("sendDocument", body, f"multipart/form-data; boundary={boundary}")

    def _post(self, method: str, data: bytes, content_type: str) -> object:
        request = urllib.request.Request(f"{self._base}/bot{self._token}/{method}", data=data,
                                         headers={"Content-Type": content_type}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status, raw = response.status, response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            description, retry_after = _error_details(_read_quietly(exc))
            raise ApiError(exc.code, description, retry_after) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise NetworkError(type(exc).__name__) from None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise NetworkError("the answer is not JSON") from None
        if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
            raise NetworkError("the answer is not a Telegram answer")
        if not body["ok"]:
            description, retry_after = _error_details(raw)
            code = body.get("error_code")
            raise ApiError(code if isinstance(code, int) and not isinstance(code, bool) else status,
                           description, retry_after)
        return body.get("result")


def _form_text(value: object) -> str:
    return "true" if value is True else "false" if value is False else str(value)


def _multipart(params: dict, filename: str, content: bytes) -> tuple[str, bytes]:
    """(boundary, body) of a multipart/form-data upload built by hand. The boundary is random and checked not to
    occur in any part, so no content can end a part early."""
    if any(char in filename for char in '"\r\n\0'):
        raise ValueError("the file name has a quote or a control character")
    texts = [(str(key), _form_text(value).encode("utf-8")) for key, value in params.items()]
    for key, _ in texts:
        if any(char in key for char in '"\r\n\0'):
            raise ValueError("a field name has a quote or a control character")
    while True:
        boundary = "agbot" + secrets.token_hex(16)
        marker = b"--" + boundary.encode("ascii")
        if marker not in content and not any(marker in value for _, value in texts):
            break
    pieces: list[bytes] = []
    for key, value in texts:
        pieces += [marker, f'Content-Disposition: form-data; name="{key}"'.encode("utf-8"), b"", value]
    pieces += [marker,
               f'Content-Disposition: form-data; name="document"; filename="{filename}"'.encode("utf-8"),
               b"Content-Type: application/octet-stream", b"", content,
               marker + b"--", b""]
    return boundary, b"\r\n".join(pieces)


def _read_quietly(response) -> bytes:
    try:
        return response.read(65536)
    except (OSError, http.client.HTTPException):
        return b""


def _error_details(raw: bytes) -> tuple[str, int | None]:
    """(description, retry_after) from a Telegram error body; ("", None) when it is not that JSON."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "", None
    if not isinstance(body, dict):
        return "", None
    description = body.get("description")
    parameters = body.get("parameters")
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if not (isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after > 0):
        retry_after = None
    return (description if isinstance(description, str) else "")[:300], retry_after


def _is_int(value: object) -> bool:
    return type(value) is int


def _write_json_atomic(path: Path, value: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("ignoring unreadable state file %s", path.name)
        return None


class Bot:
    def __init__(self, api, owner_id: int, state_dir: str | Path, inbox: str | Path, outbox: str | Path,
                 clock=time.time, sleep=time.sleep):
        self.api = api
        self.owner_id = owner_id
        self.state_dir = Path(state_dir)
        self.inbox = Path(inbox)
        self.outbox = Path(outbox)
        self.clock = clock  # wall-clock seconds: pending expiry survives a restart
        self.sleep = sleep
        self.ignored = 0
        self._failures = 0
        self._sent_parts: dict[str, int] = {}  # outbox file -> parts already sent (this process only)
        self._outbox_pause_until = 0.0
        self.offset = self._load_offset()
        self.pending = self._load_pending()

    # ---- state files -------------------------------------------------------------------------------------

    def _load_offset(self) -> int | None:
        value = _read_json(self.state_dir / OFFSET_FILE)
        if isinstance(value, dict) and _is_int(value.get("offset")) and value["offset"] >= 0:
            return value["offset"]
        return None

    def _save_offset(self, offset: int) -> None:
        self.offset = offset
        _write_json_atomic(self.state_dir / OFFSET_FILE, {"offset": offset})

    def _load_pending(self) -> dict[str, dict]:
        value = _read_json(self.state_dir / PENDING_FILE)
        found: dict[str, dict] = {}
        for key, entry in (value.items() if isinstance(value, dict) else ()):
            ok = (isinstance(entry, dict) and _KEY_RE.fullmatch(key) and _is_int(entry.get("reply_to"))
                  and isinstance(entry.get("expires"), (int, float)) and not isinstance(entry["expires"], bool)
                  and ((entry.get("kind") == "new" and isinstance(entry.get("text"), str))
                       or (entry.get("kind") == "continue" and valid_id(entry.get("id"))
                           and isinstance(entry.get("text"), str))
                       or (entry.get("kind") == "stop" and valid_id(entry.get("id")))))
            if ok:
                found[key] = entry
        return found

    def _save_pending(self) -> None:
        _write_json_atomic(self.state_dir / PENDING_FILE, self.pending)

    def _prune(self) -> None:
        now = self.clock()
        stale = [key for key, entry in self.pending.items() if entry["expires"] <= now]
        if stale:
            for key in stale:
                del self.pending[key]
            self._save_pending()

    def _add_pending(self, entry: dict) -> str:
        key = secrets.token_urlsafe(12)  # 16 characters
        self.pending[key] = {**entry, "expires": self.clock() + PENDING_TTL}
        self._save_pending()
        return key

    # ---- Telegram calls ----------------------------------------------------------------------------------

    def _send(self, text: str, markup: dict | None = None) -> object:
        params: dict = {"chat_id": self.owner_id, "text": text}
        if markup is not None:
            params["reply_markup"] = markup
        return self.api.call("sendMessage", params)

    def _quietly(self, method: str, params: dict) -> None:
        """A Telegram call whose failure changes nothing (an answer or an edit after the real work is done)."""
        try:
            self.api.call(method, params)
        except (ApiError, NetworkError) as exc:
            log.warning("%s failed: %s", method, exc)

    # ---- polling -----------------------------------------------------------------------------------------

    def poll_once(self) -> int:
        """One getUpdates call and the handling of its updates. Returns the number of updates handled.
        A failed call sleeps the backoff (5, 10, 20, 60, 60 ... seconds) and returns 0."""
        params: dict = {"timeout": POLL_TIMEOUT, "allowed_updates": ALLOWED_UPDATES}
        if self.offset is not None:
            params["offset"] = self.offset
        try:
            updates = self.api.call("getUpdates", params)
        except (ApiError, NetworkError) as exc:
            delay = BACKOFF_SECONDS[min(self._failures, len(BACKOFF_SECONDS) - 1)]
            self._failures += 1
            if isinstance(exc, ApiError) and exc.retry_after:
                delay = max(delay, min(exc.retry_after, 3600))
            log.warning("getUpdates failed (%s); retrying in %d s", exc, delay)
            self.sleep(delay)
            return 0
        self._failures = 0
        if not isinstance(updates, list):
            log.warning("getUpdates returned %s instead of a list", type(updates).__name__)
            return 0
        ignored_before = self.ignored
        for update in updates:
            self.handle_update(update)
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if _is_int(update_id):
                self._save_offset(max(update_id + 1, self.offset or 0))
        if self.ignored != ignored_before:
            log.info("ignored %d update(s) not from the owner (%d in total)", self.ignored - ignored_before,
                     self.ignored)
        return len(updates)

    def run_polling(self) -> None:
        while True:
            self.poll_once()

    # ---- updates -----------------------------------------------------------------------------------------

    def handle_update(self, update: object) -> None:
        """Handle one update. Never raises on bad data or on a failed Telegram call (they are logged)."""
        try:
            self._prune()
            self._dispatch(update)
        except Exception as exc:  # noqa: BLE001 - one bad update must not stop the daemon
            log.warning("update skipped: %s: %s", type(exc).__name__, exc)

    def _dispatch(self, update: object) -> None:
        if not isinstance(update, dict):
            raise ValueError("update is not an object")
        if "message" in update:
            message = update["message"]
            if self._message_from_owner(message):
                self._handle_message(message)
                return
        elif "callback_query" in update:
            query = update["callback_query"]
            if self._callback_from_owner(query):
                self._handle_callback(query)
                return
        self.ignored += 1

    def _owner(self, value: object) -> bool:
        return _is_int(value) and value == self.owner_id

    def _private_owner_chat(self, message: object) -> bool:
        if not isinstance(message, dict):
            return False
        chat = message.get("chat")
        return isinstance(chat, dict) and chat.get("type") == "private" and self._owner(chat.get("id"))

    def _message_from_owner(self, message: object) -> bool:
        sender = message.get("from") if isinstance(message, dict) else None
        return (isinstance(sender, dict) and self._owner(sender.get("id")) and self._private_owner_chat(message))

    def _callback_from_owner(self, query: object) -> bool:
        if not isinstance(query, dict):
            return False
        sender = query.get("from")
        return isinstance(sender, dict) and self._owner(sender.get("id")) \
            and self._private_owner_chat(query.get("message"))

    # ---- inbox -------------------------------------------------------------------------------------------

    def _enqueue(self, item: dict) -> bool:
        try:
            spool.write_item(self.inbox, item)
        except OSError as exc:
            log.error("cannot write the inbox item: %s: %s", type(exc).__name__, exc)
            return False
        return True

    # ---- messages ----------------------------------------------------------------------------------------

    def _handle_message(self, message: dict) -> None:
        message_id = message.get("message_id")
        if not _is_int(message_id):
            raise ValueError("message without a message_id")
        text = message.get("text")
        if not isinstance(text, str):
            self._send(t("telegram.not_text"))
            return
        if text.startswith("/"):
            self._handle_command(text, message_id)
        else:
            self._handle_request(text, message_id)

    def _handle_request(self, text: str, message_id: int) -> None:
        text = text.strip()
        followup = _FOLLOWUP_RE.match(text)
        if followup:
            self._handle_followup(followup.group(1), followup.group(2).strip(), message_id)
            return
        item = {"type": "new", "text": text, "reply_to": message_id}
        if not self._text_fits(text, item):
            return
        key = self._add_pending({"kind": "new", "text": text, "reply_to": message_id})
        self._send(t("telegram.confirm_new", preview=_preview(text)),
                   _buttons(key, t("telegram.button.start"), t("telegram.button.cancel")))

    def _handle_followup(self, mission_id: str, text: str, message_id: int) -> None:
        item = {"type": "continue", "id": mission_id, "text": text, "reply_to": message_id}
        if not self._text_fits(text, item):
            return
        key = self._add_pending({"kind": "continue", "id": mission_id, "text": text, "reply_to": message_id})
        self._send(t("telegram.confirm_continue", preview=_preview(text), id=mission_id),
                   _buttons(key, t("telegram.button.start"), t("telegram.button.cancel")))

    def _text_fits(self, text: str, item: dict) -> bool:
        """The size rules of a request text (10..20,000 bytes, and the inbox JSON of `item` at most 32 KB).
        Explains to the owner and returns False when they are not met."""
        size = len(text.encode("utf-8", "surrogatepass"))
        if size < REQUEST_MIN_BYTES:
            self._send(t("telegram.too_short"))
            return False
        if size > REQUEST_MAX_BYTES:
            self._send(t("telegram.too_long"))
            return False
        if len(json.dumps(item, ensure_ascii=False).encode("utf-8", "surrogatepass")) > INBOX_ITEM_MAX_BYTES:
            self._send(t("telegram.too_long"))  # JSON escapes (quotes, newlines) make it too big for the inbox
            return False
        return True

    def _handle_command(self, text: str, message_id: int) -> None:
        words = text.split()
        command = words[0][1:].split("@", 1)[0].lower()  # "/list@MyBot" is the same as "/list"
        args = words[1:]
        if command == "list" and not args:
            self._relay({"type": "list", "reply_to": message_id})
        elif command == "continue":
            # the text may span lines and keeps its own spacing: take it from the raw message, not from `words`
            parts = text.split(None, 2)
            body = parts[2].strip() if len(parts) > 2 else ""
            if len(parts) < 2 or not valid_id(parts[1]) or not body:
                self._send(t("telegram.usage_continue"))
            else:
                self._handle_followup(parts[1], body, message_id)
        elif command in ("status", "report", "files", "stop"):
            if len(args) != 1 or not valid_id(args[0]):
                self._send(t("telegram.usage_id", command=command))
            elif command == "stop":
                key = self._add_pending({"kind": "stop", "id": args[0], "reply_to": message_id})
                self._send(t("telegram.confirm_stop", id=args[0]),
                           _buttons(key, t("telegram.button.stop"), t("telegram.button.cancel")))
            else:
                self._relay({"type": command, "id": args[0], "reply_to": message_id})
        else:  # /help, /start, an unknown command, or a known one with stray arguments
            self._send(t("telegram.usage"))

    def _relay(self, item: dict) -> None:
        if not self._enqueue(item):
            self._send(t("telegram.unavailable"))

    # ---- button presses ----------------------------------------------------------------------------------

    def _handle_callback(self, query: dict) -> None:
        message_id = query["message"].get("message_id")
        if not _is_int(message_id):
            raise ValueError("callback without a message_id")
        if isinstance(query.get("id"), str):
            self._quietly("answerCallbackQuery", {"callback_query_id": query["id"]})
        data = query.get("data")
        action, _, key = data.partition(":") if isinstance(data, str) else ("", "", "")
        entry = self.pending.pop(key, None) if action in ("ok", "no") and _KEY_RE.fullmatch(key) else None
        if entry is None:
            self._send(t("telegram.stale"))
            return
        self._save_pending()
        edit = {"chat_id": self.owner_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}
        if action == "no":
            self._quietly("editMessageText", {**edit, "text": t("telegram.cancelled")})
            return
        if entry["kind"] == "new":
            item = {"type": "new", "text": entry["text"], "reply_to": entry["reply_to"]}
        elif entry["kind"] == "continue":
            item = {"type": "continue", "id": entry["id"], "text": entry["text"], "reply_to": entry["reply_to"]}
        else:
            item = {"type": "stop", "id": entry["id"], "reply_to": entry["reply_to"]}
        if not self._enqueue(item):
            self.pending[key] = entry  # nothing was created: the owner may press the button again
            self._save_pending()
            self._send(t("telegram.retry_button"))
            return
        self._quietly("editMessageText", {
            **edit, "text": t("telegram.stop_accepted") if entry["kind"] == "stop" else t("telegram.accepted")
        })

    # ---- outbox ------------------------------------------------------------------------------------------

    def flush_outbox(self) -> int:
        """Send every finished outbox file once (oldest first). Returns how many files were finished (sent or
        rejected and deleted). Stops at the first retryable failure so that order is kept."""
        if self.clock() < self._outbox_pause_until:
            return 0
        try:
            dfd = spool.open_dir(self.outbox)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            log.error("cannot open the outbox: %s: %s", type(exc).__name__, exc)
            return 0
        finished = 0
        try:
            names = spool.list_names(dfd)
            self._sent_parts = {name: n for name, n in self._sent_parts.items() if name in names}
            for name in names:
                if not self._flush_item(dfd, name):
                    break
                finished += 1
        finally:
            os.close(dfd)
        return finished

    def _flush_item(self, dfd: int, name: str) -> bool:
        """True when the file is finished (deleted); False to retry later."""
        item = None
        try:
            item = spool.read_item(dfd, name, OUTBOX_ITEM_MAX_BYTES)
            kind, payload, reply_to = _outbox_fields(item)
        except spool.SpoolError as exc:
            log.warning("deleting invalid outbox item %s: %s", name, exc)
            self._discard(dfd, name, item)
            return True
        if kind == "document":
            return self._flush_document(dfd, name, payload, reply_to)
        done = self._sent_parts.get(name, 0)
        for index in range(done, len(payload)):
            params: dict = {"chat_id": self.owner_id, "text": payload[index], "parse_mode": "HTML"}
            if index == 0 and reply_to is not None:
                params["reply_to_message_id"] = reply_to
                params["allow_sending_without_reply"] = True
            verdict = self._deliver(name, lambda params=params: self.api.call("sendMessage", params))
            if verdict is not None:
                if verdict:
                    self._discard(dfd, name)
                return verdict
            self._sent_parts[name] = index + 1
        self._discard(dfd, name)
        return True

    def _flush_document(self, dfd: int, name: str, fields: dict, reply_to: int | None) -> bool:
        document = fields["document"]
        try:
            content = spool.read_document(dfd, document, DOCUMENT_MAX_BYTES)
        except spool.SpoolError as exc:
            log.warning("deleting outbox item %s: its document %s %s", name, document, exc)
            self._discard(dfd, name, fields)
            return True
        params: dict = {"chat_id": self.owner_id}
        if "caption" in fields:
            params["caption"] = fields["caption"]
            params["parse_mode"] = "HTML"
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
            params["allow_sending_without_reply"] = True
        verdict = self._deliver(name, lambda: self.api.send_document(params, fields["filename"], content))
        if verdict is not None:
            if verdict:
                self._discard(dfd, name, fields)
            return verdict
        self._discard(dfd, name, fields)
        return True

    def _deliver(self, name: str, send) -> bool | None:
        """Run one Telegram send. None: it worked. True: Telegram rejected it (4xx), so the item is to be deleted.
        False: retry later (5xx, 429 which also pauses the outbox, or a network error)."""
        try:
            send()
        except (ApiError, NetworkError) as exc:
            if retryable(exc):
                log.warning("outbox item %s: will retry: %s", name, exc)
                if isinstance(exc, ApiError) and exc.retry_after:
                    self._outbox_pause_until = self.clock() + min(exc.retry_after, 3600)
                return False
            log.warning("Telegram rejected outbox item %s: %s; deleting it", name, exc)
            return True
        return None

    def _discard(self, dfd: int, name: str, item: dict | None = None) -> None:
        """Delete the outbox item, and the document file it names (the entry itself, never what a link points to)
        when `item` has a valid document name."""
        self._sent_parts.pop(name, None)
        document = item.get("document") if isinstance(item, dict) else None
        for entry in ((document,) if isinstance(document, str) and spool.is_document_name(document) else ()) + (name,):
            try:
                spool.remove(dfd, entry)
            except OSError as exc:
                log.error("cannot delete outbox file %s: %s", entry, exc)

    def run_outbox(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.flush_outbox()
            except Exception as exc:  # noqa: BLE001 - the sender thread must not die
                log.error("outbox pass failed: %s: %s", type(exc).__name__, exc)
            stop.wait(OUTBOX_INTERVAL)


def _buttons(key: str, yes: str, no: str) -> dict:
    return {"inline_keyboard": [[{"text": yes, "callback_data": f"ok:{key}"},
                                 {"text": no, "callback_data": f"no:{key}"}]]}


def _preview(text: str) -> str:
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


def _outbox_fields(item: dict) -> tuple[str, list[str] | dict, int | None]:
    """(kind, payload, reply_to) of an outbox item: ("html", [message, ...]) or ("document", the item's own fields
    without reply_to). SpoolError when the item is not exactly one of the two shapes."""
    reply_to = item.get("reply_to")
    if reply_to is not None and not (_is_int(reply_to) and reply_to > 0):
        raise spool.SpoolError("has a bad reply_to")
    if "document" in item:
        if set(item) - {"document", "filename", "caption", "reply_to"}:
            raise spool.SpoolError("has unknown keys")
        document, filename, caption = item["document"], item.get("filename"), item.get("caption")
        if not (isinstance(document, str) and spool.is_document_name(document)):
            raise spool.SpoolError("has a bad document name")
        if not (isinstance(filename, str) and _FILENAME_RE.fullmatch(filename)):
            raise spool.SpoolError("has a bad filename")
        if "caption" in item and not (isinstance(caption, str) and len(caption) <= CAPTION_LIMIT):
            raise spool.SpoolError("has a bad caption")
        return "document", {key: item[key] for key in ("document", "filename", "caption") if key in item}, reply_to
    if set(item) - {"html", "reply_to"}:
        raise spool.SpoolError("has unknown keys")
    parts = item.get("html")
    if not (isinstance(parts, list) and parts
            and all(isinstance(part, str) and 0 < len(part) <= HTML_LIMIT for part in parts)):
        raise spool.SpoolError("has no valid html list")
    return "html", parts, reply_to


def load_config(path: str | Path) -> tuple[str, int]:
    """(token, owner_id) from the JSON file; ConfigError (never containing the token) when it is not valid."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    except ValueError:
        raise ConfigError(f"{path} is not valid JSON") from None
    if not isinstance(value, dict) or set(value) != {"token", "owner_id"}:
        raise ConfigError(f'{path} must be a JSON object with exactly "token" and "owner_id"')
    token, owner_id = value["token"], value["owner_id"]
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ConfigError(f'{path}: "token" must be a non-empty string of letters, digits, ":", "_" and "-"')
    if not _is_int(owner_id) or owner_id <= 0:
        raise ConfigError(f'{path}: "owner_id" must be a positive integer')
    return token, owner_id


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if config.SETTINGS_ERROR is not None:
        print(t("cli.config.invalid", path=config.SETTINGS_PATH, error=config.SETTINGS_ERROR), file=sys.stderr)
        return 1
    env = os.environ
    try:
        token, owner_id = load_config(env.get("AGENTIC_TELEGRAM_CONFIG", DEFAULT_CONFIG))
    except ConfigError as exc:
        print(t("telegram.error.config", error=exc), file=sys.stderr)
        return 2
    state_dir = Path(env.get("AGENTIC_BOT_STATE", DEFAULT_BOT_STATE))
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        print(t("telegram.error.state_dir", path=state_dir, error=exc.strerror), file=sys.stderr)
        return 2
    bot = Bot(TelegramApi(env.get("AGENTIC_TELEGRAM_API", DEFAULT_API), token), owner_id, state_dir,
              env.get("AGENTIC_INBOX", config.DEFAULT_INBOX), env.get("AGENTIC_OUTBOX", config.DEFAULT_OUTBOX))
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    stop = threading.Event()
    threading.Thread(target=bot.run_outbox, args=(stop,), name="outbox", daemon=True).start()
    log.info("bot started")
    try:
        bot.run_polling()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
