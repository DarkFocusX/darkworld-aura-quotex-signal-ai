"""Signal eligibility policy — the hard production gate (spec §5).

A signal may only be emitted when EVERY requirement below passes:

    1  DATA SOURCE IS REAL       operator market data, not simulated/fixture
    2  DATA IS FRESH             within the freshness budget for the feed type
    3  MODEL READY_MARKET        trained on operator-supplied real market data
    4  ENOUGH CANDLES            the M1 feature block can be computed
    5  MANDATORY GATES PASS      data/feature/model/regime/consistency checks
    6  CONFIDENCE ≥ THRESHOLD    configured minimum (default 85)
    7  AGREEMENT ≥ THRESHOLD     configured minimum model agreement (default 0.60)
    8  NO ANOMALY VETO           IsolationForest did not flag the feature vector
    9  SIGNAL LOCK PASSES        one signal per asset per candle

Anything else is NO TRADE with the first blocking requirement as the reason.

`environment`
-------------
production   the full policy applies — a demo model or a synthetic feed can NEVER
             produce a signal, no matter how high the confidence is.
development  requirement 1 and 3 are downgraded to warnings (explicitly, and the
             emitted signal is labelled DEVELOPMENT + DEMO so it cannot be
             confused with a production signal).

The policy can only ever *remove* signals. It never raises confidence, never
overrides a failed gate and never invents a direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..constants import (
    ENV_DEVELOPMENT,
    GATES,
    MODEL_STATE_READY_MARKET,
    TRADABLE_MIN_QUALITY,
)

REQUIREMENT_ORDER = (
    "DATA SOURCE IS REAL",
    "DATA IS FRESH",
    "MODEL READY_MARKET",
    "ENOUGH CANDLES",
    "MANDATORY GATES PASS",
    "CONFIDENCE ≥ THRESHOLD",
    "AGREEMENT ≥ THRESHOLD",
    "NO ANOMALY VETO",
    "SIGNAL LOCK PASSES",
)


@dataclass
class Requirement:
    name: str
    passed: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    downgraded: bool = False   # would have failed in production, allowed in development

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.name,
            "passed": self.passed,
            "status": "PASS" if self.passed else "FAIL",
            "downgraded": self.downgraded,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class EligibilityDecision:
    allowed: bool
    requirements: list[Requirement]
    blocked_by: list[str] = field(default_factory=list)
    reason: str | None = None
    environment: str = "production"
    label: str = "PRODUCTION"

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": "ALLOW_SIGNAL" if self.allowed else "NO_TRADE",
            "environment": self.environment,
            "label": self.label,
            "reason": self.reason,
            "blocked_by": self.blocked_by,
            "requirements": [r.as_dict() for r in self.requirements],
        }


def _development(settings: Settings) -> bool:
    return str(getattr(settings, "environment", "production")).lower() == ENV_DEVELOPMENT


def evaluate_eligibility(
    *,
    settings: Settings,
    feed: dict[str, Any] | None,
    checks: dict[str, Any] | None,
    confidence: float | None,
    quality: str | None,
    ensemble: dict[str, Any] | None,
    lock: dict[str, Any] | None,
    candles: int | None,
    freshness_seconds: float | None,
    model_status: dict[str, Any],
    data_validation: dict[str, Any] | None = None,
) -> EligibilityDecision:
    """Evaluate every production requirement and return a structured decision."""
    dev = _development(settings)
    feed = feed or {}
    checks = checks or {}
    ensemble = ensemble or {}
    lock = lock or {}
    results: list[Requirement] = []

    # 1 ── data source is real -------------------------------------------------
    source_class = str(feed.get("source_class") or "UNKNOWN").upper()
    simulated = bool(feed.get("is_simulated", feed.get("simulated", False)))
    is_real = source_class == "MARKET" and not simulated
    real_passed = is_real or dev
    results.append(Requirement(
        "DATA SOURCE IS REAL", real_passed,
        detail=(
            f"Feed is {source_class} via {feed.get('provider', 'unknown')}"
            + ("" if is_real else f" — {feed.get('source_label', 'synthetic data')}")
        ),
        evidence={
            "source_class": source_class,
            "provider": feed.get("provider"),
            "source_label": feed.get("source_label"),
            "badge": feed.get("badge"),
            "is_simulated": simulated,
            "is_replay": bool(feed.get("is_replay")),
            "is_real_market_data": is_real,
        },
        downgraded=(not is_real and dev),
    ))

    # 2 ── data is fresh -------------------------------------------------------
    budget = GATES["max_data_lag_seconds"] if feed.get("realtime") else GATES["max_replay_lag_seconds"]
    fresh = freshness_seconds is not None and freshness_seconds <= budget
    results.append(Requirement(
        "DATA IS FRESH", bool(fresh),
        detail=(
            "Feed timestamp missing" if freshness_seconds is None
            else f"Newest candle closed {freshness_seconds:.0f}s ago (budget {budget:.0f}s)"
        ),
        evidence={"freshness_seconds": freshness_seconds, "budget_seconds": budget},
    ))

    # 3 ── model trained on real market data ----------------------------------
    state = str(model_status.get("state", "NOT_READY"))
    market_ready = state == MODEL_STATE_READY_MARKET
    model_passed = market_ready or dev
    results.append(Requirement(
        "MODEL READY_MARKET", model_passed,
        detail=(
            f"Model state {state}"
            + (f" (trained on {model_status.get('data_source', 'unknown')} data)"
               if state != MODEL_STATE_READY_MARKET else "")
            + (" — allowed because TRADING_ENV=development" if (not market_ready and dev) else "")
        ),
        evidence={
            "state": state,
            "version": model_status.get("version"),
            "data_source": model_status.get("data_source"),
            "market_ready": model_status.get("market_ready", market_ready),
            "trained_at_utc": model_status.get("trained_at_utc"),
        },
        downgraded=(not market_ready and dev),
    ))

    # 4 ── enough candles ------------------------------------------------------
    candle_count = int(candles or 0)
    enough = candle_count >= GATES["min_candles"]
    results.append(Requirement(
        "ENOUGH CANDLES", enough,
        detail=f"{candle_count} closed candles (minimum {GATES['min_candles']})",
        evidence={"candles": candle_count, "minimum": GATES["min_candles"]},
    ))

    # 5 ── mandatory gates -----------------------------------------------------
    blocking = list(checks.get("blocking") or [])
    gate_passed = bool(checks.get("passed")) and not blocking
    results.append(Requirement(
        "MANDATORY GATES PASS", gate_passed,
        detail="All mandatory checks passed" if gate_passed
        else "Failed: " + (", ".join(blocking) if blocking else "validation suite did not complete"),
        evidence={
            "blocking": blocking,
            "reasons": checks.get("reasons") or [],
            "checks": [
                {"check": c.get("check"), "passed": c.get("passed"), "detail": c.get("detail")}
                for c in (checks.get("checks") or [])
            ],
        },
    ))

    # 6 ── confidence threshold ------------------------------------------------
    threshold = float(settings.min_confidence)
    conf_value = float(confidence) if confidence is not None else None
    conf_passed = conf_value is not None and conf_value >= threshold
    results.append(Requirement(
        "CONFIDENCE ≥ THRESHOLD", conf_passed,
        detail=(
            "Confidence not computed" if conf_value is None
            else f"Model confidence {conf_value:.1f}% (threshold {threshold:.1f}%)"
        ),
        evidence={"confidence": conf_value, "threshold": threshold, "quality": quality,
                  "tradable_min_quality": TRADABLE_MIN_QUALITY},
    ))

    # 7 ── model agreement -----------------------------------------------------
    agreement_threshold = float(getattr(settings, "min_model_agreement", GATES["min_model_agreement"]))
    agreement = ensemble.get("agreement")
    agreement_value = float(agreement) if agreement is not None else None
    agreement_passed = agreement_value is not None and agreement_value >= agreement_threshold
    results.append(Requirement(
        "AGREEMENT ≥ THRESHOLD", agreement_passed,
        detail=(
            "Ensemble agreement unavailable" if agreement_value is None
            else f"Agreement {agreement_value:.2f} (threshold {agreement_threshold:.2f})"
        ),
        evidence={"agreement": agreement_value, "threshold": agreement_threshold,
                  "spread": ensemble.get("spread"), "models_used": ensemble.get("models_used")},
    ))

    # 8 ── anomaly veto --------------------------------------------------------
    anomaly = bool(ensemble.get("anomaly_flag"))
    results.append(Requirement(
        "NO ANOMALY VETO", not anomaly,
        detail="No anomaly detected" if not anomaly
        else "IsolationForest flagged the current feature vector as out-of-distribution",
        evidence={"anomaly_flag": anomaly, "anomaly_score": ensemble.get("anomaly_score")},
    ))

    # 9 ── signal lock ---------------------------------------------------------
    locked = bool(lock.get("locked"))
    results.append(Requirement(
        "SIGNAL LOCK PASSES", not locked,
        detail=(
            f"No signal stored for the {lock.get('candle_ist')} IST candle" if not locked
            else f"A signal already exists for the {lock.get('candle_ist')} IST candle"
        ),
        evidence=dict(lock),
    ))

    blocked = [r for r in results if not r.passed]
    allowed = not blocked
    label = "PRODUCTION" if not dev else "DEVELOPMENT"
    reason = None
    if blocked:
        first = blocked[0]
        reason = f"{first.name}: {first.detail}"
    return EligibilityDecision(
        allowed=allowed,
        requirements=results,
        blocked_by=[r.name for r in blocked],
        reason=reason,
        environment=str(getattr(settings, "environment", "production")),
        label=label,
    )
