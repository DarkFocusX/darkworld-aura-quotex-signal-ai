"""Confidence engine behaviour and the six validation gates."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.constants import GATES, TRADABLE_MIN_QUALITY
from backend.data.base import FeedMeta, make_frame
from backend.features.rules import RuleScore
from backend.models.ensemble import EnsembleResult, ModelVote
from backend.signals.confidence import compute_confidence, model_component, quality_for
from backend.validation.checks import (
    confidence_check,
    consistency_check,
    data_check,
    feature_check,
    model_check,
    regime_check,
)

import numpy as np
import pandas as pd


# ------------------------------------------------------------------ helpers
def make_ensemble(p_put=0.18, p_neutral=0.28, p_call=0.54, votes=None) -> EnsembleResult:
    votes = votes or [
        ModelVote("lightgbm", p_put, p_neutral, p_call, 0.5, "CALL"),
        ModelVote("random_forest", p_put + 0.03, p_neutral - 0.02, p_call - 0.01, 0.3, "CALL"),
        ModelVote("logistic_regression", p_put + 0.05, p_neutral, p_call - 0.05, 0.2, "CALL"),
    ]
    direction = "CALL" if p_call >= p_put else "PUT"
    direction_prob = max(p_call, p_put)
    return EnsembleResult(
        p_put=p_put,
        p_neutral=p_neutral,
        p_call=p_call,
        direction=direction,
        direction_probability=direction_prob,
        directional_probability_normalised=direction_prob / (p_put + p_call),
        agreement=1.0,
        spread=0.01,
        votes=votes,
    )


def rule(name: str, score: float, aligned: bool = True, evidence: float = 0.4) -> RuleScore:
    return RuleScore(name, score, aligned, evidence, {})


def base_rules(score: float = 88.0, aligned: bool = True) -> dict:
    return {
        "trend": rule("trend", score, aligned),
        "momentum": rule("momentum", score, aligned),
        "candle": rule("candle", score, aligned),
        "structure": rule("structure", score, aligned),
        "volatility": rule("volatility", score),
        "regime": rule("regime", score, aligned),
    }


# -------------------------------------------------------------- confidence
def test_quality_bands_follow_the_documented_thresholds():
    assert quality_for(95)[0] == "VERY_HIGH"
    assert quality_for(90)[0] == "VERY_HIGH"
    assert quality_for(89.9)[0] == "HIGH"
    assert quality_for(85)[0] == "HIGH"
    assert quality_for(84.9)[0] == "MODERATE"
    assert quality_for(70) == ("NO_TRADE", "NO TRADE")


def test_confident_ensemble_and_strong_rules_produce_tradable_confidence():
    breakdown = compute_confidence(
        ensemble=make_ensemble(0.12, 0.20, 0.68),
        rule_scores=base_rules(92.0),
        freshness=rule("freshness", 100.0),
    )
    assert breakdown.confidence >= TRADABLE_MIN_QUALITY
    assert breakdown.tradable is True
    assert 0 <= breakdown.confidence <= 100


def test_uncertain_ensemble_cannot_reach_the_threshold():
    """A neutral-heavy (coin-flip) ensemble must not become a tradable signal."""
    breakdown = compute_confidence(
        ensemble=make_ensemble(0.24, 0.52, 0.24),
        rule_scores=base_rules(95.0),
        freshness=rule("freshness", 100.0),
    )
    assert breakdown.confidence < TRADABLE_MIN_QUALITY
    assert breakdown.tradable is False
    assert any("model" in note for note in breakdown.notes)


def test_model_component_is_monotonic_in_probability():
    low, _ = model_component(make_ensemble(0.30, 0.40, 0.30))
    mid, _ = model_component(make_ensemble(0.22, 0.34, 0.44))
    high, _ = model_component(make_ensemble(0.10, 0.22, 0.68))
    assert low < mid < high


def test_weak_rules_pull_confidence_below_threshold_even_with_a_good_model():
    breakdown = compute_confidence(
        ensemble=make_ensemble(0.12, 0.20, 0.68),
        rule_scores=base_rules(42.0, aligned=False),
        freshness=rule("freshness", 30.0),
    )
    assert breakdown.confidence < TRADABLE_MIN_QUALITY


def test_component_weights_sum_to_one():
    breakdown = compute_confidence(
        ensemble=make_ensemble(), rule_scores=base_rules(), freshness=rule("freshness", 80.0)
    )
    total = sum(component["weight"] for component in breakdown.components.values())
    assert total == pytest.approx(1.0, abs=1e-6)


# ------------------------------------------------------------------- gates
def synthetic_frame(minutes: int = 200, close_lag_seconds: int = 5, min_candles: int = 50):
    index = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("min"), periods=minutes, freq="1min")
    rng = np.random.default_rng(4)
    close = 1.10 + np.cumsum(rng.normal(0, 1e-4, minutes))
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 1e-5,
            "low": close - 1e-5,
            "close": close,
            "volume": 100.0,
        },
        index=index,
    )
    meta = FeedMeta(
        asset="EURUSD", provider="test", source_label="TEST FEED", mode="REPLAY",
        realtime=False, is_simulated=True, delayed_seconds=0.0,
        latest_candle_close_utc=(datetime.now(tz=timezone.utc)).isoformat(),
        retrieved_at_utc=(datetime.now(tz=timezone.utc)).isoformat(),
    )
    return make_frame("EURUSD", df, meta, min_candles=min_candles)


def test_data_check_rejects_stale_feed():
    frame = synthetic_frame()
    ok = data_check(frame, lag_seconds=20.0, max_lag_seconds=180.0)
    assert ok.passed
    stale = data_check(frame, lag_seconds=900.0, max_lag_seconds=180.0)
    assert not stale.passed
    assert "Stale data" in stale.detail


def test_data_check_rejects_insufficient_candles():
    frame = synthetic_frame(minutes=40, min_candles=10)
    result = data_check(frame, lag_seconds=5.0, max_lag_seconds=180.0)
    assert not result.passed
    assert "Insufficient candles" in result.detail


def test_model_check_rejects_conflicting_models():
    votes = [
        ModelVote("lightgbm", 0.05, 0.10, 0.85, 0.5, "CALL"),
        ModelVote("random_forest", 0.80, 0.10, 0.10, 0.5, "PUT"),
    ]
    ensemble = make_ensemble(0.42, 0.10, 0.48, votes=votes)
    ensemble.agreement = 0.5
    result = model_check(ensemble, {"state": "READY"})
    assert not result.passed
    assert "Conflicting models" in result.detail or "spread" in result.detail


def test_model_check_rejects_when_neutral_is_the_top_class():
    """A coin-flip ensemble must be rejected as uncertainty, not turned into a direction."""
    ensemble = make_ensemble(0.26, 0.48, 0.26)
    result = model_check(ensemble, {"state": "READY"})
    assert not result.passed
    assert "NEUTRAL" in result.detail and "coin-flip" in result.detail


def test_model_check_needs_two_usable_models():
    votes = [ModelVote("lightgbm", 0.1, 0.2, 0.7, 1.0, "CALL")]
    ensemble = make_ensemble(0.1, 0.2, 0.7, votes=votes)
    result = model_check(ensemble, {"state": "READY"})
    assert not result.passed
    assert "at least 2" in result.detail


def test_model_check_rejects_anomaly_flag():
    ensemble = make_ensemble(0.1, 0.2, 0.7)
    ensemble.anomaly_flag = True
    result = model_check(ensemble, {"state": "READY"})
    assert not result.passed
    assert "Anomaly" in result.detail


def test_feature_check_detects_missing_and_non_finite_values():
    from backend.features.pipeline import FEATURE_NAMES

    good = {name: 0.0 for name in FEATURE_NAMES}
    assert feature_check(good, list(FEATURE_NAMES)).passed

    missing = dict(good)
    missing.pop("rsi14")
    assert not feature_check(missing, list(FEATURE_NAMES)).passed

    bad = dict(good)
    bad["atr_pct"] = float("nan")
    assert not feature_check(bad, list(FEATURE_NAMES)).passed


def test_regime_check_rejects_extreme_and_dead_volatility():
    components = {
        "volatility": {"detail": {"zone": "OUT_OF_BOUNDS", "atr_pct": 1.5, "volatility_ratio": 9.0}},
        "regime": {"detail": {"regime": "TRENDING", "adx14": 30, "efficiency_ratio_10": 0.4}},
    }
    result = regime_check(components, {})
    assert not result.passed
    assert "Extreme" in result.detail


def test_regime_check_rejects_choppy_market():
    components = {
        "volatility": {"detail": {"zone": "IDEAL", "atr_pct": 0.02, "volatility_ratio": 1.0}},
        "regime": {"detail": {"regime": "CHOPPY", "adx14": 9.0, "efficiency_ratio_10": 0.04}},
    }
    result = regime_check(components, {})
    assert not result.passed
    assert "Unfavorable market regime" in result.detail


def test_confidence_check_enforces_threshold_and_floor():
    assert confidence_check(90.0, 85.0).passed
    below = confidence_check(80.0, 85.0)
    assert not below.passed and "below threshold" in below.detail
    floor = confidence_check(82.0, 70.0)
    assert not floor.passed and "tradable floor" in floor.detail


def test_consistency_check_requires_rule_alignment():
    aligned_components = {name: {"aligned": True, "evidence": 0.5} for name in ("trend", "momentum", "candle", "structure")}
    assert consistency_check("CALL", aligned_components).passed

    conflicting = dict(aligned_components)
    conflicting["trend"] = {"aligned": False, "evidence": -0.6}
    conflicting["momentum"] = {"aligned": False, "evidence": -0.4}
    result = consistency_check("CALL", conflicting)
    assert not result.passed
    assert "Conflicting indicators" in result.detail


def test_consistency_check_blocks_against_a_wall_of_resistance():
    components = {name: {"aligned": True, "evidence": 0.4} for name in ("trend", "momentum", "candle")}
    components["structure"] = {"aligned": False, "evidence": GATES["min_structure_evidence"] - 0.4}
    result = consistency_check("CALL", components)
    assert not result.passed
    assert "Structure blocks" in result.detail
