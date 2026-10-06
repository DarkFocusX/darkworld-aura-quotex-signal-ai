"""Verified outcome engine (spec §6).

At expiry the ledger asks one question: *what does the verified candle say?*
It never asks the model.

    entry price   → the OPEN of the stored entry candle (the fill), re-checked
                    against the record when the signal was issued;
    expiry price  → the close of the stored candle that closes exactly at the
                    signal's expiry minute;
    rule          → CALL wins if ``expiry_close > entry_close + minimum_move``,
                    PUT wins if ``expiry_close < entry_close − minimum_move``,
                    otherwise the move was inside the noise band → VOID (recorded
                    as UNRESOLVED with the exact reason).

If either candle is missing the signal stays PENDING until the grace window
passes and then becomes UNRESOLVED. A missing candle is never guessed, never
interpolated, and never replaced by the prediction.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..constants import GATES
from ..data import store
from ..features import indicators as ind
from . import ledger
from ..timeutil import to_utc, utc_iso, utc_now_naive
from . import ledger

GRACE_MINUTES = 15
DEFAULT_NEUTRAL_VALUE = 0.25


def _atr_before(asset: str, when: datetime, *, period: int = 14) -> float | None:
    """Wilder ATR (the same indicator the research target uses) computed from the
    stored candles *at or before* the decision minute — causal only."""
    import numpy as np
    import pandas as pd

    rows = store.candles(asset, limit=400)
    if len(rows) < period + 2:
        return None
    cutoff = to_utc(when)
    kept = [r for r in rows
            if to_utc(datetime.fromisoformat(r["ts_utc"].replace("Z", "+00:00"))) <= cutoff]
    if len(kept) < period + 2:
        return None
    frame = pd.DataFrame(
        {k: [float(r[k]) for r in kept] for k in ("open", "high", "low", "close")},
        index=pd.DatetimeIndex([pd.Timestamp(r["ts_utc"]) for r in kept], tz="UTC"),
    )
    values = ind.atr(frame, period).dropna()
    if values.empty:
        return None
    atr = float(values.iloc[-1])
    return atr if np.isfinite(atr) and atr > 0 else None


def resolve_due(*, now: datetime | None = None, grace_minutes: int = GRACE_MINUTES) -> dict[str, Any]:
    """Settle every paper signal whose expiry candle has closed (verified only)."""
    now = to_utc(now or utc_now_naive())
    ledger.ensure_schema()
    settled: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for row in ledger.rows(limit=500, statuses=[ledger.PENDING]):
        entry_utc = datetime.fromisoformat(str(row["entry_time_utc"]).replace("Z", "+00:00"))
        expiry_minutes = max(1, int(row["expiry_minutes"] or 1))
        settlement_candle_open = entry_utc + timedelta(minutes=expiry_minutes - 1)
        settlement_close = settlement_candle_open + timedelta(minutes=1)
        if settlement_close > now:
            pending.append({"signal_id": row["signal_id"], "reason": "expiry candle still forming"})
            continue

        # Fill convention (same as the research target and the paper tracker): the trade
        # fills at the OPEN of the entry candle and settles on the CLOSE of the candle
        # `expiry_minutes` later. Both prices must come from stored, verified candles.
        entry_close = store.verified_price(row["asset"], entry_utc, field="open")
        expiry_close = store.verified_price(row["asset"], settlement_candle_open, field="close")
        if entry_close is None or expiry_close is None:
            missing = "entry" if entry_close is None else "expiry"
            overdue = (now - settlement_close) >= timedelta(minutes=grace_minutes)
            reason = (f"{missing} candle could not be verified from the store after {grace_minutes} minutes "
                      f"— kept UNRESOLVED rather than guessed")
            if overdue:
                ledger.settle(row["signal_id"], "VOID", None, reason, source="UNVERIFIED")
                settled.append({"signal_id": row["signal_id"], "result": "UNRESOLVED", "reason": reason})
            else:
                pending.append({"signal_id": row["signal_id"], "reason": reason})
            continue

        atr = _atr_before(row["asset"], entry_utc)
        minimum_move = abs(DEFAULT_NEUTRAL_VALUE * atr) if atr else 0.0
        # Same rule as the research M1_EXPIRY_OUTCOME target, applied to the two
        # verified prices (CALL needs +threshold, PUT needs −threshold, else neutral).
        move = expiry_close - entry_close
        label = 2 if minimum_move > 0 and move > minimum_move else (
            0 if minimum_move > 0 and move < -minimum_move else 1)
        if minimum_move <= 0:
            # No ATR window in the store (e.g. a very young feed): fall back to the
            # zero-threshold rule and say so — still verified prices, never a guess.
            result, reason = (("VOID", "exit price equals entry price — no decisive outcome")
                              if expiry_close == entry_close else
                              ("WIN" if (str(row["direction"]).upper() == "CALL") == (expiry_close > entry_close)
                               else "LOSS",
                               _reason(str(row["direction"]).upper(), entry_close, expiry_close, 0.0,
                                       (str(row["direction"]).upper() == "CALL") == (expiry_close > entry_close))))
        elif label == 1:
            result, reason = "VOID", (
                f"verified move {move:+.6f} was inside the noise band "
                f"(minimum move {minimum_move:.6f}) — no decisive outcome")
        direction = str(row["direction"]).upper()
        if minimum_move <= 0:
            # No ATR window in the store (a very young feed): fall back to the
            # zero-threshold rule and say so — still verified prices, never a guess.
            if expiry_close == entry_close:
                result, reason = "VOID", "exit price equals entry price — no decisive outcome"
            else:
                won = (expiry_close > entry_close) if direction == "CALL" else (expiry_close < entry_close)
                result = "WIN" if won else "LOSS"
                reason = _reason(direction, entry_close, expiry_close, 0.0, won)
        elif label == 1:
            result, reason = "VOID", (
                f"verified move {move:+.6f} was inside the noise band "
                f"(minimum move {minimum_move:.6f}) — no decisive outcome")
        else:
            won = (label == 2) if direction == "CALL" else (label == 0)
            result = "WIN" if won else "LOSS"
            reason = _reason(direction, entry_close, expiry_close, minimum_move, won)

        ok = ledger.settle(row["signal_id"], result, expiry_close, reason,
                           source="VERIFIED_CANDLE")
        settled.append({"signal_id": row["signal_id"], "result": result, "exit_price": expiry_close,
                        "entry_price": entry_close, "reason": reason, "settled": ok})
    return {
        "checked": len(settled) + len(pending),
        "settled": settled,
        "still_pending": pending,
        "at_utc": utc_iso(now),
        "policy": ("Outcomes are read from verified candles only; the model prediction is never used to "
                   "decide a WIN/LOSS."),
        "max_data_lag_seconds": GATES["max_data_lag_seconds"],
    }


def _reason(direction: str, entry: float, expiry: float, minimum_move: float, won: bool) -> str:
    move = expiry - entry
    verdict = "reached" if won else "did not reach"
    return (f"{direction} {verdict} the required move: verified {entry:.6f} → {expiry:.6f} "
            f"({move:+.6f}, minimum move {minimum_move:.6f})")


def settle_ledger_rows(*, now: datetime | None = None, grace_minutes: int = GRACE_MINUTES) -> dict[str, Any]:
    """Alias kept for readability in the scanner and API."""
    return resolve_due(now=now, grace_minutes=grace_minutes)
