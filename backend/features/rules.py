"""Rule-based context scores.

These produce *signed* evidence in [-1, +1] per analysis dimension and are then
mapped to 0–100 "agreement with the proposed direction" scores for the
confidence engine. They never produce a direction on their own: the direction
comes from the model ensemble, and these rules can veto or degrade it.

Every function returns (score_0_100, detail_dict) so the UI can explain itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..constants import GATES


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(np.clip(value, lo, hi))


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if not np.isfinite(f) else f


@dataclass
class RuleScore:
    name: str
    score: float                  # 0..100 agreement score
    aligned: bool                 # signed evidence supports the direction
    evidence: float               # signed evidence in [-1, 1]
    detail: dict[str, Any]


def direction_sign(direction: str) -> int:
    return 1 if direction.upper() == "CALL" else -1


# ----------------------------------------------------------------------- trend
def trend_score(snapshot: dict[str, Any], features: dict[str, float], direction: str) -> RuleScore:
    sign = direction_sign(direction)
    atr_norm = _safe(features.get("atr_norm"), 0.0)
    stack_bull = 1.0 if snapshot.get("ema_stack") == "BULL" else -1.0 if snapshot.get("ema_stack") == "BEAR" else 0.0

    ema9_21 = _safe(features.get("ema9_21"))
    ema21_50 = _safe(features.get("ema21_50"))
    slope21 = _safe(features.get("ema21_slope"))
    di_diff = _safe(features.get("di_diff"))
    adx = _safe(features.get("adx14"))

    evidence = (
        0.30 * _clip(ema9_21 / 0.80)
        + 0.20 * _clip(ema21_50 / 1.20)
        + 0.25 * _clip(slope21 / 0.50)
        + 0.15 * _clip(di_diff / 0.35)
        + 0.10 * stack_bull
    )
    strength = _clip(adx / 30.0, 0.0, 1.0)
    aligned = evidence * sign
    score = 50.0 + 45.0 * _clip(aligned) * (0.60 + 0.40 * strength)
    if atr_norm <= 0:
        score = 50.0
    detail = {
        "ema_stack": snapshot.get("ema_stack"),
        "ema9_21_atr": round(ema9_21, 3),
        "ema21_50_atr": round(ema21_50, 3),
        "ema21_slope": round(slope21, 3),
        "adx14": round(adx, 1),
        "di_diff": round(di_diff, 3),
        "evidence": round(evidence, 3),
        "aligned_evidence": round(aligned, 3),
    }
    return RuleScore("trend", round(float(np.clip(score, 0, 100)), 2), bool(aligned > 0.05), round(evidence, 3), detail)


# -------------------------------------------------------------------- momentum
def momentum_score(snapshot: dict[str, Any], features: dict[str, float], direction: str) -> RuleScore:
    sign = direction_sign(direction)
    rsi7 = _safe(snapshot.get("rsi7"), 50.0)
    rsi14 = _safe(snapshot.get("rsi14"), 50.0)
    hist = _safe(features.get("macd_hist_norm"))
    hist_slope = _safe(features.get("macd_hist_slope"))
    ret_3 = _safe(features.get("ret_3"))
    atr_norm = max(_safe(features.get("atr_norm"), 1e-6), 1e-6)

    rsi_term = _clip((rsi14 - 50.0) / 15.0)
    if rsi14 >= 78 or rsi14 <= 22:  # exhaustion zone — momentum is stretched
        rsi_term *= 0.45
    evidence = (
        0.28 * rsi_term
        + 0.22 * _clip(hist / 0.60)
        + 0.18 * _clip(hist_slope / 0.20)
        + 0.32 * _clip((ret_3 / atr_norm) / 2.5)
    )
    aligned = evidence * sign
    score = 50.0 + 45.0 * _clip(aligned)
    detail = {
        "rsi7": round(rsi7, 1),
        "rsi14": round(rsi14, 1),
        "macd_hist_atr": round(hist, 3),
        "macd_hist_slope": round(hist_slope, 3),
        "ret_3_atr": round(ret_3 / atr_norm, 3),
        "evidence": round(evidence, 3),
        "aligned_evidence": round(aligned, 3),
    }
    return RuleScore("momentum", round(float(np.clip(score, 0, 100)), 2), bool(aligned > 0.05), round(evidence, 3), detail)


# ---------------------------------------------------------------------- candle
def candle_score(snapshot: dict[str, Any], features: dict[str, float], direction: str) -> RuleScore:
    sign = direction_sign(direction)
    last = snapshot.get("last_candle") or {}
    prev = snapshot.get("prev_candle") or {}
    body_dir = 1.0 if last.get("direction") == "UP" else -1.0 if last.get("direction") == "DOWN" else 0.0
    prev_dir = 1.0 if prev.get("direction") == "UP" else -1.0 if prev.get("direction") == "DOWN" else 0.0
    body_ratio = _safe(last.get("body_ratio"), 0.0)
    close_pos = _safe(last.get("close_pos"), 0.5)
    upper_wick = _safe(last.get("upper_wick_ratio"), 0.0)
    lower_wick = _safe(last.get("lower_wick_ratio"), 0.0)

    body_term = body_dir * (0.35 + 0.65 * body_ratio)
    confirm_term = 0.5 * prev_dir
    # close position: closing near the high supports CALL, near the low supports PUT
    pos_term = (close_pos - 0.5) * 2.0
    # rejection wicks: long lower wick = buyers defended (CALL), long upper wick = sellers (PUT)
    wick_term = (lower_wick - upper_wick)
    evidence = 0.42 * body_term + 0.22 * confirm_term + 0.20 * pos_term + 0.16 * _clip(wick_term / 0.5)
    aligned = evidence * sign
    score = 50.0 + 45.0 * _clip(aligned)
    detail = {
        "candle_direction": last.get("direction"),
        "body_ratio": round(body_ratio, 3),
        "close_position": round(close_pos, 3),
        "upper_wick_ratio": round(upper_wick, 3),
        "lower_wick_ratio": round(lower_wick, 3),
        "prev_candle": prev.get("direction"),
        "evidence": round(evidence, 3),
        "aligned_evidence": round(aligned, 3),
    }
    return RuleScore("candle", round(float(np.clip(score, 0, 100)), 2), bool(aligned > 0.05), round(evidence, 3), detail)


# ------------------------------------------------------------------- structure
def structure_score(snapshot: dict[str, Any], features: dict[str, float], direction: str) -> RuleScore:
    sign = direction_sign(direction)
    sr = snapshot.get("support_resistance") or {}
    res_dist = _safe(sr.get("resistance_distance_atr"), 99.0)
    sup_dist = _safe(sr.get("support_distance_atr"), 99.0)
    range_pos = _safe(sr.get("range_position"), 0.5)

    if sign > 0:
        room = _clip(res_dist / 2.0, 0.0, 1.0)          # room to resistance
        position_term = 1.0 - abs(range_pos - 0.35) / 0.65  # prefer entering in the lower half
        risk = _clip(sup_dist / 1.5, 0.0, 1.0)          # support nearby = structure behind us
    else:
        room = _clip(sup_dist / 2.0, 0.0, 1.0)
        position_term = 1.0 - abs(range_pos - 0.65) / 0.65
        risk = _clip(res_dist / 1.5, 0.0, 1.0)

    evidence = 0.55 * room + 0.25 * _clip(position_term) + 0.20 * risk
    evidence = _clip(evidence * 2.0 - 1.0, -1.0, 1.0)  # 0.5 → neutral, 1.0 (plenty of room) → +1
    score = 50.0 + 45.0 * _clip(evidence)
    detail = {
        "nearest_support": sr.get("nearest_support"),
        "nearest_resistance": sr.get("nearest_resistance"),
        "resistance_distance_atr": round(res_dist, 2),
        "support_distance_atr": round(sup_dist, 2),
        "range_position": round(range_pos, 3),
        "evidence": round(evidence, 3),
    }
    return RuleScore("structure", round(float(np.clip(score, 0, 100)), 2), bool(evidence > 0.0), round(evidence, 3), detail)


# ------------------------------------------------------------------ volatility
def volatility_score(snapshot: dict[str, Any], features: dict[str, float]) -> RuleScore:
    atr_pct = _safe(features.get("atr_pct"), 0.0)
    median_pct = _safe(snapshot.get("median_atr_pct_200"), 0.0) / 100.0
    ratio = (atr_pct / median_pct) if median_pct > 0 else 1.0

    hard_lo, hard_hi = GATES["min_volatility_pct"], GATES["max_volatility_pct"]
    if atr_pct <= 0 or atr_pct < hard_lo or atr_pct > hard_hi:
        score = 0.0
        zone = "OUT_OF_BOUNDS"
    elif 0.75 <= ratio <= 1.75:
        score = 100.0
        zone = "IDEAL"
    elif ratio < 0.75:
        score = float(np.clip(55.0 + (ratio - 0.35) * 120.0, 0.0, 100.0))
        zone = "QUIET"
    else:
        score = float(np.clip(100.0 - (ratio - 1.75) * 55.0, 0.0, 100.0))
        zone = "EXPANDED"
    detail = {
        "atr_pct": round(atr_pct * 100, 4),
        "median_atr_pct_asset": round(median_pct * 100, 4),
        "volatility_ratio": round(ratio, 3),
        "zone": zone,
        "bounds_pct": [round(hard_lo * 100, 4), round(hard_hi * 100, 4)],
    }
    return RuleScore("volatility", round(score, 2), score >= 50.0, 0.0, detail)


# ---------------------------------------------------------------------- regime
def regime_score(snapshot: dict[str, Any], features: dict[str, float], direction: str, anomaly: bool) -> RuleScore:
    adx = _safe(features.get("adx14"), 0.0)
    eff10 = _safe(snapshot.get("efficiency_ratio_10"), 0.0)
    eff20 = _safe(snapshot.get("efficiency_ratio_20"), 0.0)
    trend_persist = _safe(features.get("trend_persist"))
    sign = direction_sign(direction)

    structure_q = _clip((adx - 15.0) / 15.0, 0.0, 1.0) * 0.5 + _clip(eff10 / 0.35, 0.0, 1.0) * 0.5
    if adx < 12.0 or eff10 < 0.10:
        regime = "CHOPPY"
    elif adx >= 25.0 and eff10 >= 0.25:
        regime = "TRENDING"
    else:
        regime = "TRANSITIONAL"

    base = {"CHOPPY": 28.0, "TRANSITIONAL": 62.0, "TRENDING": 92.0}[regime]
    base *= 0.85 + 0.15 * structure_q
    persist_align = _clip(trend_persist * sign)
    score = base * (0.80 + 0.20 * _clip(persist_align, 0.0, 1.0))
    if anomaly:
        score = 0.0
        regime = "ANOMALOUS"
    detail = {
        "regime": regime,
        "adx14": round(adx, 1),
        "efficiency_ratio_10": round(eff10, 3),
        "efficiency_ratio_20": round(eff20, 3),
        "trend_persistence": round(trend_persist, 3),
        "anomaly_flagged": anomaly,
    }
    return RuleScore("regime", round(float(np.clip(score, 0, 100)), 2), score >= 55.0, 0.0, detail)


# ------------------------------------------------------------------- freshness
def freshness_score(lag_seconds: float, realtime: bool, max_lag: float) -> RuleScore:
    if not np.isfinite(lag_seconds):
        return RuleScore("freshness", 0.0, False, 0.0, {"lag_seconds": None, "feed": "no timestamp"})
    if max_lag <= 0:
        max_lag = 120.0
    if lag_seconds <= max(5.0, max_lag * 0.35):
        score = 100.0
    elif lag_seconds >= max_lag:
        score = 0.0
    else:
        span = max_lag - max(5.0, max_lag * 0.35)
        score = 100.0 * (1.0 - (lag_seconds - max(5.0, max_lag * 0.35)) / span)
    detail = {
        "lag_seconds": round(lag_seconds, 1),
        "feed_realtime": realtime,
        "max_lag_seconds": max_lag,
    }
    return RuleScore("freshness", round(float(np.clip(score, 0, 100)), 2), score > 0, 0.0, detail)
