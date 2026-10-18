"""The iteration loop. All state is a fold over the ledger, so a killed run resumes exactly."""

import fcntl
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from agentic import (admin, canary, checks, commands, config, faults, llm, mutation, notify, outage, planner, ratchet,
                     report, review, worker)
from agentic.config import OUTAGE_ALERT_SECONDS, REVIEW_OUTAGE_MAX_SECONDS, Config
from agentic.i18n import t
from agentic.ledger import Ledger, LedgerError, parse_ts, utcnow
from agentic.mission import Mission, load_mission, parse_mission

FAILING, PASSING, INVALID, GAVE_UP = "failing", "passing", "invalid_check", "gave_up"


class MissionBusy(RuntimeError):
    """Another supervisor process is working on this mission."""


@contextmanager
def mission_lock(cfg: Config, mission_id: str) -> Iterator[None]:
    """Exclusive, non-blocking flock on <mission folder>/lock for as long as the block runs.

    The descriptor is not inheritable (PEP 446), so workers and checks never hold the lock, and the
    kernel drops it when the process dies, however it dies.
    """
    fd = os.open(cfg.lock_file(mission_id), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MissionBusy(f"mission {mission_id!r} is already running (locked: {cfg.lock_file(mission_id)})")
        yield
    finally:
        os.close(fd)


class LoopState:
    """Everything the loop knows, rebuilt by applying ledger records in order."""

    def __init__(self) -> None:
        self.started: datetime | None = None
        self.criteria: list[tuple[str, str]] = []  # (id, text) in mission order
        self.limits: dict = {}
        self.ladder: list[dict] = []
        self.goal = ""
        self.baseline_done = False
        self.inherited_pass = False  # a follow-up's baseline found criteria already passing on the inherited folder
        self.status: dict[str, str] = {}
        self.step = 0
        self.stalls = 0
        self.best: int | None = None  # size of the smallest failing set so far
        self.seen: dict[frozenset, list[int]] = {}  # failing set -> iterations that produced it
        self.oscillating = False  # the latest iteration repeated a non-consecutive earlier set
        self.iteration = 0  # number of the latest started iteration
        self.active: list[str] = []  # criteria checked in that iteration
        self.results: dict[str, bool] = {}  # their check results so far
        self.pending: str | None = None  # "escalate" | "give_up" | "review_veto": decided, not yet recorded
        self.note: str | None = None
        self.approach: str | None = None  # planned missions only
        self.plan = planner.PlanState()
        self.vetoes = 0  # review_veto records so far
        self.findings: list[dict] = []  # vetoing findings shown to the worker until the next review
        self.reviewed_iteration: int | None = None  # the iteration the latest review looked at
        self.pending_findings: list[dict] = []  # verified findings of a review whose veto is not yet recorded
        self.finished = False
        self.final_status: str | None = None
        # Model outages (SPEC-D section 6).
        self.outage_started: datetime | None = None  # first worker_unavailable of the current worker outage
        self.outage_attempts = 0  # worker_unavailable records in the current outage
        self.outage_waits = 0  # of those, the ones followed by a wait
        self.outage_alert_seq: int | None = None  # the record whose notification announces the current outage
        self.next_model: str | None = None  # alternate provider for the next attempt; None: the step's model
        self.wait_until: datetime | None = None  # no worker or review call before this moment
        self.review_attempts = 0  # plan_unavailable records of the review since the latest review record
        self.review_waited = 0  # their waits, in seconds
        # Canary and mutation score (SPEC-F sections 2 and 3).
        self.manifest: dict[str, str] = {}  # baseline worker folder: relative path -> sha256 (or link text)
        self.canary_failed: set[str] = set()  # criteria whose latest canary showed a pass without the worker's files
        self.canary_iteration: int | None = None  # the iteration the latest canary looked at (None: none yet)
        self.awaiting_canary = False  # the latest iteration ended with nothing failing; its canary is not recorded
        self.mutations: list[dict] = []  # data of the mutation records
        self.mutation_due = False  # the canary held for every criterion and a (first or second) score is wanted
        self.strengthened = False  # checks_strengthened was recorded (at most once per mission)
        self.strengthen_ok = False  # ... and it replaced the checks
        self.rechecking = False  # the current checks are the strengthened ones run on the same iteration's work
        self.hygiene: list[str] = []  # worker files the latest `hygiene` record lists (removed from check snapshots)
        self.updates_recorded: set[str] = set()  # criteria with a checks_update record (SPEC-H section 2)
        self.update_rejected = False  # the ratchet rejected the strengthened checks (checks_update_rejected)

    def apply(self, record: dict) -> None:
        getattr(self, "_on_" + record["type"])(record)

    def failing(self) -> list[str]:
        return [cid for cid, _ in self.criteria if self.status.get(cid) == FAILING]

    def remaining(self) -> list[str]:
        return [cid for cid, _ in self.criteria if self.status.get(cid) in (FAILING, PASSING)]

    def missing_checks(self) -> list[str]:
        return [cid for cid in self.active if cid not in self.results]

    def _on_mission_started(self, record: dict) -> None:
        data = record["data"]
        self.started = parse_ts(record["ts"])
        self.goal = data["goal"]
        self.criteria = [(c["id"], c["text"]) for c in data["criteria"]]
        self.limits = data["limits"]
        self.ladder = data["ladder"]
        self.approach = data.get("approach")

    def _on_plan_started(self, record: dict) -> None:
        self.plan.apply(record)

    _on_plan_step = _on_plan_finished = _on_plan_failed = _on_plan_filter = _on_plan_started

    def _on_baseline(self, record: dict) -> None:
        data = record["data"]
        results, inherited = data["results"], set(data.get("inherited_pass", []))  # old ledgers have no such key
        self.inherited_pass = bool(inherited)
        self.manifest = data.get("manifest", {})  # old ledgers: every file counts as written by the worker
        for cid, _ in self.criteria:
            if cid in inherited:
                self.status[cid] = PASSING
            else:
                self.status[cid] = INVALID if results[cid]["passed"] else FAILING
        self.best = len(self.failing())
        self.baseline_done = True

    def _on_iteration_worker(self, record: dict) -> None:
        data = record["data"]
        self.iteration = data["iteration"]
        self.active = list(data["active"])
        self.results = {}
        self.note = data["note"]
        self.outage_started, self.outage_attempts, self.outage_waits = None, 0, 0
        self.outage_alert_seq, self.next_model, self.wait_until = None, None, None

    def _on_worker_unavailable(self, record: dict) -> None:
        """No checks, no stall, same iteration number and note: only the outage bookkeeping changes."""
        data, moment = record["data"], parse_ts(record["ts"])
        if self.outage_started is None:
            self.outage_started, self.outage_alert_seq = moment, None
        self.outage_attempts = data["attempt"]
        if data["wait_seconds"]:
            self.outage_waits += 1
        self.next_model = data["next_model"]
        self.wait_until = moment + timedelta(seconds=data["wait_seconds"])
        if self.outage_alert_seq is None and (moment - self.outage_started).total_seconds() >= OUTAGE_ALERT_SECONDS:
            self.outage_alert_seq = record["seq"]

    def _on_plan_unavailable(self, record: dict) -> None:
        data = record["data"]
        if data["step"] != "review":
            self.plan.apply(record)
            return
        self.review_attempts = data["attempt"]
        self.review_waited += data["wait_seconds"]
        self.wait_until = parse_ts(record["ts"]) + timedelta(seconds=data["wait_seconds"])

    def _on_check_result(self, record: dict) -> None:
        data = record["data"]
        self.status[data["criterion"]] = PASSING if data["passed"] else FAILING
        self.results[data["criterion"]] = data["passed"]
        if not data["passed"]:
            self.canary_failed.discard(data["criterion"])  # failing for its own reason now
        if self.missing_checks():
            return
        if self.rechecking:  # strengthened checks on work already judged: statuses change, no new iteration result
            self.rechecking = False
        elif all(self.results.values()):
            self.awaiting_canary = True  # the iteration's result is known once the canary ran
        else:
            self._finish_iteration(frozenset())

    def _finish_iteration(self, unproven: frozenset) -> None:
        """`unproven`: criteria whose check passed but whose canary did not hold; they count as failing."""
        failing = frozenset(cid for cid in self.active if not self.results[cid]) | unproven
        if self.best is None or len(failing) < self.best:
            self.best = len(failing)
            self.stalls = 0
        else:
            self.stalls += 1
        # Same (non-empty) set as an earlier iteration that was not the immediately preceding one.
        self.oscillating = bool(failing) and any(n < self.iteration - 1 for n in self.seen.get(failing, []))
        self.seen.setdefault(failing, []).append(self.iteration)
        escalate_after, give_up_after = self.limits["escalate_after"], self.limits["give_up_after"]
        on_last_step = self.step + 1 >= len(self.ladder)
        if failing and self.stalls >= escalate_after and not on_last_step:
            self.pending = "escalate"
        elif failing and on_last_step and self.stalls >= escalate_after + give_up_after:
            self.pending = "give_up"

    def _on_canary(self, record: dict) -> None:
        data = record["data"]
        unproven = frozenset(cid for cid, r in data["results"].items() if not r["held"])
        for cid in data["results"]:
            if cid in unproven:
                self.canary_failed.add(cid)
                self.status[cid] = FAILING  # no longer counts as achieved
            else:
                self.canary_failed.discard(cid)
        self.canary_iteration = data["iteration"]
        if self.awaiting_canary:
            self.awaiting_canary = False
            self._finish_iteration(unproven)
        elif data["iteration"] == 0:  # a follow-up's inherited passes: those that did not hold are failing again
            self.best = len(self.failing())
        wanted = not self.mutations or (self.strengthen_ok and len(self.mutations) == 1)
        self.mutation_due = not unproven and wanted

    def _on_mutation(self, record: dict) -> None:
        self.mutations.append(record["data"])
        self.mutation_due = False
        if len(self.mutations) == 1 and not self.strengthened and mutation.weak(record["data"]):
            self.pending = "strengthen"

    def _on_checks_strengthened(self, record: dict) -> None:
        self.strengthened, self.pending = True, None
        self.strengthen_ok = record["data"]["ok"]
        if self.strengthen_ok:  # run the new checks on the current work, then the canary and the score again
            self.active, self.results, self.rechecking = self.remaining(), {}, True
            self.canary_iteration = None

    def _on_checks_update(self, record: dict) -> None:
        self.updates_recorded.add(record["data"]["criterion"])

    def _on_checks_update_rejected(self, record: dict) -> None:
        self.update_rejected = True

    def _on_notice(self, record: dict) -> None:
        pass

    def _on_hygiene(self, record: dict) -> None:
        self.hygiene = list(record["data"]["files"])

    def _on_escalated(self, record: dict) -> None:
        self.step = record["data"]["to_step"]
        self.stalls = 0
        self.pending = None

    def _on_gave_up(self, record: dict) -> None:
        for cid in record["data"]["criteria"]:
            self.status[cid] = GAVE_UP
        self.pending = None

    def _on_stopped_time(self, record: dict) -> None:
        pass

    def _on_review(self, record: dict) -> None:
        data = record["data"]
        self.reviewed_iteration = data["iteration"]
        self.findings = []
        self.review_attempts, self.review_waited, self.wait_until = 0, 0, None
        verified = [f for f in data["findings"] if f["verified"]]
        if data["ok"] and verified and self.vetoes < review.MAX_VETOES:
            self.pending, self.pending_findings = "review_veto", verified

    def _on_review_veto(self, record: dict) -> None:
        self.vetoes += 1
        self.findings = record["data"]["findings"]
        self.pending, self.pending_findings = None, []
        for finding in self.findings:  # a criterion the reviewer caught cheating is failing again
            if self.status.get(finding["criterion"]) == PASSING:
                self.status[finding["criterion"]] = FAILING

    def _on_mission_finished(self, record: dict) -> None:
        self.finished = True
        self.final_status = record["data"]["status"]


Emit = Callable[[str, dict], None]


class Runner:
    def __init__(self, cfg: Config, mission: Mission, state: LoopState, clock: Callable[[], datetime],
                 sleep: Callable[[float], None], emit: Emit):
        self.cfg, self.mission, self.state, self.clock, self.sleep = cfg, mission, state, clock, sleep
        self.emit = emit  # append to the ledger, apply to `state`, notify
        self.workdir = cfg.workdir(mission.id)

    def check(self, iteration: int, criterion_id: str) -> checks.CheckResult:
        return checks.run_check(self.cfg, self.mission.id, iteration, criterion_id,
                                self.mission.criterion(criterion_id).check, self.workdir)

    def record_hygiene(self) -> None:
        """SPEC-H section 1: list the worker files every check snapshot drops because they could hijack a check.
        Recorded when the list differs from the latest record, so a resumed run does not repeat it."""
        found = checks.hijacking_files(self.workdir)
        if found != self.state.hygiene:
            self.emit("hygiene", {"iteration": self.state.iteration, "files": found})

    def baseline(self) -> None:
        """Red baseline: every check must fail on the initial worker folder. A follow-up starts from its parent's
        result: a check that passes there is already achieved (`inherited_pass`), not invalid, once the canary
        holds. The manifest of the folder lets the canary tell the worker's files from those that were there
        before; a follow-up keeps the parent's empty start, so every inherited file counts as the worker's."""
        self.record_hygiene()
        results = {c.id: self.check(0, c.id).to_data() for c in self.mission.criteria}
        passed = [cid for cid, r in results.items() if r["passed"]]
        if self.mission.parent is None:
            self.emit("baseline", {"results": results, "invalid": passed,
                                   "manifest": canary.manifest(self.workdir)})
        else:
            self.emit("baseline", {"results": results, "invalid": [], "inherited_pass": passed, "manifest": {}})

    def finish_missing_checks(self) -> None:
        self.record_hygiene()
        for cid in self.state.missing_checks():
            self.emit("check_result", {"iteration": self.state.iteration, "criterion": cid,
                                       **self.check(self.state.iteration, cid).to_data()})

    def resolve_pending(self) -> None:
        s = self.state
        if s.pending == "review_veto":
            self.emit("review_veto", {"iteration": s.iteration,
                                      "findings": review.veto_findings(self.mission, s.pending_findings)})
        elif s.pending == "escalate":
            to_step = s.step + 1
            self.emit("escalated", {"iteration": s.iteration, "from_step": s.step, "to_step": to_step,
                                    "model": s.ladder[to_step]["model"], "stalls": s.stalls})
        elif s.pending == "strengthen":
            self.strengthen()
        else:
            self.emit("gave_up", {"iteration": s.iteration, "criteria": s.failing(), "attempts": s.iteration})

    def passing(self) -> list[str]:
        return [cid for cid, _ in self.state.criteria if self.state.status.get(cid) == PASSING]

    def canary_due(self) -> bool:
        """After an iteration whose checks all passed; after the strengthened checks passed; before a mission ends
        with passes the canary has not seen; and for a follow-up's inherited passes right after the baseline."""
        s = self.state
        if s.awaiting_canary:
            return True
        if s.iteration == 0:
            return s.inherited_pass and s.canary_iteration is None
        return not s.failing() and s.canary_iteration != s.iteration and bool(self.passing())

    def run_canary(self) -> None:
        data = canary.run(self.cfg, self.mission, self.workdir, self.passing(), self.state.manifest)
        self.emit("canary", {"iteration": self.state.iteration, **data})

    def run_mutation(self, python_files: list[str]) -> None:
        data = mutation.run(self.cfg, self.mission, self.workdir, self.passing(), python_files, self.clock)
        self.emit("mutation", {"iteration": self.state.iteration, **data})

    def killed_mutants(self) -> list[mutation.Mutant]:
        """The mutants of the first mutation score that the old checks killed, generated again (deterministic; the
        worker folder has not changed since)."""
        details = self.state.mutations[0]["details"]
        files = canary.changed_python(self.workdir, self.state.manifest)
        return [m for m, d in zip(mutation.generate(self.workdir, files, self.mission.id), details)
                if d["killed"] and (m.file, m.line, m.operator) == (d["file"], d["line"], d["operator"])]

    def strengthen(self) -> None:
        """Weak checks: the planner writes stronger ones once per mission (SPEC-F section 3). The ratchet (SPEC-H
        section 2) accepts the change only when the checks did not get weaker; then each changed check is recorded
        as `checks_update`, the new checks replace the old ones in mission.json and run on the current work.
        Otherwise the old checks stay and `checks_update_rejected` lists the violations."""
        s = self.state
        survivors = [{k: d[k] for k in ("file", "line", "operator")} for d in s.mutations[0]["details"]
                     if d["killed"] is False]
        run = planner.Planning(self.cfg, s.plan, self.emit, self.clock, self.sleep)
        try:
            result = planner.strengthen(run, self.mission, survivors)
        except llm.StepFailed as exc:
            self.emit("checks_strengthened", {"iteration": s.iteration, "ok": False, "replaced": [], "kept": [],
                                              "error": planner.public_reason(exc)})
            return
        new = parse_mission(result.raw)
        changed = ratchet.changed(self.mission, new)
        if not s.update_rejected and not s.updates_recorded:  # a resumed run keeps the verdict already recorded
            found = ratchet.violations(self.cfg, self.mission, new, result.reasons, self.workdir,
                                       self.killed_mutants())
            if found:
                self.emit("checks_update_rejected", {"iteration": s.iteration, "criteria": changed,
                                                     "violations": found})
        if s.update_rejected:
            self.emit("checks_strengthened", {"iteration": s.iteration, "ok": False, "replaced": [],
                                              "kept": [c.id for c in new.criteria], "error": "ratchet"})
            return
        for cid in changed:
            if cid not in s.updates_recorded:
                self.emit("checks_update", {
                    "iteration": s.iteration, "criterion": cid,
                    "old_sha256": ratchet.check_sha256(self.mission.criterion(cid).check),
                    "new_sha256": ratchet.check_sha256(new.criterion(cid).check),
                    "reason": result.reasons.get(cid, ratchet.DEFAULT_REASON)})
        planner.write_mission_file(self.cfg.mission_file(self.mission.id), result.raw)
        self.mission = new
        self.emit("checks_strengthened", {"iteration": s.iteration, "ok": True, "replaced": result.replaced,
                                          "kept": result.kept})

    def deadline(self) -> datetime:
        return self.state.started + timedelta(hours=self.state.limits["mission_hours"])

    def time_is_up(self) -> bool:
        return self.clock() >= self.deadline()

    def finish(self, status: str) -> None:
        self.emit("mission_finished", {"status": status, "iterations": self.state.iteration})

    def iterate(self) -> None:
        s = self.state
        step = s.ladder[s.step]
        model = s.next_model or step["model"]  # the alternate provider is used for one attempt only
        status = {cid: s.status[cid] for cid in s.remaining()}
        requirements = [(cid, text, status[cid] == PASSING) for cid, text in s.criteria if cid in status]
        prompt = worker.build_prompt(s.goal, requirements, s.note, s.oscillating, s.approach, s.findings,
                                     [cid for cid, _ in s.criteria if cid in s.canary_failed])
        before = outage.folder_digest(self.workdir)
        result = worker.run_worker(self.cfg, model, step["thinking"], step["iteration_minutes"], prompt, self.workdir)
        if result.leftovers:
            files = list(result.leftovers)
            self.emit("notice", {"kind": "worker_leftover", "iteration": s.iteration + 1, "files": files,
                                 "text": t("loop.worker_leftover_notice", files=", ".join(files))})
        reason = outage.worker_reason(result.exit_code, result.timed_out, result.stderr, result.duration,
                                      lambda: outage.folder_digest(self.workdir) == before)
        common = {"iteration": s.iteration + 1, "step": s.step, "model": model, "thinking": step["thinking"],
                  "exit_code": result.exit_code, "timed_out": result.timed_out,
                  "duration_seconds": round(result.duration, 3), "note": result.note, "stderr": result.stderr}
        if reason:
            alternate = config.ALTERNATE_PROVIDER.get(model) if model == step["model"] else None
            self.emit("worker_unavailable", {
                **common, "reason": reason, "attempt": s.outage_attempts + 1, "next_model": alternate,
                "wait_seconds": 0 if alternate else outage.wait_seconds(s.outage_waits),
            })
            return
        self.emit("iteration_worker", {**common, "iteration_minutes": step["iteration_minutes"],
                                       "active": list(status)})

    def review_due(self) -> bool:
        """Planned missions get a cheat review once an iteration ended with nothing failing. A follow-up whose
        inherited files already pass at baseline is reviewed at iteration 0."""
        s = self.state
        return (s.approach is not None and (s.iteration > 0 or s.inherited_pass)
                and s.reviewed_iteration != s.iteration
                and any(v == PASSING for v in s.status.values()))

    def run_review(self) -> None:
        """One review call. A reviewer outage waits (at most REVIEW_OUTAGE_MAX_SECONDS in total), then the
        review is recorded as failed like any other failed review."""
        s = self.state
        data, outage_line = review.run_review(self.cfg, self.mission, str(self.workdir))
        if outage_line and s.review_waited < REVIEW_OUTAGE_MAX_SECONDS:
            wait = min(outage.wait_seconds(s.review_attempts), REVIEW_OUTAGE_MAX_SECONDS - s.review_waited)
            self.emit("plan_unavailable", {"step": "review", "round": 0, "iteration": s.iteration,
                                           "attempt": s.review_attempts + 1, "reason": outage_line,
                                           "wait_seconds": wait})
            return
        self.emit("review", {"iteration": s.iteration, **data})

    def advance(self) -> None:
        """Perform the next missing piece of work; the ledger decides what that is."""
        s = self.state
        if s.missing_checks():
            self.finish_missing_checks()
        elif s.pending:
            self.resolve_pending()
        elif self.canary_due():
            self.run_canary()
        elif not s.failing() and s.mutation_due and (python := canary.changed_python(self.workdir, s.manifest)):
            self.run_mutation(python)
        elif not s.failing() and self.review_due():
            if not outage.sleep_until(s.wait_until, self.clock, self.sleep):
                self.run_review()
        elif not s.failing():
            self.finish("complete" if all(v == PASSING for v in s.status.values()) else "partial")
        elif self.time_is_up():
            elapsed = (self.clock() - s.started).total_seconds()
            self.emit("stopped_time", {"iteration": s.iteration, "elapsed_seconds": round(elapsed, 3)})
            self.finish("stopped_time")
        elif not outage.sleep_until(s.wait_until, self.clock, self.sleep, limit=self.deadline()):
            self.iterate()


def run(mission_id: str, cfg: Config, clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep) -> str:
    """Run (or resume) a mission to its end, write the report, return the final status."""
    if not cfg.mission_file(mission_id).exists() and not cfg.request_file(mission_id).exists():
        _load(cfg, mission_id)  # raises the usual "cannot read mission" error
    with mission_lock(cfg, mission_id):
        # Holding the lock, nothing else can be using scratch space: drop snapshots a killed run left.
        shutil.rmtree(cfg.scratch_root(mission_id), ignore_errors=True)
        return _run_locked(mission_id, cfg, clock, sleep)


def _run_locked(mission_id: str, cfg: Config, clock: Callable[[], datetime], sleep: Callable[[float], None]) -> str:
    ledger = Ledger(cfg.ledger_file(mission_id), clock)
    records = ledger.read()  # verifies the chain first
    state = LoopState()
    for record in records:
        state.apply(record)
    notifier = notify.Notifier(cfg, mission_id, ledger)

    def emit(record_type: str, data: dict) -> None:
        record = ledger.append(record_type, data)
        state.apply(record)
        faults.point(faults.APPLIED, record)
        notifier.on_record(record, state)  # best effort; never raises
        if record["seq"] == 1:
            make_append_only(ledger.path, emit)

    plan = state.plan
    if plan.failed:
        mission = None
    elif (plan.started and not plan.finished) or (not plan.started and not cfg.mission_file(mission_id).exists()):
        # A request without a mission file, or planning that was interrupted: plan (or finish planning).
        mission = planner.plan(cfg, mission_id, plan, emit, clock, sleep)
    else:
        mission = _load(cfg, mission_id)
    if mission is None:  # planning failed: there is no mission to run
        if not state.finished:
            emit("mission_finished", {"status": "plan_failed", "iterations": 0})
        report.write_report(cfg, mission_id, ledger.read())
        return state.final_status
    admin.ensure_workdir(cfg, mission_id)
    runner = Runner(cfg, mission, state, clock, sleep, emit)
    if state.started is None:
        runner.emit("mission_started", mission.public_view())
    elif [cid for cid, _ in state.criteria] != [c.id for c in mission.criteria]:
        raise LedgerError("the mission file's criteria differ from the ones recorded in the ledger")
    if not state.baseline_done:
        runner.baseline()
    while not state.finished:
        runner.advance()
    report.write_report(cfg, mission_id, ledger.read())
    return state.final_status


def _load(cfg: Config, mission_id: str) -> Mission:
    mission = load_mission(cfg.mission_file(mission_id))
    if mission.id != mission_id:
        raise ValueError(f"mission file declares id {mission.id!r}, expected {mission_id!r}")
    return mission


def __getattr__(name: str):
    if name == "APPEND_ONLY_NOTICE":
        return t("loop.append_only_notice")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def make_append_only(path: Path, emit: Emit) -> None:
    """SPEC-F section 4: a new ledger gets the append-only attribute (through the command runner). A failure is
    recorded as a `notice` and is not fatal."""
    result = commands.run_command(["chattr", "+a", str(path)])
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())[:200]
        emit("notice", {"kind": "append_only", "text": t("loop.append_only_notice"), "exit_code": result.returncode,
                        "detail": detail})
