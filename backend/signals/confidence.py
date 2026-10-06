"""Weighted confidence engine.

confidence = Σ weightᵢ · scoreᵢ   (weights documented in constants.CONFIDENCE_WEIGHTS)

Components:
    model             calibrated ensemble probability of the chosen direction
    model_agreement   weighted agreement across models (dispersion penalty)
    trend             EMA stack / slope / ADX alignment
    momentum          RSI / MACD / recent-return alignment
    candle            last closed candle structure + 2-candle confirmation
    structure         support/resistance room for the trade
    volatility        suitability of the current ATR for a 1-minute expiry
    regime            trend vs chop, anomaly veto
    freshness         how old the newest candle is

Important: uncertainty is NEVER converted into confidence. A neutral-heavy
ensemble scores low on the model component; the engine's preferred outcome is
NO TRADE whenever the evidence is thin.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..constants import CONFIDENCE_WEIGHTS, QUALITY_BANDS, TRADABLE_MIN_QUALITY
from ..features.rules import RuleScore


def _clip01(value: float) -> float:
    return float(np.clip(value, 0.0, 1.0))


def quality_for(confidence: float) -> tuple[str, str]:
    for floor, code, label in QUALITY_BANDS:
        if confidence >= floor:
            return code, label
    return "NO_TRADE", "NO TRADE"


def model_component(ensemble) -> tuple[float, dict[str, Any]]:
    """Map ensemble probabilities to a 0–100 model score (documented mapping)."""
    raw = float(ensemble.direction_probability)              # 3-class probability of the chosen side
    norm = float(ensemble.directional_probability_normalised)  # winner / (CALL+PUT)
    neutral = float(ensemble.p_neutral)

    raw_term = _clip01((raw - 0.33) / 0.20)     # 0.33 → 0, 0.53+ → 1
    norm_term = _clip01((norm - 0.52) / 0.28)   # 0.52 → 0, 0.80+ → 1
    neutral_penalty = _clip01((neutral - 0.34) / 0.25)  # neutral-heavy ensembles are discounted

    score = 100.0 * (0.40 * raw_term + 0.60 * norm_term) * (1.0 - 0.45 * neutral_penalty)
    detail = {
        "p_direction": round(raw, 4),
        "p_direction_normalised": round(norm, 4),
        "p_neutral": round(neutral, 4),
        "raw_term": round(raw_term, 3),
        "normalised_term": round(norm_term, 3),
        "neutral_penalty": round(neutral_penalty, 3),
        "mapping": "0.40·f(p_dir) + 0.60·f(p_norm), discounted by NEUTRAL mass",
    }
    return round(float(np.clip(score, 0, 100)), 2), detail


def agreement_component(ensemble) -> tuple[float, dict[str, Any]]:
    agreement = float(ensemble.agreement)
    score = 55.0 + 45.0 * _clip01((agreement - 0.60) / 0.40)
    if ensemble.no_trade_votes:
        score -= 6.0 * ensemble.no_trade_votes
    detail = {
        "agreement": round(agreement, 4),
        "agreeing_models": ensemble.agreeing_models,
        "no_trade_votes": ensemble.no_trade_votes,
        "spread": round(float(ensemble.spread), 4),
    }
    return round(float(np.clip(score, 0, 100)), 2), detail


@dataclass
class ConfidenceBreakdown:
    confidence: float
    quality: str
    quality_label: str
    tradable: bool
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=lambda: dict(CONFIDENCE_WEIGHTS))
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "confidence": round(self.confidence, 2),
            "quality": self.quality,
            "quality_label": self.quality_label,
            "tradable": self.tradable,
            "components": self.components,
            "weights": self.weights,
            "notes": self.notes,
        }


def compute_confidence(
    *,
    ensemble,
    rule_scores: dict[str, RuleScore],
    freshness: RuleScore,
) -> ConfidenceBreakdown:
    model_score, model_detail = model_component(ensemble)
    agreement_score, agreement_detail = agreement_component(ensemble)

    scores: dict[str, tuple[float, dict[str, Any]]] = {
        "model": (model_score, model_detail),
        "model_agreement": (agreement_score, agreement_detail),
        "trend": (rule_scores["trend"].score, rule_scores["trend"].detail),
        "momentum": (rule_scores["momentum"].score, rule_scores["momentum"].detail),
        "candle": (rule_scores["candle"].score, rule_scores["candle"].detail),
        "structure": (rule_scores["structure"].score, rule_scores["structure"].detail),
        "volatility": (rule_scores["volatility"].score, rule_scores["volatility"].detail),
        "regime": (rule_scores["regime"].score, rule_scores["regime"].detail),
        "freshness": (freshness.score, freshness.detail),
    }

    total_weight = sum(CONFIDENCE_WEIGHTS.values())
    confidence = 0.0
    components: dict[str, dict[str, Any]] = {}
    for name, (score, detail) in scores.items():
        weight = CONFIDENCE_WEIGHTS[name] / total_weight
        contribution = weight * score
        confidence += contribution
        components[name] = {
            "score": round(float(score), 2),
            "weight": round(weight, 4),
            "contribution": round(contribution, 2),
            "aligned": bool(getattr(rule_scores.get(name), "aligned", score >= 50.0)),
            "evidence": round(float(getattr(rule_scores.get(name), "evidence", 0.0)), 3),
            "detail": detail,
        }

    confidence = float(np.clip(confidence, 0.0, 100.0))
    code, label = quality_for(confidence)
    tradable = confidence >= TRADABLE_MIN_QUALITY and code in {"VERY_HIGH", "HIGH"}
    notes: list[str] = []
    weakest = sorted(components.items(), key=lambda kv: kv[1]["score"])[:2]
    for name, payload in weakest:
        if payload["score"] < 55:
            notes.append(f"{name.replace('_', ' ')} component weak ({payload['score']:.0f}/100)")
    if ensemble.anomaly_flag:
        notes.append("anomaly detector veto")
    return ConfidenceBreakdown(confidence=confidence, quality=code, quality_label=label,
                             tradable=tradable, components=components, notes=notes)
