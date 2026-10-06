"""Feature engineering: determinism, causality (no look-ahead) and label rules."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.data.registry import registry
from backend.features.pipeline import (
    FEATURE_NAMES,
    FeatureError,
    build_feature_frame,
    label_frame,
    latest_features,
)
from backend.validation.time_series import leakage_probe


@pytest.fixture(scope="module")
def frame():
    return registry.get_frame("EURUSD", limit=400, data_source_mode="simulated", use_cache=False)


def test_feature_block_is_complete_and_finite(frame):
    snapshot = latest_features(frame.df)
    assert set(snapshot.values) == set(FEATURE_NAMES)
    assert snapshot.missing == []
    assert all(np.isfinite(v) for v in snapshot.values.values())
    assert snapshot.price == pytest.approx(float(frame.df["close"].iloc[-1]))


def test_features_are_deterministic(frame):
    first = build_feature_frame(frame.df).iloc[-1].to_numpy()
    second = build_feature_frame(frame.df.copy()).iloc[-1].to_numpy()
    assert np.allclose(first, second, equal_nan=True)


def test_no_lookahead_features_ignore_future_rows(frame):
    """Rewriting the tail of the series must not change earlier feature rows."""
    mutated = frame.df.copy()
    mutated.iloc[-6:] = mutated.iloc[-6:] * 1.35
    original_tail = build_feature_frame(frame.df).iloc[:-6]
    mutated_tail = build_feature_frame(mutated).iloc[:-6]
    pd.testing.assert_frame_equal(original_tail, mutated_tail, check_like=True, rtol=1e-9, atol=1e-12)


def test_label_is_forward_looking_and_last_row_is_unlabelled(frame):
    labels = label_frame(frame.df, horizon=1, neutral_atr_fraction=0.25)
    assert pd.isna(labels["y"].iloc[-1])                      # no future candle → no label
    assert set(labels["y"].dropna().unique()) <= {0.0, 1.0, 2.0}
    # verify one label by hand
    row = 100
    move = float(frame.df["close"].iloc[row + 1] - frame.df["close"].iloc[row])
    threshold = float(labels["threshold"].iloc[row])
    expected = 2.0 if move > threshold else 0.0 if move < -threshold else 1.0
    assert float(labels["y"].iloc[row]) == expected


def test_leakage_probe_flags_a_planted_leak(frame):
    features = build_feature_frame(frame.df).replace([np.inf, -np.inf], np.nan).dropna()
    labels = label_frame(frame.df, horizon=1, neutral_atr_fraction=0.25)["y"].dropna()
    common = features.index.intersection(labels.index)
    aligned_features = features.loc[common]
    labels = labels.loc[common]
    assert leakage_probe(aligned_features, labels)["ok"] is True

    cheating = aligned_features.copy()
    cheating["planted_future"] = labels.values
    report = leakage_probe(cheating, labels)
    assert report["ok"] is False
    assert report["suspicious"][0]["feature"] == "planted_future"


def test_feature_engine_raises_on_insufficient_candles():
    tiny = registry.get_frame("EURUSD", limit=90, data_source_mode="simulated", use_cache=False).df.tail(20)
    with pytest.raises(FeatureError):
        build_feature_frame(tiny)
