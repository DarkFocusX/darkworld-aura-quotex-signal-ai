"""Model-quality phase tests (spec §18 of the research phase).

Covers the new research surface:

    * M1 expiry targets and the next-candle comparison target (§1)
    * multi-horizon labelling 1/2/3/5 (§2)
    * feature causality for the extended feature set (§3)
    * calibration and threshold sweep / validation-only selection (§6, §7)
    * chronological paper replay and price-based outcome resolution (§8)
    * provider adapter interface and data-quality reporting (§10, §11)
    * artifact provenance and model/data compatibility (§3, §17)
    * research index persistence round-trip

Nothing here retrains the production model and nothing here weakens a gate.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.research import feature_lab as FL
from backend.research import lab as LAB
from backend.research import targets as T

MINUTE = timedelta(minutes=1)


# ------------------------------------------------------------------ fixtures
def _ohlc(rows: int = 400, *, seed: int = 7, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
    rng = np.random.default_rng(seed)
    index = pd.date_range(start=start, periods=rows, freq="1min", tz="UTC")
    close = 100.0 + np.cumsum(rng.normal(0, 0.05, rows))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.02, rows))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.02, rows))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": 10.0}, index=index)


# --------------------------------------------------------------- targets (§1)
def test_m1_expiry_target_fills_at_next_open_and_settles_at_horizon_close():
    df = _ohlc(200)
    spec = T.TargetSpec(target=T.TARGET_M1_EXPIRY, horizon=3, neutral_mode="price",
                        neutral_value=0.01)
    frame = T.build_target(df, spec)

    for i in (0, 10, 50, 150):
        assert frame.entry_price.iloc[i] == pytest.approx(df["open"].iloc[i + 1])
        assert frame.expiry_price.iloc[i] == pytest.approx(df["close"].iloc[i + 3])
        move = frame.expiry_price.iloc[i] - frame.entry_price.iloc[i]
        if move > 0.01:
            assert frame.y.iloc[i] == T.CLASS_CALL
        elif move < -0.01:
            assert frame.y.iloc[i] == T.CLASS_PUT
        else:
            assert frame.y.iloc[i] == T.CLASS_NEUTRAL


def test_target_rows_without_a_complete_future_window_are_nan():
    df = _ohlc(120)
    frame = T.build_target(df, T.TargetSpec(horizon=5, neutral_mode="price", neutral_value=0.01))
    assert frame.y.iloc[-5:].isna().all(), "the last horizon rows must not carry a label"
    assert frame.y.iloc[:100].notna().all()


def test_next_candle_target_stays_a_separate_comparable_experiment():
    df = _ohlc(240)
    expiry = T.build_target(df, T.TargetSpec(target=T.TARGET_M1_EXPIRY, horizon=1,
                                             neutral_mode="atr", neutral_value=0.25))
    next_candle = T.build_target(df, T.TargetSpec(target=T.TARGET_NEXT_CANDLE, horizon=1,
                                                  neutral_mode="atr", neutral_value=0.25))
    assert expiry.spec.identifier() != next_candle.spec.identifier()
    assert next_candle.entry_price.iloc[5] == pytest.approx(df["close"].iloc[5])
    assert expiry.entry_price.iloc[5] == pytest.approx(df["open"].iloc[6])
    dist = next_candle.distribution()
    assert sum(dist.values()) > 100 and len(dist) == 3   # not degenerate


def test_multi_horizon_labels_are_built_for_every_requested_horizon():
    df = _ohlc(300)
    frames = T.build_target_set(df, horizons=(1, 2, 3, 5), neutral_value=0.25)
    assert len(frames) == 8
    labelled = {}
    for key, frame in frames.items():
        labelled[frame.spec.horizon] = int(frame.y.notna().sum())
        assert frame.spec.horizon in (1, 2, 3, 5)
    assert labelled[1] > labelled[2] > labelled[3] > labelled[5]


def test_realised_outcome_reads_prices_not_predictions():
    df = _ohlc(80)
    frame = T.build_target(df, T.TargetSpec(horizon=1, neutral_mode="price", neutral_value=0.0))
    up = int(np.argmax((frame.expiry_price - frame.entry_price).fillna(0).to_numpy()))
    direction = "CALL" if frame.expiry_price.iloc[up] > frame.entry_price.iloc[up] else "PUT"
    assert T.realised_outcome(frame, up, direction) == "WIN"
    flipped = "PUT" if direction == "CALL" else "CALL"
    if frame.expiry_price.iloc[up] != frame.entry_price.iloc[up]:
        assert T.realised_outcome(frame, up, flipped) == "LOSS"


# -------------------------------------------------------------- features (§3)
def test_extended_features_are_causal():
    """Features for early rows must not move when future candles change."""
    df = _ohlc(320, seed=3)
    baseline = FL.build_features(df, None, FL.FEATURE_SET_EXTENDED)
    cut = 260
    mutated = df.copy()
    rng = np.random.default_rng(99)
    mutated.iloc[cut:, :] = mutated.iloc[cut:, :] + rng.normal(0, 5.0, (len(df) - cut, 5))
    after = FL.build_features(mutated, None, FL.FEATURE_SET_EXTENDED)

    before_rows = baseline.iloc[:cut].fillna(-999.0).to_numpy()
    after_rows = after.iloc[:cut].fillna(-999.0).to_numpy()
    np.testing.assert_allclose(before_rows, after_rows, rtol=0, atol=1e-12)


def test_extended_feature_set_is_superset_of_base_and_versioned():
    df = _ohlc(240)
    base = FL.build_features(df, None, FL.FEATURE_SET_BASE)
    extended = FL.build_features(df, None, FL.FEATURE_SET_EXTENDED)
    assert extended.shape[1] == base.shape[1] + len(FL.EXTENDED_FEATURE_NAMES)
    assert FL.feature_set_version(FL.FEATURE_SET_BASE) == "m1-features-1.0"
    assert FL.feature_set_version(FL.FEATURE_SET_EXTENDED) == "m1-features-lab-1.0"
    assert "minute_sin" in FL.EXTENDED_FEATURE_NAMES


# --------------------------------------------------- folds / leakage guard (§1)
def test_walk_forward_folds_keep_an_embargo_and_never_shuffle():
    config = LAB.LabConfig(folds=3, min_train_rows=400)
    folds = LAB._folds(3000, config, embargo=5)
    assert folds, "expected at least one fold"
    folds = sorted(folds, key=lambda f: f.test_start)
    previous_test_end = 0
    for fold in folds:
        assert fold.train_start == 0, "training always starts at the oldest row (expanding window)"
        assert fold.embargo >= 5
        assert fold.train_end + fold.embargo <= fold.test_start, "embargo must separate train from test"
        assert fold.test_start >= previous_test_end, "test windows must advance in time, never overlap"
        previous_test_end = fold.test_end


# ---------------------------------------------------- statistics / gates (§6, §7)
def test_wilson_lower_bound_is_conservative():
    assert LAB.wilson_lower_bound(50, 100) < 0.5 < LAB.wilson_lower_bound(70, 100)
    assert LAB.wilson_lower_bound(0, 0) is None


def test_threshold_sweep_reports_every_gate_and_streaks_match():
    rng = np.random.default_rng(5)
    n = 900
    y = rng.choice([0, 1, 2], size=n, p=[0.34, 0.32, 0.34])
    proba = np.full((n, 3), 0.2)
    proba[np.arange(n), y] = 0.6
    marker = rng.random(n)
    proba[marker < 0.15] = np.array([0.05, 0.1, 0.85])   # strong CALL calls
    proba[marker > 0.9] = np.array([0.85, 0.1, 0.05])    # strong PUT calls

    sweep = LAB.threshold_sweep(y, proba)
    assert [row["threshold"] for row in sweep] == list(LAB.THRESHOLD_GRID)
    for row in sweep:
        assert row["signals"] == row["wins"] + row["losses"]
        if row["signals"]:
            assert row["hit_rate"] == pytest.approx(row["wins"] / row["signals"], abs=5e-4)
            assert 0.0 <= row["coverage"] <= 1.0
    # higher gates can never emit more signals than lower gates
    counts = [row["signals"] for row in sweep]
    assert counts == sorted(counts, reverse=True)


def test_threshold_selection_needs_evidence_and_otherwise_says_no_trade():
    confident = [{"threshold": 0.60, "signals": 5, "hit_rate": 0.9, "wilson_lower_bound": 0.6},
                 {"threshold": 0.85, "signals": 300, "hit_rate": 0.62, "wilson_lower_bound": 0.56},
                 {"threshold": 0.90, "signals": 120, "hit_rate": 0.70, "wilson_lower_bound": 0.61}]
    choice = LAB.select_threshold(confident)
    assert choice["threshold"] == 0.85, "smallest qualifying gate wins; 0.60 had too few signals"

    coin_flip = [{"threshold": 0.85, "signals": 400, "hit_rate": 0.51, "wilson_lower_bound": 0.46}]
    none = LAB.select_threshold(coin_flip)
    assert none["threshold"] is None
    assert "no threshold produced" in none["reason"]

    gate = LAB.gate_test(np.array([2, 0, 2]), np.array([[0.1, 0.1, 0.8], [0.8, 0.1, 0.1], [0.1, 0.1, 0.8]]),
                         None)
    assert gate["signals"] == 0 and "NO TRADE" in gate["note"]


def test_calibration_report_counts_add_up():
    rng = np.random.default_rng(11)
    n = 700
    y = rng.choice([0, 1, 2], size=n)
    proba = rng.dirichlet([1, 1, 1], size=n)
    block = LAB.metrics_block(y, proba)
    table = block["calibration"]
    assert sum(bucket["count"] for bucket in table["bins"]) == n
    for bucket in table["bins"]:
        if bucket["count"]:
            assert 0.0 <= bucket["accuracy"] <= 1.0
    assert block["samples"] == n


# --------------------------------------------------------------- replay (§8)
def _meta(rows: int) -> pd.DataFrame:
    times = pd.date_range(start=datetime(2026, 9, 1, tzinfo=timezone.utc), periods=rows, freq="1min")
    return pd.DataFrame({
        "__time": times,
        "__asset": ["USDJPY"] * rows,
        "__entry": np.linspace(100.0, 101.0, rows),
        "__expiry": np.linspace(100.0, 101.0, rows) + 0.5,
    })


def test_replay_is_chronological_and_outcomes_come_from_prices():
    rows = 60
    meta = _meta(rows)
    y = np.full(rows, 2)
    proba = np.zeros((rows, 3))
    proba[:, 2] = 0.9
    proba[:, 0] = 0.05
    proba[:, 1] = 0.05

    replay = LAB.replay_holdout(meta, y, proba,
                                LAB.ReplayConfig(dataset_version="ds-test",
                                                 features_version="feat-test",
                                                 model_version="lab:test", threshold=0.8))
    assert replay["signals"] == rows
    assert replay["wins"] == rows and replay["losses"] == 0
    times = [r["time_utc"] for r in replay["signals_detail"]]
    assert times == sorted(times), "signals must be emitted in chronological order"
    for signal in replay["signals_detail"]:
        assert signal["result"] == "WIN"          # expiry > entry and direction CALL
        assert signal["dataset_version"] == "ds-test"
        assert signal["model_version"] == "lab:test"
        assert signal["entry_price"] and signal["expiry_price"]


def test_replay_with_no_threshold_emits_nothing():
    rows = 25
    replay = LAB.replay_holdout(_meta(rows), np.full(rows, 2), np.full((rows, 3), [0.1, 0.1, 0.8]),
                                LAB.ReplayConfig(dataset_version="d", features_version="f",
                                                 model_version="m", threshold=None))
    assert replay["signals"] == 0 and replay["wins"] == 0
    assert replay["no_trade_rate"] == 1.0


# ------------------------------------------------------- adapters + quality (§10, §11)
def test_provider_adapters_expose_the_contract():
    from backend.data.providers import BrokerFeedProvider, CsvProvider, SimulatedProvider

    for provider in (CsvProvider(), SimulatedProvider(), BrokerFeedProvider()):
        health = provider.health()
        for key in ("provider", "connected", "status", "realtime_capable", "supports_subscribe",
                    "is_simulated"):
            assert key in health, f"{provider.name} health missing {key}"
        assert callable(provider.connect) and callable(provider.disconnect)
        assert callable(provider.latest_candles) and callable(provider.latest_tick)
        assert callable(provider.subscribe)

    assert CsvProvider().health()["supports_subscribe"] is False
    assert SimulatedProvider().health()["is_simulated"] is True
    assert BrokerFeedProvider().health()["status"] in {"NOT_CONFIGURED", "AWAITING_TICK_STREAM"}


def test_data_quality_block_grades_and_reports_gaps():
    from backend.data.validation import data_quality_block, validate_candles
    from tests.helpers import synthetic_market_frame

    frame = synthetic_market_frame("EURUSD", minutes=300)
    report = validate_candles(frame.df, min_candles=120)
    block = data_quality_block(report, rows=len(frame.df))
    assert block["grade"] in {"A", "B", "C", "D", "F"}
    assert block["rows"] == len(frame.df)
    assert block["resolution_minutes"] == pytest.approx(1.0)
    assert block["gaps"] is not None
    assert block["checked"] is True
    assert data_quality_block(None)["grade"] == "UNAVAILABLE"


def test_registry_adapter_health_marks_replay_sources():
    from backend.data.registry import registry

    health = registry.adapter_health()
    assert health["csv"]["status"] in {"REPLAY_ONLY", "UNAVAILABLE"}
    assert health["csv"]["realtime_capable"] is False
    assert health["_summary"]["note"]
    assert health["broker_ws"]["realtime_capable"] is True


# --------------------------------------------- artifact provenance / compatibility (§3, §17)
def test_production_artifact_carries_full_provenance():
    artifact = Path("backend/models/artifacts/v1-market/metadata.json")
    if not artifact.exists():
        pytest.skip("v1-market artifact not present in this workspace")
    meta = json.loads(artifact.read_text(encoding="utf-8"))
    for key in ("data_source", "data_source_class", "row_count", "asset_list", "feature_version",
                "model_version", "training_timestamp", "validation_metrics", "holdout_metrics",
                "feature_names", "temperature"):
        assert key in meta, f"artifact metadata missing {key}"
    assert meta["data_source_class"] == "MARKET"
    assert meta["row_count"] > 1000
    assert meta["asset_list"] == ["USDJPY", "USDCHF"]
    assert meta["feature_version"] == "m1-features-1.0"


def test_model_states_separate_demo_from_market():
    from backend.models.service import ModelService

    market = Path("backend/models/artifacts/v1-market")
    demo = Path("backend/models/artifacts/v2-demo")
    if not (market.exists() and demo.exists()):
        pytest.skip("artifacts not present in this workspace")
    market_state = ModelService(market, auto_load=True).status()
    demo_state = ModelService(demo, auto_load=True).status()
    assert market_state["state"] == "READY_MARKET" and market_state["production_usable"] is True
    assert demo_state["state"] == "READY_DEMO" and demo_state["production_usable"] is False
    assert market_state["state"] != demo_state["state"]


# ------------------------------------------------------- persistence round-trip
def _fake_record(experiment_id: str = "M1_EXPIRY_OUTCOME-h1-base-logistic_regression") -> dict:
    return {
        "experiment_id": experiment_id,
        "target": {"target": T.TARGET_M1_EXPIRY, "horizon_minutes": 1, "reference": "fill"},
        "feature_set": {"feature_set": "base", "feature_version": "m1-features-1.0"},
        "models": ["logistic_regression"],
        "regime": "all",
        "rows": {"total": 1234, "labelled": 1200, "per_asset": {"USDJPY": 1234}},
        "validation_metrics": {"accuracy": 0.4, "macro_f1": 0.39, "roc_auc": 0.55,
                               "brier": 0.6, "directional_precision": 0.5},
        "holdout_metrics": {"accuracy": 0.38, "macro_f1": 0.37, "roc_auc": 0.53,
                            "brier": 0.61, "directional_precision": 0.49},
        "calibration": {"holdout": {"expected_calibration_error": 0.02, "bins": []}},
        "threshold_selection": {"threshold": 0.85, "reason": "test"},
        "selected_threshold": 0.85,
        "selection_reason": "test",
        "holdout_gate_test": {"signals": 10, "hit_rate": 0.6, "wins": 6, "losses": 4},
        "timings_seconds": {"folds": 0.5, "holdout": 0.2},
        "paper_replay": {"signals": 10, "trades": 10, "wins": 6, "losses": 4, "win_rate": 0.6,
                         "no_trade_rate": 0.99, "max_losing_streak": 2},
        "walk_forward_paper": {"steps": [], "totals": {"signals": 0, "wins": 0, "losses": 0,
                                                        "win_rate": None, "max_losing_streak": 0}},
        "_signals": [{"time_utc": "2026-09-01T00:00:00+00:00", "asset": "USDJPY",
                      "direction": "CALL", "confidence": 88.0, "entry_price": 1.1,
                      "expiry_price": 1.1004, "result": "WIN"}],
    }


def test_experiment_index_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(LAB, "EXPERIMENTS_DIR", tmp_path)
    config = LAB.LabConfig(assets=("USDJPY", "USDCHF"), rows_per_asset=500)
    fingerprints = {"dataset_version": "ds-test", "rows": 1000, "assets": ["USDJPY", "USDCHF"],
                    "period_start_utc": "2026-09-01T00:00:00+00:00",
                    "period_end_utc": "2026-09-02T00:00:00+00:00", "skipped": []}
    index = LAB.save_experiments([_fake_record()], config, fingerprints,
                                 run_notes={"runtime_seconds": 1.0, "xgboost": "not installed"})

    assert index["ranking_policy"].startswith("no 'best model' ranking is produced")
    assert index["experiments"][0]["selected_threshold"] == 0.85
    loaded = LAB.load_index()
    assert loaded["available"] is True and loaded["run_id"] == index["run_id"]
    record = LAB.load_experiment("M1_EXPIRY_OUTCOME-h1-base-logistic_regression")
    assert record is not None and record["selected_threshold"] == 0.85
    signals = LAB.load_signals(index["run_id"], "M1_EXPIRY_OUTCOME-h1-base-logistic_regression")
    assert len(signals) == 1 and signals[0]["result"] == "WIN"
    summary = LAB.summarise(loaded)
    assert summary["experiment_count"] == 1
    assert summary["cells_with_a_selected_threshold"] == 1
    assert summary["cells_without_evidence_of_edge"] == 0
    assert summary["paper_wins_total"] == 6 and summary["paper_losses_total"] == 4


def test_evaluate_combo_short_dataset_is_rejected_cleanly():
    tiny = _ohlc(40)
    data = LAB.MarketData(frames={"USDJPY": tiny}, provenance={}, skipped={})
    from backend.features.pipeline import FeatureError

    with pytest.raises((ValueError, FeatureError)):
        LAB.evaluate_combo(data, LAB.LabConfig(assets=("USDJPY",), rows_per_asset=40, folds=2,
                                               min_train_rows=20),
                           T.TargetSpec(horizon=1), FL.FEATURE_SET_BASE, {},
                           families=("logistic_regression",), verbose=False)


def test_partial_checkpoint_round_trip(tmp_path, monkeypatch):
    """An interrupted grid must be resumable without losing completed cells."""
    monkeypatch.setattr(LAB, "PARTIAL_DIR", tmp_path / "partial")
    key = "fingerprint-abc"
    record = _fake_record("M1_EXPIRY_OUTCOME-h1-base-logistic_regression")
    signals = [{"time_utc": "2026-09-01T00:00:00+00:00", "result": "WIN"}]
    LAB._store_partial(key, record, signals)

    resumed = LAB._load_partial(key)
    assert list(resumed) == ["M1_EXPIRY_OUTCOME-h1-base-logistic_regression"]
    payload = resumed["M1_EXPIRY_OUTCOME-h1-base-logistic_regression"]
    assert payload["record"]["selected_threshold"] == 0.85
    assert payload["signals"] == signals

    # a half-written file must be ignored, not crash the resume
    (tmp_path / "partial" / key / "broken.json").write_text("{not json", encoding="utf-8")
    assert len(LAB._load_partial(key)) == 1

    LAB._clear_partial(key)
    assert LAB._load_partial(key) == {}


def test_non_headline_horizons_use_the_core_family(monkeypatch, tmp_path):
    """Every horizon is measured; only the headline horizon runs the full benchmark."""
    config = LAB.LabConfig(horizons=(1, 3), models=("logistic_regression", "random_forest", "lightgbm"),
                           core_models=("lightgbm",), feature_sets=("base",), targets=(
                               T.TARGET_NEXT_CANDLE,))
    grid: list[tuple] = []
    for horizon in config.horizons:
        families_here = config.models if horizon == 1 else config.core_models
        grid.append((horizon, families_here))
    by_horizon = dict(grid)
    assert len(by_horizon[1]) == 3 and by_horizon[3] == ("lightgbm",)
