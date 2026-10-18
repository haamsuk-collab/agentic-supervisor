"""Notifications to the owner through the Telegram bot's outbox (SPEC-D.md section 5, SPEC-E.md section 4).

Best effort: a failure is logged to stderr and never changes the mission. A missing outbox folder (no bot
installed, hand-run tests) silently skips notifications. Every card is built from ledger data that the report may
show, so no check argv or check file content can reach it.
"""

import json
import sys

from agentic import briefing, cards, commands, outbox, report, results, spool
from agentic.config import Config, valid_id
from agentic.i18n import t
from agentic.ledger import parse_ts
from agentic.worker import one_line

PLAN_CRITERIA_SHOWN = 5
SILENT_PREFIX = "canary-"  # the daily honesty canary (SPEC-F section 5) never notifies the owner


def silenced(mission_id: str) -> bool:
    return mission_id.startswith(SILENT_PREFIX)


def send(cfg: Config, blocks: list[str], name: str | None = None) -> bool:
    """Put one card into the outbox. False when it was skipped: no outbox folder, or `name` already exists (that
    event was already handed to the bot). Other errors propagate."""
    try:
        outbox.write_cards(cfg, blocks, name=name)
    except (FileNotFoundError, FileExistsError):
        return False
    return True


def send_report(cfg: Config, mission_id: str, text: str, name: str | None = None) -> bool:
    """report.md as the document `report-<id>.md`; a message instead when it is over the document limit."""
    try:
        outbox.write_document(cfg, text.encode("utf-8"), f"report-{mission_id}.md",
                              cards.esc(t("notify.report_caption", id=mission_id)), name=name)
    except (FileNotFoundError, FileExistsError):
        return False
    except spool.DocumentTooLarge as exc:
        return send(cfg, cards.text_card(t("notify.report_too_large", size=results.format_size(exc.size))),
                    name=name)
    return True


class Notifier:
    """Turns freshly appended ledger records of one mission into outbox cards."""

    def __init__(self, cfg: Config, mission_id: str, ledger):
        self.cfg, self.mission_id, self.ledger = cfg, mission_id, ledger

    def on_record(self, record: dict, state) -> None:
        """`state` is the LoopState after the record was applied. Never raises."""
        try:
            if silenced(self.mission_id):
                return
            # One file name per ledger record: a second write of the same event is refused (O_EXCL).
            name = f"{self.mission_id}-{record['seq']}"
            if record["type"] == "mission_finished":
                built = report.build(self.cfg, self.mission_id, self.ledger.read())
                if send(self.cfg, cards.finish_card(built.briefing), name=name):
                    send_report(self.cfg, self.mission_id, built.text, name=f"{name}-report")
                return
            blocks = self.card(record, state)
            if blocks is not None:
                send(self.cfg, blocks, name=name)
        except Exception as exc:  # best effort: the mission goes on whatever happens here
            print(t("notify.log_failed", id=self.mission_id, seq=record.get("seq"), error=exc),
                  file=sys.stderr)

    def card(self, record: dict, state) -> list[str] | None:
        data, mid = record["data"], self.mission_id
        kind = record["type"]
        if kind == "plan_finished":
            criteria = json.loads(self.cfg.mission_file(mid).read_text(encoding="utf-8"))["criteria"]
            count = t("notify.plan_finished.criteria", count=len(criteria))
            if data["dropped"]:
                count += t("notify.plan_finished.dropped", count=len(data["dropped"]))
            shown = [f"· {c['id']} {one_line(c['text'])}" for c in criteria[:PLAN_CRITERIA_SHOWN]]
            return cards.event_card(t("notify.plan_finished.title"), mid, [count, *shown])
        if kind == "escalated":
            before = briefing.short_model(state.ladder[data["from_step"]]["model"])
            return cards.event_card(t("notify.escalated.title"), mid, [
                f"{before} → {briefing.short_model(data['model'])}",
                t("notify.escalated.stalls", stalls=data["stalls"])])
        if kind == "worker_unavailable":
            if state.outage_alert_seq != record["seq"]:
                return None
            minutes = int((parse_ts(record["ts"]) - state.outage_started).total_seconds() // 60)
            return cards.event_card(t("notify.outage.title"), mid, [
                t("notify.outage.waiting", minutes=minutes, model=briefing.short_model(data["model"])),
                t("notify.outage.reason", reason=one_line(data["reason"]))])
        if kind == "review_veto":
            criteria = list(dict.fromkeys(f["criterion"] for f in data["findings"]))
            return cards.event_card(t("notify.review_veto.title"), mid, [
                t("notify.review_veto.found", count=len(criteria), criteria=", ".join(criteria)),
                t("notify.review_veto.retrying")])
        return None


def notify_failure(cfg: Config, mission_id: str) -> None:
    """`agentic notify-failure <id>`, run by OnFailure= when the mission's unit failed.

    systemd shows SubState `auto-restart` while it is about to restart the unit; anything else means it gave up.
    """
    if not valid_id(mission_id):
        raise ValueError(f"{mission_id!r} is not a valid mission id")
    if silenced(mission_id):
        return
    result = commands.run_command(["systemctl", "show", "--property=SubState", "--value",
                                   f"agentic@{mission_id}.service"])
    if result.returncode == 0 and result.stdout.strip() == "auto-restart":
        blocks = cards.event_card(t("notify.crash.title"), mission_id, [t("notify.crash.restarting")])
    else:  # nothing runs any more, so "running automatically" would be false
        blocks = cards.event_card(t("notify.crash.title"), mission_id, [t("notify.crash.stopped")],
                                  todo=t("notify.crash.todo", id=mission_id))
    send(cfg, blocks)
