"""Mission administration (SPEC-D.md section 4): start, stop and inspect missions.

Used by `agentic mission ...` (hand, root) and by the inbox processor (Telegram, root). systemctl and runuser
run through `commands.run_command`, which tests replace with a fake.
"""

import contextlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agentic import commands, config, report
from agentic.briefing import ACHIEVED, Briefing
from agentic.config import REQUEST_MAX_BYTES, Config, valid_id
from agentic.display import display_time
from agentic.i18n import t
from agentic.ledger import Ledger, LedgerError, parse_ts
from agentic.mission import MissionError, load_mission, validate_request
from agentic.report import load_briefing

EVENTS_SHOWN = 5
FIELD_MAX_CHARS = 200
DEFAULT_ID_TRIES = 100
ACTIVE_STATES = ("active", "activating")  # `systemctl is-active` states that count as a running mission


class UnknownMission(MissionError):
    def __init__(self, mission_id: str):
        super().__init__(f"unknown mission {mission_id!r}")
        self.mission_id = mission_id


class MissionExists(MissionError):
    pass


class CommandFailed(MissionError):
    """systemctl or runuser exited non-zero."""


class TooManyMissions(MissionError):
    """MAX_ACTIVE_MISSIONS missions are already running."""


class MissionActive(MissionError):
    """The parent of a follow-up is still running."""

    def __init__(self) -> None:
        super().__init__(t("admin.mission_active"))


def require_id(mission_id: str) -> str:
    if not valid_id(mission_id):
        raise MissionError(f"{mission_id!r} is not a valid mission id")
    return mission_id


def _run(argv: list[str]) -> commands.CommandResult:
    result = commands.run_command(argv)
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())[:FIELD_MAX_CHARS]
        raise CommandFailed(f"{' '.join(argv[:2])} failed (exit {result.returncode}): {detail}")
    return result


def _write_new_file(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def create_request(cfg: Config, mission_id: str, raw: bytes) -> None:
    """The mission folder (0700, an existing one is refused) holding request.md (0600). `raw` is validated."""
    folder = cfg.mission_dir(mission_id)
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        raise MissionExists(f"mission {mission_id!r} already exists: {folder}") from None
    _write_new_file(cfg.request_file(mission_id), raw)


def install_mission_file(cfg: Config, path: str | Path) -> str:
    """Validate the mission file `path` and copy it to the mission's state folder (0600).
    An installed mission with different content is refused. Returns the mission id."""
    mission = load_mission(path)
    target = cfg.mission_file(mission.id)
    content = Path(path).read_bytes()
    if target.exists():
        if target.read_bytes() != content:
            raise MissionError(f"{target} already exists and differs from {path}")
    else:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_new_file(target, content)
    return mission.id


def as_worker(argv: list[str]) -> list[str]:
    """argv run as the worker account (SPEC-F section 1): worker folders are made and copied by it, never by root."""
    return ["runuser", "-u", config.WORKER_USER, "--", *argv]


def create_workdir(cfg: Config, mission_id: str) -> None:
    _run(as_worker(["mkdir", "-p", str(cfg.workdir(mission_id))]))


def ensure_workdir(cfg: Config, mission_id: str) -> None:
    """The supervisor's own guarantee that the worker folder exists. With the production worker command it is made
    by the worker user; with an AGENTIC_WORKER_CMD override (tests, hand runs) the worker runs as the current user,
    so the current user makes it."""
    workdir = cfg.workdir(mission_id)
    if workdir.is_dir():
        return
    if cfg.worker_cmd is None:
        create_workdir(cfg, mission_id)
    else:
        workdir.mkdir(parents=True, exist_ok=True)


def _launch(cfg: Config, mission_id: str) -> None:
    """The worker folder is made by the worker user, never by root; then the supervisor unit starts."""
    create_workdir(cfg, mission_id)
    _run(["systemctl", "enable", "--now", f"agentic@{mission_id}.service"])


def _create_with_default_id(cfg: Config, raw: bytes) -> str:
    """create_request under the default id m-<UTC yyyymmdd-hhmmss>; when that folder exists (two requests in one
    second) -2, -3, ... is added. Returns the id."""
    base = datetime.now(timezone.utc).strftime("m-%Y%m%d-%H%M%S")
    for n in range(1, DEFAULT_ID_TRIES + 1):
        mission_id = base if n == 1 else f"{base}-{n}"
        try:
            create_request(cfg, mission_id, raw)
            return mission_id
        except MissionExists:
            continue
    raise MissionExists(f"no free mission id starting with {base}")


def new_mission(cfg: Config, text: str, mission_id: str | None = None) -> str:
    """Create a mission from a plain-language request and start its supervisor. Returns the mission id.

    Refused (TooManyMissions, nothing created) while MAX_ACTIVE_MISSIONS or more mission units are active or
    activating. Default id: m-<UTC yyyymmdd-hhmmss>; when that folder exists (two requests in one second) -2, -3,
    ... is added.
    """
    raw = validate_request(text.encode("utf-8"))
    if len(active_missions(cfg)) >= config.MAX_ACTIVE_MISSIONS:
        raise TooManyMissions(t("admin.too_many_missions", limit=config.MAX_ACTIVE_MISSIONS))
    if mission_id is not None:
        create_request(cfg, require_id(mission_id), raw)
    else:
        mission_id = _create_with_default_id(cfg, raw)
    _launch(cfg, mission_id)
    return mission_id


FOLLOWUP_SUFFIX = re.compile(r"-c\d+$")
ELLIPSIS = "…"


def _shortened(build, body: str) -> str | None:
    """`body` itself when build(body) fits REQUEST_MAX_BYTES, else its longest prefix (ending with an ellipsis)
    that does, or None when even a lone ellipsis does not fit."""
    def fits(candidate: str) -> bool:
        return len(build(candidate).encode("utf-8")) <= REQUEST_MAX_BYTES

    if fits(body):
        return body
    if not fits(ELLIPSIS):
        return None
    low, high = 1, len(body) - 1  # characters kept including the ellipsis; `low` always fits
    while low < high:
        mid = (low + high + 1) // 2
        if fits(body[:mid - 1] + ELLIPSIS):
            low = mid
        else:
            high = mid - 1
    return body[:low - 1] + ELLIPSIS


def compose_followup_request(parent_id: str, parent_request: str | None, briefing: Briefing | None,
                             text: str) -> str:
    """The request.md of a follow-up mission, deterministic (SPEC-E section 6). `parent_request` is None for a
    hand-written parent. Over REQUEST_MAX_BYTES the previous request is cut first, then the previous result;
    when the new text alone is too long, MissionError."""
    none_text = t("admin.followup.none")
    evidence = briefing.evidence if briefing else []
    achieved = "; ".join(f"{i.id} {i.text}" for i in evidence if i.mark == ACHIEVED) or none_text
    open_items = "; ".join(f"{i.id} {i.text} ({i.reason})" for i in briefing.remaining) if briefing else ""
    result = t("admin.followup.result", achieved=achieved, open=open_items or none_text)
    previous = parent_request.strip() if parent_request is not None else t("admin.followup.hand_written")
    new = text.strip()

    def build(previous: str, result: str) -> str:
        return t("admin.followup.request", parent_id=parent_id, previous=previous, result=result, new=new)

    cut = _shortened(lambda body: build(body, result), previous)
    if cut is not None:
        return build(cut, result)
    cut = _shortened(lambda body: build(ELLIPSIS, body), result)
    if cut is None:
        raise MissionError(t("admin.followup.too_long"))
    return build(ELLIPSIS, cut)


def _followup_id_and_create(cfg: Config, parent_id: str, raw: bytes) -> str:
    """Create the follow-up's mission folder: <parent base>-c<n> for the smallest free n; when that is not a valid
    id (too long), the default id scheme."""
    base = FOLLOWUP_SUFFIX.sub("", parent_id)
    n = 1
    while valid_id(candidate := f"{base}-c{n}"):
        try:
            create_request(cfg, candidate, raw)
            return candidate
        except MissionExists:
            n += 1
    return _create_with_default_id(cfg, raw)


def continue_mission(cfg: Config, parent_id: str, text: str) -> str:
    """Start a follow-up of a finished mission: a new mission whose worker folder is a copy of the parent's.
    Returns the new mission id.

    Refused with nothing created: UnknownMission, MissionActive (the parent's unit is active or activating),
    a bad `text` (MissionError), TooManyMissions. The folder is copied as the worker user, never by root.
    """
    _require_known(cfg, parent_id)
    if _service_state(parent_id) in ACTIVE_STATES:
        raise MissionActive()
    validate_request(text.encode("utf-8"))
    if len(active_missions(cfg)) >= config.MAX_ACTIVE_MISSIONS:
        raise TooManyMissions(t("admin.too_many_missions", limit=config.MAX_ACTIVE_MISSIONS))
    try:
        briefing = load_briefing(cfg, parent_id)
    except LedgerError:
        raise MissionError(t("admin.followup.broken_ledger")) from None
    try:
        parent_request = cfg.request_file(parent_id).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        parent_request = None
    raw = validate_request(compose_followup_request(parent_id, parent_request, briefing, text).encode("utf-8"))
    mission_id = _followup_id_and_create(cfg, parent_id, raw)
    parent_path = cfg.parent_file(mission_id)
    _write_new_file(parent_path, f"{parent_id}\n".encode("utf-8"))
    latest_ns = max(
        (cfg.parent_file(m).stat(follow_symlinks=False).st_mtime_ns
         for m in mission_ids(cfg) if m != mission_id and cfg.parent_file(m).exists()),
        default=0,
    )
    if parent_path.stat(follow_symlinks=False).st_mtime_ns <= latest_ns:
        os.utime(parent_path, ns=(latest_ns + 1, latest_ns + 1), follow_symlinks=False)
    if briefing is not None:
        report.write_report(cfg, parent_id, Ledger(cfg.ledger_file(parent_id)).read())
    create_workdir(cfg, mission_id)
    _run(as_worker(["cp", "-a", f"{cfg.workdir(parent_id)}/.", f"{cfg.workdir(mission_id)}/"]))
    _run(["systemctl", "enable", "--now", f"agentic@{mission_id}.service"])
    return mission_id


def start_mission(cfg: Config, path: str | Path) -> str:
    """Install a hand-written mission.json and start its supervisor. Returns the mission id."""
    mission_id = require_id(install_mission_file(cfg, path))
    _launch(cfg, mission_id)
    return mission_id


def _require_known(cfg: Config, mission_id: str) -> None:
    require_id(mission_id)
    if not cfg.mission_dir(mission_id).is_dir():
        raise UnknownMission(mission_id)


def stop_mission(cfg: Config, mission_id: str) -> None:
    _require_known(cfg, mission_id)
    _run(["systemctl", "disable", "--now", f"agentic@{mission_id}.service"])


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def migrate_workspaces(cfg: Config, old_root: str | Path) -> list[tuple[str, str]]:
    """Move every `<old_root>/<id>/work` to `cfg.workdir(id)` as the worker user (SPEC-F section 1).

    Root packs the old folder into a root-only archive; the worker user unpacks it into the new folder, so every
    new file belongs to the worker account. The old folder is deleted only once the new one exists. Idempotent: a
    mission whose folder was already moved is reported as such. Returns (id, outcome) for every mission found in
    the old root or the state folder; a missing folder or a failed command is reported, never raised.
    """
    old_root = Path(old_root)
    try:
        old_ids = [name for name in os.listdir(old_root) if valid_id(name)]
    except FileNotFoundError:
        old_ids = []
    outcomes = []
    for mission_id in sorted(set(old_ids) | set(mission_ids(cfg))):
        old, new = old_root / mission_id / "work", cfg.workdir(mission_id)
        if not _is_real_dir(old):
            outcomes.append((mission_id, "already moved" if _is_real_dir(new) else f"missing: no folder {old}"))
            continue
        archive = cfg.state / "scratch" / f"migrate-{mission_id}.tar"
        try:
            archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            create_workdir(cfg, mission_id)
            _run(["tar", "-C", str(old), "-cf", str(archive), "."])
            result = commands.run_command(as_worker(["tar", "-C", str(new), "-xf", "-"]), stdin_path=str(archive))
            if result.returncode != 0:
                detail = " ".join((result.stderr or result.stdout).split())[:FIELD_MAX_CHARS]
                raise CommandFailed(f"runuser tar failed (exit {result.returncode}): {detail}")
            if not _is_real_dir(new):
                raise CommandFailed(f"{new} does not exist after unpacking")
            shutil.rmtree(old)
            with contextlib.suppress(OSError):
                old.parent.rmdir()  # <old_root>/<id>, when nothing else is left in it
            outcomes.append((mission_id, "moved"))
        except (CommandFailed, OSError) as exc:
            outcomes.append((mission_id, f"failed: {exc}"))
        finally:
            archive.unlink(missing_ok=True)
    return outcomes


def mission_ids(cfg: Config) -> list[str]:
    try:
        entries = sorted(os.listdir(cfg.state / "missions"))
    except FileNotFoundError:
        return []
    return [name for name in entries if valid_id(name) and cfg.mission_dir(name).is_dir()]


def _service_state(mission_id: str) -> str:
    """`systemctl is-active` prints the state (active, inactive, failed...) even when it exits non-zero."""
    result = commands.run_command(["systemctl", "is-active", f"agentic@{mission_id}.service"])
    return result.stdout.strip() or "unknown"


def active_missions(cfg: Config) -> list[str]:
    """Missions whose unit is active or activating."""
    return [mission_id for mission_id in mission_ids(cfg) if _service_state(mission_id) in ACTIVE_STATES]


def _records(cfg: Config, mission_id: str) -> tuple[list[dict], str | None]:
    """(records, problem). A missing ledger is no records; a broken one is no records plus the problem."""
    try:
        return Ledger(cfg.ledger_file(mission_id)).read(), None
    except LedgerError:
        return [], t("admin.ledger_corrupted")
    except OSError as exc:
        return [], t("admin.ledger_unreadable", error=exc.strerror)


def _time(record: dict) -> str:
    try:
        return display_time(parse_ts(record["ts"]))
    except (KeyError, TypeError, ValueError):
        return "?"


def _line(mission_id: str, records: list[dict], problem: str | None) -> str:
    parts = [mission_id, _service_state(mission_id)]
    if problem:
        parts.append(problem)
    elif records:
        last = records[-1]
        iterations = sum(1 for r in records if r["type"] == "iteration_worker")
        parts += [t("admin.last_record", type=last["type"], time=_time(last)),
                  t("admin.iterations", count=iterations)]
    else:
        parts.append(t("admin.no_records"))
    return " · ".join(parts)


def mission_line(cfg: Config, mission_id: str) -> str:
    """id · service state · last record type and time · iteration count."""
    _require_known(cfg, mission_id)
    records, problem = _records(cfg, mission_id)
    return _line(mission_id, records, problem)


def list_missions(cfg: Config) -> list[str]:
    lines = []
    for mission_id in mission_ids(cfg):
        records, problem = _records(cfg, mission_id)
        lines.append(_line(mission_id, records, problem))
    return lines


def _short(value: object) -> str:
    return " ".join(str(value).split())[:FIELD_MAX_CHARS]


def _count(value: object) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


def event_text(record: dict) -> str:
    """One line about a ledger record in the configured language. Never check data: a check's argv, files and
    output are not shown, and neither are the answers of the planner's check and critique steps; a check result
    is only pass/fail."""
    kind, data = record["type"], record["data"]
    try:
        if kind == "mission_started":
            return t("admin.event.mission_started", count=_count(data.get("criteria")))
        if kind == "baseline":
            return t("admin.event.baseline", count=_count(data.get("invalid")))
        if kind == "iteration_worker":
            timed_out = t("admin.event.timed_out_suffix") if data.get("timed_out") else ""
            return t("admin.event.iteration_worker", iteration=data.get("iteration"),
                     model=_short(data.get("model")), thinking=_short(data.get("thinking")),
                     exit_code=data.get("exit_code"), timed_out=timed_out)
        if kind == "check_result":
            result = t("admin.event.passed") if data.get("passed") else t("admin.event.failed")
            return t("admin.event.check_result", iteration=data.get("iteration"),
                     criterion=_short(data.get("criterion")), result=result)
        if kind == "escalated":
            return t("admin.event.escalated", model=_short(data.get("model")))
        if kind == "gave_up":
            criteria = data.get("criteria") or []
            return t("admin.event.gave_up", criteria=", ".join(_short(c) for c in criteria))
        if kind == "stopped_time":
            return t("admin.event.stopped_time", iteration=data.get("iteration"))
        if kind == "mission_finished":
            status = data.get("status")
            return t("admin.event.mission_finished", status=report.status_text(status, _short(status)),
                     iterations=data.get("iterations"))
        if kind == "plan_started":
            return t("admin.event.plan_started")
        if kind == "plan_step":
            result = t("admin.event.step_ok") if data.get("ok") else t("admin.event.step_failed")
            return t("admin.event.plan_step", step=_short(data.get("step")), round=data.get("round"),
                     result=result)
        if kind == "plan_finished":
            dropped = _count(data.get("dropped"))
            tail = t("admin.event.plan_finished_dropped", count=dropped) if dropped else ""
            return t("admin.event.plan_finished", dropped=tail)
        if kind == "plan_failed":
            return t("admin.event.plan_failed", reason=_short(data.get("reason")))
        if kind == "review":
            if not data.get("ok"):
                return t("admin.event.review_failed")
            verified = sum(1 for f in data.get("findings") or [] if isinstance(f, dict) and f.get("verified"))
            return t("admin.event.review", count=verified)
        if kind == "review_veto":
            return t("admin.event.review_veto")
        if kind == "worker_unavailable":
            return t("admin.event.worker_unavailable", model=_short(data.get("model")),
                     reason=_short(data.get("reason")), attempt=data.get("attempt"),
                     minutes=int(data.get("wait_seconds") or 0) // 60)
        if kind == "plan_unavailable":
            return t("admin.event.plan_unavailable", step=_short(data.get("step")),
                     reason=_short(data.get("reason")), minutes=int(data.get("wait_seconds") or 0) // 60)
        if kind == "canary":
            results = data.get("results") or {}
            held = sum(1 for r in results.values() if r.get("held"))
            return t("admin.event.canary", total=len(results), held=held, files=data.get("changed_files"))
        if kind == "mutation":
            skipped = int(data.get("skipped") or 0)
            ran = int(data.get("mutants") or 0) - skipped
            tail = t("admin.event.mutation_skipped", skipped=skipped) if skipped else ""
            return t("admin.event.mutation", ran=ran, killed=data.get("killed"), tail=tail)
        if kind == "checks_strengthened":
            return t("admin.event.checks_strengthened", count=_count(data.get("replaced")))
        if kind == "hygiene":
            return t("admin.event.hygiene", count=_count(data.get("files")))
        if kind == "plan_filter":
            return t("admin.event.plan_filter", step=_short(data.get("step")), round=data.get("round"),
                     checked=data.get("checked"), rejected=data.get("rejected"))
        if kind == "checks_update":
            return t("admin.event.checks_update", criterion=_short(data.get("criterion")))
        if kind == "checks_update_rejected":
            return t("admin.event.checks_update_rejected", count=_count(data.get("criteria")))
        if kind == "notice":
            return t("admin.event.notice", text=_short(data.get("text")))
    except (AttributeError, TypeError, ValueError):
        pass
    return t("admin.event.fallback", kind=kind)


def mission_status(cfg: Config, mission_id: str) -> str:
    """The mission line plus its last events, one Korean line each."""
    _require_known(cfg, mission_id)
    records, problem = _records(cfg, mission_id)
    lines = [_line(mission_id, records, problem)]
    followups = report.followup_ids(cfg, mission_id)
    if followups:
        lines.append(t("report.followups_line", followups=", ".join(followups)))
    lines += [f"- {_time(r)} {event_text(r)}" for r in records[-EVENTS_SHOWN:]]
    return "\n".join(lines)


def report_text(cfg: Config, mission_id: str) -> str:
    """The content of report.md, or the no-report text."""
    _require_known(cfg, mission_id)
    try:
        text = cfg.report_file(mission_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return t("admin.no_report")
    if report.followup_ids(cfg, mission_id):
        records, problem = _records(cfg, mission_id)
        if records and not problem:
            return report.write_report(cfg, mission_id, records).read_text(encoding="utf-8")
    return text


def __getattr__(name: str):
    if name == "NO_REPORT_TEXT":
        return t("admin.no_report")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
