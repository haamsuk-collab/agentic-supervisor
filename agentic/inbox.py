"""The inbox processor (SPEC-D.md sections 1 and 3, SPEC-E.md sections 4-6), run as root by `agentic inbox`.

The bot (account agbot) writes one JSON request per file into the inbox. Each request is read defensively,
handled through agentic.admin, answered with one outbox item (an HTML card, or a document), and deleted, also
when it was rejected.
"""

import fcntl
import os
import sys

from agentic import admin, cards, config, outbox, report, results, spool
from agentic.config import INBOX_ITEM_MAX_BYTES, Config, valid_id
from agentic.i18n import t
from agentic.ledger import LedgerError
from agentic.worker import user_uid

TYPES = ("new", "list", "status", "report", "stop", "files", "continue")
NEEDS_ID = ("status", "report", "stop", "files", "continue")
NEEDS_TEXT = ("new", "continue")
ALLOWED_KEYS = {"type", "text", "id", "reply_to"}
ERROR_MAX_CHARS = 500


class ItemRejected(ValueError):
    """The request is not valid; the message is the reason."""


def _log(message: str) -> None:
    print(t("inbox.log", message=message), file=sys.stderr)


def _valid_reply_to(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(item: dict) -> dict:
    """The item when it follows the inbox contract (SPEC-D.md section 3, SPEC-E.md); ItemRejected otherwise."""
    unknown = sorted(set(item) - ALLOWED_KEYS)
    if unknown:
        raise ItemRejected(f"unknown keys {unknown}")
    kind = item.get("type")
    if kind not in TYPES:
        raise ItemRejected(f"unknown type {kind!r}")
    if "text" in item and (kind not in NEEDS_TEXT or not isinstance(item["text"], str)):
        raise ItemRejected("text is only allowed in a new or continue item, and must be a string")
    if kind in NEEDS_TEXT and "text" not in item:
        raise ItemRejected(f"a {kind} item needs text")
    if "id" in item and kind not in NEEDS_ID:
        raise ItemRejected(f"{kind} takes no id")
    if kind in NEEDS_ID and not valid_id(item.get("id")):
        raise ItemRejected(f"{kind} needs a valid mission id")
    if "reply_to" in item and not _valid_reply_to(item["reply_to"]):
        raise ItemRejected("reply_to must be a positive integer")
    return item


def _briefing(cfg: Config, mission_id: str) -> tuple[list[dict], object, str | None]:
    """(records, briefing or None, problem or None) of a known mission."""
    records, problem = admin._records(cfg, mission_id)
    if problem or not records:
        return records, None, problem
    try:
        return records, report.build(cfg, mission_id, records).briefing, None
    except (KeyError, TypeError, ValueError) as exc:  # a ledger this code cannot read: say so, never raise
        return records, None, t("inbox.report_build_failed", error=_short(exc))


def _list(cfg: Config) -> list[str]:
    entries = []
    for mission_id in admin.mission_ids(cfg):
        records, b, problem = _briefing(cfg, mission_id)
        entries.append((mission_id, records[-1]["ts"] if records else None, b, problem))
    return cards.list_card(entries) if entries else cards.text_card(t("inbox.no_missions"))


def _status(cfg: Config, mission_id: str) -> list[str]:
    admin._require_known(cfg, mission_id)  # UnknownMission for a mission that does not exist
    records, b, problem = _briefing(cfg, mission_id)
    if b is None:
        return cards.text_card(f"{mission_id} · {problem or t('inbox.no_records')}")
    events = [(admin._time(r), admin.event_text(r)) for r in records[-cards.EVENTS_SHOWN:]]
    return cards.status_card(b, events)


def _send_document(cfg: Config, data: bytes, filename: str, caption: str, reply_to: object) -> None:
    outbox.write_document(cfg, data, filename, cards.esc(caption),
                          reply_to=reply_to if _valid_reply_to(reply_to) else None)


def _report(cfg: Config, mission_id: str, reply_to: object) -> list[str] | None:
    text = admin.report_text(cfg, mission_id)
    if text == t("admin.no_report"):
        return cards.text_card(text)
    try:
        _send_document(cfg, text.encode("utf-8"), f"report-{mission_id}.md",
                       t("inbox.report_caption", id=mission_id), reply_to)
    except spool.DocumentTooLarge as exc:
        return cards.text_card(t("inbox.report_too_large", size=results.format_size(exc.size)))
    return None


def _report_now(cfg: Config, mission_id: str) -> str:
    """report.md, or (before the supervisor wrote one) the report of the ledger so far."""
    text = admin.report_text(cfg, mission_id)  # UnknownMission for a mission that does not exist
    if text == t("admin.no_report"):
        records, problem = admin._records(cfg, mission_id)
        if records and not problem:
            text = report.build(cfg, mission_id, records).text
    return text


def _files(cfg: Config, mission_id: str, reply_to: object) -> list[str] | None:
    """The worker folder as results-<id>.zip with report-<id>.md at its root (SPEC-E.md section 5)."""
    report_text = _report_now(cfg, mission_id)
    owner = user_uid(config.WORKER_USER) if os.geteuid() == 0 else None  # hard links to root files are skipped
    try:
        data, count = results.build_zip(cfg.workdir(mission_id), f"report-{mission_id}.md", report_text,
                                        owner_uid=owner)
    except results.NoResults:
        return cards.text_card(t("inbox.no_files", id=mission_id))
    except results.TooLarge as exc:
        return cards.text_card(t("inbox.files_too_large", size=results.format_size(exc.size, exc.at_least)))
    try:
        _send_document(cfg, data, f"results-{mission_id}.zip",
                       t("inbox.files_caption", id=mission_id, count=count), reply_to)
    except spool.DocumentTooLarge as exc:
        return cards.text_card(t("inbox.files_too_large", size=results.format_size(exc.size)))
    return None


def _answer(cfg: Config, item: dict) -> list[str] | None:
    """The reply card, or None when the reply (a document) was already written."""
    kind = item["type"]
    if kind == "new":
        mission_id = admin.new_mission(cfg, item["text"])
        return cards.event_card(t("inbox.accepted_title"), mission_id, [t("inbox.planning")])
    if kind == "list":
        return _list(cfg)
    mission_id = item["id"]
    if kind == "continue":
        new_id = admin.continue_mission(cfg, mission_id, item["text"])
        return cards.event_card(t("inbox.accepted_title"), new_id,
                                [t("inbox.planning"), t("inbox.continued_from", id=mission_id)])
    if kind == "status":
        return _status(cfg, mission_id)
    if kind == "report":
        return _report(cfg, mission_id, item.get("reply_to"))
    if kind == "files":
        return _files(cfg, mission_id, item.get("reply_to"))
    admin.stop_mission(cfg, mission_id)
    return cards.text_card(t("inbox.stopped", id=mission_id))


def _reply(cfg: Config, blocks: list[str], reply_to: object) -> None:
    try:
        outbox.write_cards(cfg, blocks, reply_to=reply_to if _valid_reply_to(reply_to) else None)
    except (OSError, ValueError) as exc:
        _log(f"cannot write the reply: {exc}")


def _short(text: object) -> str:
    return " ".join(str(text).split())[:ERROR_MAX_CHARS]


def _handle(cfg: Config, dfd: int, name: str, bot_uid: int) -> None:
    try:
        item = spool.read_item(dfd, name, INBOX_ITEM_MAX_BYTES, owner_uid=bot_uid)
    except spool.SpoolError as exc:
        _log(f"rejected {name!r}: {exc}")
        _reply(cfg, cards.text_card(t("inbox.rejected_file", error=exc)), None)
        return
    reply_to = item.get("reply_to")
    try:
        validate(item)
        blocks = _answer(cfg, item)
    except ItemRejected as exc:
        _log(f"rejected {name!r}: {exc}")
        blocks = cards.text_card(t("inbox.rejected", error=_short(exc)))
    except admin.UnknownMission as exc:
        blocks = cards.text_card(t("inbox.unknown_mission", id=exc.mission_id))
    except (admin.MissionActive, admin.TooManyMissions) as exc:
        blocks = cards.text_card(str(exc))
    except LedgerError:
        blocks = cards.text_card(t("inbox.ledger_corrupted", id=item.get("id")))
    except Exception as exc:  # nothing may reach the bot, and the next item must still be handled
        _log(f"{item.get('type')!r} failed: {type(exc).__name__}: {exc}")
        blocks = cards.text_card(t("inbox.failed", error=_short(exc)))
    if blocks is not None:
        _reply(cfg, blocks, reply_to)


def process(cfg: Config) -> int:
    """Handle every inbox item, oldest first, one processor at a time. Returns the exit code (0)."""
    lock_fd = os.open(cfg.inbox_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            dfd = spool.open_dir(cfg.inbox)
        except FileNotFoundError:
            return 0  # no bot installed
        try:
            bot_uid = spool.owner_of(cfg.bot_user)[0]
            for name in spool.list_names(dfd):
                try:
                    _handle(cfg, dfd, name, bot_uid)
                finally:
                    try:  # always, also after a rejection; a symlink is unlinked, not followed
                        spool.remove(dfd, name)
                    except OSError as exc:  # e.g. a planted non-empty folder: log it, go on with the rest
                        _log(f"cannot delete {name!r}: {exc}")
        finally:
            os.close(dfd)
    finally:
        os.close(lock_fd)
    return 0
