"""Run acceptance checks on a snapshot copy of the worker folder. The exit code is the only result."""

import contextlib
import hashlib
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentic.config import SNAPSHOT_SKIP, TAIL_BYTES, Config
from agentic.ledger import manifest_digest
from agentic.mission import Check


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ProcResult:
    exit_code: int | None  # None: timed out or could not start
    timed_out: bool
    stdout: str  # last TAIL_BYTES
    stderr: str
    # sha256 and size of the full output (not just the kept tail); a process that never started wrote nothing
    stdout_sha256: str = EMPTY_SHA256
    stdout_bytes: int = 0
    stderr_sha256: str = EMPTY_SHA256
    stderr_bytes: int = 0


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_sha256: str = EMPTY_SHA256
    stdout_bytes: int = 0
    stderr_sha256: str = EMPTY_SHA256
    stderr_bytes: int = 0
    snapshot_sha256: str | None = None  # tree_digest of the snapshot before the check files were written
    manifest: dict[str, str] | None = None  # the tree_manifest behind snapshot_sha256 (kept in memory only)

    def to_data(self) -> dict:
        return {"passed": self.passed, "exit_code": self.exit_code, "timed_out": self.timed_out,
                "stdout": self.stdout, "stderr": self.stderr,
                "stdout_sha256": self.stdout_sha256, "stdout_bytes": self.stdout_bytes,
                "stderr_sha256": self.stderr_sha256, "stderr_bytes": self.stderr_bytes,
                "snapshot_sha256": self.snapshot_sha256}


def _tail(handle, limit: int = TAIL_BYTES) -> str:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(max(0, size - limit))
    return handle.read().decode("utf-8", errors="replace")


def _digest(handle) -> tuple[str, int]:
    """sha256 and byte count of everything written to `handle`."""
    handle.seek(0)
    digest, size = hashlib.sha256(), 0
    while chunk := handle.read(READ_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


CHECK_PATH = "/usr/bin:/bin"  # SPEC-H section 1: the fixed PATH of every check process
CHECK_HOME = ".home"  # HOME of a check process, inside its snapshot
HIJACK_NAMES = frozenset({"conftest.py", "sitecustomize.py", "usercustomize.py"})
MODULE_SUFFIXES = (".py", ".pyc", ".so")


def _hijacks(name: str, is_dir: bool, path: str) -> bool:
    """A worker file that a check importing or running Python could pick up instead of the real thing: a module or
    package named like a standard-library top-level module, conftest/sitecustomize/usercustomize, or a .pth file."""
    if is_dir:
        return name in sys.stdlib_module_names and os.path.isfile(os.path.join(path, "__init__.py"))
    if name in HIJACK_NAMES or name.endswith(".pth"):
        return True
    return name.endswith(MODULE_SUFFIXES) and name.split(".", 1)[0] in sys.stdlib_module_names


def hijacking_files(root: str | Path) -> list[str]:
    """Relative posix paths (sorted) of the entries `remove_hijacking_files` deletes from a copy of `root`.
    Symlinks count by their own name and are never followed; SNAPSHOT_SKIP folders are not copied, so not listed."""
    found: list[str] = []
    _scan(str(root), "", found)
    return sorted(found)


def _scan(path: str, rel: str, found: list[str]) -> None:
    try:
        entries = list(os.scandir(path))
    except (FileNotFoundError, NotADirectoryError):
        return
    for entry in entries:
        is_dir = entry.is_dir(follow_symlinks=False)
        if is_dir and entry.name in SNAPSHOT_SKIP:
            continue
        if _hijacks(entry.name, is_dir, entry.path):
            found.append(rel + entry.name)
        elif is_dir:
            _scan(entry.path, rel + entry.name + "/", found)


def remove_hijacking_files(snapshot: str | Path) -> list[str]:
    """SPEC-H section 1: delete, inside a snapshot only, every entry that could hijack a check. Returns them."""
    found = hijacking_files(snapshot)
    for rel in found:
        path = os.path.join(snapshot, rel)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    return found


def check_env(snapshot: str | Path) -> dict[str, str]:
    """The whole environment of a check process: a fixed PATH and a HOME inside the snapshot, nothing inherited
    (so no PYTHON* variable either)."""
    return {"PATH": CHECK_PATH, "HOME": os.path.join(str(snapshot), CHECK_HOME)}


def run_process(argv: list[str], cwd: str | Path, timeout: float, stdout_bytes: int = TAIL_BYTES,
                stdin: bytes | None = None, env: dict[str, str] | None = None) -> ProcResult:
    """Run argv (never through a shell) in its own process group. stdin is /dev/null, or `stdin` when given.

    The group is killed on timeout, and also after a normal exit so no stray children survive.
    Output goes to temp files, so a chatty or orphaned child cannot block or exhaust memory.
    `stdin` is fed from a temp file as well: a child that does not read it cannot block the supervisor.
    Only the last `stdout_bytes` of stdout (and the last TAIL_BYTES of stderr) are returned.
    `env`, when given, is the child's whole environment; otherwise it inherits the supervisor's.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, contextlib.ExitStack() as stack:
        source = subprocess.DEVNULL
        if stdin is not None:
            source = stack.enter_context(tempfile.TemporaryFile())
            source.write(stdin)
            source.seek(0)
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=source, stdout=out, stderr=err, start_new_session=True,
                                    env=env)
        except OSError as exc:
            return ProcResult(None, False, "", f"cannot start {argv[0]!r}: {exc}")
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            # Wait without reaping, so the pid (= process group id) cannot be recycled before we kill the group.
            while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.02)
        except BaseException:  # interrupted (signal, test time limit): never leave the child running
            _kill_group(proc.pid)
            proc.wait()
            raise
        _kill_group(proc.pid)
        proc.wait()
        out_sha, out_bytes = _digest(out)
        err_sha, err_bytes = _digest(err)
        return ProcResult(None if timed_out else proc.returncode, timed_out, _tail(out, stdout_bytes), _tail(err),
                          out_sha, out_bytes, err_sha, err_bytes)


def build_command(check: Check, snapshot: str | Path, mode: str) -> list[str]:
    """The argv that is actually executed for a check. In systemd mode the unit gets the same clean environment
    as a local check (systemd-run does not pass the caller's environment on)."""
    if mode == "local":
        return list(check.argv)
    props = [
        "DynamicUser=yes", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=yes",
        "NoNewPrivileges=yes", f"RuntimeMaxSec={check.timeout}", f"WorkingDirectory={snapshot}",
        f"ReadWritePaths={snapshot}",
    ]
    if not check.network:
        props.append("PrivateNetwork=yes")
    cmd = ["systemd-run", "--wait", "--pipe", "--collect", "--quiet"]
    for prop in props:
        cmd += ["-p", prop]
    cmd += [f"--setenv={key}={value}" for key, value in check_env(snapshot).items()]
    return cmd + ["--", *check.argv]


def snapshot_tree(src: str | Path, dst: str | Path) -> None:
    """Copy src to dst. Symlinks are copied as links (never followed); fifos/sockets/devices are skipped."""
    os.makedirs(dst, exist_ok=True)
    with os.scandir(src) as entries:
        for entry in entries:
            if entry.name in SNAPSHOT_SKIP:
                continue
            target = os.path.join(dst, entry.name)
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), target)
            elif entry.is_dir(follow_symlinks=False):
                snapshot_tree(entry.path, target)
            elif entry.is_file(follow_symlinks=False):
                shutil.copy2(entry.path, target)


def tree_manifest(root: str | Path) -> dict[str, str]:
    """What `snapshot_tree` copies, by relative posix path: sha256 of each regular file, `symlink:<link text>` of each
    symlink (never followed). Entries named in SNAPSHOT_SKIP, fifos, sockets and devices are left out, exactly as the
    copy leaves them out, so a folder and its snapshot have the same manifest. A missing folder is empty."""
    found: dict[str, str] = {}
    _manifest_walk(str(root), "", found)
    return found


def _manifest_walk(path: str, rel: str, found: dict[str, str]) -> None:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError):
        return
    for entry in entries:
        if entry.name in SNAPSHOT_SKIP:
            continue
        name = rel + entry.name
        if entry.is_symlink():
            found[name] = "symlink:" + os.readlink(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            _manifest_walk(entry.path, name + "/", found)
        elif entry.is_file(follow_symlinks=False):
            found[name] = _file_sha256(entry.path)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        while chunk := os.read(fd, READ_CHUNK):
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _write_check_file(root: str, rel: str, content: str) -> None:
    """Write a check file into the snapshot without ever following a worker-planted symlink."""
    parts = PurePosixPath(rel).parts
    current = root
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current) or (os.path.lexists(current) and not os.path.isdir(current)):
            os.unlink(current)
        os.makedirs(current, exist_ok=True)
    leaf = os.path.join(current, parts[-1])
    if os.path.isdir(leaf) and not os.path.islink(leaf):
        shutil.rmtree(leaf)
    elif os.path.lexists(leaf):
        os.unlink(leaf)
    fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def _open_up(root: str) -> None:
    """Let the systemd DynamicUser read and write the snapshot. Symlinks are never chmod-ed."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                os.chmod(path, 0o777)
            elif stat.S_ISREG(info.st_mode):
                os.chmod(path, stat.S_IMODE(info.st_mode) | 0o666)
    os.chmod(root, 0o777)


def _make_traversable(path: Path, stop: Path) -> None:
    """The DynamicUser must walk down to the snapshot: o+x on scratch/, scratch/<id>/, scratch/<id>/<n>/."""
    while True:
        os.chmod(path, 0o711)
        if path == stop or stop not in path.parents:
            break
        path = path.parent


def run_check(cfg: Config, mission_id: str, iteration: int, criterion_id: str,
              check: Check, workdir: str | Path) -> CheckResult:
    """Snapshot the worker folder, run one check on the copy, delete the copy, return the result.

    The snapshot directory name carries 128 random bits so it cannot be guessed.
    """
    snapshot = cfg.scratch_dir(mission_id, iteration) / f"{criterion_id}-{secrets.token_hex(16)}"
    snapshot.mkdir(parents=True, mode=0o700)
    try:
        snapshot_tree(workdir, snapshot)
        manifest = tree_manifest(snapshot)  # what the check sees of the worker's folder, before any check file
        remove_hijacking_files(snapshot)  # before the check files: those are the planner's, never removed
        home = snapshot / CHECK_HOME
        if home.is_dir() and not home.is_symlink():  # a worker-planted .home must not configure the check's tools
            shutil.rmtree(home)
        elif os.path.lexists(home):
            home.unlink()
        home.mkdir()
        for rel, content in check.files.items():
            _write_check_file(str(snapshot), rel, content)
        if cfg.check_mode == "systemd":
            _open_up(str(snapshot))
            _make_traversable(snapshot.parent, cfg.state / "scratch")
        # systemd enforces RuntimeMaxSec itself; the outer limit is only a backstop.
        outer = check.timeout + (10 if cfg.check_mode == "systemd" else 0)
        proc = run_process(build_command(check, snapshot, cfg.check_mode), snapshot, outer,
                           env=check_env(snapshot))
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
        try:
            snapshot.parent.rmdir()  # the iteration directory, once its last snapshot is gone
        except OSError:
            pass
    return CheckResult(proc.exit_code == 0 and not proc.timed_out, proc.exit_code, proc.timed_out,
                       proc.stdout, proc.stderr, proc.stdout_sha256, proc.stdout_bytes, proc.stderr_sha256,
                       proc.stderr_bytes, manifest_digest(manifest), manifest)


LEAK_LENGTH = 16


def leaks_check_content(text: str, checks: list[Check]) -> bool:
    """True if `text` shares a LEAK_LENGTH-character substring with any check's argv or file contents."""
    grams = {text[i:i + LEAK_LENGTH] for i in range(len(text) - LEAK_LENGTH + 1)}
    if not grams:
        return False
    for check in checks:
        for haystack in (*check.argv, " ".join(check.argv), *check.files.values()):
            if any(gram in haystack for gram in grams):
                return True
    return False
