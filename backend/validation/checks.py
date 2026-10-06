"""Pre-signal validation checks.

Six checks run before any signal may be emitted:

    DATA CHECK                candles present, complete, fresh enough for the feed type
    MODEL CHECK               artifacts loaded, no model errors, agreement/uncertainty acceptable
    FEATURE CHECK             full feature vector, finite, within training-ish range
    REGIME CHECK              volatility bounds, ADX/efficiency regime, anomaly detector
    CONFIDENCE CHECK          computed confidence ≥ configured threshold
    SIGNAL CONSISTENCY CHECK  rules align with the model direction, structure not blocking

Each check returns structured evidence. A BLOCK failure means NO TRADE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from ..constants import GATES, TRADABLE_MIN_QUALITY
from ..features.pipeline import FEATURE_NAMES
from ..timeutil import utc_iso

SEVERITY_BLOCK = "BLOCK"
SEVERITY_WARN = "WARN"


@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str = SEVERITY_BLOCK
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "detail": self.detail,
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------- DATA
def data_check(frame, lag_seconds: float, max_lag_seconds: float) -> CheckResult:
    candles = int(len(frame.df))
    meta = frame.meta
    evidence = {
        "candles": candles,
        "provider": meta.provider,
        "source_label": meta.source_label,
        "feed_mode": meta.mode,
        "realtime": meta.realtime,
        "is_simulated": meta.is_simulated,
        "lag_seconds": None if not np.isfinite(lag_seconds) else round(float(lag_seconds), 1),
        "max_lag_seconds": max_lag_seconds,
        "latest_candle_utc": meta.latest_candle_open_utc,
        "has_volume": meta.has_volume,
    }
    if candles < GATES["min_candles"]:
        return CheckResult("DATA CHECK", False, SEVERITY_BLOCK,
                           f"Insufficient candles: {candles} (minimum {GATES['min_candles']})", evidence)
    if not np.isfinite(lag_seconds):
        return CheckResult("DATA CHECK", False, SEVERITY_BLOCK, "Feed did not provide a candle timestamp", evidence)
    if lag_seconds > max_lag_seconds:
        return CheckResult(
            "DATA CHECK", False, SEVERITY_BLOCK,
            f"Stale data: newest candle closed {lag_seconds:.0f}s ago "
            f"(limit {max_lag_seconds:.0f}s for a {meta.mode.lower()} feed)", evidence,
        )
    gap_note = _gap_check(frame)
    if gap_note:
        evidence["gaps"] = gap_note
    suffix = f" — {gap_note} missing candle(s) detected in the window (tolerated)" if gap_note else ""
    return CheckResult("DATA CHECK", True, SEVERITY_BLOCK,
                       f"Data verified: {candles} candles from {meta.source_label}{suffix}", evidence)


def _gap_check(frame, tolerance: int = 3) -> str | None:
    import pandas as pd

    index = frame.df.index
    if len(index) < 3:
        return None
    deltas = index.to_series().diff().dropna()
    missing = int((deltas > pd.Timedelta(seconds=90)).sum())
    return f"{missing}" if missing > tolerance else None


# -------------------------------------------------------------------- MODEL
def model_check(ensemble, model_status: dict) -> CheckResult:
    evidence = {
        "models_used": [v.model for v in ensemble.votes if v.status == "OK"],
        "models_failed": [v.model for v in ensemble.votes if v.status == "ERROR"],
        "model_votes": [v.as_dict() for v in ensemble.votes],
        "agreement": round(ensemble.agreement, 4),
        "min_agreement": GATES["min_model_agreement"],
        "spread": round(ensemble.spread, 4),
        "max_spread": GATES["max_model_spread"],
        "p_direction": round(ensemble.direction_probability, 4),
        "p_direction_normalised": round(ensemble.directional_probability_normalised, 4),
        "p_neutral": round(ensemble.p_neutral, 4),
        "min_direction_probability": GATES["min_direction_probability"],
    }
    if not ensemble.votes:
        return CheckResult("MODEL CHECK", False, SEVERITY_BLOCK, "No model votes available", evidence)
    ok_votes = [v for v in ensemble.votes if v.status == "OK"]
    if len(ok_votes) < 2:
        return CheckResult(
            "MODEL CHECK", False, SEVERITY_BLOCK,
            f"Only {len(ok_votes)} model(s) produced a usable probability — ensemble needs at least 2", evidence,
        )
    if ensemble.anomaly_flag:
        return CheckResult("MODEL CHECK", False, SEVERITY_BLOCK,
                           "Anomaly detector flagged the current market state as out-of-distribution", evidence)
    if ensemble.agreement < GATES["min_model_agreement"]:
        return CheckResult(
            "MODEL CHECK", False, SEVERITY_BLOCK,
            f"Conflicting models: weighted agreement {ensemble.agreement:.2f} < {GATES['min_model_agreement']}", evidence,
        )
    if ensemble.spread > GATES["max_model_spread"]:
        return CheckResult(
            "MODEL CHECK", False, SEVERITY_BLOCK,
            f"Models disagree: probability spread {ensemble.spread:.3f} > {GATES['max_model_spread']}", evidence,
        )
    if ensemble.p_neutral > max(ensemble.p_call, ensemble.p_put):
        return CheckResult(
            "MODEL CHECK", False, SEVERITY_BLOCK,
            f"Models report uncertainty: NEUTRAL probability {ensemble.p_neutral:.2f} is the top class "
            "— this is a coin-flip setup, not a trade", evidence,
        )
    if ensemble.direction_probability < GATES["min_direction_probability"]:
        return CheckResult(
            "MODEL CHECK", False, SEVERITY_BLOCK,
            f"Directional probability too low: {ensemble.direction_probability:.2f} < "
            f"{GATES['min_direction_probability']}", evidence,
        )
    return CheckResult(
        "MODEL CHECK", True, SEVERITY_BLOCK,
        f"{len(ok_votes)} models agree (weighted agreement {ensemble.agreement:.2f}, spread {ensemble.spread:.3f})",
        evidence,
    )


# ------------------------------------------------------------------ FEATURE
def feature_check(feature_values: dict[str, float], training_feature_names: list[str] | None = None) -> CheckResult:
    names = training_feature_names or list(FEATURE_NAMES)
    missing = [n for n in names if n not in feature_values]
    nonfinite = [n for n in names if n in feature_values and not np.isfinite(feature_values[n])]
    evidence = {
        "expected_features": len(names),
        "missing": missing[:10],
        "non_finite": nonfinite[:10],
    }
    if missing:
        return CheckResult("FEATURE CHECK", False, SEVERITY_BLOCK,
                           f"Feature block incomplete: {len(missing)} missing ({', '.join(missing[:4])}…)", evidence)
    if nonfinite:
        return CheckResult("FEATURE CHECK", False, SEVERITY_BLOCK,
                           f"Feature block contains non-finite values: {', '.join(nonfinite[:4])}", evidence)
    extremes = [n for n in names if abs(float(feature_values[n])) > 1e4]
    if extremes:
        return CheckResult("FEATURE CHECK", False, SEVERITY_BLOCK,
                           f"Implausible feature magnitude: {', '.join(extremes[:4])}", evidence)
    evidence["checked"] = len(names)
    return CheckResult("FEATURE CHECK", True, SEVERITY_BLOCK, f"{len(names)} features computed and finite", evidence)


# ------------------------------------------------------------------- REGIME
def regime_check(components: dict[str, Any], snapshot_extras: dict[str, Any]) -> CheckResult:
    vol = components.get("volatility", {})
    reg = components.get("regime", {})
    evidence = {
        "volatility_zone": vol.get("detail", {}).get("zone"),
        "atr_pct": vol.get("detail", {}).get("atr_pct"),
        "volatility_ratio": vol.get("detail", {}).get("volatility_ratio"),
        "regime": reg.get("detail", {}).get("regime"),
        "adx14": reg.get("detail", {}).get("adx14"),
        "efficiency_ratio_10": reg.get("detail", {}).get("efficiency_ratio_10"),
        "min_volatility_pct": GATES["min_volatility_pct"],
        "max_volatility_pct": GATES["max_volatility_pct"],
    }
    zone = evidence["volatility_zone"]
    if zone == "OUT_OF_BOUNDS":
        atr_pct = evidence["atr_pct"] or 0.0
        direction_word = "below" if atr_pct * 100 < GATES["min_volatility_pct"] * 100 else "above"
        return CheckResult(
            "REGIME CHECK", False, SEVERITY_BLOCK,
            f"{'Extreme' if direction_word == 'above' else 'Dead'} volatility: ATR {atr_pct:.4f}% of price is "
            f"{direction_word} the tradable band "
            f"({GATES['min_volatility_pct'] * 100:.4f}%–{GATES['max_volatility_pct'] * 100:.4f}%)", evidence,
        )
    if evidence["regime"] == "ANOMALOUS":
        return CheckResult("REGIME CHECK", False, SEVERITY_BLOCK,
                           "Abnormal market regime: anomaly detector veto", evidence)
    if evidence["regime"] == "CHOPPY":
        return CheckResult(
            "REGIME CHECK", False, SEVERITY_BLOCK,
            f"Unfavorable market regime: choppy/mean-reverting "
            f"(ADX {evidence['adx14']}, efficiency {evidence['efficiency_ratio_10']})", evidence,
        )
    return CheckResult("REGIME CHECK", True, SEVERITY_BLOCK,
                       f"Regime OK: {evidence['regime']}, volatility {zone}", evidence)


# --------------------------------------------------------------- CONFIDENCE
def confidence_check(confidence: float, threshold: float) -> CheckResult:
    evidence = {"confidence": round(confidence, 2), "threshold": round(threshold, 2),
                "tradable_min_quality": TRADABLE_MIN_QUALITY}
    if confidence < threshold:
        return CheckResult("CONFIDENCE CHECK", False, SEVERITY_BLOCK,
                           f"Confidence {confidence:.1f}% is below threshold {threshold:.1f}%", evidence)
    if confidence < TRADABLE_MIN_QUALITY:
        return CheckResult("CONFIDENCE CHECK", False, SEVERITY_BLOCK,
                           f"Confidence {confidence:.1f}% below the {TRADABLE_MIN_QUALITY:.0f}% tradable floor", evidence)
    return CheckResult("CONFIDENCE CHECK", True, SEVERITY_BLOCK,
                       f"Confidence {confidence:.1f}% ≥ threshold {threshold:.1f}%", evidence)


# -------------------------------------------------------------- CONSISTENCY
def consistency_check(direction: str, components: dict[str, Any], min_aligned: int = 3) -> CheckResult:
    groups = ("trend", "momentum", "candle", "structure")
    aligned = [g for g in groups if components.get(g, {}).get("aligned")]
    opposing = [g for g in groups if components.get(g, {}).get("aligned") is False]
    evidence = {
        "direction": direction,
        "aligned_rules": aligned,
        "opposing_rules": opposing,
        "required_aligned": min_aligned,
        "structure_evidence": components.get("structure", {}).get("evidence"),
    }
    if len(aligned) < min_aligned:
        detail = (
            f"Conflicting indicators: only {len(aligned)}/{len(groups)} rule groups ({', '.join(aligned) or 'none'}) "
            f"support {direction}; need {min_aligned}"
        )
        if opposing:
            detail += f" — opposing: {', '.join(opposing)}"
        return CheckResult("SIGNAL CONSISTENCY CHECK", False, SEVERITY_BLOCK, detail, evidence)
    structure_evidence = float(components.get("structure", {}).get("evidence", 0.0))
    if structure_evidence < GATES["min_structure_evidence"]:
        return CheckResult(
            "SIGNAL CONSISTENCY CHECK", False, SEVERITY_BLOCK,
            "Structure blocks the trade: too little room before the opposing level", evidence,
        )
    return CheckResult("SIGNAL CONSISTENCY CHECK", True, SEVERITY_BLOCK,
                       f"{len(aligned)}/{len(groups)} rule groups align with {direction}", evidence)


def run_all_checks(*, data: CheckResult, model: CheckResult, features: CheckResult,
                   regime: CheckResult, confidence: CheckResult, consistency: CheckResult) -> dict[str, Any]:
    results = [data, model, features, regime, confidence, consistency]
    blocking = [r for r in results if not r.passed and r.severity == SEVERITY_BLOCK]
    return {
        "checks": [r.as_dict() for r in results],
        "passed": not blocking,
        "blocking": [r.name for r in blocking],
        "reasons": [r.detail for r in blocking],
    }


def max_lag_for(meta, *, realtime: bool | None = None) -> float:
    rt = meta.realtime if realtime is None else realtime
    return float(GATES["max_data_lag_seconds" if rt else "max_replay_lag_seconds"])


def stale_reason(meta, lag_seconds: float) -> str:
    return (
        f"Stale data: {meta.source_label} — newest candle closed {lag_seconds:.0f}s ago "
        f"(limit {max_lag_for(meta):.0f}s)"
    )


def describe_feed(meta) -> str:
    badge = meta.badge
    extra = " (not market data)" if meta.is_simulated else ""
    return f"{meta.source_label} · {badge}{extra}"


def utc_now_iso() -> str:
    return utc_iso()
