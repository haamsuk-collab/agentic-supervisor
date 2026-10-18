"""Fault injection for the crash-point tests (SPEC-H section 7).

`HOOK` is None in production, so `point` does nothing. A test sets it to a callable that raises at the moment it
wants the supervisor to die: right after a ledger record became durable (`APPENDED`), or right after the loop
applied that record to its state (`APPLIED`).
"""

from typing import Callable

APPENDED, APPLIED = "appended", "applied"
HOOK: Callable[[str, dict], None] | None = None


def point(where: str, record: dict) -> None:
    if HOOK is not None:
        HOOK(where, record)
