"""Time-series validation: no shuffling, embargo respected, holdout untouched."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.validation import metrics as M
from backend.validation import time_series as TS


def test_walk_forward_folds_are_chronological_with_embargo():
    folds = TS.walk_forward_splits(5000, n_splits=4, min_train_rows=500, embargo=2, test_fraction=0.3)
    assert len(folds) >= 2
    previous_test_end = -1
    for fold in folds:
        assert fold.train_start == 0
        assert fold.train_end <= fold.test_start - fold.embargo      # embargo gap respected
        assert fold.test_start >= previous_test_end                  # never reuses past test rows
        assert fold.train_end > fold.train_start
        previous_test_end = fold.test_end


def test_walk_forward_rejects_too_few_samples():
    with pytest.raises(TS.ValidationError):
        TS.walk_forward_splits(300, n_splits=5, min_train_rows=800)


def test_holdout_is_the_last_block_and_not_overlapping():
    train, test = TS.holdout_split(1000, holdout_fraction=0.2, embargo=2)
    assert train.start == 0
    assert train.stop == 800 - 2
    assert test.start == 800
    assert test.stop == 1000
    assert train.stop < test.start


def test_ordered_index_check_rejects_shuffled_or_duplicated_time():
    ordered = pd.date_range("2026-01-01", periods=10, freq="1min", tz="UTC")
    TS.assert_ordered_index(ordered)

    shuffled = ordered.to_series().sample(frac=1, random_state=1).index
    with pytest.raises(TS.ValidationError):
        TS.assert_ordered_index(shuffled)

    duplicated = pd.DatetimeIndex(list(ordered) + [ordered[3]])
    with pytest.raises(TS.ValidationError):
        TS.assert_ordered_index(duplicated)


def test_classification_metrics_are_computed_not_guessed():
    y_true = [0, 0, 1, 2, 2, 2]
    y_pred = [0, 2, 1, 2, 0, 2]
    report = M.classification_metrics(y_true, y_pred)
    assert report["samples"] == 6
    assert report["accuracy"] == pytest.approx(4 / 6, abs=1e-4)
    assert report["confusion_matrix"][2][2] == 2
    assert 0 <= report["macro_f1"] <= 1
    assert report["per_class"]["CALL"]["support"] == 3


def test_roc_auc_and_brier_handle_probabilities():
    rng = np.random.default_rng(0)
    y_true = rng.integers(0, 3, 300)
    proba = rng.dirichlet([2, 2, 2], size=300)
    auc = M.roc_auc_ovr(y_true, proba)
    assert "macro" in auc
    brier = M.brier_score(y_true, proba)
    assert 0 <= brier <= 2


def test_directional_stats_count_streaks_conservatively():
    y_true = [2, 0, 2, 0, 2]
    y_pred = [2, 0, 2, 0, 0]           # last call is wrong
    stats = M.directional_signal_stats(y_true, y_pred)
    assert stats["signals"] == 5
    assert stats["losses"] == 1
    assert stats["max_losing_streak"] == 1

    always_wrong = M.directional_signal_stats([0, 0, 0], [2, 2, 2])
    assert always_wrong["max_losing_streak"] == 3
    assert always_wrong["win_rate"] == 0.0


def test_neutral_only_predictions_produce_no_signals():
    stats = M.directional_signal_stats([0, 1, 2], [1, 1, 1])
    assert stats["signals"] == 0
    assert stats["win_rate"] is None


def test_calibration_table_and_temperature_fit():
    rng = np.random.default_rng(3)
    y_true = rng.integers(0, 3, 400)
    proba = np.full((400, 3), 1 / 3) + rng.normal(0, 0.01, (400, 3))
    proba = np.clip(proba, 1e-6, 1)
    proba = proba / proba.sum(axis=1, keepdims=True)
    table = M.calibration_table(y_true, proba, bins=5)
    assert table["samples"] == 400
    assert len(table["bins"]) == 5
    temperature = M.fit_temperature(y_true, proba)
    assert 0.5 <= temperature <= 3.0
