"""Outcome tracking.

A signal is only ever marked WIN/LOSS when the *verified* closing candle exists
in the feed. Until then it stays PENDING with an explicit reason. A tie (exit
price exactly equal to the entry price) is recorded as VOID, never as a win.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..data.base import MarketDataUnavailable
from ..data.registry import registry
from ..database import db
from ..settings_service import get_settings
from ..timeutil import ist_clock, to_utc, utc_iso, utc_now_naive

# How long after expiry we keep retrying before declaring a signal UNRESOLVED.
UNRESOLVED_GRACE_MINUTES = 15


def _price_at(frame, timestamp_utc: datetime, field: str) -> float | None:
    ts = to_utc(timestamp_utc)
    try:
        return float(frame.df.loc[ts, field])
    except KeyError:
        return None


def evaluate_open_signals(*, now: datetime | None = None) -> dict[str, Any]:
    now = to_utc(now or utc_now_naive())
    settings = get_settings()
    open_signals = db.get_open_signals()
    evaluated: list[dict[str, Any]] = []
    pending = 0
    unresolved = 0

    frames: dict[str, Any] = {}
    for signal in open_signals:
        asset = signal["asset"]
        entry = datetime.fromisoformat(signal["entry_time_utc"].replace("Z", "+00:00"))
        expiry_minutes = int(signal.get("expiry_minutes") or 1)
        exit_candle_time = entry + timedelta(minutes=max(1, expiry_minutes) - 1)
        exit_close_time = exit_candle_time + timedelta(minutes=1)

        if exit_close_time > now:
            pending += 1
            evaluated.append({"signal_uid": signal["signal_uid"], "result": "PENDING",
                              "reason": "expiry candle still forming"})
            continue

        if asset not in frames:
            try:
                frames[asset] = registry.get_frame(asset, limit=300,
                                                   data_source_mode=settings.data_source_mode,
                                                   use_cache=False)
            except MarketDataUnavailable as exc:
                frames[asset] = exc
            except Exception as exc:  # noqa: BLE001
                frames[asset] = MarketDataUnavailable(asset, f"feed error: {type(exc).__name__}", "tracker")

        overdue = (now - exit_close_time).total_seconds() > UNRESOLVED_GRACE_MINUTES * 60

        frame = frames[asset]
        if isinstance(frame, MarketDataUnavailable):
            result, reason = _unverifiable(
                overdue, f"cannot verify outcome — {frame.reason}"
            )
            if result == "UNRESOLVED":
                db.mark_outcome(signal["signal_uid"], result, None, reason, status="UNRESOLVED",
                                resolved_at_utc=utc_iso(now))
                unresolved += 1
            else:
                pending += 1
            evaluated.append({"signal_uid": signal["signal_uid"], "result": result,
                              "reason": reason, "feed_error": frame.reason})
            continue

        entry_price = _price_at(frame, entry, "open")
        exit_price = _price_at(frame, exit_candle_time, "close")
        if entry_price is None or exit_price is None:
            result, reason = _unverifiable(
                overdue,
                f"candles for {ist_clock(entry)}–{ist_clock(exit_close_time)} are not present in the feed",
            )
            if result == "UNRESOLVED":
                db.mark_outcome(signal["signal_uid"], result, None, reason, status="UNRESOLVED",
                                resolved_at_utc=utc_iso(now))
                unresolved += 1
            else:
                pending += 1
            evaluated.append({"signal_uid": signal["signal_uid"], "result": result, "reason": reason})
            continue

        if exit_price == entry_price:
            result, reason = "VOID", f"price unchanged at expiry ({entry_price:.5f})"
        elif signal["direction"] == "CALL":
            result = "WIN" if exit_price > entry_price else "LOSS"
            reason = f"open {entry_price:.5f} → close {exit_price:.5f}"
        else:
            result = "WIN" if exit_price < entry_price else "LOSS"
            reason = f"open {entry_price:.5f} → close {exit_price:.5f}"

        db.mark_outcome(signal["signal_uid"], result, exit_price, reason, resolved_at_utc=utc_iso(now))
        evaluated.append({"signal_uid": signal["signal_uid"], "result": result, "reason": reason,
                          "entry_price": entry_price, "exit_price": exit_price})

    return {
        "evaluated": evaluated,
        "open": len(open_signals),
        "pending": pending,
        "unresolved": unresolved,
        "resolved": len([e for e in evaluated if e["result"] in {"WIN", "LOSS", "VOID"}]),
        "checked_at_utc": utc_iso(now),
        "note": (
            "WIN/LOSS is only ever written from the verified closing candle. If that candle "
            "cannot be verified within the grace period the signal is marked UNRESOLVED — never "
            "guessed from the model's prediction."
        ),
    }


def _unverifiable(overdue: bool, reason: str) -> tuple[str, str]:
    """Decide between PENDING (still may resolve) and UNRESOLVED (cannot be verified)."""
    if overdue:
        return "UNRESOLVED", reason + f" (still unverifiable after the {UNRESOLVED_GRACE_MINUTES} min grace period)"
    return "PENDING", reason
