"""Append-only, hash-chained ledger stored as JSON Lines."""

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agentic import faults

GENESIS = "0" * 64
RECORD_TYPES = (
    "mission_started", "baseline", "iteration_worker", "check_result",
    "escalated", "gave_up", "stopped_time", "mission_finished",
    "plan_started", "plan_step", "plan_finished", "plan_failed", "review", "review_veto",
    "worker_unavailable", "plan_unavailable",
    "canary", "mutation", "checks_strengthened", "notice",
    "hygiene", "plan_filter", "checks_update", "checks_update_rejected",  # SPEC-H sections 1-3
)
_FIELDS = {"seq", "ts", "type", "data", "prev", "hash"}
FS_IOC_GETFLAGS = 0x80086601  # linux/fs.h, as used by lsattr
FS_APPEND_FL = 0x00000020  # the `a` attribute (chattr +a)


class LedgerError(Exception):
    """The ledger chain is broken; nothing may be read from it."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    count: int  # number of valid records before the first problem
    bad_seq: int | None = None  # 1-based position of the first bad record
    reason: str | None = None
    bound: bool | None = None  # mission_started carries a contract hash; False: an old, unbound ledger; None: none


class Ledger:
    def __init__(self, path: str | Path, clock: Callable[[], datetime] = utcnow):
        self.path = Path(path)
        self.clock = clock
        self._tail: tuple[int, str] | None = None  # (seq, hash) of the last record

    def _scan(self) -> tuple[list[dict], VerifyResult]:
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return [], VerifyResult(True, 0)
        records: list[dict] = []
        prev = GENESIS
        pieces = blob.split(b"\n")
        if pieces and pieces[-1] == b"":
            pieces.pop()
        elif pieces:
            return [], VerifyResult(False, 0, len(pieces), "last line is incomplete")

        def bad(reason: str) -> tuple[list[dict], VerifyResult]:
            return records, VerifyResult(False, len(records), len(records) + 1, reason)

        for piece in pieces:
            try:
                rec = json.loads(piece.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return bad("line is not valid JSON")
            if not isinstance(rec, dict) or set(rec) != _FIELDS:
                return bad("record has wrong fields")
            if rec["seq"] != len(records) + 1:
                return bad(f"seq is {rec['seq']!r}, expected {len(records) + 1}")
            if rec["type"] not in RECORD_TYPES or not isinstance(rec["data"], dict):
                return bad("unknown type or malformed data")
            if rec["prev"] != prev:
                return bad("prev does not match the previous record's hash")
            if rec["hash"] != compute_hash(rec):
                return bad("hash does not match the record content")
            records.append(rec)
            prev = rec["hash"]
        bound, problem = binding(records)
        if problem is not None:
            index, reason = problem
            return records[:index], VerifyResult(False, index, index + 1, reason, bound)
        return records, VerifyResult(True, len(records), bound=bound)

    def verify(self) -> VerifyResult:
        """Recompute the whole chain and report the first bad record."""
        return self._scan()[1]

    def read(self) -> list[dict]:
        """Verify, then return all records. Raises LedgerError on a broken chain."""
        records, result = self._scan()
        if not result.ok:
            raise LedgerError(f"ledger {self.path} is broken at record {result.bad_seq}: {result.reason}")
        self._tail = (len(records), records[-1]["hash"]) if records else (0, GENESIS)
        return records

    def append(self, record_type: str, data: dict) -> dict:
        if record_type not in RECORD_TYPES:
            raise ValueError(f"unknown record type {record_type!r}")
        if self._tail is None:
            self.read()
        seq, prev = self._tail
        record = {"seq": seq + 1, "ts": format_ts(self.clock()), "type": record_type,
                  "data": data, "prev": prev}
        record["hash"] = compute_hash(record)
        line = (canonical(record) + "\n").encode("utf-8")
        is_new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if is_new:
            _fsync_dir(self.path.parent)
        self._tail = (record["seq"], record["hash"])
        durable = json.loads(line)
        faults.point(faults.APPENDED, durable)
        return durable


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def manifest_digest(manifest: dict[str, str]) -> str:
    """sha256 of the canonical JSON of a tree manifest (relative path -> content sha256 or link text)."""
    return hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()


def binding(records: list[dict]) -> tuple[bool | None, tuple[int, str] | None]:
    """Whether the ledger binds its evidence (mission_started has contract_sha256), and the first record whose
    binding does not hold as (0-based index, reason). An old ledger without these fields is unbound, never broken.

    Every proof of a mission_finished record must name, by seq and hash, the latest check_result of its criterion,
    which passed, ran in the last iteration on the final snapshot, with the check and the contract in force then."""
    started = next((r for r in records if r["type"] == "mission_started"), None)
    if started is None:
        return None, None
    if "contract_sha256" not in started["data"]:
        return False, None
    index = records.index(started)
    try:
        contract = started["data"]["contract_sha256"]
        check_hash = dict(started["data"].get("check_sha256") or {})
        criteria = [c["id"] for c in started["data"]["criteria"]]
        latest: dict[str, int] = {}  # criterion -> seq of its latest check_result so far
        last_iteration = 0
        for index, rec in enumerate(records):
            kind, data = rec["type"], rec["data"]
            if kind == "iteration_worker":
                last_iteration = max(last_iteration, data["iteration"])
            elif kind == "check_result":
                latest[data["criterion"]] = rec["seq"]
            elif kind == "checks_update":
                check_hash[data["criterion"]] = data["new_sha256"]
                contract = data.get("new_contract_sha256", contract)
            elif kind == "checks_strengthened" and data.get("ok") and data.get("contract_sha256"):
                contract = data["contract_sha256"]
            elif kind == "mission_finished":
                reason = _proofs_problem(records, index, criteria, check_hash, contract, latest, last_iteration)
                if reason is not None:
                    return True, (index, reason)
    except (KeyError, TypeError, ValueError, AttributeError):
        return True, (index, "malformed evidence-binding fields")
    return True, None


def _proofs_problem(records: list[dict], index: int, criteria: list[str], check_hash: dict[str, str],
                    contract: str, latest: dict[str, int], last_iteration: int) -> str | None:
    data = records[index]["data"]
    proofs, final = data.get("proofs"), data.get("final_snapshot_sha256")
    if not isinstance(proofs, list) or "final_snapshot_sha256" not in data:
        return "mission_finished has no proofs or final_snapshot_sha256"
    if data.get("iterations") != last_iteration:
        return f"mission_finished names iteration {data.get('iterations')!r}, the last one is {last_iteration}"
    if "final_manifest" in data and (not isinstance(data["final_manifest"], dict)
                                     or manifest_digest(data["final_manifest"]) != final):
        return "final_manifest does not match final_snapshot_sha256"
    proven: set[str] = set()
    for proof in proofs:
        if not isinstance(proof, dict) or set(proof) != {"criterion", "seq", "hash"}:
            return "malformed proof"
        cid, seq = proof["criterion"], proof["seq"]
        if cid not in criteria or cid in proven:
            return f"proof for an unknown or repeated criterion {cid!r}"
        proven.add(cid)
        if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= index:
            return f"proof of {cid} points outside the ledger"
        target = records[seq - 1]
        if target["hash"] != proof["hash"] or target["type"] != "check_result":
            return f"proof of {cid} does not name check_result {seq} by its hash"
        result = target["data"]
        if result["criterion"] != cid:
            return f"proof of {cid} names a check_result of {result['criterion']}"
        if result["passed"] is not True or result.get("exit_code") != 0:
            return f"proof of {cid} names a check_result that did not pass"
        if result["iteration"] != last_iteration:
            return f"proof of {cid} is from iteration {result['iteration']}, not the last one ({last_iteration})"
        if latest.get(cid) != seq:
            return f"proof of {cid} is not the latest check_result of {cid}"
        if final is None or result.get("snapshot_sha256") != final:
            return f"proof of {cid} ran on another snapshot than final_snapshot_sha256"
        if result.get("check_sha256") != check_hash.get(cid):
            return f"proof of {cid} ran another check than the one in force"
        if result.get("contract_sha256") != contract:
            return f"proof of {cid} ran under another contract than the one in force"
    if data.get("status") == "complete" and proven != set(criteria):
        return "complete without a proof for every criterion"
    return None


def append_only(path: str | Path) -> bool | None:
    """Whether the file has the append-only attribute (chattr +a). None when it cannot be read (missing file, or a
    file system without inode flags)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        flags = fcntl.ioctl(fd, FS_IOC_GETFLAGS, b"\0" * 8)
    except OSError:
        return None
    finally:
        os.close(fd)
    return bool(int.from_bytes(flags[:4], "little") & FS_APPEND_FL)


ANCHOR_HEX_LEN = 12
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_LINE_RE = re.compile(
    r"(?<![a-z0-9-])([a-z0-9][a-z0-9-]{0,40})\s+([1-9]\d*)\s+([0-9a-fA-F]{12})(?![0-9a-fA-F\w-])"
)


def format_anchor(mission_id: str, seq: int, head_hash: str) -> str:
    return f"{mission_id} {seq} {head_hash[:ANCHOR_HEX_LEN]}"


def parse_anchors(text: str) -> list[tuple[str, int, str]]:
    """Extract `(mission_id, seq, 12hex)` anchors from `text`, tolerating surrounding Telegram card lines/emoji/HTML."""
    anchors: list[tuple[str, int, str]] = []
    for raw_line in text.splitlines():
        cleaned = _HTML_TAG_RE.sub(" ", raw_line)
        for match in _ANCHOR_LINE_RE.finditer(cleaned):
            anchors.append((match.group(1), int(match.group(2)), match.group(3).lower()))
    return anchors


def verify_anchor(path: str | Path, seq: int, prefix: str) -> tuple[str, str | None]:
    """Compare ledger `path` against an anchor `(seq, 12hex)`.
    Returns `("ok", None)`, `("missing", None)`, or `("mismatch", detail)`.
    """
    ledger_path = Path(path)
    if not ledger_path.exists():
        return "missing", None
    records, result = Ledger(ledger_path)._scan()
    if not result.ok:
        return "mismatch", f"broken at record {result.bad_seq}: {result.reason}"
    if seq < 1 or len(records) < seq:
        return "mismatch", f"ledger has {len(records)} records, need seq {seq}"
    actual = str(records[seq - 1]["hash"])[:ANCHOR_HEX_LEN].lower()
    if actual != prefix.lower():
        return "mismatch", f"record {seq} hash starts with {actual}"
    return "ok", None
