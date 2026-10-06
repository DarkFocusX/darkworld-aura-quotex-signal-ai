"""Prediction targets for model research (spec §1 / §2).

Two targets are supported, and they are *kept separate* — the existing
next-candle direction model is never silently replaced:

    NEXT_CANDLE_DIRECTION   the historical target: the sign of
                            `close[i+h] - close[i]` (decision candle close →
                            h candles ahead), with an ATR neutral band.

    M1_EXPIRY_OUTCOME       the target that matches what paper trading actually
                            measures: the trade is filled at the OPEN of the
                            entry candle and expires at the CLOSE of that candle
                            (h = 1 minute) or h candles later. This is exactly
                            how `backend/signals/tracker.py` resolves outcomes,
                            so the label and the paper result use the same fill
                            model.

Both are strictly forward-looking: row `i` only ever uses bars `> i`, so a
feature matrix built from bar `i` can never peek at its own label. The caller is
responsible for the embargo (`>= horizon`) between train and test.

Neutral / movement thresholds are configurable and can be expressed either as a
fraction of the ATR at the decision candle (default) or as an absolute price
move.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..features import indicators as ind

TARGET_NEXT_CANDLE = "NEXT_CANDLE_DIRECTION"
TARGET_M1_EXPIRY = "M1_EXPIRY_OUTCOME"
TARGETS = (TARGET_NEXT_CANDLE, TARGET_M1_EXPIRY)

# class encoding shared with the models: 0 = PUT (down), 1 = NEUTRAL, 2 = CALL (up)
CLASS_PUT, CLASS_NEUTRAL, CLASS_CALL = 0, 1, 2
CLASS_NAMES = {CLASS_PUT: "PUT", CLASS_NEUTRAL: "NEUTRAL", CLASS_CALL: "CALL"}


@dataclass(frozen=True)
class TargetSpec:
    """How a label is built. `horizon` is in minutes.

    reference
        ``theoretical`` (default for M1_EXPIRY) fills at the open of the entry
        candle — the same fill the paper tracker uses.
        ``close`` (default for NEXT_CANDLE) measures from the decision candle's
        close, i.e. the plain "where is price in h minutes" question.
    neutral_mode
        ``atr``  threshold = neutral_value × ATR at the decision candle
        ``price`` threshold = neutral_value (an absolute move, e.g. 0.0002)
    """

    target: str = TARGET_M1_EXPIRY
    horizon: int = 1
    neutral_mode: str = "atr"
    neutral_value: float = 0.25
    reference: str | None = None
    label: str = ""

    def resolved_reference(self) -> str:
        if self.reference:
            return self.reference
        return "theoretical" if self.target == TARGET_M1_EXPIRY else "close"

    def identifier(self) -> str:
        ref = {"theoretical": "fill", "close": "close"}[self.resolved_reference()]
        return (f"{self.target}|h{self.horizon}|{self.neutral_mode}"
                f"{self.neutral_value:g}|{ref}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "horizon_minutes": self.horizon,
            "reference": self.resolved_reference(),
            "neutral_mode": self.neutral_mode,
            "neutral_value": self.neutral_value,
            "identifier": self.identifier(),
            "label": self.label,
        }


@dataclass
class TargetFrame:
    """Labels plus the prices a paper trade would have used."""

    spec: TargetSpec
    index: pd.DatetimeIndex
    y: pd.Series                     # 0 PUT / 1 NEUTRAL / 2 CALL (float, NaN when undefined)
    entry_price: pd.Series           # price the trade would be filled at
    expiry_price: pd.Series          # price the trade settles at
    move: pd.Series                  # expiry_price - entry_price
    threshold: pd.Series             # the minimum move required for a directional label
    atr: pd.Series
    meta: dict[str, Any] = field(default_factory=dict)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {"y": self.y, "entry_price": self.entry_price, "expiry_price": self.expiry_price,
             "move": self.move, "threshold": self.threshold, "atr": self.atr}
        )

    def distribution(self) -> dict[str, int]:
        counts = self.y.value_counts(dropna=True)
        return {CLASS_NAMES[int(k)]: int(v) for k, v in counts.items()}

    def summary(self) -> dict[str, Any]:
        valid = self.y.notna()
        rates = self.move[valid]
        return {
            **self.spec.as_dict(),
            "rows": int(len(self.y)),
            "labelled_rows": int(valid.sum()),
            "class_distribution": self.distribution(),
            "neutral_share": float((self.y == CLASS_NEUTRAL).mean()) if valid.any() else None,
            "mean_abs_move": float(rates.abs().mean()) if len(rates) else None,
            "mean_threshold": float(self.threshold[valid].mean()) if valid.any() else None,
            "meta": self.meta,
        }


def _threshold(df: pd.DataFrame, spec: TargetSpec) -> tuple[pd.Series, pd.Series]:
    atr = ind.atr(df, 14)
    if spec.neutral_mode == "atr":
        return (spec.neutral_value * atr).abs(), atr
    if spec.neutral_mode == "price":
        return pd.Series(float(spec.neutral_value), index=df.index), atr
    raise ValueError(f"neutral_mode must be 'atr' or 'price', got {spec.neutral_mode!r}")


def build_target(df: pd.DataFrame, spec: TargetSpec) -> TargetFrame:
    """Build one target. Uses only bars strictly after the decision candle."""
    if spec.horizon < 1:
        raise ValueError("horizon must be >= 1 minute")
    if len(df) <= spec.horizon + 15:
        raise ValueError(f"need at least {spec.horizon + 16} candles, got {len(df)}")

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    threshold, atr = _threshold(df, spec)
    reference = spec.resolved_reference()

    if reference == "close":
        # "where is price h minutes after the decision candle closes"
        entry = close
        expiry = close.shift(-spec.horizon)
    else:
        # paper-trade fill: enter at the open of the next candle, settle on the
        # close of the candle `horizon` minutes later
        entry = open_.shift(-1)
        expiry = close.shift(-spec.horizon)

    move = expiry - entry
    y = pd.Series(np.nan, index=df.index, dtype=float)
    valid = move.notna() & threshold.notna() & entry.notna()
    y[valid & (move > threshold)] = float(CLASS_CALL)
    y[valid & (move < -threshold)] = float(CLASS_PUT)
    y[valid] = y[valid].fillna(float(CLASS_NEUTRAL))

    return TargetFrame(
        spec=spec, index=df.index, y=y, entry_price=entry, expiry_price=expiry,
        move=move, threshold=threshold, atr=atr,
        meta={
            "leakage_note": "label rows use only bars after the decision candle; embargo must be >= horizon",
            "fill_model": (
                "enter at open[i+1], settle at close[i+horizon] (matches tracker.py)"
                if reference == "theoretical"
                else "measure from close[i] to close[i+horizon] (theoretical direction)"
            ),
            "available_at": f"bar i+{spec.horizon} close",
        },
    )


def build_target_set(df: pd.DataFrame, horizons: tuple[int, ...] = (1, 2, 3, 5),
                     *, neutral_value: float = 0.25) -> dict[str, TargetFrame]:
    """Every (target, horizon) combination needed for the research grid."""
    out: dict[str, TargetFrame] = {}
    for horizon in horizons:
        for spec in (
            TargetSpec(target=TARGET_M1_EXPIRY, horizon=horizon, neutral_mode="atr",
                       neutral_value=neutral_value),
            TargetSpec(target=TARGET_NEXT_CANDLE, horizon=horizon, neutral_mode="atr",
                       neutral_value=neutral_value),
        ):
            out[spec.identifier()] = build_target(df, spec)
    return out


def realised_outcome(target: TargetFrame, i: int, direction: str) -> str | None:
    """WIN / LOSS / VOID for the trade implied by row `i` — read from prices only.

    Never derived from the prediction: it is a comparison of the entry and
    expiry prices that the dataset actually contains.
    """
    entry = target.entry_price.iloc[i]
    expiry = target.expiry_price.iloc[i]
    if pd.isna(entry) or pd.isna(expiry):
        return None
    if expiry == entry:
        return "VOID"
    up = expiry > entry
    if str(direction).upper() == "CALL":
        return "WIN" if up else "LOSS"
    if str(direction).upper() == "PUT":
        return "LOSS" if up else "WIN"
    return None
