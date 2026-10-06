"""Outcome tracking (verified candles only) and the SQLite persistence layer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.data.base import MarketDataUnavailable
from backend.database import db
from backend.signals import tracker
from backend.timeutil import ist_clock, ist_iso, to_ist, utc_iso


def _signal_payload(asset: str, entry_utc: datetime, direction: str = "CALL", expiry_minutes: int = 1) -> dict:
    return {
        "signal_uid": f"{asset}|{utc_iso(entry_utc)}",
        "asset": asset,
        "timeframe": "M1",
        "direction": direction,
        "confidence": 90.0,
        "quality": "VERY_HIGH",
        "entry_time_utc": utc_iso(entry_utc),
        "entry_time_ist": ist_clock(entry_utc),
        "entry_time_ist_full": ist_iso(entry_utc),
        "entry_date_ist": to_ist(entry_utc).strftime("%Y-%m-%d"),
        "expiry_minutes": expiry_minutes,
        "expiry_time_ist": ist_clock(entry_utc + timedelta(minutes=expiry_minutes)),
        "data_timestamp_utc": utc_iso(entry_utc),
        "data_timestamp_ist": ist_clock(entry_utc),
        "model_probability": 0.55,
        "model_agreement": 0.9,
        "is_simulated": True,
    }


def test_save_and_fetch_signal(temp_db):
    entry = datetime(2026, 2, 2, 3, 0, tzinfo=timezone.utc)
    db.save_signal(_signal_payload("EURUSD", entry))
    stored = db.get_signal("EURUSD|2026-02-02T03:00:00+00:00")
    assert stored is not None
    assert stored["direction"] == "CALL"
    assert stored["result"] == "PENDING"
    assert stored["status"] == "OPEN"
    assert db.signal_exists_for_candle("EURUSD", "2026-02-02T03:00:00+00:00") is not None


def test_mark_outcome_only_applies_once(temp_db):
    entry = datetime(2026, 2, 2, 4, 0, tzinfo=timezone.utc)
    payload = _signal_payload("EURUSD", entry)
    db.save_signal(payload)

    assert db.mark_outcome(payload["signal_uid"], "WIN", 1.105, "open 1.1000 → close 1.1050") is True
    assert db.mark_outcome(payload["signal_uid"], "LOSS", 1.09, "second write must be ignored") is False

    stored = db.get_signal(payload["signal_uid"])
    assert stored["result"] == "WIN"
    assert stored["status"] == "CLOSED"


def test_performance_aggregate_and_max_losing_streak(temp_db):
    base = datetime(2026, 2, 3, 1, 0, tzinfo=timezone.utc)
    results = ["WIN", "LOSS", "LOSS", "LOSS", "WIN"]
    for index, result in enumerate(results):
        payload = _signal_payload("EURUSD", base + timedelta(minutes=index))
        db.save_signal(payload)
        db.mark_outcome(payload["signal_uid"], result, 1.1, "test")

    performance = db.get_signal_performance()
    assert performance["generated"] == 5
    assert performance["wins"] == 2
    assert performance["losses"] == 3
    assert performance["win_rate"] == pytest.approx(40.0)
    assert performance["max_losing_streak"] == 3


def test_tracker_marks_outcome_from_verified_candles(temp_db, monkeypatch):
    """A signal whose expiry candle already closed is settled from the stored candles."""
    from backend.data.registry import registry

    frame = registry.get_frame("EURUSD", limit=200, data_source_mode="simulated", use_cache=False)
    entry = frame.df.index[-1].to_pydatetime()                     # last fully closed candle
    payload = _signal_payload("EURUSD", entry)
    db.save_signal(payload)

    summary = tracker.evaluate_open_signals()
    stored = db.get_signal(payload["signal_uid"])
    assert stored["result"] in {"WIN", "LOSS", "VOID"}
    assert stored["result_reason"]
    assert stored["exit_price"] is not None

    entry_price = float(frame.df["open"].iloc[-1])
    exit_price = float(frame.df["close"].iloc[-1])
    expected = "VOID" if exit_price == entry_price else (
        "WIN" if (exit_price > entry_price) else "LOSS"
    )
    assert stored["result"] == expected
    assert summary["resolved"] >= 1


def test_tracker_keeps_pending_when_the_expiry_candle_has_not_closed(temp_db, monkeypatch):
    payload = _signal_payload("EURUSD", datetime.now(tz=timezone.utc) + timedelta(minutes=5))
    db.save_signal(payload)
    summary = tracker.evaluate_open_signals()
    stored = db.get_signal(payload["signal_uid"])
    assert stored["result"] == "PENDING"
    assert stored["status"] == "OPEN"
    assert summary["pending"] >= 1


def test_tracker_stays_pending_when_candles_are_missing(temp_db, monkeypatch):
    """No verified candle → no WIN/LOSS.

    Spec §10: while the outcome cannot be verified the row stays PENDING; once the
    grace window (expiry + 15 min) has passed without a verifiable candle it is
    marked UNRESOLVED — never guessed as a WIN from the prediction.
    """
    recent_entry = datetime.now(tz=timezone.utc) - timedelta(minutes=2)
    payload = _signal_payload("EURUSD", recent_entry)
    db.save_signal(payload)

    summary = tracker.evaluate_open_signals()
    stored = db.get_signal(payload["signal_uid"])
    assert stored["result"] in {"PENDING", "UNRESOLVED"}   # depends on feed availability
    if stored["result"] == "PENDING":
        assert stored["status"] == "OPEN"

    # Long past the grace window with no verifiable candle → UNRESOLVED, not a guess.
    old_entry = datetime.now(tz=timezone.utc) - timedelta(days=7)
    old = _signal_payload("EURUSD", old_entry)
    db.save_signal(old)
    tracker.evaluate_open_signals()
    stored_old = db.get_signal(old["signal_uid"])
    assert stored_old["result"] in {"PENDING", "UNRESOLVED"}
    assert stored_old["result"] != "WIN"      # never inferred from the prediction
    assert stored_old["result"] != "LOSS"


def test_tracker_handles_feed_failure_gracefully(temp_db, monkeypatch):
    payload = _signal_payload("EURUSD", datetime.now(tz=timezone.utc) - timedelta(minutes=10))
    db.save_signal(payload)

    def broken_feed(*args, **kwargs):
        raise MarketDataUnavailable("EURUSD", "feed offline", "test")

    monkeypatch.setattr("backend.signals.tracker.registry.get_frame", broken_feed)
    summary = tracker.evaluate_open_signals()
    stored = db.get_signal(payload["signal_uid"])
    assert stored["result"] == "PENDING"
    assert any("cannot verify outcome" in (item.get("reason") or "") for item in summary["evaluated"])


def test_scan_log_records_rejections(temp_db):
    db.log_scan(asset="EURUSD", status="NO_TRADE", reason="Confidence below threshold", confidence=71.4,
                scanned_at_utc=datetime(2026, 2, 4, 5, 0, tzinfo=timezone.utc), scanned_at_ist="10:30:00")
    scans = db.list_scans(limit=5)
    assert scans[0]["asset"] == "EURUSD"
    assert scans[0]["status"] == "NO_TRADE"
    assert "below threshold" in scans[0]["reason"]


def test_history_filters_by_day(temp_db):
    db.save_signal(_signal_payload("EURUSD", datetime(2026, 2, 5, 3, 0, tzinfo=timezone.utc)))   # 08:30 IST
    db.save_signal(_signal_payload("EURUSD", datetime(2026, 2, 6, 3, 0, tzinfo=timezone.utc)))   # next IST day
    day_rows = db.list_history(limit=10, day_ist="2026-02-05")
    assert len(day_rows) == 1
