"""Spool folders shared with the Telegram bot (SPEC-D.md section 1). Every item is one JSON object in one file.

Writers create `<name>.tmp` with O_CREAT|O_EXCL|O_NOFOLLOW (mode 0600) and rename it into place inside the
folder, so a reader never sees half a file and no symlink is ever followed. Readers skip `*.tmp` names.

An outbox item may refer to a document (SPEC-E.md section 4): the file `<name>.doc` in the same folder, written
the same way before its item. Item readers skip `*.doc` names; the bot deletes the document with its item.
"""

import errno
import json
import os
import pwd
import secrets
import stat
from pathlib import Path

from agentic.config import DOC_SUFFIX, DOCUMENT_MAX_BYTES

TMP_SUFFIX = ".tmp"


class SpoolError(ValueError):
    """An item that must be rejected. The message is the reason."""


class DocumentTooLarge(ValueError):
    """A document over DOCUMENT_MAX_BYTES; nothing was written."""

    def __init__(self, size: int):
        super().__init__(f"the document has {size} bytes; the limit is {DOCUMENT_MAX_BYTES}")
        self.size = size


def open_dir(directory: str | Path) -> int:
    """A descriptor of the spool folder itself (never through a symlink). FileNotFoundError if it is missing."""
    return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def owner_of(user: str) -> tuple[int, int]:
    """(uid, gid) of `user`; KeyError if the account does not exist."""
    entry = pwd.getpwnam(user)
    return entry.pw_uid, entry.pw_gid


def root_owner(user: str) -> tuple[int, int] | None:
    """The owner root must give its files: the bot account when running as root, else None (tests, hand runs)."""
    return owner_of(user) if os.geteuid() == 0 else None


def valid_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\0" not in name and not name.startswith(".") \
        and not name.endswith(TMP_SUFFIX)


def is_document_name(name: str) -> bool:
    return valid_name(name) and name.endswith(DOC_SUFFIX) and len(name) > len(DOC_SUFFIX)


def write_item(directory: str | Path, payload: dict, name: str | None = None,
               owner: tuple[int, int] | None = None) -> str:
    """Write `payload` as the file `name` (default: secrets.token_hex(16)) and return the name.

    FileNotFoundError: the folder does not exist. FileExistsError: `name` or `name.tmp` already exists
    (whatever it is, a planted symlink included). `owner`: fchown the file to (uid, gid) before it is renamed.
    """
    name = name or secrets.token_hex(16)
    if not valid_name(name) or name.endswith(DOC_SUFFIX):
        raise ValueError(f"invalid spool file name {name!r}")
    _write_file(directory, name, json.dumps(payload, ensure_ascii=False).encode("utf-8"), owner)
    return name


def write_document(directory: str | Path, data: bytes, name: str | None = None,
                   owner: tuple[int, int] | None = None) -> str:
    """Write `data` as the document file `<name>.doc` (default name: secrets.token_hex(16)) exactly like an item
    and return the file name. DocumentTooLarge (nothing written) over DOCUMENT_MAX_BYTES; FileNotFoundError and
    FileExistsError as for write_item."""
    if len(data) > DOCUMENT_MAX_BYTES:
        raise DocumentTooLarge(len(data))
    name = (name or secrets.token_hex(16)) + DOC_SUFFIX
    if not is_document_name(name):
        raise ValueError(f"invalid spool file name {name!r}")
    _write_file(directory, name, data, owner)
    return name


def _write_file(directory: str | Path, name: str, data: bytes, owner: tuple[int, int] | None) -> None:
    dfd = open_dir(directory)
    try:
        try:
            os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "spool item already exists", f"{directory}/{name}")
        tmp = name + TMP_SUFFIX
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            if owner is not None:
                os.fchown(fd, *owner)
        except BaseException:
            os.close(fd)
            os.unlink(tmp, dir_fd=dfd)
            raise
        os.close(fd)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        os.close(dfd)


def list_names(dfd: int) -> list[str]:
    """Names of finished items, oldest first (modification time, then name). `*.tmp`, `*.doc` (documents, read
    through their item) and dot files are skipped."""
    found = []
    for name in os.listdir(dfd):
        if not valid_name(name) or name.endswith(DOC_SUFFIX):
            continue
        try:
            info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        found.append((info.st_mtime_ns, name))
    return [name for _, name in sorted(found)]


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SpoolError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constants(name: str) -> object:
    raise SpoolError(f"{name} is not allowed")


def read_item(dfd: int, name: str, max_bytes: int, owner_uid: int | None = None) -> dict:
    """The JSON object in `name`. Only a regular file (no symlink), owned by `owner_uid` when given, at most
    `max_bytes`. Raises SpoolError with the reason otherwise."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SpoolError("is a symlink") from None
        raise SpoolError(f"cannot be opened: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SpoolError("is not a regular file")
        if owner_uid is not None and info.st_uid != owner_uid:
            raise SpoolError(f"is owned by uid {info.st_uid}, not {owner_uid}")
        if info.st_size > max_bytes:
            raise SpoolError(f"is larger than {max_bytes} bytes")
        chunks, size = [], 0
        while size <= max_bytes:
            chunk = os.read(fd, max_bytes + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > max_bytes:
            raise SpoolError(f"is larger than {max_bytes} bytes")
    finally:
        os.close(fd)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_no_duplicates,
                           parse_constant=_no_constants)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise SpoolError("is not valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise SpoolError("is not a JSON object")
    return value


def read_document(dfd: int, name: str, max_bytes: int) -> bytes:
    """The bytes of the document file `name`. Only a regular file (no symlink) of at most `max_bytes`.
    Raises SpoolError with the reason otherwise."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SpoolError("is a symlink") from None
        raise SpoolError(f"cannot be opened: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SpoolError("is not a regular file")
        if info.st_size > max_bytes:
            raise SpoolError(f"is larger than {max_bytes} bytes")
        chunks, size = [], 0
        while size <= max_bytes:
            chunk = os.read(fd, max_bytes + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > max_bytes:
            raise SpoolError(f"is larger than {max_bytes} bytes")
    finally:
        os.close(fd)
    return b"".join(chunks)


def remove(dfd: int, name: str) -> None:
    """Delete the entry `name` itself (a symlink is removed, not followed). A missing entry is fine."""
    try:
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        os.rmdir(name, dir_fd=dfd)
