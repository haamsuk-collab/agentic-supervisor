"""Load and validate mission files."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentic import config
from agentic.config import REQUEST_MAX_BYTES, valid_id

DEFAULT_LIMITS = {"mission_hours": 24, "iteration_minutes": 45, "escalate_after": 2, "give_up_after": 2}
DEFAULT_CHECK_TIMEOUT = 120
# Optional public fields of a mission that was planned from a request (Stage C).
APPROACH_MAX_CHARS = 3000
ASSUMPTIONS_MAX_ITEMS = 30
ASSUMPTION_MAX_CHARS = 500


class MissionError(ValueError):
    pass


@dataclass(frozen=True)
class Check:
    argv: tuple[str, ...]
    timeout: int
    network: bool
    files: dict[str, str]


@dataclass(frozen=True)
class Criterion:
    id: str
    text: str
    check: Check


@dataclass(frozen=True)
class Mission:
    id: str
    goal: str
    criteria: tuple[Criterion, ...]
    limits: dict
    ladder: tuple[dict, ...]
    request: str | None = None  # planned missions only
    approach: str | None = None
    assumptions: tuple[str, ...] | None = None
    parent: str | None = None  # follow-up missions: the mission whose result this one continues

    def criterion(self, criterion_id: str) -> Criterion:
        for c in self.criteria:
            if c.id == criterion_id:
                return c
        raise KeyError(criterion_id)

    def public_view(self) -> dict:
        """Everything the ledger and the worker prompt may know. Never contains check data."""
        view = {
            "id": self.id,
            "goal": self.goal,
            "criteria": [{"id": c.id, "text": c.text} for c in self.criteria],
            "limits": dict(self.limits),
            "ladder": [dict(step) for step in self.ladder],
        }
        if self.request is not None:
            view["request"] = self.request
        if self.approach is not None:
            view["approach"] = self.approach
        if self.assumptions is not None:
            view["assumptions"] = list(self.assumptions)
        if self.parent is not None:
            view["parent"] = self.parent
        return view

    def contract(self) -> dict:
        """The whole validated mission (defaults applied, checks included): what the supervisor checks against.
        Never written to the ledger; only its sha256 is."""
        view = self.public_view()
        view["criteria"] = [{"id": c.id, "text": c.text, "check": check_dict(c.check)} for c in self.criteria]
        return view


def check_dict(check: Check) -> dict:
    return {"argv": list(check.argv), "timeout": check.timeout, "network": check.network, "files": dict(check.files)}


def contract_sha256(mission: Mission) -> str:
    """sha256 of the canonical JSON (sorted keys, no spaces, UTF-8) of `mission.contract()`."""
    text = json.dumps(mission.contract(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _object(value: object, where: str, required: set[str], optional: set[str]) -> dict:
    if not isinstance(value, dict):
        raise MissionError(f"{where} must be an object")
    unknown = set(value) - required - optional
    if unknown:
        raise MissionError(f"{where}: unknown keys {sorted(unknown)}")
    missing = required - set(value)
    if missing:
        raise MissionError(f"{where}: missing keys {sorted(missing)}")
    return value


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissionError(f"{where} must be a non-empty string")
    return value


def _int(value: object, where: str, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        bound = f"{low}..{high}" if high is not None else f">= {low}"
        raise MissionError(f"{where} must be an integer {bound}")
    return value


def _positive_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise MissionError(f"{where} must be a number > 0")
    return value


def _file_path(path: object, where: str) -> str:
    _string(path, where)
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\0" in path or pure.parts in ((), (".",)):
        raise MissionError(f"{where}: {path!r} must be a relative path inside the folder")
    return path


def parse_check(value: object, where: str) -> Check:
    obj = _object(value, where, {"argv"}, {"timeout", "network", "files"})
    argv = obj["argv"]
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and "\0" not in a for a in argv)):
        raise MissionError(f"{where}.argv must be a non-empty list of strings")
    timeout = _int(obj.get("timeout", DEFAULT_CHECK_TIMEOUT), f"{where}.timeout", 1, 600)
    network = obj.get("network", False)
    if not isinstance(network, bool):
        raise MissionError(f"{where}.network must be true or false")
    raw_files = obj.get("files", {})
    if not isinstance(raw_files, dict):
        raise MissionError(f"{where}.files must be an object")
    files = {}
    for name, content in raw_files.items():
        _file_path(name, f"{where}.files key")
        if not isinstance(content, str):
            raise MissionError(f"{where}.files[{name!r}] must be a string")
        files[name] = content
    return Check(tuple(argv), timeout, network, files)


def _parse_limits(value: object) -> dict:
    obj = _object(value, "limits", set(), set(DEFAULT_LIMITS))
    limits = {**DEFAULT_LIMITS, **obj}
    _positive_number(limits["mission_hours"], "limits.mission_hours")
    _positive_number(limits["iteration_minutes"], "limits.iteration_minutes")
    _int(limits["escalate_after"], "limits.escalate_after", 1)
    _int(limits["give_up_after"], "limits.give_up_after", 1)
    return limits


def _parse_ladder(value: object, limits: dict) -> tuple[dict, ...]:
    if not isinstance(value, list) or not value:
        raise MissionError("ladder must be a non-empty list")
    steps = []
    for i, raw in enumerate(value):
        where = f"ladder[{i}]"
        obj = _object(raw, where, {"model"}, {"thinking", "iteration_minutes"})
        steps.append({
            "model": _string(obj["model"], f"{where}.model"),
            "thinking": _string(obj.get("thinking", "high"), f"{where}.thinking"),
            "iteration_minutes": _positive_number(
                obj.get("iteration_minutes", limits["iteration_minutes"]), f"{where}.iteration_minutes"),
        })
    return tuple(steps)


def _bounded_string(value: object, where: str, max_chars: int) -> str:
    _string(value, where)
    if len(value) > max_chars:
        raise MissionError(f"{where} must be at most {max_chars} characters")
    return value


def _parse_assumptions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > ASSUMPTIONS_MAX_ITEMS:
        raise MissionError(f"assumptions must be a list of at most {ASSUMPTIONS_MAX_ITEMS} strings")
    return tuple(_bounded_string(item, f"assumptions[{i}]", ASSUMPTION_MAX_CHARS) for i, item in enumerate(value))


def parse_mission(raw: object) -> Mission:
    obj = _object(raw, "mission", {"id", "goal", "criteria"},
                  {"limits", "ladder", "request", "approach", "assumptions", "parent"})
    if not valid_id(obj["id"]):
        raise MissionError(f"mission id {obj['id']!r} must match ^[a-z0-9][a-z0-9-]{{0,40}}$")
    goal = _string(obj["goal"], "goal")
    raw_criteria = obj["criteria"]
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise MissionError("criteria must be a non-empty list")
    criteria, seen = [], set()
    for i, item in enumerate(raw_criteria):
        where = f"criteria[{i}]"
        c = _object(item, where, {"id", "text", "check"}, set())
        if not valid_id(c["id"]):
            raise MissionError(f"{where}.id {c['id']!r} must match ^[a-z0-9][a-z0-9-]{{0,40}}$")
        if c["id"] in seen:
            raise MissionError(f"{where}.id {c['id']!r} is duplicated")
        seen.add(c["id"])
        criteria.append(Criterion(c["id"], _string(c["text"], f"{where}.text"),
                                  parse_check(c["check"], f"{where}.check")))
    limits = _parse_limits(obj.get("limits", {}))
    ladder = _parse_ladder(obj["ladder"], limits) if "ladder" in obj else _parse_ladder(config.WORKER_LADDER, limits)
    request = approach = assumptions = parent = None
    if "parent" in obj:
        parent = obj["parent"]
        if not valid_id(parent):
            raise MissionError(f"parent {parent!r} must match ^[a-z0-9][a-z0-9-]{{0,40}}$")
    if "request" in obj:
        request = _string(obj["request"], "request")
        if len(request.encode("utf-8")) > REQUEST_MAX_BYTES:
            raise MissionError(f"request must be at most {REQUEST_MAX_BYTES} bytes")
    if "approach" in obj:
        approach = _bounded_string(obj["approach"], "approach", APPROACH_MAX_CHARS)
    if "assumptions" in obj:
        assumptions = _parse_assumptions(obj["assumptions"])
    return Mission(obj["id"], goal, tuple(criteria), limits, ladder, request, approach, assumptions, parent)


def load_mission(path: str | Path) -> Mission:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MissionError(f"cannot read mission {path}: {exc}") from exc
    return parse_mission(raw)


def validate_request(raw: bytes) -> bytes:
    """The request text as bytes: UTF-8, not empty, at most REQUEST_MAX_BYTES, no NUL characters."""
    if len(raw) > REQUEST_MAX_BYTES:
        raise MissionError(f"the request is {len(raw)} bytes; the limit is {REQUEST_MAX_BYTES}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MissionError(f"the request is not valid UTF-8: {exc}") from exc
    if not text.strip():
        raise MissionError("the request is empty")
    if "\0" in text:
        raise MissionError("the request contains a NUL character")
    return raw
