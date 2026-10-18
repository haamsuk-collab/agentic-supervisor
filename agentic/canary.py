"""The canary (SPEC-F section 2): a pass counts only if the check is shown to depend on the worker's files.

Each passing check runs once more on a copy of the worker folder in which every regular file the worker wrote or
changed (against the baseline manifest) is truncated to zero bytes. A check that still passes there does not look
at the worker's work: the canary did not hold.
"""

import hashlib
import os
import secrets
import shutil
from pathlib import Path

from agentic import checks
from agentic.config import SNAPSHOT_SKIP, Config
from agentic.mission import Mission

READ_CHUNK = 1024 * 1024
SYMLINK_PREFIX = "symlink:"


def manifest(root: str | Path) -> dict[str, str]:
    """Relative posix path -> sha256 of the content for regular files, `symlink:<link text>` for symlinks (never
    followed). SNAPSHOT_SKIP folders are skipped at any depth, other entries ignored. A missing folder is empty."""
    found: dict[str, str] = {}
    _walk(str(root), "", found)
    return found


def _walk(path: str, rel: str, found: dict[str, str]) -> None:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError):
        return
    for entry in entries:
        name = rel + entry.name
        if entry.is_symlink():
            found[name] = SYMLINK_PREFIX + os.readlink(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            if entry.name not in SNAPSHOT_SKIP:
                _walk(entry.path, name + "/", found)
        elif entry.is_file(follow_symlinks=False):
            found[name] = _file_sha256(entry.path)


def _file_sha256(path: str) -> str:
    content = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        while chunk := os.read(fd, READ_CHUNK):
            content.update(chunk)
    finally:
        os.close(fd)
    return content.hexdigest()


def changed_files(current: dict[str, str], baseline: dict[str, str]) -> list[str]:
    """Regular files that are new or changed against the baseline manifest (symlinks are not files)."""
    return [rel for rel, digest in current.items()
            if not digest.startswith(SYMLINK_PREFIX) and baseline.get(rel) != digest]


def changed_python(workdir: str | Path, baseline: dict[str, str]) -> list[str]:
    """The changed `.py` files of the worker folder (the mutation score runs only when there is one)."""
    return [rel for rel in changed_files(manifest(workdir), baseline) if rel.endswith(".py")]


def emptied_copy(workdir: str | Path, target: Path, baseline: dict[str, str]) -> int:
    """Snapshot the worker folder into `target` and truncate every regular file of the copy that is new or changed
    against `baseline`. Returns how many files were emptied."""
    checks.snapshot_tree(workdir, target)
    emptied = changed_files(manifest(target), baseline)
    for rel in emptied:
        fd = os.open(target / rel, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC)
        os.close(fd)
    return len(emptied)


def run(cfg: Config, mission: Mission, workdir: str | Path, criteria: list[str], baseline: dict[str, str]) -> dict:
    """Run each criterion's check on the emptied copy. The data of the `canary` record (without the iteration):
    `{"results": {cid: {"held": bool, "exit_code"}}, "changed_files": n}`. Never check content."""
    target = cfg.scratch_root(mission.id) / f"canary-{secrets.token_hex(16)}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        changed = emptied_copy(workdir, target, baseline)
        results = {}
        for cid in criteria:
            outcome = checks.run_check(cfg, mission.id, "canary", cid, mission.criterion(cid).check, target)
            results[cid] = {"held": not outcome.passed, "exit_code": outcome.exit_code}
    finally:
        shutil.rmtree(target, ignore_errors=True)
    return {"results": results, "changed_files": changed}
