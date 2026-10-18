"""The Korean mission report (SPEC-E.md section 3), rendered from the briefing (agentic.briefing).

Generated from ledger records only, plus the clearly labelled DECISIONS.md the worker wrote. Every dynamic
field outside a code block is Markdown-escaped, and AI-written text stays inside `> ` quotes, so worker or
planner text cannot forge a heading, a table row or a ✅ line.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from agentic import briefing as bf
from agentic.briefing import Briefing
from agentic.config import DECISIONS_FILE, DECISIONS_MAX_BYTES, Config, valid_id
from agentic.display import zone_label
from agentic.i18n import t
from agentic.ledger import Ledger
from agentic.mission import MissionError
from agentic.planner import read_parent
from agentic.worker import one_line

STATUS_KEYS = {
    "complete": "report.status.complete",
    "partial": "report.status.partial",
    "stopped_time": "report.status.stopped_time",
    "plan_failed": "report.status.plan_failed",
}
RESULT_KEYS = {
    bf.ACHIEVED: "report.result.achieved",
    bf.FAILED: "report.result.failed",
    bf.UNVERIFIABLE: "report.result.unverifiable",
}
REQUEST_PREVIEW_CHARS = 300
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|])")
_NATURAL_SPLIT = re.compile(r"(\d+)")


def status_text(status: object, default: str | None = None) -> str | None:
    key = STATUS_KEYS.get(status)
    return t(key) if key else default


def __getattr__(name: str):
    if name == "STATUS_TEXT":
        return {k: t(v) for k, v in STATUS_KEYS.items()}
    if name == "EMPTY":
        return t("report.empty")
    if name == "RESULT_WORDS":
        return {k: t(v) for k, v in RESULT_KEYS.items()}
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def md(text: str) -> str:
    """One line of dynamic text, safe outside a code block: Markdown control characters are backslash-escaped."""
    return _MD_SPECIAL.sub(r"\\\1", one_line(text))


def fence_for(text: str) -> str:
    """A backtick fence longer than any backtick run in `text` (at least three)."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def read_decisions(workdir: str | Path) -> list[str]:
    """Non-empty lines of the worker's DECISIONS.md. Symlinks and non-regular files are ignored."""
    try:
        fd = os.open(Path(workdir) / DECISIONS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        text = os.read(fd, DECISIONS_MAX_BYTES).decode("utf-8", errors="replace")
    finally:
        os.close(fd)
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def preview(request: str) -> str:
    text = one_line(request)
    return text[:REQUEST_PREVIEW_CHARS] + "…" if len(text) > REQUEST_PREVIEW_CHARS else text


def _criterion_line(item: bf.Item) -> str:
    line = f"- [{md(item.id)}] {md(item.text)}"
    if item.mark == bf.ACHIEVED:
        line += f" — {t('report.criterion.last_check', last_check=item.last_check, exit_code=item.exit_code)}"
        if item.reason:
            line += f" — ⚠️ {md(item.reason)}"
    else:
        line += f" — {md(item.reason)}"
    return line


def render_briefing(b: Briefing) -> str:
    """report.md text of a briefing."""
    empty = t("report.empty")
    lines = [
        t("report.title", id=md(b.mission_id)),
        t("report.conclusion", headline=b.headline, total=b.total, achieved=b.achieved),
        "",
        t("report.summary_header"),
        "|---|---|---|---|---|",
        f"| {b.achieved}/{b.total} ({b.percent}%) | {bf.hours_minutes(b.duration)} | "
        f"{t('report.iterations_cell', n=b.iterations)} | "
        f"{md(' → '.join(b.models_used)) or empty} | {b.ended_at or t('report.in_progress')} |",
        "",
    ]
    head = []
    if b.request:
        head.append(t("report.request_line", request=md(preview(b.request))))
    if b.parent:
        head.append(t("report.parent_line", parent=md(b.parent)))
    if b.evidence_line:
        head.append(md(b.evidence_line))
    if b.followups:
        head.append(t("report.followups_line", followups=md(", ".join(b.followups))))
    if head:
        lines += head + [""]
    lines.append(t("report.section.todo"))
    lines += [f"{n}. {md(todo)}" for n, todo in enumerate(b.todo, 1)] or [bf.no_todo()]
    lines.append(t("report.section.decisions"))
    for n, d in enumerate(b.decisions, 1):
        lines.append(t("report.decision.heading", n=n, subject=md(d.subject)))
        lines += [f"- {md(fact)}" for fact in d.facts]
        for key, text in d.options:
            option = f"- **{key})** {md(text)}"
            if key == d.recommended:
                option += f" {t('report.decision.recommended', reason=md(d.reason))}"
            lines.append(option)
    if not b.decisions:
        lines.append(empty)
    lines.append(t("report.section.next"))
    for instruction in b.next_instructions:
        fence = fence_for(instruction.text)
        lines += [f"**{md(instruction.label)}**", f"{fence}text", instruction.text, fence]
    if not b.next_instructions:
        lines.append(empty)
    lines.append(t("report.section.results"))
    for mark, title_key in ((bf.ACHIEVED, "report.results.achieved"), (bf.FAILED, "report.results.failed"),
                            (bf.UNVERIFIABLE, "report.results.unverifiable")):
        items = [item for item in b.evidence if item.mark == mark]
        lines.append(f"### {t(title_key)} ({len(items)})")
        lines += [_criterion_line(item) for item in items] or [empty]
    lines.append(t("report.section.notices"))
    lines += [f"- {md(notice)}" for notice in b.notices] or [empty]
    lines.append(t("report.section.ai_notes"))
    # Quoted and escaped: AI-written text can never look like a heading, a table row or a ✅ line.
    plan_tag = t("report.plan_tag")
    notes = [f"> {plan_tag} {md(a)}" for a in b.ai_notes.assumptions] + [f"> {md(d)}" for d in b.ai_notes.decisions]
    lines += notes or [empty]
    lines.append(t("report.section.appendix"))
    if b.evidence:
        lines += [t("report.appendix_header", tz=zone_label()), "|---|---|---|---|---|"]
        for item in b.evidence:
            exit_code = "-" if item.exit_code is None else str(item.exit_code)
            lines.append(f"| {md(item.id)} | {t(RESULT_KEYS[item.mark])} | {item.last_check or '-'} | "
                         f"{exit_code} | {t('report.iterations_cell', n=item.attempts)} |")
    else:
        lines.append(empty)
    return "\n".join(lines) + "\n"


def public_view(records: list[dict], mission_id: str | None) -> dict:
    """The mission_started data, or just the id while there is no mission yet (planning, a failed plan)."""
    for rec in records:
        if rec["type"] == "mission_started":
            return rec["data"]
    if mission_id is None:
        raise ValueError("ledger has no mission_started record")
    return {"id": mission_id}


def _natural_key(mission_id: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part for part in _NATURAL_SPLIT.split(mission_id)]


def _parent_of(cfg: Config, child_id: str) -> str | None:
    try:
        parent = read_parent(cfg, child_id)
    except (MissionError, OSError, UnicodeDecodeError):
        return None
    if parent is not None:
        return parent
    try:
        raw = json.loads(cfg.mission_file(child_id).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    parent = raw.get("parent") if isinstance(raw, dict) else None
    return parent if valid_id(parent) else None


def _created_ns(cfg: Config, child_id: str) -> int:
    for path in (cfg.parent_file(child_id), cfg.mission_file(child_id), cfg.mission_dir(child_id)):
        try:
            return path.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            continue
    return 0


def followup_ids(cfg: Config, mission_id: str) -> list[str]:
    """Follow-up missions whose parent is `mission_id`, in creation order."""
    try:
        entries = os.listdir(cfg.state / "missions")
    except FileNotFoundError:
        return []
    found = []
    for name in entries:
        if name != mission_id and valid_id(name) and cfg.mission_dir(name).is_dir():
            if _parent_of(cfg, name) == mission_id:
                found.append((_created_ns(cfg, name), _natural_key(name), name))
    found.sort()
    return [name for _, _, name in found]


def make_briefing(records: list[dict], decisions: list[str], mission_id: str | None = None,
                  request: str | None = None, followups: list[str] | tuple[str, ...] = ()) -> Briefing:
    if not records or records[0]["type"] not in ("mission_started", "plan_started"):
        raise ValueError("ledger has no mission_started or plan_started record")
    return bf.build(records, public_view(records, mission_id), decisions, request, followups)


def render(records: list[dict], decisions: list[str], mission_id: str | None = None,
           request: str | None = None, followups: list[str] | tuple[str, ...] = ()) -> str:
    """mission_id and request are only needed for a plan that failed before mission_started was written."""
    return render_briefing(make_briefing(records, decisions, mission_id, request, followups))


def _read_request(cfg: Config, mission_id: str) -> str | None:
    try:
        return cfg.request_file(mission_id).read_text(encoding="utf-8")
    except OSError:
        return None

@dataclass(frozen=True)
class Report:
    briefing: Briefing
    text: str


def build(cfg: Config, mission_id: str, records: list[dict]) -> Report:
    """The briefing and report.md text of these records, exactly as write_report writes it."""
    b = make_briefing(records, read_decisions(cfg.workdir(mission_id)), mission_id,
                      _read_request(cfg, mission_id), followup_ids(cfg, mission_id))
    return Report(b, render_briefing(b))


def load_briefing(cfg: Config, mission_id: str) -> Briefing | None:
    """The current briefing of a mission from its ledger (verified first; LedgerError when broken), or None when
    the mission has no ledger records yet."""
    path = cfg.ledger_file(mission_id)
    if not path.exists():
        return None
    records = Ledger(path).read()
    return build(cfg, mission_id, records).briefing if records else None


def write_report(cfg: Config, mission_id: str, records: list[dict]) -> Path:
    path = cfg.report_file(mission_id)
    text = build(cfg, mission_id, records).text
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path
