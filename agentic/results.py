"""Build the `/files` zip from a worker folder.

The worker folder is untrusted: the worker user can create symlinks, hard links, fifos and
can rename things while the walk runs. The walk therefore never uses paths: every folder is
opened relative to its parent fd with O_NOFOLLOW, every file is opened with O_NOFOLLOW and
O_NONBLOCK and checked with fstat on the open fd, and the zip is streamed in chunks into a
size-limited buffer, so neither a huge file nor a fifo can exhaust memory or hang the root.
"""

import io
import os
import stat
import time
import zipfile

from . import config
from .i18n import t

CHUNK = 64 * 1024
MAX_DEPTH = 64  # each open folder holds one fd; deeper trees are cut off, not followed
_MIN_ZIP_YEAR = 1980
_MAX_ZIP_YEAR = 2107
_ZIP_FILE_ATTR = (stat.S_IFREG | 0o644) << 16
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class NoResults(Exception):
    """The worker folder is missing or holds no regular file to send."""


class TooLarge(Exception):
    """The zip is over the limit. `size` is in bytes; `at_least` is True when building stopped
    early at the limit, so `size` is only a lower bound of the real zip size."""

    def __init__(self, size: int, at_least: bool = False):
        super().__init__(f"zip is {'at least ' if at_least else ''}{size} bytes")
        self.size = size
        self.at_least = at_least


def format_size(size: int, at_least: bool = False) -> str:
    """MiB with one decimal, e.g. `21.3MB`, using the configured lower-bound wording when `size` is a lower bound."""
    text = f"{size / (1024 * 1024):.1f}MB"
    return t("results.at_least", size=text) if at_least else text

class _LimitedBuffer:
    """Non-seekable in-memory sink that raises TooLarge as soon as a write passes `max_bytes`.

    Having no seek() makes zipfile write data descriptors, so every byte is appended exactly
    once and `size` is always the true number of bytes produced. After the limit is hit, later
    writes (zipfile's close during unwinding) are ignored."""

    def __init__(self, max_bytes: int):
        self._buffer = io.BytesIO()
        self._max = max_bytes
        self.size = 0
        self.tripped = False

    def write(self, data) -> int:
        if self.tripped:
            return len(data)
        total = self.size + len(data)
        if total > self._max:
            self.tripped = True
            raise TooLarge(total, at_least=True)
        self._buffer.write(data)
        self.size = total
        return len(data)

    def tell(self) -> int:
        return self.size

    def flush(self) -> None:
        pass

    def getvalue(self) -> bytes:
        return self._buffer.getvalue()


def _zip_time(mtime: float) -> tuple:
    try:
        stamp = time.localtime(mtime)
    except (OverflowError, OSError, ValueError):
        stamp = time.localtime(0)
    if stamp.tm_year < _MIN_ZIP_YEAR:
        return (_MIN_ZIP_YEAR, 1, 1, 0, 0, 0)
    if stamp.tm_year > _MAX_ZIP_YEAR:
        return (_MAX_ZIP_YEAR, 12, 31, 23, 59, 58)
    return tuple(stamp[:6])


def _zip_info(name: str, mtime: float) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _zip_time(mtime))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = _ZIP_FILE_ATTR
    return info


def _is_utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _walk(dfd: int, prefix: str, depth: int, owner_uid, report_name: str, emit) -> None:
    """Depth-first over the folder open as `dfd`, entries in name order. Calls `emit(rel, fd, st)`
    for every regular file that passes the filters; the caller reads and closes nothing."""
    try:
        with os.scandir(dfd) as scanner:
            entries = sorted(scanner, key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if not _is_utf8(name):
            continue
        try:
            if entry.is_symlink():
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if not is_dir and not entry.is_file(follow_symlinks=False):
                continue  # device, fifo, socket: never opened
        except OSError:
            continue
        rel = prefix + name
        if is_dir:
            if name in config.SNAPSHOT_SKIP or depth >= MAX_DEPTH:
                continue
            try:
                sub = os.open(name, _DIR_FLAGS, dir_fd=dfd)
            except OSError:
                continue
            try:
                _walk(sub, rel + "/", depth + 1, owner_uid, report_name, emit)
            finally:
                os.close(sub)
            continue
        if rel == report_name:
            continue  # the supervisor's report wins
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                continue
            if owner_uid is not None and st.st_uid != owner_uid:
                continue
            emit(rel, fd, st)
        except OSError:
            continue  # vanished or unreadable mid-walk
        finally:
            os.close(fd)


def build_zip(workdir, report_name: str, report_text: str, max_bytes: int = config.DOCUMENT_MAX_BYTES,
              owner_uid: int | None = None) -> tuple[bytes, int]:
    """Zip the regular files of `workdir` plus `report_text` as `report_name` at the zip root.

    Returns (zip bytes, number of files in the zip including the report). Raises NoResults when
    the folder is missing or contributes no file, and TooLarge when the zip would pass `max_bytes`."""
    try:
        root = os.open(os.fspath(workdir), _DIR_FLAGS)
    except OSError:
        raise NoResults(f"no worker folder: {workdir}") from None
    sink = _LimitedBuffer(max_bytes)
    count = 0

    def add_file(rel: str, fd: int, st: os.stat_result) -> None:
        nonlocal count
        with archive.open(_zip_info(rel, st.st_mtime), "w") as out:
            count += 1  # the entry exists from here on, even if a read error cuts it short
            while True:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                out.write(chunk)

    try:
        archive = zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED)
        try:
            archive.writestr(_zip_info(report_name, time.time()), report_text.encode("utf-8"))
            _walk(root, "", 0, owner_uid, report_name, add_file)
            if count == 0:
                raise NoResults(f"no regular files in {workdir}")
            archive.close()
        except BaseException:
            sink.tripped = True  # unwinding must not write (or raise) any more
            archive.close()
            raise
    finally:
        os.close(root)
    data = sink.getvalue()
    if len(data) > max_bytes:
        raise TooLarge(len(data), at_least=False)
    return data, count + 1
