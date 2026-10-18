"""Telegram HTML cards (SPEC-E.md section 4). The only place that emits Telegram markup.

Every dynamic string goes through `esc` (html.escape without quotes); the only tags written are <b>, <i>, <code>,
<pre> and <blockquote expandable>. A card is a list of blocks; `pack` joins blocks into messages of at most
MESSAGE_LIMIT characters and splits only between blocks. A single block that is too long becomes <pre> chunks of
its plain text, cut between characters, so no tag and no entity is ever cut.
"""

import html
import re

from agentic import briefing as bf
from agentic.briefing import Briefing
from agentic.display import zone_label
from agentic.i18n import t
MESSAGE_LIMIT = 4000
ALLOWED_TAGS = ("b", "i", "code", "pre", "blockquote")
RULE = "━━━━━━━━━━━━━━━━━━"
AI_NOTES_CHARS = 3000  # the card shows this much of the AI notes; the full list is in report.md
EVENTS_SHOWN = 3
LIST_SHOWN = 20
_TAG_RE = re.compile(r"</?(?:b|i|code|pre|blockquote)(?: expandable)?>")


def esc(text: object) -> str:
    """The one escape function for every dynamic string in a card."""
    return html.escape(str(text), quote=False)


# ---- splitting -------------------------------------------------------------------------------------------

def plain_text(block: str) -> str:
    """The text a block shows: our tags removed, entities decoded."""
    return html.unescape(_TAG_RE.sub("", block))


def pre_chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """<pre> blocks of at most `limit` characters (tags included) that together show `text`."""
    room = limit - len("<pre></pre>")
    chunks, current, size = [], [], 0
    for char in text:
        piece = esc(char)
        if size + len(piece) > room:
            chunks.append("<pre>" + "".join(current) + "</pre>")
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current:
        chunks.append("<pre>" + "".join(current) + "</pre>")
    return chunks


def pack(blocks: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Messages of at most `limit` characters, blocks joined by a newline, split only between blocks."""
    pieces: list[str] = []
    for block in blocks:
        pieces += [block] if len(block) <= limit else pre_chunks(plain_text(block), limit)
    messages: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > limit:
            messages.append(current)
            current = ""
        current = f"{current}\n{piece}" if current else piece
    if current:
        messages.append(current)
    return messages


# ---- shared pieces ---------------------------------------------------------------------------------------

def header(title: str, mission_id: str) -> str:
    return f"<b>{esc(title)} │ {esc(mission_id)}</b>\n{RULE}"


def _achievement(b: Briefing) -> str:
    return f"{t('cards.achievement', achieved=b.achieved, total=b.total, percent=b.percent)}\n{bf.bar(b.percent)}"


def _todo(b: Briefing) -> str:
    lines = [f" {n}. {esc(todo)}" for n, todo in enumerate(b.todo, 1)] or [f" {esc(bf.no_todo())}"]
    return f"<b>{t('cards.heading.todo')}</b>\n" + "\n".join(lines)

def _decision(n: int, d: bf.Decision) -> str:
    lines = [f" <b>{n}. {esc(d.subject)}</b>"]
    lines += [f"  · {esc(fact)}" for fact in d.facts]
    for key, text in d.options:
        line = f"  {esc(key)}) {esc(text)}"
        if key == d.recommended:
            line += f" {t('cards.recommended', reason=esc(d.reason))}"
        lines.append(line)
    return "\n".join(lines)


def _instruction(instruction: bf.NextInstruction) -> str:
    return f" {esc(instruction.label)}\n<code>{esc(instruction.text)}</code>"


def _ai_notes(b: Briefing) -> str:
    plan_tag = t("cards.plan_tag")
    lines = [f"{plan_tag} {esc(a)}" for a in b.ai_notes.assumptions] + [esc(d) for d in b.ai_notes.decisions]
    shown, size = [], 0
    for line in lines:
        if size + len(line) + 1 > AI_NOTES_CHARS:
            break
        shown.append(line)
        size += len(line) + 1
    if len(shown) < len(lines):
        shown.append(t("cards.ai_notes_more", count=len(lines) - len(shown), id=esc(b.mission_id)))
    return f"<blockquote expandable>{t('cards.heading.ai_notes')}\n" + "\n".join(shown) + "</blockquote>"

# ---- cards -----------------------------------------------------------------------------------------------

def finish_card(b: Briefing) -> list[str]:
    """The mission_finished notification and the /status reply of a finished mission."""
    models = " → ".join(b.models_used) or t("cards.none")
    summary = t("cards.finish.summary", duration=bf.hours_minutes(b.duration),
                iterations=b.iterations, models=esc(models))
    top = [
        header(b.headline, b.mission_id),
        _achievement(b),
        summary,
        f"🕒 {esc(b.ended_at or b.updated_at)}",
    ]
    if b.evidence_line:
        top.append(esc(b.evidence_line))
    blocks = [
        "\n".join(top),
        _todo(b),
    ]
    for n, decision in enumerate(b.decisions, 1):
        blocks.append((f"<b>{t('cards.heading.decisions')}</b>\n" if n == 1 else "") + _decision(n, decision))
    for n, instruction in enumerate(b.next_instructions):
        head = f"<b>{t('cards.heading.next')}</b>  {t('cards.tap_to_copy')}\n" if n == 0 else ""
        blocks.append(head + _instruction(instruction))
    if b.remaining:
        blocks.append(f"<b>{t('cards.heading.remaining')}</b>\n" + "\n".join(
            f" {item.mark} {esc(item.id)} {esc(item.text)} ({esc(item.reason)})" for item in b.remaining))
    if b.notices:
        blocks.append(f"<b>{t('cards.heading.notices')}</b>\n" + "\n".join(f" {esc(n)}" for n in b.notices))
    if len(b.ai_notes):
        blocks.append(_ai_notes(b))
    return blocks


def progress_card(b: Briefing, events: list[tuple[str, str]]) -> list[str]:
    """/status of a mission that is still planning or running. `events`: (display time, summary), oldest first."""
    model = t("cards.progress.model_prefix", model=esc(b.current_model)) if b.current_model else ""
    top = [
        header(b.headline, b.mission_id),
        _achievement(b),
        t("cards.progress.iterations", model_prefix=model, iterations=b.iterations),
        t("cards.progress.elapsed", duration=bf.hours_minutes(b.duration)),
        t("cards.progress.updated_at", updated_at=esc(b.updated_at)),
    ]
    if b.followups:
        top.append(esc(t("briefing.notice.followups", followups=", ".join(b.followups))))
    blocks = [
        "\n".join(top),
        _todo(b),
    ]
    if events:
        blocks.append(f"<b>{t('cards.heading.events', tz=zone_label())}</b>\n" + "\n".join(
            f" · {esc(moment)} {esc(text)}" for moment, text in events[-EVENTS_SHOWN:]))
    return blocks


def status_card(b: Briefing, events: list[tuple[str, str]]) -> list[str]:
    return finish_card(b) if b.finished else progress_card(b, events)


def event_card(title: str, mission_id: str, lines: list[str], todo: str | None = None) -> list[str]:
    """Other cards (plan finished, model escalated, model waiting, review veto, unit crash, accepted): the header,
    a few lines, the todo. `lines` are plain text and escaped here."""
    if todo is None:
        todo = bf.running_todo()
    body = "\n".join(esc(line) for line in lines)
    return [f"{header(title, mission_id)}\n{body}\n{t('cards.event_todo', todo=esc(todo))}"]

def list_card(entries: list[tuple[str, str | None, Briefing | None, str | None]]) -> list[str]:
    """/list: one block per mission, newest first, at most LIST_SHOWN.
    entries: (mission id, sort key = latest record ts or None, briefing or None, problem text or None)."""
    ordered = sorted(entries, key=lambda e: (e[1] is not None, e[1] or "", e[0]), reverse=True)[:LIST_SHOWN]
    blocks = []
    for mission_id, _, b, problem in ordered:
        if b is not None:
            blocks.append(f"<b>{esc(mission_id)}</b> · {esc(b.headline)} · {b.achieved}/{b.total} · "
                          f"{esc(b.updated_at)}")
        else:
            blocks.append(f"<b>{esc(mission_id)}</b> · {esc(problem or t('cards.no_records'))}")
    return blocks


def text_card(text: str) -> list[str]:
    """A plain reply (errors, short answers): escaped, one block per line group."""
    return [esc(text)]
