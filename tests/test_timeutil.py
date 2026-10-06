"""IST timezone conversion and minute-boundary maths."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.timeutil import (
    IST,
    floor_minute,
    ist_clock,
    ist_iso,
    next_minute_boundary,
    parse_any_ts,
    seconds_to_next_minute,
    to_ist,
    to_utc,
)


def test_naive_datetime_is_treated_as_utc_and_converted_to_ist():
    naive = datetime(2026, 3, 10, 1, 0, 0)          # 01:00 UTC
    assert to_ist(naive).strftime("%H:%M:%S") == "06:30:00"     # IST = UTC+5:30


def test_ist_offset_is_half_hour():
    converted = to_ist(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert converted.utcoffset() == timedelta(hours=5, minutes=30)
    assert converted.tzinfo == IST


def test_round_trip_utc_ist_utc():
    original = datetime(2026, 7, 4, 18, 45, 30, tzinfo=timezone.utc)
    assert to_utc(to_ist(original)) == original


def test_ist_clock_format_uses_24h_zero_padding():
    assert ist_clock(datetime(2026, 1, 1, 1, 5, 7, tzinfo=timezone.utc)) == "06:35:07"
    assert ist_iso(datetime(2026, 1, 1, 1, 5, 7, tzinfo=timezone.utc)).startswith("2026-01-01T06:35:07")


def test_floor_minute_truncates_seconds_and_microseconds():
    ts = datetime(2026, 5, 5, 10, 17, 42, 999000, tzinfo=timezone.utc)
    assert floor_minute(ts) == datetime(2026, 5, 5, 10, 17, 0, tzinfo=timezone.utc)


def test_next_minute_boundary_is_strictly_after_input():
    ts = datetime(2026, 5, 5, 10, 17, 0, tzinfo=timezone.utc)
    assert next_minute_boundary(ts) == datetime(2026, 5, 5, 10, 18, 0, tzinfo=timezone.utc)
    ts_late = datetime(2026, 5, 5, 10, 17, 59, 500000, tzinfo=timezone.utc)
    assert next_minute_boundary(ts_late) == datetime(2026, 5, 5, 10, 18, 0, tzinfo=timezone.utc)


def test_seconds_to_next_minute_bounds():
    assert seconds_to_next_minute(datetime(2026, 5, 5, 10, 17, 0, tzinfo=timezone.utc)) == pytest.approx(60.0)
    assert seconds_to_next_minute(datetime(2026, 5, 5, 10, 17, 30, tzinfo=timezone.utc)) == pytest.approx(30.0)


def test_parse_any_ts_handles_epoch_and_iso_and_z_suffix():
    assert parse_any_ts(1767225600).year == 2026
    assert parse_any_ts("1767225600000").year == 2026          # milliseconds
    assert parse_any_ts("2026-01-01T00:00:00Z").tzinfo is not None
    assert parse_any_ts("2026-01-01T05:30:00+05:30") == datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_any_ts("")


def test_ist_day_rollover_differs_from_utc():
    """18:31 UTC is already the next day in IST — the daily boundary must reflect that."""
    utc = datetime(2026, 6, 1, 18, 31, tzinfo=timezone.utc)
    assert to_ist(utc).strftime("%Y-%m-%d") == "2026-06-02"
    assert to_ist(utc).strftime("%H:%M:%S") == "00:01:00"


# --------------------------------------------------------------------- final phase §8
def test_exact_minute_boundaries_map_to_the_next_candle():
    """At :00.000 the candle that just closed is the *previous* minute.

    Regression guard for the entry/expiry maths: the engine predicts the candle that
    opens at the next boundary and settles it `expiry_minutes` later — for M1 that is
    exactly one minute of price action, never two, never zero.
    """
    from backend.timeutil import floor_minute

    at_boundary = datetime(2026, 10, 2, 12, 34, 0, 0, tzinfo=timezone.utc)
    assert floor_minute(at_boundary) == at_boundary                       # the current candle
    assert next_minute_boundary(at_boundary) == datetime(2026, 10, 2, 12, 35, tzinfo=timezone.utc)
    assert seconds_to_next_minute(at_boundary) == pytest.approx(60.0)     # a full minute remains

    almost = datetime(2026, 10, 2, 12, 34, 59, 999999, tzinfo=timezone.utc)
    assert next_minute_boundary(almost) == datetime(2026, 10, 2, 12, 35, tzinfo=timezone.utc)
    assert seconds_to_next_minute(almost) == pytest.approx(0.000001, abs=0.01)


def test_entry_and_expiry_are_exactly_one_m1_candle_apart():
    """entry = next boundary, expiry = entry + expiry_minutes (the M1 settlement candle)."""
    from backend.timeutil import floor_minute

    now = datetime(2026, 10, 2, 12, 34, 17, 500000, tzinfo=timezone.utc)
    entry = next_minute_boundary(now)
    expiry = entry + timedelta(minutes=1)
    assert entry == datetime(2026, 10, 2, 12, 35, tzinfo=timezone.utc)
    assert expiry == datetime(2026, 10, 2, 12, 36, tzinfo=timezone.utc)
    # the prediction may only use closed candles: the candle that closes last is the one
    # before entry, i.e. floor_minute(now)
    assert floor_minute(now) == datetime(2026, 10, 2, 12, 34, tzinfo=timezone.utc)
    assert floor_minute(now) < entry


def test_ist_conversion_keeps_the_minute_grid():
    """The half-hour IST offset must not shift a candle onto a different second."""
    utc = datetime(2026, 10, 2, 12, 34, tzinfo=timezone.utc)
    ist = to_ist(utc)
    assert (ist.minute, ist.second) == (4, 0)          # 18:04 IST
    assert to_utc(ist) == utc
    assert to_ist(floor_minute(ist)).minute == ist.minute


def test_countdown_never_exceeds_one_minute_on_an_m1_feed():
    """The dashboard countdown is backend-computed and bounded to (0, 60]."""
    for second in (0, 1, 29, 30, 59):
        moment = datetime(2026, 10, 2, 12, 34, second, tzinfo=timezone.utc)
        remaining = seconds_to_next_minute(moment)
        assert 0 < remaining <= 60.0
        assert remaining == pytest.approx(60 - second)
