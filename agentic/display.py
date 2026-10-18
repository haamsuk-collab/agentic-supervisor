"""The one way a moment is shown to a person (report.md, Telegram replies and notifications, `mission` output).

The ledger keeps UTC (`ledger.format_ts`); only what people read is converted, to config.DISPLAY_TIMEZONE.
"""

from datetime import datetime
from functools import cache
from zoneinfo import ZoneInfo

from agentic import config

DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S %Z"  # e.g. 2026-10-18 14:53:21 KST


@cache
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def display_time(moment: datetime) -> str:
    """`moment` (timezone-aware) in DISPLAY_TIMEZONE, to the second. A naive datetime raises ValueError."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("display_time needs a timezone-aware datetime")
    return moment.astimezone(_zone(config.DISPLAY_TIMEZONE)).strftime(DISPLAY_FORMAT)


def zone_label(moment: datetime | None = None) -> str:
    """The abbreviation of DISPLAY_TIMEZONE at `moment` (default now), e.g. "KST" or "UTC", for fixed texts."""
    return (moment or datetime.now(_zone(config.DISPLAY_TIMEZONE))).astimezone(
        _zone(config.DISPLAY_TIMEZONE)).strftime("%Z")
