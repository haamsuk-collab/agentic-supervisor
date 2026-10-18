"""The one briefing model (SPEC-E.md sections 0-2). report.md and every Telegram card are rendered from it.

Deterministic and never AI-written: todo items, decisions, next instructions and remaining criteria come only from the
ledger, the mission's public view and the rule table below. AI-written text (planner assumptions, the worker's
DECISIONS.md lines) is kept apart in `ai_notes` and never enters a todo, a decision or an instruction.
"Achieved" counts only criteria whose latest supervisor-run check exited 0.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from agentic import config
from agentic.config import MODEL_SHORT_NAMES
from agentic.display import display_time
from agentic.i18n import CATALOGS, t
from agentic.ledger import parse_ts
from agentic.worker import one_line

STATES = ("running", "complete", "partial", "stopped_time", "plan_failed", "planning")
ACHIEVED, FAILED, UNVERIFIABLE = "✅", "❌", "⚠️"
REASON_MAX_CHARS = 300
PLAN_STEPS = ("brief", "design", "checks", "critique")

_COMPAT_KEYS = {
    "NO_TODO": "briefing.todo.none",
    "RUNNING_TODO": "briefing.todo.running",
    "DECISION_TODO": "briefing.todo.decision_one",
    "INVALID_REASON": "briefing.reason.invalid",
    "DROPPED_REASON": "briefing.reason.dropped",
    "GAVE_UP_REASON": "briefing.reason.gave_up",
    "TIME_REASON": "briefing.reason.time",
    "FAILING_REASON": "briefing.reason.failing",
    "CANARY_REASON": "briefing.reason.canary",
    "STRENGTHEN_NOTICE": "briefing.notice.strengthen",
    "STRENGTHEN_WAITING_NOTICE": "briefing.notice.strengthen_waiting",
    "STRENGTHEN_FAILED_NOTICE": "briefing.notice.strengthen_failed",
    "SUSPECT_REASON": "briefing.reason.suspect",
    "REVIEW_FAILED_NOTICE": "briefing.notice.review_failed",
    "OUTAGE_NOTICE": "briefing.notice.outage",
    "PARENT_NOTICE": "briefing.notice.parent",
    "PLAN_FAILED_NOTICE": "briefing.notice.plan_failed",
    "PLAN_RETRY_LINE": "briefing.plan_retry_line",
}


def headline(state: str) -> str:
    return t(f"briefing.headline.{state}")


def no_todo() -> str:
    return t("briefing.todo.none")


def running_todo() -> str:
    return t("briefing.todo.running")


def __getattr__(name: str):
    if name == "HEADLINES":
        return {s: headline(s) for s in STATES}
    if name in _COMPAT_KEYS:
        return CATALOGS[config.LANGUAGE][_COMPAT_KEYS[name]]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True)
class Decision:
    subject: str
    facts: list[str]
    options: list[tuple[str, str]]  # (key, text)
    recommended: str
    reason: str


@dataclass(frozen=True)
class NextInstruction:
    label: str
    text: str  # exactly what the owner copies and sends to the bot


@dataclass(frozen=True)
class Item:
    """One criterion: `mark` is ✅, ❌ or ⚠️. `reason` says why it is not achieved (for ✅: a review suspicion or "")."""
    mark: str
    id: str
    text: str
    reason: str
    last_check: str | None = None  # KST time of the latest supervisor-run check
    exit_code: int | None = None
    attempts: int = 0  # check_result records of this criterion


@dataclass(frozen=True)
class AiNotes:
    """AI-written, unverified: the planner's assumptions and the worker's DECISIONS.md lines, kept apart."""
    assumptions: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.assumptions) + len(self.decisions)


@dataclass(frozen=True)
class Briefing:
    mission_id: str
    state: str
    headline: str
    achieved: int
    total: int
    percent: int
    duration: int  # minutes, first to last ledger record
    iterations: int
    models_used: list[str]
    ended_at: str | None  # KST; None while the mission runs
    updated_at: str  # KST of the latest ledger record
    todo: list[str]
    decisions: list[Decision]
    next_instructions: list[NextInstruction]
    remaining: list[Item]
    notices: list[str]
    ai_notes: AiNotes
    evidence: list[Item]
    request: str | None = None  # planned missions: the request text
    parent: str | None = None  # follow-up missions: the parent mission id
    current_model: str | None = None  # while running: the model of the current step
    checked_at: str | None = None  # display time of the latest supervisor-run check evidence (SPEC-H section 8)
    evidence_line: str | None = None  # "감독자 검사 기준 <KST>" (SPEC-H section 8)
    followups: list[str] = field(default_factory=list)  # follow-up mission ids in creation order

    @property
    def evidence_at(self) -> str | None:
        return self.checked_at

    @property
    def evidence_basis(self) -> str | None:
        return self.evidence_line

    @property
    def finished(self) -> bool:
        return self.state not in ("running", "planning")


def short_model(model: str) -> str:
    return MODEL_SHORT_NAMES.get(model, model.rsplit("/", 1)[-1])


def hours_minutes(minutes: int) -> str:
    return t("briefing.duration", h=minutes // 60, m=minutes % 60)


def kst(ts: str) -> str:
    return display_time(parse_ts(ts))


# ---- reducing the ledger ----------------------------------------------------------------------------------

@dataclass
class _Facts:
    view: dict
    planned: bool = False
    mission_started: bool = False
    plan_failed: dict | None = None
    plan_step: dict | None = None  # the latest plan_step record's data
    brief: dict | None = None
    dropped: list[dict] = field(default_factory=list)
    invalid: set[str] = field(default_factory=set)
    gave_up: dict[str, int] = field(default_factory=dict)
    last_check: dict[str, dict] = field(default_factory=dict)  # criterion -> {"ts", "passed", "exit_code"}
    attempts: Counter = field(default_factory=Counter)
    iterations: int = 0
    models: list[str] = field(default_factory=list)
    step: int = 0
    stopped: bool = False
    final: str | None = None
    finished_ts: str | None = None
    suspect: list[dict] = field(default_factory=list)
    review_failed: bool = False
    waited: float = 0.0  # seconds really spent waiting for an unavailable model
    canary_failed: set[str] = field(default_factory=set)  # the latest canary of these passed without the files
    unproven_inherited: set[str] = field(default_factory=set)  # inherited passes no canary has confirmed yet
    mutations: list[dict] = field(default_factory=list)  # data of the mutation records
    strengthened: dict | None = None  # data of the checks_strengthened record
    hygiene: list[str] = field(default_factory=list)  # worker files dropped from check snapshots (SPEC-H section 1)
    evidence_ts: str | None = None  # ts of the latest supervisor-run check evidence (check_result or canary)


def _reduce(records: list[dict], view: dict) -> _Facts:
    f = _Facts(view)
    for index, rec in enumerate(records):
        kind, data = rec["type"], rec["data"]
        if kind == "mission_started":
            f.mission_started = True
            if not f.view.get("criteria"):
                f.view = {**data, **f.view, "criteria": data["criteria"]}
        elif kind == "plan_started":
            f.planned = True
        elif kind == "plan_step":
            f.plan_step = data
            if data["ok"] and data["step"] == "brief":
                f.brief = data["answer"]
        elif kind == "plan_finished":
            f.dropped = list(data["dropped"])
        elif kind == "plan_failed":
            f.plan_failed = data
        elif kind == "baseline":
            f.invalid = set(data["invalid"])
            f.unproven_inherited = set(data.get("inherited_pass", []))  # they count once the canary held
            for cid in data.get("inherited_pass", []):  # a follow-up's inherited pass is a supervisor-run check
                result = data["results"][cid]
                f.last_check[cid] = {"ts": rec["ts"], "passed": result["passed"], "exit_code": result["exit_code"]}
                f.evidence_ts = rec["ts"]
        elif kind == "iteration_worker":
            f.iterations += 1
            model = short_model(data["model"])
            if model not in f.models:
                f.models.append(model)
        elif kind == "check_result":
            f.evidence_ts = rec["ts"]
            f.attempts[data["criterion"]] += 1
            f.last_check[data["criterion"]] = {"ts": rec["ts"], "passed": data["passed"],
                                               "exit_code": data["exit_code"]}
            f.unproven_inherited.discard(data["criterion"])
            if not data["passed"]:
                f.canary_failed.discard(data["criterion"])  # failing for its own reason now
        elif kind == "canary":
            f.evidence_ts = rec["ts"]
            for cid, result in data["results"].items():
                f.unproven_inherited.discard(cid)
                if result["held"]:
                    f.canary_failed.discard(cid)
                else:
                    f.canary_failed.add(cid)
        elif kind == "mutation":
            f.mutations.append(data)
        elif kind == "checks_strengthened":
            f.strengthened = data
        elif kind == "hygiene":
            f.hygiene += [rel for rel in data["files"] if rel not in f.hygiene]
        elif kind == "escalated":
            f.step = data["to_step"]
        elif kind == "gave_up":
            f.gave_up.update({cid: data["attempts"] for cid in data["criteria"]})
        elif kind == "stopped_time":
            f.stopped = True
        elif kind == "review":
            f.review_failed = not data["ok"]
            f.suspect = [x for x in data["findings"] if x["verified"]]
        elif kind == "review_veto":
            f.suspect = []
            for finding in data["findings"]:  # failing again until a new check passes
                f.last_check.pop(finding["criterion"], None)
        elif kind == "mission_finished":
            f.final = data["status"]
            f.finished_ts = rec["ts"]
        if kind in ("worker_unavailable", "plan_unavailable") and data["wait_seconds"]:
            # The planned wait, cut at the next record (a deadline can end a wait early); an open wait counts 0.
            if index + 1 < len(records):
                gap = (parse_ts(records[index + 1]["ts"]) - parse_ts(rec["ts"])).total_seconds()
                f.waited += max(0.0, min(float(data["wait_seconds"]), gap))
    if f.plan_failed and f.final is None:
        f.final = "plan_failed"
    return f


def _state(f: _Facts) -> str:
    if f.final in ("complete", "partial", "stopped_time", "plan_failed"):
        return f.final
    return "planning" if f.planned and not f.mission_started else "running"


def _passed(check: dict | None) -> bool:
    return bool(check) and check["passed"] and check["exit_code"] == 0


def _items(f: _Facts) -> tuple[list[Item], dict[str, list[Item]]]:
    """Evidence (one item per criterion, dropped ones last) and the non-achieved items by rule condition."""
    suspect: dict[str, list[dict]] = {}
    for finding in f.suspect:
        suspect.setdefault(finding["criterion"], []).append(finding)
    groups: dict[str, list[Item]] = {"gave_up": [], "time": [], "failing": [], "invalid": [], "dropped": [],
                                     "canary": []}
    evidence = []
    for crit in f.view.get("criteria", []):
        cid, text = crit["id"], one_line(crit["text"])
        last = f.last_check.get(cid)
        common = {"last_check": kst(last["ts"]) if last else None,
                  "exit_code": last["exit_code"] if last else None, "attempts": f.attempts[cid]}
        if cid in f.invalid:
            item, group = Item(UNVERIFIABLE, cid, text, t("briefing.reason.invalid"), **common), "invalid"
        elif cid in f.canary_failed:
            item, group = Item(FAILED, cid, text, t("briefing.reason.canary"), **common), "canary"
        elif cid in f.gave_up:
            item, group = Item(FAILED, cid, text, t("briefing.reason.gave_up", n=f.gave_up[cid]), **common), "gave_up"
        elif _passed(last) and cid not in f.unproven_inherited:
            notes = [t("briefing.reason.suspect", file=one_line(x["file"]), quote=one_line(x["quote"]))
                     for x in suspect.get(cid, [])]
            item, group = Item(ACHIEVED, cid, text, " · ".join(notes), **common), None
        elif f.stopped:
            item, group = Item(FAILED, cid, text, t("briefing.reason.time"), **common), "time"
        else:
            item, group = Item(FAILED, cid, text, t("briefing.reason.failing"), **common), "failing"
        evidence.append(item)
        if group:
            groups[group].append(item)
    for d in f.dropped:
        item = Item(UNVERIFIABLE, d["id"], one_line(d["text"]),
                    t("briefing.reason.dropped", problem=one_line(d["problem"])[:REASON_MAX_CHARS]))
        evidence.append(item)
        groups["dropped"].append(item)
    return evidence, groups


# ---- the rule table (SPEC-E section 2) ---------------------------------------------------------------------

@dataclass
class _Ctx:
    """What the rules read, and the lists they fill in table order."""
    id: str
    state: str
    percent: int
    groups: dict[str, list[Item]]
    suspect: list[dict]
    ai_notes: AiNotes
    request: str | None
    plan_failed: dict | None
    review_failed: bool
    waited_minutes: int
    waited: float
    parent: str | None
    current: str
    strengthen: str | None = None  # the strengthening notice (SPEC-F section 3), or None
    hygiene: list[str] = field(default_factory=list)  # worker files dropped from check snapshots
    followups: list[str] = field(default_factory=list)  # follow-up missions of this mission
    todo: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    next_instructions: list[NextInstruction] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    def decide(self, decision: Decision) -> int:
        self.decisions.append(decision)
        self.todo.append(t("briefing.todo.decision_one"))
        return len(self.decisions)


def _running(c: _Ctx) -> None:
    c.todo.append(running_todo())
    c.next_instructions.append(NextInstruction(t("briefing.next.status_label"), f"/status {c.id}"))
    c.notices.append(c.current)


def _complete(c: _Ctx) -> None:
    c.todo.append(t("briefing.todo.get_files", id=c.id))
    c.next_instructions.append(NextInstruction(t("briefing.next.files_label"), f"/files {c.id}"))


def _ai_notes(c: _Ctx) -> None:
    c.todo.append(t("briefing.todo.ai_notes", count=len(c.ai_notes)))
    c.next_instructions.append(NextInstruction(t("briefing.next.ai_notes_label"),
                                               t("briefing.next.ai_notes_text", id=c.id)))


def _gave_up(c: _Ctx) -> None:
    items = c.groups["gave_up"]
    recommended = "A" if c.percent >= 80 else "B"
    reason = (t("briefing.decision.gave_up.reason_a", percent=c.percent) if recommended == "A"
              else t("briefing.decision.gave_up.reason_b", percent=c.percent))
    number = c.decide(Decision(
        t("briefing.decision.gave_up.subject", count=len(items)),
        [f"{i.id} {i.text} — {i.reason}" for i in items],
        [("A", t("briefing.decision.gave_up.option_a")), ("B", t("briefing.decision.gave_up.option_b"))],
        recommended, reason))
    lines = [t("briefing.next.gave_up_line", text=i.text) for i in items]
    lines[0] = t("briefing.next.gave_up_first", id=c.id, line=lines[0])
    c.next_instructions.append(NextInstruction(
        t("briefing.next.choose_option_label", number=number, option="B"), "\n".join(lines)))


def _failed_time(c: _Ctx) -> None:
    items = c.groups["time"]
    number = c.decide(Decision(
        t("briefing.decision.time.subject"), [f"{i.id} {i.text}" for i in items],
        [("A", t("briefing.decision.time.option_a")), ("B", t("briefing.decision.time.option_b"))],
        "A", t("briefing.decision.time.reason", count=len(items))))
    c.next_instructions.append(NextInstruction(
        t("briefing.next.choose_option_label", number=number, option="A"),
        t("briefing.next.time_text", id=c.id)))


def _unverifiable(c: _Ctx) -> None:
    items = c.groups["dropped"] + c.groups["invalid"]
    c.todo.append(t("briefing.todo.unverifiable", count=len(items)))
    c.decisions.append(Decision(
        t("briefing.decision.unverifiable.subject", count=len(items)),
        [f"{i.id} {i.text} — {i.reason}" for i in items],
        [("A", t("briefing.decision.unverifiable.option_a")), ("B", t("briefing.decision.unverifiable.option_b"))],
        "A", t("briefing.decision.unverifiable.reason")))
    c.next_instructions.append(NextInstruction(t("briefing.next.files_label"), f"/files {c.id}"))


def _suspect(c: _Ctx) -> None:
    criteria = list(dict.fromkeys(x["criterion"] for x in c.suspect))
    number = c.decide(Decision(
        t("briefing.decision.suspect.subject", count=len(criteria)),
        [f'{one_line(x["criterion"])} · {one_line(x["file"])}: "{one_line(x["quote"])}"' for x in c.suspect],
        [("A", t("briefing.decision.suspect.option_a")), ("B", t("briefing.decision.suspect.option_b"))], "A",
        t("briefing.decision.suspect.reason")))
    c.next_instructions.append(NextInstruction(
        t("briefing.next.choose_option_label", number=number, option="A"),
        t("briefing.next.suspect_text", id=c.id)))


def _plan_failed(c: _Ctx) -> None:
    c.todo.append(t("briefing.todo.plan_failed"))
    retry_line = t("briefing.plan_retry_line")
    text = f"{c.request.rstrip()}\n{retry_line}" if c.request else retry_line
    c.next_instructions.append(NextInstruction(t("briefing.next.plan_failed_label"), text))
    pf = c.plan_failed or {}
    c.notices.append(t("briefing.notice.plan_failed", step=pf.get("step", "?"),
                       reason=one_line(str(pf.get("reason", "")))[:REASON_MAX_CHARS]))


def _review_failed(c: _Ctx) -> None:
    c.notices.append(t("briefing.notice.review_failed"))


def _outage(c: _Ctx) -> None:
    c.notices.append(t("briefing.notice.outage", h=c.waited_minutes // 60, m=c.waited_minutes % 60))


def _parent(c: _Ctx) -> None:
    c.notices.append(t("briefing.notice.parent", parent=c.parent))


def _followups(c: _Ctx) -> None:
    c.notices.append(t("briefing.notice.followups", followups=", ".join(c.followups)))

def _strengthened(c: _Ctx) -> None:
    c.notices.append(c.strengthen)


def _hygiene(c: _Ctx) -> None:
    c.notices.append(t("briefing.notice.hygiene", files=", ".join(c.hygiene)))


RULES = (  # (condition, effect) in table order; every matching row adds to the lists in this order
    (lambda c: c.state in ("running", "planning"), _running),
    (lambda c: c.state == "complete", _complete),
    (lambda c: len(c.ai_notes) > 0 and c.state not in ("running", "planning"), _ai_notes),
    (lambda c: bool(c.groups["gave_up"]), _gave_up),
    (lambda c: bool(c.groups["time"]), _failed_time),
    (lambda c: bool(c.groups["dropped"] or c.groups["invalid"]), _unverifiable),
    (lambda c: bool(c.suspect), _suspect),
    (lambda c: c.state == "plan_failed", _plan_failed),
    (lambda c: c.review_failed, _review_failed),
    (lambda c: c.waited > 0, _outage),
    (lambda c: c.parent is not None, _parent),
    (lambda c: bool(c.followups), _followups),
    (lambda c: c.strengthen is not None, _strengthened),  # SPEC-F section 3: a notice, never a decision
    (lambda c: bool(c.hygiene), _hygiene),  # SPEC-H section 1
)


def strengthen_notice(f: _Facts) -> str | None:
    """The check-strengthening notice (n = mutants that ran, k = killed) once the checks were
    strengthened; the second score may still be missing, or the strengthening may have failed."""
    if f.strengthened is None or not f.mutations:
        return None
    first = {"n": f.mutations[0]["mutants"] - f.mutations[0].get("skipped", 0), "k": f.mutations[0]["killed"]}
    if not f.strengthened["ok"]:
        return t("briefing.notice.strengthen_failed", **first)
    if len(f.mutations) < 2:
        return t("briefing.notice.strengthen_waiting", **first)
    second = f.mutations[1]
    return t("briefing.notice.strengthen", **first,
             n2=second["mutants"] - second.get("skipped", 0), k2=second["killed"])


def apply_rules(c: _Ctx) -> _Ctx:
    for condition, effect in RULES:
        if condition(c):
            effect(c)
    decision_one = t("briefing.todo.decision_one")
    count = c.todo.count(decision_one)
    if count > 1:  # one line for all decisions, where the first one was
        first = c.todo.index(decision_one)
        c.todo = [item for item in c.todo if item != decision_one]
        c.todo.insert(first, t("briefing.todo.decisions", count=count))
    seen: set[str] = set()
    unique = []
    for instruction in c.next_instructions:  # e.g. "/files <id>" from two rows is shown once
        if instruction.text not in seen:
            seen.add(instruction.text)
            unique.append(instruction)
    c.next_instructions = unique
    return c


def _current(f: _Facts, state: str) -> tuple[str, str | None]:
    """The running notice ("current step, model and iteration") and the current model's short name."""
    if state == "planning":
        step = f.plan_step["step"] if f.plan_step else PLAN_STEPS[0]
        model = short_model(f.plan_step["model"].split(":")[0]) if f.plan_step else None
        tail = t("briefing.current.model_tail", model=model) if model else ""
        return t("briefing.current.planning", step=step, tail=tail), model
    ladder = f.view.get("ladder") or []
    model = short_model(ladder[f.step]["model"]) if f.step < len(ladder) else None
    tail = t("briefing.current.model_tail", model=model) if model else ""
    return t("briefing.current.running", step=f.step + 1, tail=tail, iterations=f.iterations), model

def build(records: list[dict], public_view: dict | None, decisions_lines: list[str],
          request: str | None, followups: list[str] | tuple[str, ...] = ()) -> Briefing:
    """The briefing of one mission. A pure function of its arguments.

    `public_view` is the mission's public view (mission_started data). Before that record exists (planning, a
    failed plan) pass at least {"id": <mission id>}; None takes the view from the mission_started record.
    `decisions_lines` are the worker's DECISIONS.md lines; `request` the request text (planned missions).
    """
    if not records:
        raise ValueError("a briefing needs at least one ledger record")
    view = dict(public_view) if public_view else {}
    f = _reduce(records, view)
    if "id" not in f.view:
        raise ValueError("no mission id: the ledger has no mission_started record and no public view was given")
    mission_id = f.view["id"]
    state = _state(f)
    evidence, groups = _items(f)
    total = len(evidence)
    achieved = sum(1 for item in evidence if item.mark == ACHIEVED)
    percent = round(100 * achieved / total) if total else 0
    assumptions = f.view.get("assumptions")
    if assumptions is None:
        assumptions = f.brief["assumptions"] if f.brief else []
    notes = AiNotes([one_line(a) for a in assumptions], [line.rstrip() for line in decisions_lines])
    request = f.view.get("request") or request
    parent = f.view.get("parent")
    current, current_model = _current(f, state)
    first, last = parse_ts(records[0]["ts"]), parse_ts(records[-1]["ts"])
    waited_minutes = int(f.waited // 60)
    children = [str(cid) for cid in (followups or f.view.get("followups") or ())]
    checked_at = kst(f.evidence_ts) if f.evidence_ts else None
    evidence_line = t("briefing.evidence_basis", time=checked_at) if checked_at else None
    ctx = apply_rules(_Ctx(mission_id, state, percent, groups, f.suspect, notes, request, f.plan_failed,
                           f.review_failed, waited_minutes, f.waited, parent, current, strengthen_notice(f),
                           hygiene=f.hygiene, followups=children))
    remaining = [item for item in evidence if item.mark != ACHIEVED]
    return Briefing(
        mission_id=mission_id, state=state, headline=headline(state), achieved=achieved, total=total,
        percent=percent, duration=_minutes(last - first), iterations=f.iterations, models_used=list(f.models),
        ended_at=kst(f.finished_ts) if f.finished_ts else None, updated_at=kst(records[-1]["ts"]),
        todo=ctx.todo, decisions=ctx.decisions, next_instructions=ctx.next_instructions, remaining=remaining,
        notices=ctx.notices, ai_notes=notes, evidence=evidence, request=request, parent=parent,
        current_model=current_model if state in ("running", "planning") else None,
        checked_at=checked_at, evidence_line=evidence_line, followups=children,
    )


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def bar(percent: int) -> str:
    """10 cells: ▰ for achieved, ▱ for the rest, filled round(percent / 10)."""
    filled = max(0, min(10, round(percent / 10)))
    return "▰" * filled + "▱" * (10 - filled)
