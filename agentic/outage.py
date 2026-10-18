"""Model outages (SPEC-D.md section 6): telling "the model could not be used" apart from "no progress"."""

import hashlib
import os
import re
from pathlib import Path

from agentic import config
from agentic.config import FAST_EXIT_SECONDS, SNAPSHOT_SKIP
from agentic.i18n import t

PATTERNS = (
    r"rate.?limit", r"quota", r"usage.?limit", r"too many requests", r"\b429\b", r"overloaded", r"\b529\b",
    r"\b503\b", r"service unavailable", r"credit", r"unauthori[sz]ed", r"\b401\b", r"econnrefused",
    r"connection refused", r"gateway",
)
OUTAGE_RE = re.compile("|".join(PATTERNS), re.IGNORECASE)
REASON_MAX_CHARS = 200
READ_CHUNK = 1024 * 1024


def matching_line(stderr: str) -> str | None:
    """The first stderr line (whitespace collapsed, at most REASON_MAX_CHARS) that matches an outage pattern.

    Only stderr is ever searched: omp reports model errors there (and exits 1), while the model's own text on
    stdout may mention words like "gateway" or "quota" as part of normal work.
    """
    for line in (stderr or "").splitlines():
        if OUTAGE_RE.search(line):
            return " ".join(line.split())[:REASON_MAX_CHARS]
    return None


def wait_seconds(waits_so_far: int) -> int:
    """Backoff of one outage: 5, 10, 20, 40, then 60 minutes for good."""
    return config.OUTAGE_WAITS_MINUTES[min(waits_so_far, len(config.OUTAGE_WAITS_MINUTES) - 1)] * 60


def sleep_until(moment, clock, sleep, limit=None) -> bool:
    """Sleep until `moment` (a datetime or None), but not past `limit`. True if it slept."""
    if moment is None:
        return False
    until = moment if limit is None else min(moment, limit)
    remaining = (until - clock()).total_seconds()
    if remaining <= 0:
        return False
    sleep(remaining)
    return True


def folder_digest(root: str | Path) -> str:
    """sha256 over the sorted relative paths and contents of the folder.

    SNAPSHOT_SKIP folders are skipped. Symlinks are never followed: a link counts by its path and link text.
    Other non-regular files count by their path only. A missing folder has the digest of an empty one.
    """
    digest = hashlib.sha256()
    _walk(digest, str(root), "")
    return digest.hexdigest()


def _walk(digest, path: str, rel: str) -> None:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except FileNotFoundError:
        return
    for entry in entries:
        name = rel + entry.name
        key = name.encode("utf-8", "surrogateescape") + b"\0"
        if entry.is_symlink():
            digest.update(b"L" + key + os.readlink(entry.path).encode("utf-8", "surrogateescape") + b"\0")
        elif entry.is_dir(follow_symlinks=False):
            if entry.name in SNAPSHOT_SKIP:
                continue
            digest.update(b"D" + key)
            _walk(digest, entry.path, name + "/")
        elif entry.is_file(follow_symlinks=False):
            digest.update(b"F" + key + _file_digest(entry.path))
        else:
            digest.update(b"O" + key)


def _file_digest(path: str) -> bytes:
    content = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        return b"E" + str(exc.errno).encode()
    try:
        while chunk := os.read(fd, READ_CHUNK):
            content.update(chunk)
    finally:
        os.close(fd)
    return content.digest()


def worker_reason(exit_code: int | None, timed_out: bool, stderr: str, duration: float, unchanged) -> str | None:
    """Why a worker run counts as unavailable, or None for a normal iteration.

    Non-zero exit (a timeout or a failed start counts as non-zero) and either an outage pattern in stderr (never
    the final text), or a run shorter than FAST_EXIT_SECONDS that left the folder unchanged. `unchanged` is a
    callable, so the folder is only hashed when it matters.
    """
    if exit_code == 0:
        return None
    line = matching_line(stderr)
    if line:
        return t("outage.error_line", line=line)
    if duration < FAST_EXIT_SECONDS and unchanged():
        if exit_code is not None:
            how = t("outage.how.exit_code", code=exit_code)
        elif timed_out:
            how = t("outage.how.timed_out")
        else:
            how = t("outage.how.start_failed")
        return t("outage.fast_exit", seconds=f"{duration:.0f}", how=how)
    return None
