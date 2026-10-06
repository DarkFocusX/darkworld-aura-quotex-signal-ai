"""Ensemble scoring.

Class layout (3-class):  0 = DOWN (PUT), 1 = NEUTRAL / no-edge, 2 = UP (CALL).

The neutral class is what keeps the system honest: when the feature vector looks
like a coin flip the neutral probability rises and the ensemble reports
uncertainty instead of forcing a direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

CLASS_LABELS = ("PUT", "NEUTRAL", "CALL")

# Documented base weights; renormalised across whichever models are actually
# loaded. Never hand-adjusted per signal.
BASE_WEIGHTS: dict[str, float] = {
    "lightgbm": 0.34,
    "random_forest": 0.24,
    "logistic_regression": 0.16,
    "xgboost": 0.16,
    "neural": 0.10,
}


@dataclass
class ModelVote:
    model: str
    p_put: float
    p_neutral: float
    p_call: float
    weight: float
    vote: str                 # CALL | PUT | NO_TRADE
    status: str = "OK"        # OK | ERROR | NOT_AVAILABLE
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "p_put": round(self.p_put, 4),
            "p_neutral": round(self.p_neutral, 4),
            "p_call": round(self.p_call, 4),
            "weight": round(self.weight, 4),
            "vote": self.vote,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class EnsembleResult:
    p_put: float
    p_neutral: float
    p_call: float
    direction: str | None
    direction_probability: float
    directional_probability_normalised: float
    agreement: float
    spread: float
    votes: list[ModelVote] = field(default_factory=list)
    anomaly_flag: bool = False
    anomaly_detail: str | None = None
    temperature: float = 1.0
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def no_trade_votes(self) -> int:
        return sum(1 for v in self.votes if v.vote == "NO_TRADE")

    @property
    def agreeing_models(self) -> list[str]:
        if not self.direction:
            return []
        return [v.model for v in self.votes if v.vote == self.direction and v.status == "OK"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "p_put": round(self.p_put, 4),
            "p_neutral": round(self.p_neutral, 4),
            "p_call": round(self.p_call, 4),
            "direction": self.direction,
            "direction_probability": round(self.direction_probability, 4),
            "directional_probability_normalised": round(self.directional_probability_normalised, 4),
            "agreement": round(self.agreement, 4),
            "spread": round(self.spread, 4),
            "anomaly_flag": self.anomaly_flag,
            "anomaly_detail": self.anomaly_detail,
            "anomaly_score": (self.provenance or {}).get("anomaly_score"),
            "models_used": (self.provenance or {}).get("models_used", []),
            "models_failed": (self.provenance or {}).get("models_failed", []),
            "model_version": (self.provenance or {}).get("model_version"),
            "temperature": self.temperature,
            "agreeing_models": self.agreeing_models,
            "no_trade_votes": self.no_trade_votes,
            "votes": [v.as_dict() for v in self.votes],
            "provenance": self.provenance,
        }


def apply_temperature(probs: np.ndarray, temperature: float) -> np.ndarray:
    """Probability calibration: p^(1/T) renormalised. T>1 softens overconfidence."""
    if temperature is None or temperature <= 0 or abs(temperature - 1.0) < 1e-9:
        return probs
    p = np.clip(np.asarray(probs, dtype=float), 1e-9, 1.0) ** (1.0 / float(temperature))
    total = p.sum(axis=-1, keepdims=True)
    return p / np.where(total <= 0, 1.0, total)


def aggregate(votes: list[ModelVote], temperature: float = 1.0) -> EnsembleResult:
    """Weighted probability aggregation + agreement/spread diagnostics."""
    usable = [v for v in votes if v.status == "OK"]
    if not usable:
        raise ValueError("No usable model votes to aggregate")

    weights = np.array([v.weight for v in usable], dtype=float)
    if weights.sum() <= 0:
        weights = np.ones_like(weights)
    weights = weights / weights.sum()

    probs = np.array([[v.p_put, v.p_neutral, v.p_call] for v in usable], dtype=float)
    probs = apply_temperature(probs, temperature)
    ensemble = (probs * weights[:, None]).sum(axis=0)
    ensemble = ensemble / ensemble.sum()

    p_put, p_neutral, p_call = (float(ensemble[0]), float(ensemble[1]), float(ensemble[2]))
    direction = "CALL" if p_call >= p_put else "PUT"
    direction_prob = p_call if direction == "CALL" else p_put
    directional_sum = p_call + p_put
    normalised = direction_prob / directional_sum if directional_sum > 0 else 0.5

    agreeing_weight = sum(
        w for v, w in zip(usable, weights) if (v.p_call >= v.p_put and direction == "CALL")
        or (v.p_put > v.p_call and direction == "PUT")
    )
    agreement = float(agreeing_weight)
    winning_probs = np.array([(v.p_call if direction == "CALL" else v.p_put) for v in usable], dtype=float)
    spread = float(np.std(winning_probs, ddof=0))

    return EnsembleResult(
        p_put=p_put,
        p_neutral=p_neutral,
        p_call=p_call,
        direction=direction,
        direction_probability=direction_prob,
        directional_probability_normalised=normalised,
        agreement=agreement,
        spread=spread,
        votes=votes,
        temperature=temperature,
    )


def vote_from_probs(model: str, probs: list[float] | np.ndarray, weight: float,
                    no_trade_margin: float = 0.02) -> ModelVote:
    p_put, p_neutral, p_call = (float(probs[0]), float(probs[1]), float(probs[2]))
    top = max(p_put, p_call)
    if p_neutral + no_trade_margin >= top:
        vote = "NO_TRADE"
    else:
        vote = "CALL" if p_call > p_put else "PUT"
    return ModelVote(model=model, p_put=p_put, p_neutral=p_neutral, p_call=p_call,
                     weight=weight, vote=vote)
