"""Build the worker prompt from the ledger and run one worker iteration."""

import contextlib
import hashlib
import math
import os
import pwd
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from agentic import config
from agentic.checks import run_process
from agentic.config import WORKER_GRACE_MINUTES, WORKER_STOP_TIMEOUT_SECONDS, Config, ConfigError

OSCILLATION_LINE = "The same set of requirements failed before. Try a different approach."
UNPROVEN_LINE = ("Requirement {id} is not shown to be met by your files; make the result come from the files you "
                 "write.")


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int | None
    timed_out: bool
    note: str  # last 4 KB of the worker's final text; unverified
    stderr: str
    duration: float
    leftovers: tuple[str, ...] = ()


STUB_ENV_KEYS = ("AGENTIC_STUB_DIR", "AGENTIC_STUB_PLAN", "AGENTIC_STUB_KILL_AT")
READ_CHUNK = 1024 * 1024


def one_line(text: str) -> str:
    return " ".join(text.split())


def build_prompt(goal: str, requirements: list[tuple[str, str, bool]], note: str | None,
                 oscillating: bool, approach: str | None = None, findings: tuple[dict, ...] | list[dict] = (),
                 unproven: tuple[str, ...] | list[str] = ()) -> str:
    """requirements: (id, text, passing). Takes only ledger-derived values, never check data.

    approach: planned missions only. findings: verified reviewer findings (criterion, file, quote, problem).
    unproven: criteria whose check also passed on a copy with the worker's files emptied (the canary).
    """
    lines = [
        "You are working on a mission. Work only inside the current folder.",
        f"Mission: {goal}",
    ]
    if approach is not None:
        lines.append(f"Suggested approach (from a planning AI, unverified): {one_line(approach)}")
    lines.append("Requirements (each is checked automatically by the supervisor; you cannot see the checks):")
    for cid, text, passing in requirements:
        lines.append(f"- [{cid}] {text} — currently: {'PASSING' if passing else 'FAILING'}")
    for f in findings:
        lines.append(f'Reviewer finding (verified quote from {f["file"]}): "{one_line(f["quote"])}" — '
                     f'{one_line(f["problem"])}')
    lines += [UNPROVEN_LINE.format(id=cid) for cid in unproven]
    lines.append(f"Previous iteration note (written by the previous agent, unverified): {note or 'none'}")
    if oscillating:
        lines.append(OSCILLATION_LINE)
    lines += [
        "Rules:",
        "- When something is unclear, choose the safest reasonable option and append it to DECISIONS.md",
        '  as one line: "- <decision> (reason)".',
        "- Do not stop to ask questions. Nobody will answer.",
        "- Finish by describing what you changed. Your description is not evidence; only the checks are.",
    ]
    return "\n".join(lines)


def hard_timeout_seconds(minutes: float) -> int:
    return math.ceil((minutes + WORKER_GRACE_MINUTES) * 60)


def user_uid(user: str, role: str = "worker") -> int:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        raise ConfigError(f"{role} user {user!r} does not exist on this machine") from None


def worker_uid() -> int:
    return user_uid(config.WORKER_USER)



def worker_ugid() -> tuple[int, int]:
    try:
        entry = pwd.getpwnam(config.WORKER_USER)
    except KeyError:
        raise ConfigError(f"worker user {config.WORKER_USER!r} does not exist on this machine") from None
    return entry.pw_uid, getattr(entry, "pw_gid", entry.pw_uid)

def systemd_escape(arg: str) -> str:
    """Make an argument survive systemd's ExecStart expansion unchanged.

    systemd replaces ${NAME} with an environment value (empty if unset) and $$ with $ in every
    argument; systemd 239 has no --expand-environment=no. Doubling each $ yields the original text.
    '%' needs no escaping: transient units do not expand specifiers in arguments (verified on 239).
    """
    return arg.replace("$", "$$")


def unit_prefix(user: str, workdir: str | Path, runtime_seconds: int, uid: int, argv: list[str],
                extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    """systemd-run prefix that runs argv as `user` in a one-off unit (shared by the worker and planner calls).

    When the main process ends (or the unit is stopped) systemd kills the whole control group, so
    processes the agent detached with setsid/nohup die with it. RuntimeMaxSec makes the unit itself
    enforce the timeout: killing only the systemd-run client would leave the unit running.
    Slice=user-<uid>.slice puts the unit under the user's own resource limits instead of
    system.slice. `extra` are further unit properties. Everything after "--" is escaped with systemd_escape.
    """
    props = [
        "KillMode=control-group", f"TimeoutStopSec={WORKER_STOP_TIMEOUT_SECONDS}",
        f"RuntimeMaxSec={runtime_seconds}", f"WorkingDirectory={workdir}", f"Slice=user-{uid}.slice", *extra,
    ]
    cmd = ["systemd-run", f"--uid={user}", "--wait", "--pipe", "--collect", "--quiet"]
    for prop in props:
        cmd += ["-p", prop]
    return cmd + ["--", *map(systemd_escape, argv)]


def omp_argv(agent_dir: str | Path | None = None) -> list[str]:
    if agent_dir is None:
        return list(config.OMP_ARGV)
    return [
        config.OMP_ARGV[0],
        config.OMP_ARGV[1],
        f"PI_CODING_AGENT_DIR={agent_dir}",
        *config.OMP_ARGV[2:],
    ]


def sandbox_properties(workdir: str | Path, agent_dir: str | Path | None = None) -> list[str]:
    """The worker unit's sandbox (SPEC-F section 1, SPEC-H section 4): every home folder is an empty tmpfs except
    the worker folder, the omp configuration is read-only except the state files omp writes and the throw-away
    agent directory, and the supervisor's state, the planner's home, the spool and the bot token cannot be reached."""
    return [
        "ProtectHome=tmpfs", f"BindPaths={workdir}",
        f"BindReadOnlyPaths={config.WORKER_OMP_DIR}",
        "BindPaths=" + " ".join(f"-{path}" for path in config.WORKER_OMP_STATE),
        *([f"BindPaths={agent_dir}"] if agent_dir is not None else []),
        "PrivateTmp=yes", "NoNewPrivileges=yes",
        "InaccessiblePaths=" + " ".join(config.WORKER_INACCESSIBLE),
    ]


def default_prefix(workdir: str | Path, runtime_seconds: int, uid: int,
                   agent_dir: str | Path | None = None) -> list[str]:
    """The production worker: a one-off, sandboxed systemd unit running the pinned omp as the worker user."""
    return unit_prefix(config.WORKER_USER, workdir, runtime_seconds, uid, omp_argv(agent_dir),
                       sandbox_properties(workdir, agent_dir))


def build_command(cfg: Config, model: str, thinking: str, minutes: float, prompt: str,
                  workdir: str | Path, agent_dir: str | Path | None = None) -> list[str]:
    tail = ["--model", model, "--thinking", thinking, "--max-time", f"{minutes:g}m", prompt]
    if cfg.worker_cmd is not None:  # override (tests): used verbatim, no systemd, no escaping
        return [*cfg.worker_cmd, *tail]
    prefix = default_prefix(workdir, hard_timeout_seconds(minutes), worker_uid(), agent_dir)
    return [*prefix, *map(systemd_escape, tail)]


def worker_env(cfg: Config, agent_dir: str | Path) -> dict[str, str]:
    """Clean, explicit environment for a worker run: never inherits session variables from the supervisor."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(cfg.effective_worker_home()),
        "PI_CODING_AGENT_DIR": str(agent_dir),
    }
    if cfg.worker_cmd is not None:
        for key in STUB_ENV_KEYS:
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def _copy_secret_file(src: Path, dst: Path, owner: tuple[int, int] | None) -> None:
    """Copy one agent config file without following symlinks or logging its contents."""
    try:
        in_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return
    try:
        out_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            while chunk := os.read(in_fd, READ_CHUNK):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out_fd, view):]
            if owner is not None:
                os.fchown(out_fd, *owner)
        finally:
            os.close(out_fd)
    finally:
        os.close(in_fd)


AGENT_DIR_PREFIX = ".omp-agent-"
AGENT_DIRS_REGISTRY = "agent-dirs"  # under cfg.state: one marker per live throw-away agent folder


def _registry(cfg: Config) -> Path:
    return cfg.state / AGENT_DIRS_REGISTRY


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_agent_dirs(cfg: Config) -> set[str]:
    """Absolute paths of the throw-away agent folders that supervisor processes created and still use (any mission).
    A marker whose process is gone is stale and removed. The registry lives in the supervisor's state folder, which
    a worker cannot write, so a worker cannot hide a folder of its own by naming it like an agent folder."""
    found: set[str] = set()
    try:
        entries = list(os.scandir(_registry(cfg)))
    except OSError:
        return found
    for entry in entries:
        try:
            pid_text, _, path = Path(entry.path).read_text(encoding="utf-8").partition("\n")
            pid = int(pid_text)
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            found.add(os.path.abspath(path))
        else:
            with contextlib.suppress(OSError):
                os.unlink(entry.path)
    return found


def _create_registered_dir(cfg: Config, parent: Path | None, prefix: str) -> tuple[Path, Path]:
    """Register a fresh agent folder name, then create the folder (0700): another mission's leftover scan sees the
    marker before it can see the folder. Returns (folder, marker)."""
    registry = _registry(cfg)
    registry.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = Path(parent) if parent is not None else Path(tempfile.gettempdir())
    while True:
        name = prefix + secrets.token_hex(8)
        marker = registry / name
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            continue
        agent_dir = base / name
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n{os.path.abspath(agent_dir)}")
        try:
            os.mkdir(agent_dir, 0o700)
        except FileExistsError:
            marker.unlink()
            continue
        except BaseException:
            marker.unlink()
            raise
        return agent_dir, marker


def prepare_agent_dir(cfg: Config, agent_src: str | Path | None = None) -> tuple[Path, Path]:
    """Create a registered mode-0700 throw-away omp agent directory and copy models.yml and config.yml into it.
    Returns (folder, registry marker); `release_agent_dir` removes both."""
    home = cfg.effective_worker_home()
    parent = home if home.is_dir() and os.access(home, os.W_OK) else None
    prefix = AGENT_DIR_PREFIX if parent is not None else "agentic-omp-"
    agent_dir, marker = _create_registered_dir(cfg, parent, prefix)
    try:
        os.chmod(agent_dir, 0o700)
        # Only the production command runs as the worker account; a configured command runs as the caller.
        owner = worker_ugid() if os.geteuid() == 0 and cfg.worker_cmd is None else None
        if owner is not None:
            os.chown(agent_dir, *owner)
        src = Path(agent_src) if agent_src is not None else cfg.effective_worker_agent_dir()
        for name in config.WORKER_AGENT_FILES:
            _copy_secret_file(src / name, agent_dir / name, owner)
    except BaseException:
        release_agent_dir(agent_dir, marker)
        raise
    return agent_dir, marker


def release_agent_dir(agent_dir: Path, marker: Path) -> None:
    """Delete the folder first, then its marker, so the folder is never visible without a marker."""
    shutil.rmtree(agent_dir, ignore_errors=True)
    with contextlib.suppress(OSError):
        marker.unlink()


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return "unreadable"
    try:
        while chunk := os.read(fd, READ_CHUNK):
            digest.update(chunk)
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)
    return digest.hexdigest()


def home_snapshot(home: str | Path, workdir: str | Path, workspaces: str | Path,
                  agent_dirs: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    """Snapshot of `home` excluding `workdir`, `workspaces`, the `agent_dirs` (absolute paths) and
    `config.WORKER_OMP_STATE`."""
    home_path = Path(home)
    excluded = {
        os.path.abspath(str(workdir)),
        os.path.abspath(str(workspaces)),
        os.path.abspath(str(home_path / "missions")),
        *(os.path.abspath(p) for p in config.WORKER_OMP_STATE),
        *agent_dirs,
    }
    allowed_rel = frozenset(f".omp/{rel}" for rel in config._OMP_STATE_REL)
    found: dict[str, str] = {}
    _walk_home(os.path.abspath(str(home_path)), "", excluded, allowed_rel, found)
    return found


def _home_snapshot(cfg: Config, home: Path, workdir: str | Path, own: Path) -> dict[str, str]:
    """home_snapshot without the mission's own agent folder and every live agent folder of any mission. The registry
    is read before and after the walk, so a folder another mission registers during the walk is dropped as well."""
    live = live_agent_dirs(cfg) | {os.path.abspath(own)}
    found = home_snapshot(home, workdir, cfg.workspaces, live)
    root = os.path.abspath(home)
    late = [os.path.relpath(p, root) for p in live_agent_dirs(cfg) - live
            if os.path.commonpath([p, root]) == root and p != root]
    if late:
        found = {rel: sig for rel, sig in found.items()
                 if not any(rel == top or rel.startswith(top + "/") for top in late)}
    return found


def _walk_home(path: str, rel: str, excluded: set[str], allowed_rel: frozenset[str],
               found: dict[str, str]) -> None:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        abs_path = os.path.abspath(entry.path)
        name = rel + entry.name
        if abs_path in excluded or name in allowed_rel:
            continue
        if any(name.startswith(prefix + "/") for prefix in allowed_rel):
            continue
        if entry.is_symlink():
            try:
                found[name] = "symlink:" + os.readlink(entry.path)
            except OSError:
                found[name] = "symlink:"
        elif entry.is_dir(follow_symlinks=False):
            before_len = len(found)
            _walk_home(abs_path, name + "/", excluded, allowed_rel, found)
            if len(found) == before_len and name not in (".omp", ".omp/agent"):
                found[name] = "dir"
        elif entry.is_file(follow_symlinks=False):
            found[name] = _file_digest(abs_path)
        else:
            found[name] = "special"


def classify_leftovers(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    """Sorted relative POSIX paths of entries added or modified between `before` and `after`."""
    return tuple(sorted(rel for rel, sig in after.items() if before.get(rel) != sig))


def run_worker(cfg: Config, model: str, thinking: str, minutes: float, prompt: str,
               workdir: str | Path, *, agent_src: str | Path | None = None) -> WorkerResult:
    """Run the worker once: stdin is /dev/null, cwd is the worker folder, PI_CODING_AGENT_DIR is a fresh temp dir."""
    home = cfg.effective_worker_home()
    agent_dir, marker = prepare_agent_dir(cfg, agent_src)
    try:
        before_home = _home_snapshot(cfg, home, workdir, agent_dir)
        argv = build_command(cfg, model, thinking, minutes, prompt, workdir, agent_dir=agent_dir)
        # The unit enforces RuntimeMaxSec itself; the client-side limit is only a backstop.
        outer = hard_timeout_seconds(minutes) + (10 if cfg.worker_cmd is None else 0)
        started = time.monotonic()
        proc = run_process(argv, workdir, outer, env=worker_env(cfg, agent_dir))
        duration = time.monotonic() - started
        shutil.rmtree(agent_dir, ignore_errors=True)
        after_home = _home_snapshot(cfg, home, workdir, agent_dir)
    finally:
        release_agent_dir(agent_dir, marker)
    return WorkerResult(proc.exit_code, proc.timed_out, proc.stdout, proc.stderr, duration,
                        classify_leftovers(before_home, after_home))
