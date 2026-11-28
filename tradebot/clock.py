"""US equity session times in Eastern Time."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
SESSION_MINUTES = 390


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def to_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return now.astimezone(ET)


def session_open_dt(now: datetime) -> datetime:
    et = to_et(now)
    return et.replace(hour=9, minute=30, second=0, microsecond=0)


def session_fraction_elapsed(now: datetime) -> float:
    """0.0 at 09:30 ET, 1.0 at 16:00 ET, clamped."""
    et = to_et(now)
    minutes = (et - session_open_dt(et)) / timedelta(minutes=1)
    return max(0.0, min(1.0, minutes / SESSION_MINUTES))


def is_regular_hours(now: datetime) -> bool:
    et = to_et(now)
    return et.weekday() < 5 and SESSION_OPEN <= et.time() < SESSION_CLOSE


def in_entry_window(now: datetime, not_before: str, not_after: str) -> bool:
    et = to_et(now)
    return (
        et.weekday() < 5
        and parse_hhmm(not_before) <= et.time() < parse_hhmm(not_after)
    )


def past_flatten_time(now: datetime, flatten_at: str) -> bool:
    return to_et(now).time() >= parse_hhmm(flatten_at)
