"""IST / Asia-Kolkata time utilities.

All trading timing in this system is derived from Asia/Kolkata. Browser-local
time is never used for any trading calculation (the frontend only *renders*
timestamps produced here).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900}


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def utc_now_naive() -> datetime:
    """UTC now without tzinfo — for naive-UTC columns (SQLite storage, scheduler ticks)."""
    return datetime.now(tz=UTC).replace(tzinfo=None)


def coerce_dt(value: datetime | str | int | float | None) -> datetime:
    """Accept datetimes, ISO strings or epoch values and return an aware UTC datetime."""
    if value is None:
        return now_utc()
    if isinstance(value, datetime):
        return to_utc(value)
    return parse_any_ts(value)


def to_ist(dt: datetime | str | int | float) -> datetime:
    """Convert any aware/naive datetime (or ISO/epoch value) to IST.

    Naive datetimes are assumed to be UTC.
    """
    if not isinstance(dt, datetime):
        dt = parse_any_ts(dt)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def to_utc(dt: datetime | str | int | float) -> datetime:
    if not isinstance(dt, datetime):
        return parse_any_ts(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ist_iso(dt: datetime | str | int | float | None = None, timespec: str = "seconds") -> str:
    return to_ist(coerce_dt(dt)).isoformat(timespec=timespec)


def ist_clock(dt: datetime | str | int | float | None = None) -> str:
    """06:30:00 style IST clock string."""
    return to_ist(coerce_dt(dt)).strftime("%H:%M:%S")


def ist_hhmm(dt: datetime | str | int | float | None = None) -> str:
    return to_ist(coerce_dt(dt)).strftime("%H:%M")


def utc_iso(dt: datetime | str | int | float | None = None) -> str:
    return to_utc(coerce_dt(dt)).isoformat(timespec="seconds")


def floor_minute(dt: datetime) -> datetime:
    """Floor to the opening boundary of the candle that contains `dt`."""
    dt = to_utc(dt)
    return dt.replace(second=0, microsecond=0)


def next_minute_boundary(dt: datetime, offset_seconds: int = 0) -> datetime:
    """First candle-open boundary strictly after `dt` (+ optional offset)."""
    floor = floor_minute(dt) + timedelta(seconds=offset_seconds)
    if floor <= to_utc(dt):
        floor += timedelta(minutes=1)
    return floor


def seconds_to_next_minute(dt: datetime | None = None) -> float:
    dt = to_utc(dt or now_utc())
    nxt = floor_minute(dt) + timedelta(minutes=1)
    return max(0.0, (nxt - dt).total_seconds())


def ist_minute_start_index(dt: datetime, epoch: datetime) -> int:
    """Number of whole minutes between `epoch` and `dt` (both floored, UTC based)."""
    delta = floor_minute(dt) - floor_minute(to_utc(epoch))
    return int(delta.total_seconds() // 60)


def parse_any_ts(value: str | float | int | datetime) -> datetime:
    """Parse epoch seconds/ms strings or ISO strings into an aware UTC datetime."""
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=UTC)
    s = str(value).strip()
    if not s:
        raise ValueError("empty timestamp")
    try:
        numeric = float(s)
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric > 1e11:      # milliseconds
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, tz=UTC)
        except (ValueError, OSError, OverflowError):
            pass
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def human_ist(dt: datetime) -> str:
    return to_ist(dt).strftime("%d %b %Y %H:%M:%S IST")
