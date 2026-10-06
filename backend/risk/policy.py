"""Risk policy — pre-trade constraints applied before a signal can be emitted.

Kept deliberately small and explicit. Everything here is a *blocking* rule that
can only reduce the number of signals, never increase it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import Settings
from ..timeutil import IST, floor_minute, to_ist, to_utc


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reasons": self.reasons, "meta": self.meta}


class RiskPolicy:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ timing
    def resolve_entry_time(self, now: datetime) -> datetime:
        """Next M1 candle boundary with at least `min_entry_seconds_remaining` to spare."""
        now_utc = to_utc(now)
        candidate = floor_minute(now_utc) + __import__("datetime").timedelta(minutes=1)
        remaining = (candidate - now_utc).total_seconds()
        if remaining < self.settings.min_entry_seconds_remaining:
            candidate = candidate + __import__("datetime").timedelta(minutes=1)
        return candidate

    # ------------------------------------------------------------------- daily
    def daily_signal_count(self, signal_dates_ist: list[str | None], day_ist: str) -> int:
        """Count today's emitted signals. `signal_dates_ist` holds IST calendar days."""
        return sum(1 for value in signal_dates_ist if value == day_ist)

    def evaluate(
        self,
        *,
        now: datetime,
        signals_today_dates: list[str | None],
        existing_signal_for_candle: bool,
        expiry_minutes: int | None = None,
    ) -> RiskDecision:
        reasons: list[str] = []
        expiry = expiry_minutes or self.settings.expiry_minutes
        now_ist = to_ist(now)
        day_ist = now_ist.astimezone(IST).strftime("%Y-%m-%d")
        count = self.daily_signal_count(signals_today_dates, day_ist)

        if existing_signal_for_candle:
            reasons.append("SIGNAL LOCKED: a signal already exists for this candle/asset")
        if count >= self.settings.max_signals_per_day:
            reasons.append(
                f"Daily signal limit reached ({count}/{self.settings.max_signals_per_day} for {day_ist} IST)"
            )
        if not 1 <= expiry <= 5:
            reasons.append(f"Expiry {expiry}m is outside the supported 1–5 minute range")
        if self.settings.mode not in {"PAPER", "SIGNAL_ONLY"}:
            reasons.append(f"Execution mode '{self.settings.mode}' is not supported by this build")

        return RiskDecision(
            allowed=not reasons,
            reasons=reasons,
            meta={
                "signals_today": count,
                "max_signals_per_day": self.settings.max_signals_per_day,
                "day_ist": day_ist,
                "expiry_minutes": expiry,
                "mode": self.settings.mode,
                "min_entry_seconds_remaining": self.settings.min_entry_seconds_remaining,
            },
        )
