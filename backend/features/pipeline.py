"""M1 feature engineering.

`build_feature_frame` is fully vectorised and strictly causal: the value at row t
uses only rows ≤ t. The identical code path is used for training and for live
inference, which removes train/serve skew.

`latest_features` returns the last complete row plus a human-readable snapshot
(the numbers the UI shows in MARKET CONDITIONS and the checks panel).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..constants import AssetSpec
from ..timeutil import to_ist
from . import indicators as ind

# Canonical, ordered feature list. Models are trained against exactly this order.
FEATURE_NAMES: tuple[str, ...] = (
    "ret_1", "ret_3", "ret_5", "ret_10",
    "atr_norm",
    "body_ratio", "upper_wick_ratio", "lower_wick_ratio", "close_pos", "range_norm",
    "consec",
    "ema9_dist", "ema21_dist", "ema50_dist", "ema9_21", "ema21_50",
    "ema9_slope", "ema21_slope",
    "rsi14", "rsi7", "rsi14_delta3",
    "macd_hist_norm", "macd_line_norm", "macd_hist_slope",
    "bb_pos", "bb_width_norm", "bb_width_pct",
    "atr_expansion", "atr_pct",
    "adx14", "di_diff",
    "vol_z",
    "sr_support_dist", "sr_resistance_dist", "range_position",
    "don_break_up", "don_break_dn",
    "mean_rev_z", "trend_persist", "eff_ratio_10", "eff_ratio_20",
    "hour_sin", "hour_cos", "dow_norm",
)


class FeatureError(RuntimeError):
    """Raised when the feature block cannot be computed from the candles."""

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason + (f" ({detail})" if detail else ""))
        self.reason = reason
        self.detail = detail


def build_feature_frame(df: pd.DataFrame, spec: AssetSpec | None = None) -> pd.DataFrame:
    """All model/rule features for a closed-candle OHLCV frame (UTC index)."""
    if df is None or len(df) < 60:
        raise FeatureError("Insufficient candles for feature computation", f"rows={0 if df is None else len(df)}")

    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    atr14 = ind.atr(df, 14)
    atr_safe = atr14.replace(0, np.nan)
    close_safe = c.replace(0, np.nan)

    feat = pd.DataFrame(index=df.index)
    logc = np.log(close_safe)
    feat["ret_1"] = logc.diff(1)
    feat["ret_3"] = logc.diff(3)
    feat["ret_5"] = logc.diff(5)
    feat["ret_10"] = logc.diff(10)
    feat["atr_norm"] = atr14 / close_safe

    anatomy = ind.candle_anatomy(df)
    feat["body_ratio"] = anatomy["body_ratio"]
    feat["upper_wick_ratio"] = anatomy["upper_wick_ratio"]
    feat["lower_wick_ratio"] = anatomy["lower_wick_ratio"]
    feat["close_pos"] = anatomy["close_pos"]
    feat["range_norm"] = anatomy["range"] / close_safe

    # signed consecutive-direction run
    direction = np.sign(c.diff()).fillna(0.0)
    run = direction.copy()
    streak = 0.0
    streak_values = []
    for value in run.to_numpy():
        if value == 0:
            streak = 0.0
        elif streak == 0 or np.sign(streak) == np.sign(value):
            streak += value
        else:
            streak = value
        streak_values.append(float(np.clip(streak, -6, 6)))
    feat["consec"] = streak_values

    ema9, ema21, ema50 = ind.ema(c, 9), ind.ema(c, 21), ind.ema(c, 50)
    feat["ema9_dist"] = (c - ema9) / atr_safe
    feat["ema21_dist"] = (c - ema21) / atr_safe
    feat["ema50_dist"] = (c - ema50) / atr_safe
    feat["ema9_21"] = (ema9 - ema21) / atr_safe
    feat["ema21_50"] = (ema21 - ema50) / atr_safe

    slope9 = ema9.diff(10) / atr_safe
    slope21 = ema21.diff(10) / atr_safe
    feat["ema9_slope"] = slope9.clip(-3, 3)
    feat["ema21_slope"] = slope21.clip(-3, 3)

    rsi14 = ind.rsi(c, 14)
    rsi7 = ind.rsi(c, 7)
    feat["rsi14"] = rsi14
    feat["rsi7"] = rsi7
    feat["rsi14_delta3"] = (rsi14 - rsi14.shift(3)).clip(-40, 40)

    macd_df = ind.macd(c)
    feat["macd_hist_norm"] = macd_df["hist"] / atr_safe
    feat["macd_line_norm"] = macd_df["macd"] / atr_safe
    feat["macd_hist_slope"] = (macd_df["hist"].diff(3) / atr_safe).clip(-3, 3)

    bb = ind.bollinger(c, 20, 2.0)
    width = (bb["upper"] - bb["lower"]).replace(0, np.nan)
    feat["bb_pos"] = ((c - bb["lower"]) / width).clip(-0.5, 1.5)
    feat["bb_width_norm"] = (bb["width"] / atr_safe).clip(0, 20)
    feat["bb_width_pct"] = (bb["width"] / close_safe).clip(0, 0.2)

    feat["atr_expansion"] = (atr14 / atr14.shift(50).replace(0, np.nan)).clip(0, 6) - 1.0
    feat["atr_pct"] = (atr14 / close_safe).clip(0, 0.2)

    adx_df = ind.adx(df, 14)
    feat["adx14"] = adx_df["adx"]
    feat["di_diff"] = ((adx_df["plus_di"] - adx_df["minus_di"]) / 100.0).clip(-1, 1)

    volume = v if v.notna().any() else pd.Series(np.nan, index=df.index)
    vol_mean = volume.rolling(20, min_periods=10).mean()
    vol_std = volume.rolling(20, min_periods=10).std(ddof=0).replace(0, np.nan)
    feat["vol_z"] = ((volume - vol_mean) / vol_std).clip(-4, 4).fillna(0.0)

    roll_low = l.rolling(90, min_periods=30).min().shift(2)
    roll_high = h.rolling(90, min_periods=30).max().shift(2)
    feat["sr_support_dist"] = ((c - roll_low) / atr_safe).clip(0, 60)
    feat["sr_resistance_dist"] = ((roll_high - c) / atr_safe).clip(0, 60)
    span = (roll_high - roll_low).replace(0, np.nan)
    feat["range_position"] = ((c - roll_low) / span).clip(0, 1)

    don_high = h.rolling(20, min_periods=20).max().shift(1)
    don_low = l.rolling(20, min_periods=20).min().shift(1)
    feat["don_break_up"] = (c > don_high).astype(float)
    feat["don_break_dn"] = (c < don_low).astype(float)

    feat["mean_rev_z"] = ((c - ind.sma(c, 20)) / c.rolling(20, min_periods=20).std(ddof=0).replace(0, np.nan)).clip(-5, 5)
    sign_dir = direction.replace(0, np.nan)
    feat["trend_persist"] = sign_dir.rolling(15, min_periods=8).mean().fillna(0.0)
    feat["eff_ratio_10"] = ind.efficiency_ratio(c, 10)
    feat["eff_ratio_20"] = ind.efficiency_ratio(c, 20)

    ist_index = df.index.tz_convert("Asia/Kolkata")
    hours = ist_index.hour + ist_index.minute / 60.0
    feat["hour_sin"] = np.sin(2 * np.pi * hours / 24.0)
    feat["hour_cos"] = np.cos(2 * np.pi * hours / 24.0)
    feat["dow_norm"] = (ist_index.dayofweek - 2) / 3.0  # centred on Wednesday

    feat = feat.replace([np.inf, -np.inf], np.nan)
    return feat


def label_frame(df: pd.DataFrame, horizon: int = 1, neutral_atr_fraction: float = 0.10) -> pd.DataFrame:
    """Forward labels for supervised training (uses ONLY future data in `y`).

    y = 2 (UP/CALL) if the close `horizon` minutes ahead is higher by more than
    `neutral_atr_fraction * ATR`, 0 (DOWN/PUT) if lower by the same margin,
    otherwise 1 (NEUTRAL — a coin-flip move the system refuses to bet on).
    """
    atr14 = ind.atr(df, 14)
    future_close = df["close"].shift(-horizon)
    move = future_close - df["close"]
    threshold = (neutral_atr_fraction * atr14).abs()
    y = pd.Series(1, index=df.index, dtype=float)
    y[move > threshold] = 2.0
    y[move < -threshold] = 0.0
    y[move.isna() | threshold.isna()] = np.nan
    out = pd.DataFrame({"y": y, "future_close": future_close, "move": move,
                        "threshold": threshold, "atr": atr14})
    return out


@dataclass
class FeatureSnapshot:
    values: dict[str, float]
    feature_row: pd.Series
    price: float
    missing: list[str]
    extras: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "features": {k: round(float(v), 6) for k, v in self.values.items()},
            "missing": self.missing,
            "extras": self.extras,
        }


def _round(value: Any, digits: int = 5) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return round(f, digits)


def latest_features(df: pd.DataFrame, spec: AssetSpec | None = None) -> FeatureSnapshot:
    """Feature row for the newest CLOSED candle, plus a display snapshot."""
    if len(df) < 60:
        raise FeatureError("Insufficient candles for features", f"rows={len(df)}")
    feat = build_feature_frame(df, spec)
    row = feat.iloc[-1]
    missing = [name for name in FEATURE_NAMES if name not in feat.columns or pd.isna(row[name])]
    if missing:
        raise FeatureError(
            "Feature block incomplete (missing values)",
            f"{len(missing)} of {len(FEATURE_NAMES)}: {', '.join(missing[:6])}"
            + ("…" if len(missing) > 6 else ""),
        )

    price = float(df["close"].iloc[-1])
    atr14 = float(ind.atr(df, 14).iloc[-1])
    sr = ind.support_resistance(df, price, lookback=90)
    adx_df = ind.adx(df, 14)
    macd_df = ind.macd(df["close"])
    bb = ind.bollinger(df["close"], 20, 2.0)
    ema9 = ind.ema(df["close"], 9).iloc[-1]
    ema21 = ind.ema(df["close"], 21).iloc[-1]
    ema50 = ind.ema(df["close"], 50).iloc[-1]
    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_ist = to_ist(df.index[-1].to_pydatetime())

    extras: dict[str, Any] = {
        "candle_time_ist": last_ist.strftime("%H:%M:%S"),
        "candle_time_utc": df.index[-1].isoformat(),
        "price": price,
        "atr14": _round(atr14, 6),
        "atr_pct": _round(float(row["atr_pct"]) * 100, 4),
        "ema9": _round(ema9, 6),
        "ema21": _round(ema21, 6),
        "ema50": _round(ema50, 6),
        "ema_stack": ("BULL" if ema9 > ema21 > ema50 else "BEAR" if ema9 < ema21 < ema50 else "MIXED"),
        "rsi7": _round(ind.rsi(df["close"], 7).iloc[-1], 2),
        "rsi14": _round(ind.rsi(df["close"], 14).iloc[-1], 2),
        "macd_hist": _round(macd_df["hist"].iloc[-1], 6),
        "macd_hist_prev": _round(macd_df["hist"].iloc[-2], 6),
        "adx14": _round(adx_df["adx"].iloc[-1], 2),
        "plus_di": _round(adx_df["plus_di"].iloc[-1], 2),
        "minus_di": _round(adx_df["minus_di"].iloc[-1], 2),
        "bb_upper": _round(bb["upper"].iloc[-1], 6),
        "bb_lower": _round(bb["lower"].iloc[-1], 6),
        "bb_mid": _round(bb["mid"].iloc[-1], 6),
        "efficiency_ratio_10": _round(ind.efficiency_ratio(df["close"], 10).iloc[-1], 4),
        "efficiency_ratio_20": _round(ind.efficiency_ratio(df["close"], 20).iloc[-1], 4),
        "volume_z": _round(row["vol_z"], 3),
        "has_volume": bool(df["volume"].notna().any()),
        "last_candle": {
            "open": _round(last["open"], 6), "high": _round(last["high"], 6),
            "low": _round(last["low"], 6), "close": _round(last["close"], 6),
            "direction": "UP" if last["close"] > last["open"] else "DOWN" if last["close"] < last["open"] else "FLAT",
            "body_ratio": _round(row["body_ratio"], 4),
            "upper_wick_ratio": _round(row["upper_wick_ratio"], 4),
            "lower_wick_ratio": _round(row["lower_wick_ratio"], 4),
            "close_pos": _round(row["close_pos"], 4),
        },
        "prev_candle": {
            "open": _round(prev["open"], 6), "high": _round(prev["high"], 6),
            "low": _round(prev["low"], 6), "close": _round(prev["close"], 6),
            "direction": "UP" if prev["close"] > prev["open"] else "DOWN" if prev["close"] < prev["open"] else "FLAT",
        },
        "support_resistance": {
            "nearest_support": _round(sr["nearest_support"], 6),
            "nearest_resistance": _round(sr["nearest_resistance"], 6),
            "range_high": _round(sr["range_high"], 6),
            "range_low": _round(sr["range_low"], 6),
            "range_position": _round(sr["range_position"], 4),
            "support_distance_atr": _round((price - sr["nearest_support"]) / atr14 if atr14 else None, 3),
            "resistance_distance_atr": _round((sr["nearest_resistance"] - price) / atr14 if atr14 else None, 3),
        },
        "consecutive_run": _round(row["consec"], 2),
        "median_atr_pct_200": _round(float(feat["atr_pct"].tail(200).median()) * 100, 4),
    }
    values = {name: float(row[name]) for name in FEATURE_NAMES}
    return FeatureSnapshot(values=values, feature_row=row, price=price, missing=[], extras=extras)
