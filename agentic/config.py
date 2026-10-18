"""Paths and limits. Every setting can be overridden through the environment (tests do this)."""

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_FILE = Path("/etc/agentic/agentic.toml")
OLD_WORKSPACES = "/home/agent/missions"  # Stage B-E worker folders; `agentic migrate-workspaces` moves them
DEFAULT_CHECK_MODE = "systemd"
WORKER_STOP_TIMEOUT_SECONDS = 10  # SIGTERM -> SIGKILL grace for leftovers when the unit stops
PLANNER_MAX_MINUTES = 15  # omp --max-time for one planner/reviewer call
PLANNER_RUNTIME_SECONDS = 20 * 60  # RuntimeMaxSec of the unit
PLANNER_OUTPUT_BYTES = 1024 * 1024  # how much of a model's stdout is read (the answer is at the end)
DEFAULT_INBOX_LOCK = "/run/agentic-inbox.lock"
INBOX_ITEM_MAX_BYTES = 32 * 1024
OUTBOX_ITEM_MAX_BYTES = 1024 * 1024  # an outbox item (a list of HTML messages)
# Stage E: documents in the outbox (SPEC-E.md section 4). The file itself is `<name>.doc` next to its item.
DOCUMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB = 20 MiB; larger documents are never written
DOC_SUFFIX = ".doc"
# Short model names people read (briefings, cards, report.md); other models show the part after the last "/".
MODEL_SHORT_NAMES = {
    "anthropic/claude-sonnet-5-5": "Sonnet 5.5",
    "google-antigravity/claude-sonnet-5-5": "Sonnet 5.5",
    "anthropic/claude-opus-5-5": "Opus 5.5",
    "google-antigravity/claude-opus-5-5": "Opus 5.5",
    "anthropic/claude-fable-5-1": "Fable 5.1",
    "google-antigravity/gemini-4-argon": "Argon",
}
PARENT_FILE = "parent"  # <state>/missions/<id>/parent: the parent id of a follow-up mission (root-only)
FAST_EXIT_SECONDS = 120  # a failed worker run shorter than this that changed nothing counts as unavailable
OUTAGE_ALERT_SECONDS = 30 * 60  # notify once when a worker outage has lasted this long
PLAN_OUTAGE_MAX_SECONDS = 6 * 3600  # total planner waiting before planning fails
REVIEW_OUTAGE_MAX_SECONDS = 3600  # total waiting for one review before it is recorded as failed
REQUEST_FILE = "request.md"
REQUEST_MAX_BYTES = 20 * 1024

_OMP_STATE_REL = (
    "logs", "run", "natives", "cache", "agent/cache", "agent/sessions",
    *(f"agent/{db}{suffix}" for db in ("agent.db", "models.db", "history.db", "skill-descriptions.db")
      for suffix in ("", "-wal", "-shm")),
    *(f"stats.db{suffix}" for suffix in ("", "-wal", "-shm")),
)
WORKER_AGENT_FILES = ("models.yml", "config.yml")

DEFAULT_PLANNER_STEPS: dict[str, tuple[str, ...]] = {
    "brief": ("google-antigravity/gemini-4-argon:high", "anthropic/claude-opus-5-5:high"),
    "design": ("anthropic/claude-fable-5-1:high", "anthropic/claude-opus-5-5:high"),
    "checks": ("anthropic/claude-opus-5-5:high", "anthropic/claude-fable-5-1:high"),
    "critique": ("google-antigravity/gemini-4-argon:high", "anthropic/claude-sonnet-5-5:high"),
}
DEFAULT_REVIEW_MODELS: tuple[str, ...] = (
    "google-antigravity/gemini-4-argon:high",
    "anthropic/claude-sonnet-5-5:high",
)
DEFAULT_WORKER_LADDER: tuple[dict[str, object], ...] = (
    {"model": "anthropic/claude-sonnet-5-5", "thinking": "high", "iteration_minutes": 45},
    {"model": "anthropic/claude-opus-5-5", "thinking": "high", "iteration_minutes": 45},
    {"model": "anthropic/claude-fable-5-1", "thinking": "high", "iteration_minutes": 90},
)
DEFAULT_ALTERNATE_PROVIDER: dict[str, str] = {
    "anthropic/claude-sonnet-5-5": "google-antigravity/claude-sonnet-5-5",
    "anthropic/claude-opus-5-5": "google-antigravity/claude-opus-5-5",
}
DEFAULT_OUTAGE_WAITS_MINUTES: tuple[int, ...] = (5, 10, 20, 40, 60)
SUPPORTED_LANGUAGES = ("en", "ko")
PLANNER_STEP_NAMES = ("brief", "design", "checks", "critique")
SECTIONS: dict[str, tuple[str, ...]] = {
    "accounts": ("interactive_user", "worker_user", "planner_user", "bot_user"),
    "paths": ("state", "workspaces", "spool", "worker_omp", "planner_omp", "worker_overlay",
              "guidance_sync", "guidance_check"),
    "models": ("planner_steps", "review", "worker_ladder", "alternate_provider"),
    "limits": ("max_active_missions", "outage_waits_minutes", "canary_max_age_days",
               "mutation_max_mutants", "mutation_budget_seconds"),
    "display": ("timezone", "language"),
    "telegram": ("enabled",),
}
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CHECK_MODES = ("local", "systemd")
TAIL_BYTES = 4096  # how much stdout/stderr is kept in the ledger
WORKER_GRACE_MINUTES = 5  # added to the ladder step's iteration_minutes as the hard timeout
SNAPSHOT_SKIP = frozenset({".git", "node_modules", "__pycache__", ".venv"})
DECISIONS_FILE = "DECISIONS.md"
DECISIONS_MAX_BYTES = 64 * 1024

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class ConfigError(ValueError):
    pass


def valid_id(value: object) -> bool:
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class Settings:
    interactive_user: str = "agent"
    worker_user: str = "agwork"
    planner_user: str = "agplan"
    bot_user: str = "agbot"
    state: str = "/var/lib/agentic"
    workspaces: str = "/var/lib/agwork/missions"
    spool: str = "/var/spool/agentic"
    worker_omp: str = "/opt/omp/omp"
    planner_omp: str = "/opt/omp/omp"
    worker_overlay: str = "/etc/agentic/omp-overlay.yml"
    guidance_sync: str = "/usr/local/sbin/agentic-guidance-sync"
    guidance_check: str = "/usr/local/sbin/agentic-guidance-check"
    planner_steps: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {k: tuple(v) for k, v in DEFAULT_PLANNER_STEPS.items()}
    )
    review: tuple[str, ...] = DEFAULT_REVIEW_MODELS
    worker_ladder: tuple[dict[str, object], ...] = field(
        default_factory=lambda: tuple(dict(step) for step in DEFAULT_WORKER_LADDER)
    )
    alternate_provider: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_ALTERNATE_PROVIDER)
    )
    max_active_missions: int = 3
    outage_waits_minutes: tuple[int, ...] = DEFAULT_OUTAGE_WAITS_MINUTES
    canary_max_age_days: int = 7
    mutation_max_mutants: int = 6
    mutation_budget_seconds: int = 600
    timezone: str = "UTC"
    language: str = "en"
    telegram_enabled: bool = True

    def to_dict(self) -> dict[str, dict[str, object]]:
        return {
            "accounts": {
                "interactive_user": self.interactive_user,
                "worker_user": self.worker_user,
                "planner_user": self.planner_user,
                "bot_user": self.bot_user,
            },
            "paths": {
                "state": self.state,
                "workspaces": self.workspaces,
                "spool": self.spool,
                "worker_omp": self.worker_omp,
                "planner_omp": self.planner_omp,
                "worker_overlay": self.worker_overlay,
                "guidance_sync": self.guidance_sync,
                "guidance_check": self.guidance_check,
            },
            "models": {
                "planner_steps": {step: list(self.planner_steps[step]) for step in PLANNER_STEP_NAMES},
                "review": list(self.review),
                "worker_ladder": [dict(step) for step in self.worker_ladder],
                "alternate_provider": dict(self.alternate_provider),
            },
            "limits": {
                "max_active_missions": self.max_active_missions,
                "outage_waits_minutes": list(self.outage_waits_minutes),
                "canary_max_age_days": self.canary_max_age_days,
                "mutation_max_mutants": self.mutation_max_mutants,
                "mutation_budget_seconds": self.mutation_budget_seconds,
            },
            "display": {
                "timezone": self.timezone,
                "language": self.language,
            },
            "telegram": {
                "enabled": self.telegram_enabled,
            },
        }


def _valid_username(val: object, where: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise ConfigError(f"{where}: must be a non-empty string")
    if not USER_RE.fullmatch(val):
        raise ConfigError(f"{where}: invalid user name {val!r}")
    return val


def _valid_abs_path(val: object, where: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise ConfigError(f"{where}: must be a non-empty string")
    if "\0" in val or val != val.strip():
        raise ConfigError(f"{where}: invalid path {val!r}")
    pure = PurePosixPath(val)
    if not val.startswith("/") or ".." in pure.parts or str(pure) == "/":
        raise ConfigError(f"{where}: must be an absolute path, got {val!r}")
    return str(pure)


def _valid_model_spec(val: object, where: str) -> str:
    if not isinstance(val, str) or not val.strip() or "\0" in val or any(c.isspace() for c in val):
        raise ConfigError(f"{where}: must be a 'model:thinking' string, got {val!r}")
    model, sep, thinking = val.rpartition(":")
    if not sep or not model or not thinking:
        raise ConfigError(f"{where}: must be a 'model:thinking' string, got {val!r}")
    return val


def _valid_model_spec_list(val: object, where: str) -> tuple[str, ...]:
    if not isinstance(val, list) or not val:
        raise ConfigError(f"{where}: must be a non-empty list of 'model:thinking' strings")
    return tuple(_valid_model_spec(item, where) for item in val)


def _valid_planner_steps(val: object) -> dict[str, tuple[str, ...]]:
    where = "models.planner_steps"
    if not isinstance(val, dict):
        raise ConfigError(f"{where}: must be a table")
    for step in val:
        if step not in PLANNER_STEP_NAMES:
            raise ConfigError(f"{where}.{step}: unknown planner step")
    result = {k: tuple(v) for k, v in DEFAULT_PLANNER_STEPS.items()}
    for step, specs in val.items():
        result[step] = _valid_model_spec_list(specs, f"{where}.{step}")
    return result


def _valid_worker_ladder(val: object) -> tuple[dict[str, object], ...]:
    where = "models.worker_ladder"
    if not isinstance(val, list) or not val:
        raise ConfigError(f"{where}: must be a non-empty list")
    steps: list[dict[str, object]] = []
    for i, raw in enumerate(val):
        item_where = f"{where}[{i}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{item_where}: must be a table")
        unknown = set(raw) - {"model", "thinking", "iteration_minutes"}
        if unknown:
            raise ConfigError(f"{item_where}: unknown keys {sorted(unknown)}")
        if "model" not in raw:
            raise ConfigError(f"{item_where}: missing 'model'")
        model = raw["model"]
        if not isinstance(model, str) or not model.strip() or "\0" in model:
            raise ConfigError(f"{item_where}.model: must be a non-empty string")
        thinking = raw.get("thinking", "high")
        if not isinstance(thinking, str) or not thinking.strip() or "\0" in thinking:
            raise ConfigError(f"{item_where}.thinking: must be a non-empty string")
        minutes = raw.get("iteration_minutes", 45)
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not minutes > 0:
            raise ConfigError(f"{item_where}.iteration_minutes: must be a number > 0")
        steps.append({"model": model, "thinking": thinking, "iteration_minutes": minutes})
    return tuple(steps)


def _valid_alternate_provider(val: object) -> dict[str, str]:
    where = "models.alternate_provider"
    if not isinstance(val, dict):
        raise ConfigError(f"{where}: must be a table")
    out: dict[str, str] = {}
    for k, v in val.items():
        if (not isinstance(k, str) or not k.strip() or "\0" in k
                or not isinstance(v, str) or not v.strip() or "\0" in v):
            raise ConfigError(f"{where}: keys and values must be non-empty strings")
        out[k] = v
    return out


def _valid_positive_int(val: object, where: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val < 1:
        raise ConfigError(f"{where}: must be a positive integer, got {val!r}")
    return val


def _valid_outage_waits(val: object) -> tuple[int, ...]:
    where = "limits.outage_waits_minutes"
    if not isinstance(val, list) or not val:
        raise ConfigError(f"{where}: must be a non-empty list of positive integers")
    return tuple(_valid_positive_int(item, where) for item in val)


def _valid_timezone(val: object) -> str:
    where = "display.timezone"
    if not isinstance(val, str) or not val.strip() or val != val.strip() or val.startswith(("/", ".")):
        raise ConfigError(f"{where}: must be a valid IANA timezone name, got {val!r}")
    try:
        ZoneInfo(val)
    except (ZoneInfoNotFoundError, KeyError, ValueError, OSError) as exc:
        raise ConfigError(f"{where}: unknown IANA timezone {val!r}") from exc
    return val


def _valid_language(val: object) -> str:
    where = "display.language"
    if not isinstance(val, str) or val not in SUPPORTED_LANGUAGES:
        raise ConfigError(f"{where}: must be one of {SUPPORTED_LANGUAGES}, got {val!r}")
    return val


def _valid_bool(val: object, where: str) -> bool:
    if not isinstance(val, bool):
        raise ConfigError(f"{where}: must be true or false, got {val!r}")
    return val


_UNSET = object()


def load_settings(path: Path | str | None = _UNSET, env: dict[str, str] | None = None) -> Settings:
    """Load and validate an optional TOML configuration file (`path=None` returns the defaults)."""
    if path is _UNSET:
        env = os.environ if env is None else env
        raw_env = env.get("AGENTIC_CONFIG")
        if raw_env is not None:
            if not raw_env.strip():
                raise ConfigError("AGENTIC_CONFIG: must be a non-empty path")
            path = Path(raw_env)
        elif DEFAULT_CONFIG_FILE.exists():
            path = DEFAULT_CONFIG_FILE
        else:
            return Settings()
    if path is None:
        return Settings()
    target = Path(path)
    try:
        content = target.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ConfigError(f"{exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"file is not valid UTF-8 ({exc})") from exc
    try:
        raw = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    for section, value in raw.items():
        if section not in SECTIONS:
            raise ConfigError(f"{section}: unknown section")
        if not isinstance(value, dict):
            raise ConfigError(f"{section}: must be a table")
        for key in value:
            if key not in SECTIONS[section]:
                raise ConfigError(f"{section}.{key}: unknown key")
    defaults = Settings()
    accounts = raw.get("accounts", {})
    paths = raw.get("paths", {})
    models = raw.get("models", {})
    limits = raw.get("limits", {})
    display = raw.get("display", {})
    telegram = raw.get("telegram", {})
    return Settings(
        interactive_user=(_valid_username(accounts["interactive_user"], "accounts.interactive_user")
                          if "interactive_user" in accounts else defaults.interactive_user),
        worker_user=(_valid_username(accounts["worker_user"], "accounts.worker_user")
                     if "worker_user" in accounts else defaults.worker_user),
        planner_user=(_valid_username(accounts["planner_user"], "accounts.planner_user")
                      if "planner_user" in accounts else defaults.planner_user),
        bot_user=(_valid_username(accounts["bot_user"], "accounts.bot_user")
                  if "bot_user" in accounts else defaults.bot_user),
        state=(_valid_abs_path(paths["state"], "paths.state")
               if "state" in paths else defaults.state),
        workspaces=(_valid_abs_path(paths["workspaces"], "paths.workspaces")
                    if "workspaces" in paths else defaults.workspaces),
        spool=(_valid_abs_path(paths["spool"], "paths.spool")
               if "spool" in paths else defaults.spool),
        worker_omp=(_valid_abs_path(paths["worker_omp"], "paths.worker_omp")
                    if "worker_omp" in paths else defaults.worker_omp),
        planner_omp=(_valid_abs_path(paths["planner_omp"], "paths.planner_omp")
                     if "planner_omp" in paths else defaults.planner_omp),
        worker_overlay=(_valid_abs_path(paths["worker_overlay"], "paths.worker_overlay")
                        if "worker_overlay" in paths else defaults.worker_overlay),
        guidance_sync=(_valid_abs_path(paths["guidance_sync"], "paths.guidance_sync")
                       if "guidance_sync" in paths else defaults.guidance_sync),
        guidance_check=(_valid_abs_path(paths["guidance_check"], "paths.guidance_check")
                        if "guidance_check" in paths else defaults.guidance_check),
        planner_steps=(_valid_planner_steps(models["planner_steps"])
                       if "planner_steps" in models else defaults.planner_steps),
        review=(_valid_model_spec_list(models["review"], "models.review")
                if "review" in models else defaults.review),
        worker_ladder=(_valid_worker_ladder(models["worker_ladder"])
                       if "worker_ladder" in models else defaults.worker_ladder),
        alternate_provider=(_valid_alternate_provider(models["alternate_provider"])
                            if "alternate_provider" in models else defaults.alternate_provider),
        max_active_missions=(_valid_positive_int(limits["max_active_missions"], "limits.max_active_missions")
                             if "max_active_missions" in limits else defaults.max_active_missions),
        outage_waits_minutes=(_valid_outage_waits(limits["outage_waits_minutes"])
                              if "outage_waits_minutes" in limits else defaults.outage_waits_minutes),
        canary_max_age_days=(_valid_positive_int(limits["canary_max_age_days"], "limits.canary_max_age_days")
                             if "canary_max_age_days" in limits else defaults.canary_max_age_days),
        mutation_max_mutants=(_valid_positive_int(limits["mutation_max_mutants"], "limits.mutation_max_mutants")
                              if "mutation_max_mutants" in limits else defaults.mutation_max_mutants),
        mutation_budget_seconds=(_valid_positive_int(limits["mutation_budget_seconds"],
                                                     "limits.mutation_budget_seconds")
                                 if "mutation_budget_seconds" in limits else defaults.mutation_budget_seconds),
        timezone=(_valid_timezone(display["timezone"])
                  if "timezone" in display else defaults.timezone),
        language=(_valid_language(display["language"])
                  if "language" in display else defaults.language),
        telegram_enabled=(_valid_bool(telegram["enabled"], "telegram.enabled")
                          if "enabled" in telegram else defaults.telegram_enabled),
    )


def apply_settings(s: Settings) -> None:
    """Apply `s` to all module-level configuration attributes."""
    global SETTINGS, SETTINGS_ERROR
    global INTERACTIVE_USER, WORKER_USER, WORKER_HOME, PLANNER_USER, PLANNER_HOME, BOT_USER
    global DEFAULT_STATE, DEFAULT_WORKSPACES, DEFAULT_SPOOL, DEFAULT_INBOX, DEFAULT_OUTBOX
    global OMP_BINARY, PLANNER_OMP, OMP_OVERLAY, GUIDANCE_SYNC_CMD, GUIDANCE_CHECK_CMD
    global OMP_ARGV, PLANNER_OMP_ARGV, WORKER_OMP_DIR, WORKER_OMP_AGENT_DIR, WORKER_OMP_STATE, WORKER_INACCESSIBLE
    global PLANNER_MODELS, REVIEW_MODELS, WORKER_LADDER, ALTERNATE_PROVIDER
    global MAX_ACTIVE_MISSIONS, OUTAGE_WAITS_MINUTES, CANARY_MAX_AGE_DAYS
    global MUTATION_MAX_MUTANTS, MUTATION_BUDGET_SECONDS, DISPLAY_TIMEZONE, LANGUAGE, TELEGRAM_ENABLED

    SETTINGS = s
    SETTINGS_ERROR = None
    INTERACTIVE_USER = s.interactive_user
    WORKER_USER = s.worker_user
    WORKER_HOME = f"/var/lib/{s.worker_user}"
    PLANNER_USER = s.planner_user
    PLANNER_HOME = f"/var/lib/{s.planner_user}"
    BOT_USER = s.bot_user

    DEFAULT_STATE = s.state
    DEFAULT_WORKSPACES = s.workspaces
    DEFAULT_SPOOL = s.spool
    DEFAULT_INBOX = f"{s.spool}/inbox"
    DEFAULT_OUTBOX = f"{s.spool}/outbox"
    OMP_BINARY = s.worker_omp
    PLANNER_OMP = s.planner_omp
    OMP_OVERLAY = s.worker_overlay
    GUIDANCE_SYNC_CMD = s.guidance_sync
    GUIDANCE_CHECK_CMD = s.guidance_check

    OMP_ARGV = [
        "env", f"HOME={WORKER_HOME}", OMP_BINARY,
        "-p", "--no-session", "--no-rules", "--no-skills", "--no-extensions", "--no-title",
        "--config", OMP_OVERLAY,
    ]
    PLANNER_OMP_ARGV = [
        "env", f"HOME={PLANNER_HOME}", PLANNER_OMP,
        "-p", "--no-session", "--no-tools", "--no-rules", "--no-skills", "--no-extensions", "--no-title",
    ]
    WORKER_OMP_DIR = f"{WORKER_HOME}/.omp"
    WORKER_OMP_AGENT_DIR = f"{WORKER_OMP_DIR}/agent"
    WORKER_OMP_STATE = tuple(f"{WORKER_OMP_DIR}/{rel}" for rel in _OMP_STATE_REL)
    WORKER_INACCESSIBLE = (DEFAULT_STATE, PLANNER_HOME, DEFAULT_SPOOL, "/etc/agentic/telegram.json")

    PLANNER_MODELS = {step: list(models) for step, models in s.planner_steps.items()}
    REVIEW_MODELS = list(s.review)
    WORKER_LADDER = [dict(step) for step in s.worker_ladder]
    ALTERNATE_PROVIDER = dict(s.alternate_provider)

    MAX_ACTIVE_MISSIONS = s.max_active_missions
    OUTAGE_WAITS_MINUTES = tuple(s.outage_waits_minutes)
    CANARY_MAX_AGE_DAYS = s.canary_max_age_days
    MUTATION_MAX_MUTANTS = s.mutation_max_mutants
    MUTATION_BUDGET_SECONDS = s.mutation_budget_seconds

    DISPLAY_TIMEZONE = s.timezone
    LANGUAGE = s.language
    TELEGRAM_ENABLED = s.telegram_enabled


def settings_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("AGENTIC_CONFIG")
    return Path(raw) if raw is not None else DEFAULT_CONFIG_FILE


def reload_settings(path: Path | str | None = _UNSET, env: dict[str, str] | None = None) -> Settings:
    """Load from `path` (or AGENTIC_CONFIG / `/etc/agentic/agentic.toml`) without crashing on error."""
    global SETTINGS_PATH, SETTINGS_ERROR, LANGUAGE, DISPLAY_TIMEZONE
    active_env = os.environ if env is None else env
    SETTINGS_PATH = Path(path) if isinstance(path, (str, Path)) else settings_path(active_env)
    try:
        s = load_settings(path, env=active_env)
    except ConfigError as exc:
        s = Settings()
        apply_settings(s)
        SETTINGS_ERROR = str(exc)
        return s
    apply_settings(s)
    if path is _UNSET and "AGENTIC_CONFIG" not in active_env:
        if active_env.get("AGENTIC_LANGUAGE") in SUPPORTED_LANGUAGES:
            LANGUAGE = active_env["AGENTIC_LANGUAGE"]
        if "AGENTIC_TIMEZONE" in active_env:
            DISPLAY_TIMEZONE = active_env["AGENTIC_TIMEZONE"]
    return s


SETTINGS: Settings = Settings()
SETTINGS_PATH: Path = DEFAULT_CONFIG_FILE
SETTINGS_ERROR: str | None = None
reload_settings()

@dataclass(frozen=True)
class Config:
    state: Path
    workspaces: Path
    check_mode: str
    worker_cmd: tuple[str, ...] | None  # None: the default systemd-wrapped omp command
    planner_cmd: tuple[str, ...] | None = None  # None: the default systemd-wrapped omp command as agplan
    inbox: Path = Path(DEFAULT_INBOX)
    outbox: Path = Path(DEFAULT_OUTBOX)
    inbox_lock: Path = Path(DEFAULT_INBOX_LOCK)
    bot_user: str = BOT_USER
    worker_home: Path | None = None
    worker_agent_dir: Path | None = None

    def mission_dir(self, mission_id: str) -> Path:
        return self.state / "missions" / mission_id

    def mission_file(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / "mission.json"

    def request_file(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / REQUEST_FILE

    def parent_file(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / PARENT_FILE

    def ledger_file(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / "ledger.jsonl"

    def report_file(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / "report.md"

    def lock_file(self, mission_id: str) -> Path:
        return self.mission_dir(mission_id) / "lock"

    def scratch_root(self, mission_id: str) -> Path:
        return self.state / "scratch" / mission_id

    def scratch_dir(self, mission_id: str, iteration: int) -> Path:
        return self.scratch_root(mission_id) / str(iteration)

    def workdir(self, mission_id: str) -> Path:
        return self.workspaces / mission_id / "work"


    def effective_worker_home(self) -> Path:
        return self.worker_home if self.worker_home is not None else Path(WORKER_HOME)

    def effective_worker_agent_dir(self) -> Path:
        if self.worker_agent_dir is not None:
            return self.worker_agent_dir
        if self.worker_home is not None:
            return self.worker_home / ".omp" / "agent"
        default_home = f"/var/lib/{WORKER_USER}"
        if WORKER_HOME != default_home and WORKER_OMP_AGENT_DIR == f"{default_home}/.omp/agent":
            return Path(WORKER_HOME) / ".omp" / "agent"
        return Path(WORKER_OMP_AGENT_DIR)

def load(env: dict[str, str] | None = None) -> Config:
    """Read the configuration from the environment (read on every call, never cached)."""
    if env is None:
        env = os.environ
        if "AGENTIC_STUB_DIR" in env and "AGENTIC_CONFIG" not in env:
            env["AGENTIC_LANGUAGE"] = LANGUAGE
            env["AGENTIC_TIMEZONE"] = DISPLAY_TIMEZONE
    mode = env.get("AGENTIC_CHECK_MODE", DEFAULT_CHECK_MODE)
    if mode not in CHECK_MODES:
        raise ConfigError(f"AGENTIC_CHECK_MODE must be one of {CHECK_MODES}, got {mode!r}")
    return Config(
        state=Path(env.get("AGENTIC_STATE", DEFAULT_STATE)),
        workspaces=Path(env.get("AGENTIC_WORKSPACES", DEFAULT_WORKSPACES)),
        check_mode=mode,
        worker_cmd=_command_override(env, "AGENTIC_WORKER_CMD"),
        planner_cmd=_command_override(env, "AGENTIC_PLANNER_CMD"),
        inbox=Path(env.get("AGENTIC_INBOX", DEFAULT_INBOX)),
        outbox=Path(env.get("AGENTIC_OUTBOX", DEFAULT_OUTBOX)),
        inbox_lock=Path(env.get("AGENTIC_INBOX_LOCK", DEFAULT_INBOX_LOCK)),
        bot_user=env.get("AGENTIC_BOT_USER", BOT_USER),
        worker_home=Path(env["AGENTIC_WORKER_HOME"]) if "AGENTIC_WORKER_HOME" in env else None,
        worker_agent_dir=Path(env["AGENTIC_WORKER_AGENT_DIR"]) if "AGENTIC_WORKER_AGENT_DIR" in env else None,
    )


def _command_override(env: dict[str, str], name: str) -> tuple[str, ...] | None:
    """A JSON argv list from the environment, or None when the variable is not set."""
    raw = env.get(name)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} is not valid JSON: {exc}") from exc
    if not (isinstance(parsed, list) and parsed and all(isinstance(a, str) for a in parsed)):
        raise ConfigError(f"{name} must be a non-empty JSON list of strings")
    return tuple(parsed)


def effective_settings(cfg: Config | None = None) -> Settings:
    """Effective settings combining applied module attributes and optional `cfg` environment overrides."""
    spool = DEFAULT_SPOOL
    if cfg is not None and cfg.inbox.name == "inbox" and cfg.outbox == cfg.inbox.parent / "outbox":
        spool = str(cfg.inbox.parent)
    return Settings(
        interactive_user=INTERACTIVE_USER,
        worker_user=WORKER_USER,
        planner_user=PLANNER_USER,
        bot_user=cfg.bot_user if cfg is not None else BOT_USER,
        state=str(cfg.state) if cfg is not None else DEFAULT_STATE,
        workspaces=str(cfg.workspaces) if cfg is not None else DEFAULT_WORKSPACES,
        spool=spool,
        worker_omp=OMP_BINARY,
        planner_omp=PLANNER_OMP,
        worker_overlay=OMP_OVERLAY,
        guidance_sync=GUIDANCE_SYNC_CMD,
        guidance_check=GUIDANCE_CHECK_CMD,
        planner_steps={step: tuple(PLANNER_MODELS[step]) for step in PLANNER_STEP_NAMES},
        review=tuple(REVIEW_MODELS),
        worker_ladder=tuple(dict(step) for step in WORKER_LADDER),
        alternate_provider=dict(ALTERNATE_PROVIDER),
        max_active_missions=MAX_ACTIVE_MISSIONS,
        outage_waits_minutes=tuple(OUTAGE_WAITS_MINUTES),
        canary_max_age_days=CANARY_MAX_AGE_DAYS,
        mutation_max_mutants=MUTATION_MAX_MUTANTS,
        mutation_budget_seconds=MUTATION_BUDGET_SECONDS,
        timezone=DISPLAY_TIMEZONE,
        language=LANGUAGE,
        telegram_enabled=TELEGRAM_ENABLED,
    )


def _toml_key(key: str) -> str:
    return key if BARE_KEY_RE.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    raise TypeError(f"unsupported TOML value {value!r}")


def format_settings(s: Settings | None = None) -> str:
    """Format `s` (default: current effective settings) as `[section]` / `key = value` TOML text."""
    s = effective_settings() if s is None else s
    blocks: list[str] = []
    for section, table in s.to_dict().items():
        lines = [f"[{section}]"]
        for key, value in table.items():
            lines.append(f"{key} = {_toml_value(value)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
