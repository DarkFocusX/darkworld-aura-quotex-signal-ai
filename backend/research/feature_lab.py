"""Extended (lab) feature set — spec §3 "feature research".

The production feature block (`backend/features/pipeline.py`, 44 features) already
covers most of the requested families: multi-horizon returns, body/wick ratios,
rolling volatility, ATR normalisation, EMA distance and slope, RSI, MACD, ADX/DI,
Bollinger position/width, rolling high/low distance, support/resistance distance
and time-of-day.

This module adds the *missing* families so they can be evaluated on equal terms —
and nothing here is adopted into production unless it wins out-of-sample:

    rsi7_delta3       RSI(7) slope over 3 bars
    rsi14_slope5      RSI(14) slope over 5 bars, normalised
    macd_slope3       MACD histogram slope over 3 bars
    vol_ratio_5_20    short vs long realised volatility
    atr_ratio_5_20    short vs long ATR (volatility regime without look-ahead)
    body_run          signed run-length of same-direction bodies
    range_pos_60      position inside the 60-bar range
    sr_dist_60        distance to the nearest 60-bar extreme, in ATR units
    ret_20, ret_30    longer-horizon momentum
    minute_sin/cos    intra-hour seasonality (M1 bars, cyclical encoding)

Every feature is a function of bars `<= i` only: no `.shift(-n)`, no centred
window, no future aggregate. `tests/test_research_lab.py` proves it by rebuilding
features on truncated and future-corrupted frames.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..constants import AssetSpec
from ..features import indicators as ind
from ..features.pipeline import FEATURE_NAMES, build_feature_frame

FEATURE_SET_BASE = "base"
FEATURE_SET_EXTENDED = "extended"
FEATURE_SET_VERSION_BASE = "m1-features-1.0"
FEATURE_SET_VERSION_EXTENDED = "m1-features-lab-1.0"

EXTENDED_FEATURE_NAMES: tuple[str, ...] = (
    "rsi7_delta3",
    "rsi14_slope5",
    "macd_slope3",
    "vol_ratio_5_20",
    "atr_ratio_5_20",
    "body_run",
    "range_pos_60",
    "sr_dist_60",
    "ret_20",
    "ret_30",
    "minute_sin",
    "minute_cos",
)

_DESCRIPTIONS: dict[str, str] = {
    "rsi7_delta3": "RSI(7) change over 3 bars — fast momentum slope",
    "rsi14_slope5": "(RSI(14) - RSI(14).shift(5)) / 50 — slow momentum slope",
    "macd_slope3": "MACD histogram change over 3 bars, ATR-normalised",
    "vol_ratio_5_20": "std(returns, 5) / std(returns, 20) — volatility expansion",
    "atr_ratio_5_20": "ATR(5) / ATR(20) — short vs long volatility",
    "body_run": "signed run length of same-direction candle bodies (clipped ±8)",
    "range_pos_60": "(close - min(low,60)) / (max(high,60) - min(low,60))",
    "sr_dist_60": "distance to nearest 60-bar extreme in ATR units (+/-, sign = above/below)",
    "ret_20": "20-bar return, ATR-normalised",
    "ret_30": "30-bar return, ATR-normalised",
    "minute_sin": "sin(2π · minute-of-hour / 60) — intra-hour seasonality",
    "minute_cos": "cos(2π · minute-of-hour / 60)",
}


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0, np.nan)
    return (numerator / denominator).replace([np.inf, -np.inf], np.nan)


def build_extended_features(df: pd.DataFrame) -> pd.DataFrame:
    """The additional causal features (same index as `df`)."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    atr14 = ind.atr(df, 14)

    rsi7 = ind.rsi(close, 7)
    rsi14 = ind.rsi(close, 14)
    macd_hist = ind.macd(close)["hist"]

    returns = close.pct_change()
    vol5 = returns.rolling(5, min_periods=5).std()
    vol20 = returns.rolling(20, min_periods=20).std()
    atr5 = ind.atr(df, 5)
    atr20 = ind.atr(df, 20)

    direction = np.sign(close - open_)
    run = pd.Series(0.0, index=df.index)
    streak = 0.0
    previous = 0.0
    for position, sign in enumerate(direction.to_numpy()):
        if sign == 0 or sign != previous:
            streak = sign if sign != 0 else 0.0
        else:
            streak = np.clip(streak + sign, -8, 8)
        previous = sign
        run.iloc[position] = streak

    roll_low = low.rolling(60, min_periods=20).min()
    roll_high = high.rolling(60, min_periods=20).max()
    range_pos = _safe_div(close - roll_low, (roll_high - roll_low))
    ups = _safe_div(roll_high - close, atr14)
    downs = _safe_div(close - roll_low, atr14)
    sr_dist = (ups.fillna(0) - downs.fillna(0))
    sr_dist[ups.isna() & downs.isna()] = np.nan

    minute = df.index.minute.to_numpy().astype(float)
    angle = 2.0 * np.pi * minute / 60.0

    out = pd.DataFrame(
        {
            "rsi7_delta3": (rsi7 - rsi7.shift(3)) / 50.0,
            "rsi14_slope5": (rsi14 - rsi14.shift(5)) / 50.0,
            "macd_slope3": _safe_div(macd_hist - macd_hist.shift(3), atr14),
            "vol_ratio_5_20": _safe_div(vol5, vol20),
            "atr_ratio_5_20": _safe_div(atr5, atr20),
            "body_run": run,
            "range_pos_60": range_pos,
            "sr_dist_60": sr_dist,
            "ret_20": _safe_div(close - close.shift(20), atr14),
            "ret_30": _safe_div(close - close.shift(30), atr14),
            "minute_sin": pd.Series(np.sin(angle), index=df.index),
            "minute_cos": pd.Series(np.cos(angle), index=df.index),
        },
        index=df.index,
    )
    assert tuple(out.columns) == EXTENDED_FEATURE_NAMES, "extended feature order changed — update the version"
    return out


def feature_names(feature_set: str = FEATURE_SET_EXTENDED) -> tuple[str, ...]:
    if feature_set == FEATURE_SET_BASE:
        return FEATURE_NAMES
    if feature_set == FEATURE_SET_EXTENDED:
        return FEATURE_NAMES + EXTENDED_FEATURE_NAMES
    raise ValueError(f"unknown feature set {feature_set!r}")


def feature_set_version(feature_set: str = FEATURE_SET_EXTENDED) -> str:
    return FEATURE_SET_VERSION_BASE if feature_set == FEATURE_SET_BASE else FEATURE_SET_VERSION_EXTENDED


def build_features(df: pd.DataFrame, spec: AssetSpec | None = None,
                   feature_set: str = FEATURE_SET_EXTENDED) -> pd.DataFrame:
    """Base features (+ the extended block) in a stable column order."""
    base = build_feature_frame(df, spec)
    if feature_set == FEATURE_SET_BASE:
        return base[list(FEATURE_NAMES)]
    extended = build_extended_features(df)
    combined = pd.concat([base, extended], axis=1)
    return combined[list(feature_names(feature_set))]


def describe(feature_set: str = FEATURE_SET_EXTENDED) -> dict[str, Any]:
    """What this feature set is, for the experiment records."""
    names = feature_names(feature_set)
    return {
        "feature_set": feature_set,
        "feature_version": feature_set_version(feature_set),
        "count": len(names),
        "base_count": len(FEATURE_NAMES),
        "extended_count": len(EXTENDED_FEATURE_NAMES) if feature_set == FEATURE_SET_EXTENDED else 0,
        "extended_features": [
            {"name": name, "description": _DESCRIPTIONS.get(name, "")}
            for name in (EXTENDED_FEATURE_NAMES if feature_set == FEATURE_SET_EXTENDED else ())
        ],
        "causality": "every feature is computed from bars <= i (proved in tests/test_research_lab.py)",
    }
