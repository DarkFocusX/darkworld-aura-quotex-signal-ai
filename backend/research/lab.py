"""Model-quality research lab (spec §1–§9).

What this does, honestly and reproducibly:

  * loads ONLY real operator data (the provider chain decides what is real; a
    dataset that fails validation is skipped with its reason),
  * builds two targets — NEXT_CANDLE_DIRECTION (the historical one) and
    M1_EXPIRY_OUTCOME (the one paper trading actually measures),
  * evaluates them across horizons 1 / 2 / 3 / 5 minutes, two feature sets and
    three model families on **identical** time-series folds,
  * measures calibration explicitly (probability buckets with sample counts),
  * sweeps the confidence gate 60…95 on the *validation* folds only, selects a
    threshold there under a documented rule, and then applies it exactly once to
    the untouched holdout,
  * replays the holdout chronologically as a paper simulation, recording every
    signal (timestamp, asset, direction, confidence, entry/expiry price, result,
    model/dataset/feature version) — outcomes read from prices, never inferred
    from the prediction,
  * runs a walk-forward paper-trading loop (train → validate → test → move
    forward → retrain → test again) with no future data in training.

Nothing in here can emit a signal: it only measures. The runtime engine decides
whether a signal is allowed, and its gates are unchanged.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from ..constants import ASSET_MAP, canonical_symbol
from ..data.base import MarketDataUnavailable
from ..data.registry import registry
from ..validation import metrics as M
from ..validation.time_series import Fold, walk_forward_splits
from . import feature_lab as FL
from . import targets as T
from .train import RANDOM_SEED

EXPERIMENTS_DIR = Path(__file__).resolve().parent / "experiments"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"

MODEL_LOGREG = "logistic_regression"
MODEL_RF = "random_forest"
MODEL_LGBM = "lightgbm"
MODEL_ENSEMBLE = "ensemble_mean"
MODEL_STACKED = "ensemble_stacked"
MODEL_NAMES = (MODEL_LOGREG, MODEL_RF, MODEL_LGBM)

THRESHOLD_GRID = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
MIN_SIGNALS_FOR_SELECTION = 100
HIT_RATE_FLOOR = 0.50          # a directional call must beat a coin flip...
MIN_EDGE_OVER_BREAK_EVEN = 0.0  # ...and the Wilson lower bound must clear it


# --------------------------------------------------------------------- config
@dataclass
class LabConfig:
    assets: tuple[str, ...] = ("USDJPY", "USDCHF")
    rows_per_asset: int = 40_000
    source: str = "csv"
    horizons: tuple[int, ...] = (1, 2, 3, 5)
    neutral_value: float = 0.25
    feature_sets: tuple[str, ...] = (FL.FEATURE_SET_BASE, FL.FEATURE_SET_EXTENDED)
    targets: tuple[str, ...] = (T.TARGET_NEXT_CANDLE, T.TARGET_M1_EXPIRY)
    models: tuple[str, ...] = MODEL_NAMES
    # Non-headline horizons use this smaller family list: every horizon must still
    # be measured, but the full benchmark (all families + ensemble) runs on the
    # headline horizon so the grid stays affordable on a 2-core box.
    core_models: tuple[str, ...] = (MODEL_LGBM,)
    folds: int = 3
    holdout_fraction: float = 0.2
    seed: int = RANDOM_SEED
    min_train_rows: int = 6_000
    calibration_bins: int = 10
    store_signals: int = 20_000

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "model_hyperparameters": MODEL_HYPERPARAMS,
                "threshold_grid": list(THRESHOLD_GRID),
                "min_signals_for_selection": MIN_SIGNALS_FOR_SELECTION,
                "selection_rule": (
                    "validation folds only: smallest threshold with >= "
                    f"{MIN_SIGNALS_FOR_SELECTION} signals and a Wilson 95% lower bound on the hit rate "
                    f"above {HIT_RATE_FLOOR + MIN_EDGE_OVER_BREAK_EVEN:.2f}; if no threshold qualifies the "
                    "system must return NO TRADE"
                )}


@dataclass
class MarketData:
    """Feature matrices + labels for one config, built once and reused."""

    frames: dict[str, pd.DataFrame]              # asset → validated candles
    provenance: dict[str, dict[str, Any]]        # asset → provenance/validation record
    skipped: dict[str, str]                      # asset → why it was skipped
    _cache: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = field(default_factory=dict)

    def matrix(self, feature_set: str, spec: T.TargetSpec) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(X, meta) where meta carries y / entry / expiry / time / asset for the built target."""
        key = f"{feature_set}|{spec.identifier()}"
        if key in self._cache:
            cached = self._cache[key]
            return cached[0], cached[1]

        blocks: list[pd.DataFrame] = []
        for symbol, df in self.frames.items():
            features = FL.build_features(df, ASSET_MAP.get(symbol), feature_set)
            target = T.build_target(df, spec)
            block = features.copy()
            block["__y"] = target.y
            block["__entry"] = target.entry_price
            block["__expiry"] = target.expiry_price
            block["__move"] = target.move
            block["__threshold"] = target.threshold
            block["__time"] = df.index
            block["__asset"] = symbol
            block = block.replace([np.inf, -np.inf], np.nan).dropna()
            if len(block):
                blocks.append(block)

        if not blocks:
            raise ValueError("no labelled rows for this configuration")

        combined = pd.concat(blocks).sort_values("__time")
        X = combined[[c for c in FL.feature_names(feature_set)]].astype(np.float64)
        meta = combined[["__y", "__entry", "__expiry", "__move", "__threshold", "__time", "__asset"]]
        self._cache[key] = (X, meta)
        return X, meta


# --------------------------------------------------------------------- loading
def load_market_data(config: LabConfig, *, verbose: bool = True) -> MarketData:
    """Load validated real data for each asset; skip (loudly) anything unusable."""
    frames: dict[str, pd.DataFrame] = {}
    provenance: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    mode = {"auto": "auto", "simulated": "simulated", "csv": "csv",
            "real_only": "real_only", "live": "live_only"}.get(config.source, "csv")

    for raw in config.assets:
        symbol = canonical_symbol(raw)
        if symbol is None:
            skipped[raw] = "unknown symbol"
            continue
        try:
            frame = registry.get_frame(symbol, limit=config.rows_per_asset,
                                       data_source_mode=mode, use_cache=False)
        except MarketDataUnavailable as exc:
            skipped[symbol] = f"{exc.reason} ({exc.provider})"
            if verbose:
                print(f"  ! {symbol}: {exc.reason} — skipped")
            continue
        if not frame.meta.is_market_data:
            skipped[symbol] = f"feed is {frame.meta.source_class} via {frame.meta.provider}, not market data"
            if verbose:
                print(f"  ! {symbol}: {skipped[symbol]} — skipped")
            continue
        if len(frame.df) < config.min_train_rows // 2:
            skipped[symbol] = f"only {len(frame.df)} candles"
            if verbose:
                print(f"  ! {symbol}: {skipped[symbol]} — skipped")
            continue
        frames[symbol] = frame.df
        provenance[symbol] = {
            **frame.meta.as_dict(),
            "rows": int(len(frame.df)),
            "period_start_utc": frame.df.index[0].isoformat(),
            "period_end_utc": frame.df.index[-1].isoformat(),
        }
        if verbose:
            print(f"  · {symbol}: {len(frame.df):,} rows "
                  f"({frame.df.index[0]:%Y-%m-%d %H:%M} → {frame.df.index[-1]:%Y-%m-%d %H:%M} UTC, "
                  f"{frame.meta.badge})")
    if not frames:
        raise SystemExit("No usable real dataset — nothing to research. Import candles first.")
    return MarketData(frames=frames, provenance=provenance, skipped=skipped)


def dataset_fingerprint(data: MarketData) -> dict[str, Any]:
    """Immutable description of the data an experiment ran on (spec §8 'dataset version')."""
    parts = []
    rows = 0
    for symbol, frame in sorted(data.frames.items()):
        rows += len(frame)
        parts.append(f"{symbol}:{len(frame)}:{frame.index[0].isoformat()}:{frame.index[-1].isoformat()}")
    digest = hashlib_sha(parts)
    return {
        "dataset_version": f"ds-{digest[:12]}",
        "assets": sorted(data.frames),
        "rows": rows,
        "period_start_utc": min(f.index[0] for f in data.frames.values()).isoformat(),
        "period_end_utc": max(f.index[-1] for f in data.frames.values()).isoformat(),
        "skipped": data.skipped,
    }


def hashlib_sha(parts: Iterable[str]) -> str:
    import hashlib

    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- models
# Hyperparameters are recorded with the run so a reader can reproduce a cell.
# The research RF is deliberately lighter than the production trainer's: the grid
# refits every family ~10x per cell (folds + holdout + walk-forward), so tree
# count is traded off against wall-clock, never against fold discipline.
MODEL_HYPERPARAMS: dict[str, dict[str, Any]] = {
    MODEL_LOGREG: {"C": 0.5, "max_iter": 600, "class_weight": "balanced", "scaled": True},
    MODEL_RF: {"n_estimators": 80, "max_depth": 6, "min_samples_leaf": 40,
               "class_weight": "balanced", "n_jobs": 2},
    MODEL_LGBM: {"n_estimators": 200, "learning_rate": 0.05, "num_leaves": 31, "subsample": 0.85,
                 "colsample_bytree": 0.85, "min_child_samples": 40, "reg_lambda": 1.0,
                 "class_weight": "balanced", "n_jobs": 2},
    MODEL_ENSEMBLE: {"combination": "mean of family probabilities", "families": list(MODEL_NAMES)},
}


def make_model(family: str, seed: int = RANDOM_SEED):
    if family == MODEL_LOGREG:
        from sklearn.linear_model import LogisticRegression

        return LogisticRegression(C=0.5, max_iter=600, class_weight="balanced",
                                  random_state=seed, n_jobs=1)
    if family == MODEL_RF:
        from sklearn.ensemble import RandomForestClassifier

        return RandomForestClassifier(n_estimators=80, max_depth=6, min_samples_leaf=40,
                                      class_weight="balanced", random_state=seed, n_jobs=2)
    if family == MODEL_LGBM:
        from lightgbm import LGBMClassifier

        return LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=31, subsample=0.85,
                              subsample_freq=1, colsample_bytree=0.85, min_child_samples=40,
                              reg_lambda=1.0, class_weight="balanced", random_state=seed,
                              n_jobs=2, verbose=-1)
    raise ValueError(f"unknown model family {family!r}")


def _needs_scaling(family: str) -> bool:
    return family in {MODEL_LOGREG}


def fit_predict(family: str, x_train: np.ndarray, y_train: np.ndarray,
                x_test: np.ndarray, seed: int = RANDOM_SEED) -> np.ndarray:
    """Fit one model family and return class probabilities for x_test."""
    model = make_model(family, seed)
    if _needs_scaling(family):
        from sklearn.preprocessing import StandardScaler

        scaler = StandardScaler().fit(x_train)
        x_train, x_test = scaler.transform(x_train), scaler.transform(x_test)
    model.fit(x_train, y_train)
    proba = model.predict_proba(x_test)
    return align_proba(proba, model.classes_)


def align_proba(proba: np.ndarray, classes: Sequence[int]) -> np.ndarray:
    """Guarantee a (n, 3) matrix in PUT/NEUTRAL/CALL order even if a fold misses a class."""
    out = np.zeros((proba.shape[0], 3), dtype=float)
    for column, klass in enumerate(classes):
        out[:, int(klass)] = proba[:, column]
    row_sums = out.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    return out / row_sums


def ensemble_proba(stacks: Sequence[np.ndarray]) -> np.ndarray:
    return np.mean(np.stack(stacks, axis=0), axis=0)


# ------------------------------------------------------------------ statistics
def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float | None:
    """95 % Wilson lower bound — the honest way to ask 'is this better than a coin flip?'"""
    if total <= 0:
        return None
    phat = successes / total
    denominator = 1.0 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denominator)


def directional_view(proba: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(direction_index, confidence, is_directional) for a probability matrix."""
    call, put = proba[:, T.CLASS_CALL], proba[:, T.CLASS_PUT]
    direction = np.where(call >= put, T.CLASS_CALL, T.CLASS_PUT)
    confidence = np.maximum(call, put)
    is_directional = confidence > proba[:, T.CLASS_NEUTRAL]
    return direction, confidence, is_directional


def max_losing_streak(results: Sequence[str]) -> int:
    worst = current = 0
    for result in results:
        if result == "LOSS":
            current += 1
            worst = max(worst, current)
        elif result == "WIN":
            current = 0
    return worst


def metrics_block(y_true: np.ndarray, proba: np.ndarray, bins: int = 10) -> dict[str, Any]:
    predicted = proba.argmax(axis=1)
    direction, confidence, is_directional = directional_view(proba)
    directional_pred = np.where(direction == T.CLASS_CALL, 1, -1)
    correct_direction = ((predicted == y_true) & (y_true != T.CLASS_NEUTRAL))
    tick = np.where(y_true == T.CLASS_CALL, 1, np.where(y_true == T.CLASS_PUT, -1, 0))
    hit = (np.where(is_directional, directional_pred, 0) == tick) & is_directional & (tick != 0)
    return {
        **M.classification_metrics(y_true, predicted),
        "roc_auc": M.roc_auc_ovr(y_true, proba),
        "brier": M.brier_score(y_true, proba),
        "calibration": M.calibration_table(y_true, proba, bins=bins),
        "directional_calls": int(is_directional.sum()),
        "directional_precision": round(float(hit.sum() / max(1, is_directional.sum())), 4),
        "directional_correct": int((correct_direction & (tick != 0)).sum()),
        "samples": int(len(y_true)),
    }


def threshold_sweep(y_true: np.ndarray, proba: np.ndarray,
                    grid: Sequence[float] = THRESHOLD_GRID) -> list[dict[str, Any]]:
    """Every requested gate threshold, evaluated on the same predictions."""
    direction, confidence, is_directional = directional_view(proba)
    tick = np.where(y_true == T.CLASS_CALL, 1, np.where(y_true == T.CLASS_PUT, -1, 0))
    rows: list[dict[str, Any]] = []
    for threshold in grid:
        mask = is_directional & (confidence >= threshold) & (tick != 0)
        signals = int(mask.sum())
        wins = int(((direction == T.CLASS_CALL) & mask & (tick > 0)).sum()
                   + ((direction == T.CLASS_PUT) & mask & (tick < 0)).sum())
        losses = signals - wins
        win_rate = (wins / signals) if signals else None
        results = []
        for position in np.flatnonzero(mask):
            results.append("WIN" if (direction[position] == T.CLASS_CALL) == (tick[position] > 0) else "LOSS")
        rows.append({
            "threshold": round(float(threshold), 2),
            "signals": signals,
            "coverage": round(signals / len(y_true), 5) if len(y_true) else None,
            "wins": wins,
            "losses": losses,
            "hit_rate": round(win_rate, 4) if win_rate is not None else None,
            "precision": round(win_rate, 4) if win_rate is not None else None,
            "loss_rate": round(1 - win_rate, 4) if win_rate is not None else None,
            "max_losing_streak": max_losing_streak(results),
            "wilson_lower_bound": (round(wilson_lower_bound(wins, signals), 4)
                                   if win_rate is not None else None),
        })
    return rows


def select_threshold(sweep: Sequence[dict[str, Any]], *,
                     min_signals: int = MIN_SIGNALS_FOR_SELECTION) -> dict[str, Any]:
    """Pick a threshold using VALIDATION data only, under a stated rule.

    Rule: the smallest threshold that (a) produced at least `min_signals` signals
    in validation and (b) whose Wilson 95 % lower bound on the hit rate is above
    0.50. If nothing qualifies, no threshold is selected and the correct output
    of the system stays NO TRADE.
    """
    for row in sorted(sweep, key=lambda r: r["threshold"]):
        bound = row.get("wilson_lower_bound")
        if row["signals"] >= min_signals and bound is not None and bound > HIT_RATE_FLOOR + MIN_EDGE_OVER_BREAK_EVEN:
            return {
                "threshold": row["threshold"],
                "selected_on": "validation_only",
                "reason": (f"smallest gate with >= {min_signals} validation signals and a Wilson 95% lower "
                           f"bound on the hit rate of {bound:.3f} > {HIT_RATE_FLOOR:.2f}"),
                "validation_signals": row["signals"],
                "validation_hit_rate": row["hit_rate"],
                "wilson_lower_bound": bound,
            }
    return {
        "threshold": None,
        "selected_on": "validation_only",
        "reason": ("no threshold produced enough validation signals with a hit rate whose Wilson 95% lower "
                   "bound cleared 0.50 — the evidence does not support emitting signals"),
        "validation_signals": None,
        "validation_hit_rate": None,
        "wilson_lower_bound": None,
    }


def gate_test(y_true: np.ndarray, proba: np.ndarray, threshold: float | None) -> dict[str, Any]:
    """Apply a threshold ONCE (holdout / unseen block) and report what would happen."""
    if threshold is None:
        return {"threshold": None, "signals": 0, "wins": 0, "losses": 0, "hit_rate": None,
                "loss_rate": None, "coverage": 0.0, "max_losing_streak": 0,
                "wilson_lower_bound": None, "note": "no threshold was selected on validation — NO TRADE"}
    table = threshold_sweep(y_true, proba, grid=(threshold,))
    return table[0]


# -------------------------------------------------------------------- replay
@dataclass
class ReplayConfig:
    dataset_version: str
    features_version: str
    model_version: str
    threshold: float | None


def replay_holdout(meta: pd.DataFrame, y_true: np.ndarray, proba: np.ndarray,
                   config: ReplayConfig) -> dict[str, Any]:
    """Chronological paper simulation over unseen rows.

    Signals are emitted in time order at the selected gate; each result is read
    from the entry/expiry prices that the dataset actually contains.
    """
    direction, confidence, is_directional = directional_view(proba)
    rows: list[dict[str, Any]] = []
    times = list(meta["__time"])
    assets = list(meta["__asset"])
    entry_prices = list(meta["__entry"])
    expiry_prices = list(meta["__expiry"])

    no_trade = 0
    for i in range(len(y_true)):
        fire = bool(is_directional[i]) and config.threshold is not None and confidence[i] >= config.threshold
        if not fire:
            no_trade += 1
            continue
        call = direction[i] == T.CLASS_CALL
        entry, expiry = float(entry_prices[i]), float(expiry_prices[i])
        if expiry == entry:
            result = "VOID"
        elif (expiry > entry) == call:
            result = "WIN"
        else:
            result = "LOSS"
        rows.append({
            "time_utc": pd.Timestamp(times[i]).isoformat(),
            "asset": assets[i],
            "direction": "CALL" if call else "PUT",
            "confidence": round(float(confidence[i]) * 100.0, 2),
            "entry_price": round(entry, 6),
            "expiry_price": round(expiry, 6),
            "result": result,
            "model_version": config.model_version,
            "dataset_version": config.dataset_version,
            "features_version": config.features_version,
        })

    decided = [r for r in rows if r["result"] != "VOID"]
    wins = sum(1 for r in decided if r["result"] == "WIN")
    losses = sum(1 for r in decided if r["result"] == "LOSS")
    total = len(y_true)
    return {
        "threshold": config.threshold,
        "model_version": config.model_version,
        "dataset_version": config.dataset_version,
        "features_version": config.features_version,
        "total_rows": total,
        "signals": len(rows),
        "trades": len(decided),
        "wins": wins,
        "losses": losses,
        "voids": len(rows) - len(decided),
        "win_rate": round(wins / len(decided), 4) if decided else None,
        "no_trade_rate": round(no_trade / total, 4) if total else None,
        "max_losing_streak": max_losing_streak([r["result"] for r in rows]),
        "wilson_lower_bound": wilson_lower_bound(wins, len(decided)),
        "first_signal_utc": rows[0]["time_utc"] if rows else None,
        "last_signal_utc": rows[-1]["time_utc"] if rows else None,
        "signals_detail": rows,
        "outcomes_from": "actual entry/expiry prices in the dataset — never inferred from the prediction",
    }


def walk_forward_paper(x_all: np.ndarray, y_all: np.ndarray, meta: pd.DataFrame,
                       family: str, threshold: float | None, *,
                       initial_train: int, block: int, embargo: int,
                       dataset_version: str, features_version: str,
                       model_version: str, seed: int = RANDOM_SEED) -> dict[str, Any]:
    """TRAIN → VALIDATE → TEST → MOVE FORWARD → RETRAIN → TEST AGAIN (spec §9)."""
    steps: list[dict[str, Any]] = []
    cumulative: list[str] = []
    start = initial_train
    while start + block <= len(y_all):
        train_end = start - embargo
        if train_end < 500:
            break
        proba = fit_predict(family, x_all[:train_end], y_all[:train_end], x_all[start:start + block], seed)
        block_meta = meta.iloc[start:start + block]
        replay = replay_holdout(block_meta, y_all[start:start + block], proba,
                                ReplayConfig(dataset_version=dataset_version,
                                             features_version=features_version,
                                             model_version=model_version, threshold=threshold))
        cumulative.extend([r["result"] for r in replay["signals_detail"]])
        steps.append({
            "step": len(steps) + 1,
            "train_rows": int(train_end),
            "test_rows": int(block),
            "test_start_utc": pd.Timestamp(meta["__time"].iloc[start]).isoformat(),
            "test_end_utc": pd.Timestamp(meta["__time"].iloc[start + block - 1]).isoformat(),
            "signals": replay["signals"],
            "wins": replay["wins"],
            "losses": replay["losses"],
            "win_rate": replay["win_rate"],
            "no_trade_rate": replay["no_trade_rate"],
        })
        start += block

    decided = [r for r in cumulative if r != "VOID"]
    wins = sum(1 for r in decided if r == "WIN")
    return {
        "description": "retrain-then-test loop: no future row is ever in a training window",
        "family": family,
        "note": ("the walk-forward paper loop always retrains the fastest comparable family so the grid stays "
                 "affordable; the procedure (retrain → test → move forward) is what is being measured"),
        "steps": steps,
        "initial_train_rows": initial_train,
        "block_rows": block,
        "embargo_rows": embargo,
        "totals": {
            "signals": len(cumulative),
            "trades": len(decided),
            "wins": wins,
            "losses": len(decided) - wins,
            "win_rate": round(wins / len(decided), 4) if decided else None,
            "max_losing_streak": max_losing_streak(cumulative),
        },
    }


# ----------------------------------------------------------------- experiment
def _folds(n_samples: int, config: LabConfig, embargo: int) -> list[Fold]:
    return walk_forward_splits(n_samples, n_splits=config.folds,
                               min_train_rows=max(config.min_train_rows, embargo + 500),
                               embargo=embargo, test_fraction=0.2)


def evaluate_combo(data: MarketData, config: LabConfig, spec: T.TargetSpec,
                   feature_set: str, fingerprints: dict[str, Any],
                   *, families: Sequence[str] | None = None,
                   wf_family: str | None = None,
                   regime: str = "all", row_mask: np.ndarray | None = None,
                   verbose: bool = True) -> dict[str, Any]:
    """One (target, horizon, feature set) cell of the grid, all model families."""
    X, meta = data.matrix(feature_set, spec)
    if row_mask is not None:
        mask = np.asarray(row_mask, dtype=bool)
        X, meta = X.loc[mask], meta.loc[mask]
    y = meta["__y"].to_numpy(int)
    n = len(y)
    embargo = max(1, spec.horizon)
    holdout_start = int(n * (1.0 - config.holdout_fraction))
    folds = _folds(holdout_start, config, embargo)

    families = tuple(families or config.models)
    feature_version = FL.feature_set_version(feature_set)
    model_version = f"lab:{spec.identifier()}|{feature_set}|{'-'.join(families)}"
    x_values = X.to_numpy(dtype=np.float64)

    oof = np.full((holdout_start, 3), np.nan)
    fold_rows: list[dict[str, Any]] = []
    fit_seconds = 0.0
    for fold in folds:
        train_x, train_y = x_values[fold.train_start:fold.train_end], y[fold.train_start:fold.train_end]
        test_x = x_values[fold.test_start:fold.test_end]
        stacks = []
        for family in families:
            start = time.time()
            stacks.append(fit_predict(family, train_x, train_y, test_x, config.seed))
            fit_seconds += time.time() - start
        oof[fold.test_start:fold.test_end] = ensemble_proba(stacks) if len(stacks) > 1 else stacks[0]
        fold_rows.append({**fold.describe(), "test_metrics": metrics_block(
            y[fold.test_start:fold.test_end], oof[fold.test_start:fold.test_end])})

    train_mask = ~np.isnan(oof[:, 0])          # rows covered by walk-forward validation
    oof_valid = oof[train_mask]
    y_valid = y[:holdout_start][train_mask]

    # --- the single holdout evaluation ------------------------------------
    holdout_x, holdout_y = x_values[holdout_start:], y[holdout_start:]
    stacks = []
    holdout_seconds = 0.0
    for family in families:
        start = time.time()
        stacks.append(fit_predict(family, x_values[:holdout_start], y[:holdout_start], holdout_x, config.seed))
        holdout_seconds += time.time() - start
    holdout_proba = ensemble_proba(stacks) if len(stacks) > 1 else stacks[0]

    oof_metrics = metrics_block(y_valid, oof_valid, bins=config.calibration_bins)
    holdout_metrics = metrics_block(holdout_y, holdout_proba, bins=config.calibration_bins)
    sweep = threshold_sweep(y_valid, oof_valid)
    selection = select_threshold(sweep)
    holdout_gate = gate_test(holdout_y, holdout_proba, selection["threshold"])

    temperature = M.fit_temperature(y_valid, oof_valid)

    # Walk-forward paper trading (spec §9) measures the PROCEDURE — retrain, move
    # forward, test again — so the loop retrains the fastest comparable family
    # (LightGBM by default) instead of refitting a forest ten times per cell. The
    # family actually used is recorded in the step report.
    wf_family = wf_family or (MODEL_LGBM if MODEL_LGBM in families else families[0])
    walk_forward = walk_forward_paper(
        x_values, y, meta, wf_family, selection["threshold"],
        initial_train=max(config.min_train_rows, len(x_values) // 4),
        block=max(1_500, len(x_values) // 8), embargo=embargo,
        dataset_version=fingerprints["dataset_version"], features_version=feature_version,
        model_version=model_version, seed=config.seed,
    )

    holdout_meta = meta.iloc[holdout_start:]
    replay = replay_holdout(holdout_meta, holdout_y, holdout_proba,
                            ReplayConfig(dataset_version=fingerprints["dataset_version"],
                                         features_version=feature_version,
                                         model_version=model_version,
                                         threshold=selection["threshold"]))
    all_signals = replay.pop("signals_detail")
    replay["signals_stored"] = min(len(all_signals), config.store_signals)
    replay["signals_preview"] = all_signals[:100]
    replay["signals_truncated"] = len(all_signals) > config.store_signals

    record = {
        "experiment_id": f"{spec.target}-h{spec.horizon}-{feature_set}-{'-'.join(families)}",
        "target": spec.as_dict(),
        "feature_set": FL.describe(feature_set),
        "models": list(families),
        "regime": regime,
        "rows": {"total": n, "walk_forward": int(train_mask.sum()), "holdout": int(len(holdout_y)),
                 "folds": len(folds)},
        "folds": fold_rows,
        "validation_metrics": oof_metrics,
        "holdout_metrics": holdout_metrics,
        "calibration": {
            "temperature": temperature,
            "validation": oof_metrics["calibration"],
            "holdout": holdout_metrics["calibration"],
            "note": ("buckets are the predicted-class confidence; a bucket is only meaningful with enough "
                     "samples — treat counts under ~200 as unproven"),
        },
        "threshold_sweep": sweep,
        "threshold_selection": selection,
        "holdout_gate_test": holdout_gate,
        "paper_replay": replay,
        "walk_forward_paper": walk_forward,
        "dataset": fingerprints,
        "timings_seconds": {"folds": round(fit_seconds, 1), "holdout": round(holdout_seconds, 1)},
    }
    if verbose:
        chosen = selection["threshold"]
        print(f"  · {record['experiment_id']}: oof acc {oof_metrics['accuracy']:.4f} | "
              f"holdout acc {holdout_metrics['accuracy']:.4f} | gate {chosen} | "
              f"holdout signals {holdout_gate['signals']}"
              + (f" hit {holdout_gate['hit_rate']:.3f}" if holdout_gate.get("hit_rate") is not None else ""))
    return {"record": record, "signals": all_signals}


def regime_variants(data: MarketData, config: LabConfig, spec: T.TargetSpec,
                    feature_set: str, fingerprints: dict[str, Any],
                    families: Sequence[str] | None = None,
                    wf_family: str | None = None,
                    done: dict[str, dict[str, Any]] | None = None,
                    partial_key: str | None = None,
                    verbose: bool = False) -> list[dict[str, Any]]:
    """Spec §4: does a regime filter or a per-regime model help out-of-sample?

    Regimes are computed from the *existing* causal features (no new look-ahead):
      trend      |ema9_dist| and |adx14 - 25| — directional market
      range      low efficiency ratio + price near the middle of the range
      high_vol   atr_pct in the top tercile of the training window
      low_vol    atr_pct in the bottom tercile
    A regime filter only ever *removes* rows; a per-regime model is reported
    separately so the comparison is honest.
    """
    families = tuple(families or (MODEL_LGBM,))
    out: list[dict[str, Any]] = []

    frame_X, meta = data.matrix(feature_set, spec)
    adx = frame_X["adx14"].to_numpy()
    eff = frame_X["eff_ratio_20"].to_numpy()
    atr_pct = frame_X["atr_pct"].to_numpy()
    ema_dist = np.abs(frame_X["ema9_dist"].to_numpy())
    upper = np.nanquantile(atr_pct, 0.66)
    lower = np.nanquantile(atr_pct, 0.33)

    # The unfiltered baseline is already the headline extended-feature grid cell,
    # so only genuine filters are studied here.
    masks = {
        "trend": (adx > 25) & (eff > 0.35),
        "range": (adx < 20) & (eff < 0.30),
        "high_vol": atr_pct >= upper,
        "low_vol": atr_pct <= lower,
        "anomaly_clean": ema_dist < np.nanquantile(ema_dist, 0.99),
    }
    done = done or {}
    for name, mask in masks.items():
        if mask.sum() < config.min_train_rows:
            continue
        experiment_id = f"{spec.target}-h{spec.horizon}-{feature_set}-{name}"
        if experiment_id in done:
            if verbose:
                print(f"    · {experiment_id} — skipped (checkpoint)")
            out.append(done[experiment_id]["record"])
            continue
        if verbose:
            print(f"    · {experiment_id}")
        record = evaluate_combo(data, config, spec, feature_set, fingerprints,
                                families=families, wf_family=wf_family,
                                regime=name, row_mask=mask, verbose=False)["record"]
        record["experiment_id"] = experiment_id
        record["regime_filter"] = {
            "regime": name,
            "rows_kept": int(mask.sum()),
            "rows_total": int(len(mask)),
            "rule": {
                "trend": "adx14 > 25 and efficiency_ratio_20 > 0.35",
                "range": "adx14 < 20 and efficiency_ratio_20 < 0.30",
                "high_vol": f"atr_pct >= tercile ({upper:.6g})",
                "low_vol": f"atr_pct <= tercile ({lower:.6g})",
                "anomaly_clean": "|ema9_dist| below the 99th percentile (drop outliers)",
            }[name],
        }
        out.append(record)
    return out


# ------------------------------------------------------------------ reporting
PARTIAL_DIR = EXPERIMENTS_DIR / ".partial"


def _partial_key(fingerprints: dict[str, Any], config: LabConfig) -> str:
    """Everything that changes what a cell measures must be part of this key, so a
    resumed run never mixes cells computed under different policies."""
    return hashlib_sha([fingerprints.get("dataset_version", "?"), config.as_dict()["selection_rule"],
                        ",".join(config.models), ",".join(config.core_models),
                        str(config.folds), str(config.neutral_value),
                        "wf=lgbm-if-present", "rf=80x6-leaf40", "v2"])


def _load_partial(key: str) -> dict[str, dict[str, Any]]:
    """Resume support: completed cells of an interrupted run."""
    directory = PARTIAL_DIR / key
    done: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return done
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue                      # half-written cell from a hard kill
        done[payload["record"]["experiment_id"]] = payload
    return done


def _store_partial(key: str, record: dict[str, Any], signals: list[dict[str, Any]]) -> None:
    directory = PARTIAL_DIR / key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['experiment_id']}.json"
    payload = {"record": _jsonable(record), "signals": _jsonable(signals[: 5_000])}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _clear_partial(key: str) -> None:
    directory = PARTIAL_DIR / key
    if directory.exists():
        for path in directory.glob("*.json"):
            path.unlink()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def save_experiments(records: list[dict[str, Any]], config: LabConfig, fingerprints: dict[str, Any],
                     *, run_notes: dict[str, Any] | None = None) -> dict[str, Any]:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"lab-{stamp}"
    index_rows = []
    for record in records:
        signals = record.pop("_signals", [])
        record = _jsonable(record)
        record.setdefault("run_id", run_id)
        path = EXPERIMENTS_DIR / f"{run_id}_{record['experiment_id']}.json"
        record["paper_replay"]["signals_file"] = None
        if signals:
            signal_path = EXPERIMENTS_DIR / f"{run_id}_{record['experiment_id']}_signals.jsonl"
            stored = signals[: config.store_signals]
            with signal_path.open("w", encoding="utf-8") as handle:
                for row in stored:
                    handle.write(json.dumps(_jsonable(row)) + "\n")
            record["paper_replay"]["signals_file"] = signal_path.name
            record["paper_replay"]["signals_file_rows"] = len(stored)
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
        index_rows.append({
            "experiment_id": record["experiment_id"],
            "run_id": run_id,
            "file": path.name,
            "target": record["target"]["target"],
            "horizon_minutes": record["target"]["horizon_minutes"],
            "reference": record["target"]["reference"],
            "feature_set": record["feature_set"]["feature_set"],
            "features_version": record["feature_set"]["feature_version"],
            "models": record["models"],
            "regime": record.get("regime", "all"),
            "rows": record["rows"],
            "validation": {k: record["validation_metrics"].get(k) for k in
                           ("accuracy", "macro_f1", "roc_auc", "brier", "directional_precision")},
            "holdout": {k: record["holdout_metrics"].get(k) for k in
                        ("accuracy", "macro_f1", "roc_auc", "brier", "directional_precision")},
            "expected_calibration_error": record["calibration"]["holdout"].get("expected_calibration_error"),
            "calibration_holdout": record["calibration"]["holdout"],
            "walk_forward_step_count": len(record["walk_forward_paper"]["steps"]),
            "selected_threshold": record["threshold_selection"]["threshold"],
            "selection_reason": record["threshold_selection"]["reason"],
            "holdout_gate": {k: record["holdout_gate_test"].get(k) for k in
                             ("signals", "wins", "losses", "hit_rate", "loss_rate", "max_losing_streak")},
            "paper_replay": {k: record["paper_replay"].get(k) for k in
                             ("signals", "wins", "losses", "win_rate", "no_trade_rate", "max_losing_streak")},
            "walk_forward_totals": record["walk_forward_paper"]["totals"],
            "timings_seconds": record["timings_seconds"],
        })

    index = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "config": config.as_dict(),
        "dataset": fingerprints,
        "run_notes": run_notes or {},
        "experiments": index_rows,
        "disclaimer": ("These are offline validation measurements on historical data. They are not a "
                       "promise of future performance and are never to be combined with live paper results."),
        "ranking_policy": "no 'best model' ranking is produced — every cell is reported as measured",
    }
    (EXPERIMENTS_DIR / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    (EXPERIMENTS_DIR / "latest.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


def load_index(limit: int | None = None, run_id: str | None = None) -> dict[str, Any]:
    index_path = EXPERIMENTS_DIR / ("index.json" if not run_id else f"{run_id}_index.json")
    if not index_path.exists():
        return {"available": False, "reason": "no research run has been executed yet", "experiments": []}
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["available"] = True
    if limit:
        payload["experiments"] = payload["experiments"][:limit]
    return payload


def load_experiment(experiment_id: str, run_id: str | None = None) -> dict[str, Any] | None:
    pattern = f"*_{experiment_id}.json" if not run_id else f"{run_id}_{experiment_id}.json"
    matches = sorted(EXPERIMENTS_DIR.glob(pattern)) if EXPERIMENTS_DIR.exists() else []
    if not matches:
        return None
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def load_signals(run_id: str, experiment_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Paper-replay signals, chronological — read from the stored JSONL when present."""
    record = load_experiment(experiment_id, run_id)
    if not record:
        return []
    replay = record.get("paper_replay", {})
    filename = replay.get("signals_file")
    if filename:
        path = Path(filename)
        path = path if path.is_absolute() else EXPERIMENTS_DIR / path
        if path.exists():
            rows: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    rows.append(json.loads(line))
                    if len(rows) >= limit:
                        break
            if rows:
                return rows
    return list(replay.get("signals_preview") or [])[:limit]


def summarise(index: dict[str, Any]) -> dict[str, Any]:
    """Aggregate view for the dashboard — deliberately contains no ranking."""
    rows = index.get("experiments", [])
    thresholds = [r["selected_threshold"] for r in rows]
    signals = [r["paper_replay"]["signals"] for r in rows]
    decided = [(r["paper_replay"].get("wins", 0), r["paper_replay"].get("losses", 0)) for r in rows]
    wins = sum(w for w, _ in decided)
    losses = sum(l for _, l in decided)
    return {
        "run_id": index.get("run_id"),
        "generated_at_utc": index.get("generated_at_utc"),
        "dataset": index.get("dataset"),
        "experiment_count": len(rows),
        "cells_with_a_selected_threshold": sum(1 for t in thresholds if t is not None),
        "cells_without_evidence_of_edge": sum(1 for t in thresholds if t is None),
        "paper_signals_total": sum(signals),
        "paper_wins_total": wins,
        "paper_losses_total": losses,
        "paper_win_rate_pooled": round(wins / (wins + losses), 4) if (wins + losses) else None,
        "note": ("pooled totals across experiments are descriptive only — they are not a performance claim "
                 "and are never mixed with live paper-trading results"),
    }


# --------------------------------------------------------------- orchestration
def run_lab(config: LabConfig, *, verbose: bool = True,
            headline_horizon: int = 1,
            regime: bool = True,
            ensemble: bool = True) -> dict[str, Any]:
    """Run the whole research grid and persist it. Returns the index payload."""
    started = time.time()
    if verbose:
        print("Quotex Signal AI — model quality lab")
        print(f"  assets: {list(config.assets)}   source: {config.source}")
    data = load_market_data(config, verbose=verbose)
    fingerprints = dataset_fingerprint(data)
    if verbose:
        print(f"  dataset version: {fingerprints['dataset_version']}  rows: {fingerprints['rows']:,}")
        if data.skipped:
            print(f"  skipped: {data.skipped}")

    records: list[dict[str, Any]] = []
    signals_by_id: dict[str, list[dict[str, Any]]] = {}
    partial_key = _partial_key(fingerprints, config)
    done = _load_partial(partial_key)
    if done and verbose:
        print(f"  resuming: {len(done)} cell(s) already completed in this configuration")

    wf_family = (MODEL_LGBM if MODEL_LGBM in (*config.models, *config.core_models)
                 else (config.core_models or config.models)[0])
    if verbose:
        print(f"  walk-forward paper loop retrains '{wf_family}' in every cell (policy recorded in each step report)")

    grid: list[tuple[T.TargetSpec, str, tuple[str, ...]]] = []
    for target in config.targets:
        for horizon in config.horizons:
            spec = T.TargetSpec(target=target, horizon=horizon, neutral_mode="atr",
                                neutral_value=config.neutral_value)
            families_here = config.models if horizon == headline_horizon else config.core_models
            for feature_set in config.feature_sets:
                for family in families_here:
                    grid.append((spec, feature_set, (family,)))
                if ensemble and horizon == headline_horizon:
                    grid.append((spec, feature_set, tuple(config.models)))

    for position, (spec, feature_set, families) in enumerate(grid, 1):
        label = f"{spec.target} h{spec.horizon} {feature_set} {'+'.join(families)}"
        experiment_id = f"{spec.target}-h{spec.horizon}-{feature_set}-{'-'.join(families)}"
        if experiment_id in done:
            if verbose:
                print(f"[{position}/{len(grid)}] {label} — skipped (checkpoint)")
            payload = done[experiment_id]
            records.append(payload["record"])
            signals_by_id[experiment_id] = payload.get("signals", [])
            continue
        if verbose:
            print(f"[{position}/{len(grid)}] {label}")
        try:
            result = evaluate_combo(data, config, spec, feature_set, fingerprints,
                                    families=families, wf_family=wf_family, verbose=verbose)
        except ValueError as exc:      # not enough labelled rows for this cell
            if verbose:
                print(f"    ! skipped: {exc}")
            continue
        records.append(result["record"])
        signals_by_id[result["record"]["experiment_id"]] = result["signals"]
        _store_partial(partial_key, result["record"], result["signals"])
        if verbose:
            print(f"    · checkpointed ({len(records)}/{len(grid)})")

    if regime:
        spec = T.TargetSpec(target=T.TARGET_M1_EXPIRY, horizon=headline_horizon,
                            neutral_mode="atr", neutral_value=config.neutral_value)
        if verbose:
            print("[regime] M1_EXPIRY_OUTCOME regime filters")
        try:
            for record in regime_variants(data, config, spec, FL.FEATURE_SET_EXTENDED,
                                          fingerprints, families=(MODEL_LGBM,), wf_family=wf_family,
                                          done=done, partial_key=partial_key, verbose=verbose):
                records.append(record)
                signals_by_id[record["experiment_id"]] = []
                if partial_key:
                    _store_partial(partial_key, record, [])
        except ValueError as exc:
            if verbose:
                print(f"    ! regime study skipped: {exc}")

    for record in records:
        record["_signals"] = signals_by_id.get(record["experiment_id"], [])

    _clear_partial(partial_key)
    index = save_experiments(records, config, fingerprints,
                             run_notes={"runtime_seconds": round(time.time() - started, 1),
                                        "xgboost": xgboost_status(),
                                        "neural_challenger": "skipped — torch is not installed",
                                        "python": __import__("platform").python_version()})
    report_path = render_markdown(index, EXPERIMENTS_DIR)
    index["report_markdown"] = str(report_path)
    if verbose:
        print(f"\n✅ {len(records)} experiments written to {EXPERIMENTS_DIR}")
        print(f"   report: {report_path}")
        print(f"   runtime: {index['run_notes']['runtime_seconds']}s")
    return index


def xgboost_status() -> str:
    try:
        import xgboost  # noqa: F401

        return f"available {xgboost.__version__}"
    except ImportError:
        return "not installed — benchmarked without it (recorded, not silently skipped)"


def render_markdown(index: dict[str, Any], directory: Path) -> Path:
    rows = index["experiments"]
    lines = [
        "# Model quality research run",
        "",
        f"- run id: `{index['run_id']}`",
        f"- generated: {index['generated_at_utc']}",
        f"- dataset version: `{index['dataset']['dataset_version']}` "
        f"({index['dataset']['rows']:,} rows, {', '.join(index['dataset']['assets'])})",
        f"- period: {index['dataset']['period_start_utc']} → {index['dataset']['period_end_utc']}",
        f"- skipped assets: {index['dataset']['skipped'] or 'none'}",
        f"- runtime: {index.get('run_notes', {}).get('runtime_seconds')}s",
        f"- xgboost: {index.get('run_notes', {}).get('xgboost')}",
        "",
        "> Offline validation measurements only. Not a promise of future performance and never to be "
        "combined with live paper results. No 'best model' ranking is produced — read the table.",
        "",
        "## Validation and holdout metrics",
        "",
        "| experiment | rows | val acc | val F1 | val AUC | hold F1 | hold AUC | hold Brier | hold dir.prec | ECE | gate | hold signals | hold hit |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        validation = row["validation"]
        holdout = row["holdout"]
        gate = row["selected_threshold"]
        gate_stats = row["holdout_gate"]
        lines.append(
            f"| {row['experiment_id']} | {row['rows']['total']:,} | "
            f"{_fmt(validation.get('accuracy'))} | {_fmt(validation.get('macro_f1'))} | "
            f"{_fmt(validation.get('roc_auc'))} | {_fmt(holdout.get('macro_f1'))} | "
            f"{_fmt(holdout.get('roc_auc'))} | {_fmt(holdout.get('brier'))} | "
            f"{_fmt(holdout.get('directional_precision'))} | "
            f"{_fmt(row.get('expected_calibration_error'))} | "
            f"{gate if gate is not None else 'none'} | {gate_stats.get('signals')} | "
            f"{_fmt(gate_stats.get('hit_rate'))} |"
        )

    no_evidence = [r for r in rows if r["selected_threshold"] is None]
    lines += [
        "",
        "## What the thresholds say",
        "",
        f"- experiments where **no threshold qualified on validation**: {len(no_evidence)} of {len(rows)}",
        "- selection rule: smallest gate with ≥ 100 validation signals whose Wilson 95 % lower bound on the "
        "hit rate clears 0.50 — i.e. evidence, not hope",
        "- when no threshold qualifies, the correct behaviour of the running system is NO TRADE",
        "",
        "## Paper replay (untouched holdout, selected gate applied once)",
        "",
        "| experiment | signals | trades | wins | losses | win rate | no-trade rate | max losing streak |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        replay = row["paper_replay"]
        lines.append(
            f"| {row['experiment_id']} | {replay.get('signals')} | {replay.get('trades')} | "
            f"{replay.get('wins')} | {replay.get('losses')} | {_fmt(replay.get('win_rate'))} | "
            f"{_fmt(replay.get('no_trade_rate'))} | {replay.get('max_losing_streak')} |"
        )

    lines += [
        "",
        "## Walk-forward paper trading (retrain → test again)",
        "",
        "| experiment | steps | signals | wins | losses | win rate | max losing streak |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        totals = row["walk_forward_totals"]
        lines.append(
            f"| {row['experiment_id']} | {len(row.get('walk_forward_steps') or []) or row.get('walk_forward_step_count', '—')} | "
            f"{totals.get('signals')} | {totals.get('wins')} | {totals.get('losses')} | "
            f"{_fmt(totals.get('win_rate'))} | {totals.get('max_losing_streak')} |"
        )

    lines += ["", "## Calibration (holdout, selected gate)", "",
              "Confidence is a model score. These buckets are what the score actually delivered:", ""]
    for row in rows[:6]:
        calibration = row.get("calibration_holdout") or {}
        if not calibration:
            continue
        lines.append(f"**{row['experiment_id']}** — ECE {calibration.get('expected_calibration_error')}")
        lines.append("")
        lines.append("| bucket | samples | mean confidence | realised accuracy | gap |")
        lines.append("| --- | --- | --- | --- | --- |")
        for bucket in calibration.get("bins", []):
            lines.append(f"| {bucket['bin']} | {bucket['count']} | {_fmt(bucket['mean_confidence'])} | "
                         f"{_fmt(bucket['accuracy'])} | {_fmt(bucket['gap'])} |")
        lines.append("")
    if len(rows) > 6:
        lines.append(f"(calibration tables for the remaining {len(rows) - 6} experiments are in the "
                     "per-experiment JSON files)")
        lines.append("")

    stamp = index["run_id"].replace("lab-", "")
    path = directory / f"model_quality_{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)
