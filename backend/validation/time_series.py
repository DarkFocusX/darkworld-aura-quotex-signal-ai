"""Time-series validation utilities.

Rules enforced here:
  * chronological order only — no random shuffling across time
  * an embargo (gap) of at least `horizon` candles between train and test so the
    forward-looking label of the last training row cannot peek into the test set
  * an untouched final holdout block
  * an automated leakage probe
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

import numpy as np
import pandas as pd


class ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: int
    train_end: int      # exclusive
    test_start: int
    test_end: int       # exclusive
    embargo: int

    def describe(self) -> dict:
        return {
            "fold": self.index,
            "train_rows": self.train_end - self.train_start,
            "test_rows": self.test_end - self.test_start,
            "embargo_rows": self.embargo,
            "train_range": [self.train_start, self.train_end - 1],
            "test_range": [self.test_start, self.test_end - 1],
        }


def assert_ordered_index(index: Iterable) -> None:
    idx = pd.Index(index)
    if not idx.is_monotonic_increasing:
        raise ValidationError("Time index must be sorted ascending — shuffling time-series data causes leakage")
    if idx.has_duplicates:
        raise ValidationError("Time index contains duplicate timestamps")


def walk_forward_splits(
    n_samples: int,
    n_splits: int = 5,
    min_train_rows: int = 400,
    embargo: int = 1,
    test_fraction: float = 0.2,
) -> list[Fold]:
    """Expanding-window walk-forward folds (train past → test future)."""
    if n_samples < min_train_rows + 50:
        raise ValidationError(
            f"Not enough samples for walk-forward validation: {n_samples} < {min_train_rows + 50}"
        )
    test_size = max(30, int(n_samples * test_fraction / max(1, n_splits)))
    folds: list[Fold] = []
    first_test_start = n_samples - n_splits * test_size
    if first_test_start <= min_train_rows:
        first_test_start = min_train_rows
        test_size = max(20, (n_samples - first_test_start) // n_splits)
    cursor = first_test_start
    fold_id = 0
    while cursor + test_size <= n_samples and fold_id < n_splits:
        train_end = cursor - embargo
        test_start = cursor
        test_end = min(n_samples, cursor + test_size)
        if train_end - 0 >= min_train_rows:
            folds.append(Fold(fold_id, 0, train_end, test_start, test_end, embargo))
            fold_id += 1
        cursor = test_end
    if not folds:
        raise ValidationError("Could not build any walk-forward fold with the requested parameters")
    return folds


def holdout_split(n_samples: int, holdout_fraction: float = 0.2, embargo: int = 1) -> tuple[slice, slice]:
    """Final untouched holdout: last `holdout_fraction` of the series."""
    cut = int(n_samples * (1.0 - holdout_fraction))
    if cut <= 0 or cut >= n_samples:
        raise ValidationError("Invalid holdout split for this sample count")
    train = slice(0, max(1, cut - embargo))
    test = slice(cut, n_samples)
    return train, test


def expanding_slices(n_samples: int, n_splits: int = 4, min_train_fraction: float = 0.4,
                     embargo: int = 1) -> Iterator[tuple[slice, slice]]:
    start = int(n_samples * min_train_fraction)
    step = max(1, (n_samples - start) // n_splits)
    cursor = start
    while cursor < n_samples:
        end = min(n_samples, cursor + step)
        yield slice(0, max(1, cursor - embargo)), slice(cursor, end)
        cursor = end


def leakage_probe(x: pd.DataFrame, y: pd.Series, threshold: float = 0.985) -> dict:
    """Flag features whose contemporaneous correlation with the label is so high
    that they almost certainly encode future information."""
    aligned = pd.concat([x, y.rename("__y")], axis=1).dropna()
    if len(aligned) < 50:
        return {"checked": 0, "suspicious": [], "ok": True, "note": "too few rows for a leak probe"}
    target = aligned["__y"].to_numpy(dtype=float)
    suspicious: list[dict] = []
    for column in x.columns:
        values = aligned[column].to_numpy(dtype=float)
        if np.std(values) == 0:
            continue
        corr = float(np.corrcoef(values, target)[0, 1])
        if abs(corr) >= threshold:
            suspicious.append({"feature": column, "correlation": round(corr, 4)})
    return {
        "checked": int(x.shape[1]),
        "suspicious": suspicious,
        "ok": not suspicious,
        "note": "no feature nearly determines the label" if not suspicious else "possible look-ahead leakage",
    }
