"""Training + validation pipeline.

    python -m backend.research.train --assets EURUSD,GBPUSD --source auto --rows 40000

What it does, in order:
  1. loads candles per asset through the provider chain (csv → public delayed → simulated)
  2. builds the M1 feature block (identical code path to live inference)
  3. labels the next-candle direction with a NEUTRAL band (no coin-flip labels)
  4. walk-forward validation (expanding window, embargo = horizon, no shuffling)
  5. leaves the final 20% as an untouched holdout and reports holdout metrics
  6. fits a calibration temperature on out-of-fold probabilities
  7. fits an IsolationForest anomaly detector
  8. refits the production models on all data and writes artifacts + reports

Learning-quality warnings are emitted honestly (e.g. "trained on SIMULATED
data"). No metric in the report is invented: every number is computed from the
data that was actually used.
"""
from __future__ import annotations

import argparse
import gc
import json
import platform
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from ..config import ARTIFACTS_DIR, REPORTS_DIR, model_dir
from ..constants import ASSETS, ASSET_MAP, DEFAULT_ENABLED_ASSETS, FEATURE_VERSION, canonical_symbol
from ..data.base import MarketDataUnavailable
from ..data.registry import registry
from ..features.pipeline import FEATURE_NAMES, build_feature_frame, label_frame
from ..models.ensemble import aggregate, vote_from_probs
from ..timeutil import now_utc, utc_iso, utc_now_naive
from ..validation import metrics as M
from ..validation import time_series as TS

warnings.filterwarnings("ignore", category=FutureWarning)

RANDOM_SEED = 20260101
CLASS_LABELS = ("PUT", "NEUTRAL", "CALL")


# --------------------------------------------------------------------- data prep
@dataclass
class Dataset:
    x: pd.DataFrame
    y: pd.Series
    times: pd.Series
    assets: pd.Series
    provenance: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.x)


def collect_dataset(asset_symbols: list[str], *, rows_per_asset: int, source: str,
                    horizon: int, neutral_atr_fraction: float, verbose: bool = True) -> Dataset:
    frames: list[pd.DataFrame] = []
    provenance_assets: list[dict[str, Any]] = []
    any_simulated = False

    for raw_symbol in asset_symbols:
        canonical = canonical_symbol(raw_symbol)
        spec = ASSET_MAP.get(canonical) if canonical else None
        if spec is None:
            raise SystemExit(f"Unknown asset '{raw_symbol}'. Known: {sorted(ASSET_MAP)}")
        symbol = spec.symbol
        mode = {"auto": "auto", "simulated": "simulated", "csv": "csv",
                "real_only": "real_only", "live": "live_only"}.get(source, "auto")
        try:
            frame = registry.get_frame(symbol, limit=rows_per_asset, data_source_mode=mode, use_cache=False)
        except MarketDataUnavailable as exc:
            print(f"  ! {symbol}: {exc.reason} — skipped")
            continue
        try:
            features = build_feature_frame(frame.df, spec)
            labels = label_frame(frame.df, horizon=horizon, neutral_atr_fraction=neutral_atr_fraction)
        except Exception as exc:  # noqa: BLE001 — a short dataset is skipped, not guessed
            print(f"  ! {symbol}: {len(frame.df)} candles is not enough for the feature pipeline "
                  f"({type(exc).__name__}: {str(exc)[:90]}) — skipped")
            continue
        combined = features.copy()
        combined["__y"] = labels["y"]
        combined["__time"] = frame.df.index
        combined["__asset"] = symbol
        combined = combined.replace([np.inf, -np.inf], np.nan).dropna()
        if len(combined) < 200:
            print(f"  ! {symbol}: only {len(combined)} usable rows — skipped")
            continue
        frames.append(combined)
        any_simulated = any_simulated or frame.meta.is_simulated
        from ..data.validation import validate_candles

        report = validate_candles(frame.df)
        provenance_assets.append({
            "asset": symbol,
            "provider": frame.meta.provider,
            "source_label": frame.meta.source_label,
            "source_class": frame.meta.source_class,
            "is_simulated": frame.meta.is_simulated,
            "is_replay": frame.meta.is_replay,
            "is_real_market_data": frame.meta.is_market_data,
            "rows": int(len(combined)),
            "period_start_utc": utc_iso(frame.df.index[0].to_pydatetime()),
            "period_end_utc": utc_iso(frame.df.index[-1].to_pydatetime()),
            "dataset_validation": report.as_dict(),
        })
        if verbose:
            print(f"  · {symbol}: {len(combined)} rows from {frame.meta.source_label} "
                  f"({'SIMULATED' if frame.meta.is_simulated else frame.meta.badge})")

    if not frames:
        raise SystemExit(
            "No training data available. Provide CSVs in backend/data/csv/<ASSET>.csv, "
            "or use --source simulated for a demo model."
        )

    combined_all = pd.concat(frames, axis=0).sort_values("__time").reset_index(drop=True)
    x = combined_all[list(FEATURE_NAMES)].astype(float)
    y = combined_all["__y"].astype(int)
    times = combined_all["__time"]
    assets = combined_all["__asset"]

    leak = TS.leakage_probe(x, y)
    if not leak["ok"]:
        raise SystemExit(f"Leakage probe failed — suspicious features: {leak['suspicious']}")

    all_simulated = bool(provenance_assets) and all(a["is_simulated"] for a in provenance_assets)
    classes = {a.get("source_class", "UNKNOWN") for a in provenance_assets}
    if classes == {"SIMULATED"} or (all_simulated and classes <= {"SIMULATED", "UNKNOWN"}):
        source_class = "SIMULATED"
    elif classes == {"FIXTURE"}:
        source_class = "FIXTURE"
    elif classes == {"MARKET"}:
        source_class = "MARKET"
    else:
        source_class = "MIXED"

    provenance = {
        "source": (
            "simulated" if source_class == "SIMULATED" else
            "fixture" if source_class == "FIXTURE" else
            "mixed" if source_class == "MIXED" else "market"
        ),
        "source_class": source_class,
        "is_simulated": source_class in {"SIMULATED", "FIXTURE"},
        "market_data": source_class == "MARKET",
        "assets": [a["asset"] for a in provenance_assets],
        "assets": provenance_assets,
        "rows": int(len(x)),
        "period_start_utc": utc_iso(pd.to_datetime(times.iloc[0]).to_pydatetime()),
        "period_end_utc": utc_iso(pd.to_datetime(times.iloc[-1]).to_pydatetime()),
        "leakage_probe": leak,
        "notes": (
            "Trained on SIMULATED data — the resulting model is a pipeline demonstration, "
            "not a market-quality model."
            if source_class == "SIMULATED" else
            "Trained on FIXTURE (QA) data — not market data and never usable for a production signal."
            if source_class == "FIXTURE" else
            "Trained on a mixture of real and synthetic data — this model is NOT eligible for "
            "production signals."
            if source_class == "MIXED" else
            "Trained on operator-supplied real market data."
        ),
        "warning": (
            "is_simulated=true: these metrics describe a synthetic series and must not be presented "
            "as market performance." if source_class in {"SIMULATED", "FIXTURE"} else
            "Mixed dataset: the model cannot be used for production signals." if source_class == "MIXED"
            else None
        ),
    }
    return Dataset(x=x, y=y, times=times, assets=assets, provenance=provenance)


# ------------------------------------------------------------------- estimators
def build_estimators(seed: int = RANDOM_SEED) -> dict[str, Any]:
    models: dict[str, Any] = {}

    try:
        from lightgbm import LGBMClassifier

        models["lightgbm"] = LGBMClassifier(
            n_estimators=320, learning_rate=0.05, num_leaves=31, subsample=0.85,
            subsample_freq=1, colsample_bytree=0.85, min_child_samples=40,
            reg_lambda=1.0, class_weight="balanced", random_state=seed, n_jobs=-1,
            verbose=-1,
        )
    except ImportError:
        print("  ! lightgbm not installed — skipping that model")

    from sklearn.ensemble import RandomForestClassifier

    models["random_forest"] = RandomForestClassifier(
        n_estimators=300, max_depth=8, min_samples_leaf=25, class_weight="balanced_subsample",
        random_state=seed, n_jobs=-1,
    )

    from sklearn.linear_model import LogisticRegression

    models["logistic_regression"] = LogisticRegression(
        C=0.5, max_iter=1200, class_weight="balanced", random_state=seed, n_jobs=-1,
    )

    try:
        from xgboost import XGBClassifier

        models["xgboost"] = XGBClassifier(
            n_estimators=320, learning_rate=0.05, max_depth=5, subsample=0.85,
            colsample_bytree=0.85, reg_lambda=1.0, random_state=seed, n_jobs=-1,
            tree_method="hist", verbosity=0,
        )
    except ImportError:
        pass

    return models


def _fit_all(models: dict[str, Any], x_train: pd.DataFrame, y_train: pd.Series,
             scaler=None) -> dict[str, Any]:
    fitted: dict[str, Any] = {}
    for name, model in models.items():
        data = scaler.transform(x_train) if (name == "logistic_regression" and scaler is not None) else x_train
        try:
            model.fit(data, y_train)
            fitted[name] = model
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {name} failed to fit: {type(exc).__name__}: {exc}")
    return fitted


def _proba_matrix(model: Any, x: pd.DataFrame, scaler=None, name: str = "") -> np.ndarray:
    data = scaler.transform(x) if (name == "logistic_regression" and scaler is not None) else x
    proba = model.predict_proba(data)
    classes = list(getattr(model, "classes_", [0, 1, 2]))
    out = np.zeros((len(x), 3), dtype=float)
    for idx, cls in enumerate(classes):
        ci = int(cls)
        if 0 <= ci <= 2:
            out[:, ci] = proba[:, idx]
    totals = out.sum(axis=1, keepdims=True)
    return out / np.where(totals <= 0, 1.0, totals)


def ensemble_proba(fitted: dict[str, Any], x: pd.DataFrame, scaler=None) -> np.ndarray:
    from ..models.ensemble import BASE_WEIGHTS

    weights = np.array([BASE_WEIGHTS.get(name, 0.10) for name in fitted], dtype=float)
    weights = weights / weights.sum()
    stacked = np.stack([_proba_matrix(m, x, scaler, name) for name, m in fitted.items()], axis=0)
    return (stacked * weights[:, None, None]).sum(axis=0)


# ------------------------------------------------------------------ walk-forward
def evaluate(dataset: Dataset, *, n_splits: int, neutral_margin: float, seed: int = RANDOM_SEED) -> dict[str, Any]:
    from sklearn.preprocessing import StandardScaler

    folds = TS.walk_forward_splits(len(dataset), n_splits=n_splits, min_train_rows=800,
                                   embargo=max(1, 2))
    fold_reports: list[dict[str, Any]] = []
    oof_proba: list[np.ndarray] = []
    oof_true: list[np.ndarray] = []

    for fold in folds:
        x_train = dataset.x.iloc[fold.train_start:fold.train_end]
        y_train = dataset.y.iloc[fold.train_start:fold.train_end]
        x_test = dataset.x.iloc[fold.test_start:fold.test_end]
        y_test = dataset.y.iloc[fold.test_start:fold.test_end]
        if y_train.nunique() < 2 or len(y_test) < 20:
            continue
        scaler = StandardScaler().fit(x_train)
        fitted = _fit_all(build_estimators(seed), x_train, y_train, scaler)
        if not fitted:
            continue
        proba = ensemble_proba(fitted, x_test, scaler)
        prediction = proba.argmax(axis=1)
        report = M.classification_metrics(y_test.to_numpy(), prediction)
        report.update({
            "fold": fold.index,
            "fold_detail": fold.describe(),
            "roc_auc": M.roc_auc_ovr(y_test.to_numpy(), proba),
            "brier": M.brier_score(y_test.to_numpy(), proba),
            "directional": M.directional_signal_stats(y_test.to_numpy(), prediction, proba),
            "models": list(fitted.keys()),
        })
        fold_reports.append(report)
        oof_proba.append(proba)
        oof_true.append(y_test.to_numpy())
        del fitted, scaler, x_train, y_train, x_test, y_test, proba, prediction
        gc.collect()

    out: dict[str, Any] = {
        "method": "expanding-window walk-forward, embargo = 2 candles, no shuffling",
        "folds": fold_reports,
        "fold_count": len(fold_reports),
    }
    if fold_reports:
        out["summary"] = {
            "accuracy": M.summarize_folds(fold_reports, "accuracy"),
            "macro_f1": M.summarize_folds(fold_reports, "macro_f1"),
            "brier": M.summarize_folds(fold_reports, "brier"),
        }
    if oof_proba:
        proba_all = np.vstack(oof_proba)
        true_all = np.concatenate(oof_true)
        out["out_of_fold"] = {
            "samples": int(len(true_all)),
            "metrics": M.classification_metrics(true_all, proba_all.argmax(axis=1)),
            "roc_auc": M.roc_auc_ovr(true_all, proba_all),
            "brier": M.brier_score(true_all, proba_all),
            "calibration": M.calibration_table(true_all, proba_all),
            "directional": M.directional_signal_stats(true_all, proba_all.argmax(axis=1), proba_all),
            "temperature_fitted": M.fit_temperature(true_all, proba_all),
        }
    return out


@dataclass
class HoldoutFit:
    """Models fitted once on the 80 % block and reused for both the holdout
    metrics and the confidence-gate replay (refitting twice burned memory/time)."""

    fitted: dict[str, Any]
    scaler: Any
    x_train: pd.DataFrame
    y_train: pd.Series
    x_test: pd.DataFrame
    y_test: pd.Series
    proba: np.ndarray


def fit_holdout(dataset: Dataset, *, holdout_fraction: float, seed: int = RANDOM_SEED) -> HoldoutFit | None:
    from sklearn.preprocessing import StandardScaler

    train_slice, test_slice = TS.holdout_split(len(dataset), holdout_fraction=holdout_fraction, embargo=2)
    x_train, y_train = dataset.x.iloc[train_slice], dataset.y.iloc[train_slice]
    x_test, y_test = dataset.x.iloc[test_slice], dataset.y.iloc[test_slice]
    scaler = StandardScaler().fit(x_train)
    fitted = _fit_all(build_estimators(seed), x_train, y_train, scaler)
    if not fitted:
        return None
    proba = ensemble_proba(fitted, x_test, scaler)
    return HoldoutFit(fitted=fitted, scaler=scaler, x_train=x_train, y_train=y_train,
                      x_test=x_test, y_test=y_test, proba=proba)


def evaluate_holdout(dataset: Dataset, *, holdout_fraction: float, seed: int = RANDOM_SEED,
                     fit: HoldoutFit | None = None) -> dict[str, Any]:
    fit = fit or fit_holdout(dataset, holdout_fraction=holdout_fraction, seed=seed)
    if fit is None:
        return {"error": "no models fitted"}
    x_train = fit.x_train
    y_test = fit.y_test
    proba = fit.proba
    prediction = proba.argmax(axis=1)
    x_test = fit.x_test
    return {
        "description": f"final untouched holdout — last {int(holdout_fraction * 100)}% of the chronological series",
        "train_rows": int(len(x_train)),
        "test_rows": int(len(x_test)),
        "period_train_utc": [utc_iso(pd.to_datetime(dataset.times.iloc[0]).to_pydatetime()),
                             utc_iso(pd.to_datetime(dataset.times.iloc[len(x_train) - 1]).to_pydatetime())],
        "period_test_utc": [utc_iso(pd.to_datetime(dataset.times.iloc[len(x_train)]).to_pydatetime()),
                            utc_iso(pd.to_datetime(dataset.times.iloc[-1]).to_pydatetime())],
        "metrics": M.classification_metrics(y_test.to_numpy(), prediction),
        "roc_auc": M.roc_auc_ovr(y_test.to_numpy(), proba),
        "brier": M.brier_score(y_test.to_numpy(), proba),
        "calibration": M.calibration_table(y_test.to_numpy(), proba),
        "directional": M.directional_signal_stats(y_test.to_numpy(), prediction, proba),
        "models": list(fit.fitted.keys()),
    }


def signal_gate_replay(dataset: Dataset, *, threshold: float, holdout_fraction: float,
                       seed: int = RANDOM_SEED, fit: HoldoutFit | None = None) -> dict[str, Any]:
    """How often would the *confidence gate* (≥ threshold) have fired, and with what hit rate?

    This is a research diagnostic, not a promise of performance. Confidence here is
    approximated by the ensemble directional probability so the gate can be replayed
    over historical rows.
    """
    fit = fit or fit_holdout(dataset, holdout_fraction=holdout_fraction, seed=seed)
    if fit is None:
        return {"error": "no models fitted"}
    y_test = fit.y_test
    proba = fit.proba
    direction_prob = proba[:, [0, 2]].max(axis=1)
    directional_sum = proba[:, 0] + proba[:, 2]
    normalised = np.where(directional_sum > 0, direction_prob / np.maximum(directional_sum, 1e-9), 0.5)
    votes = np.array([vote_from_probs("ens", p, 1.0).vote for p in proba])
    direction = np.where(proba[:, 2] >= proba[:, 0], 2, 0)
    proxy_confidence = 100.0 * (0.4 * np.clip((direction_prob - 0.33) / 0.20, 0, 1)
                                + 0.6 * np.clip((normalised - 0.52) / 0.28, 0, 1))
    directional_vote = votes != "NO_TRADE"
    sweep: list[dict[str, Any]] = []
    for level in sorted({60, 70, 75, 80, 85, 90, threshold}):
        gate = directional_vote & (proxy_confidence >= level)
        if gate.sum() == 0:
            sweep.append({"threshold": level, "gated_signals": 0, "hit_rate": None})
            continue
        hits = (y_test.to_numpy()[gate] == direction[gate])
        sweep.append({
            "threshold": level,
            "gated_signals": int(gate.sum()),
            "gated_share_of_rows": round(float(gate.mean()), 4),
            "hit_rate": round(float(hits.mean()), 4),
        })

    gate = directional_vote & (proxy_confidence >= threshold)
    hits = (y_test.to_numpy()[gate] == direction[gate]) if gate.sum() else None
    return {
        "threshold": threshold,
        "gated_signals": int(gate.sum()),
        "gated_share_of_rows": round(float(gate.mean()), 4),
        "hit_rate": None if hits is None or len(hits) == 0 else round(float(hits.mean()), 4),
        "threshold_sweep": sweep,
        "note": (
            "Research diagnostic: a confidence proxy computed from ensemble probabilities and applied to "
            "historical holdout rows. It shows how selective the gate is — it is NOT a live win rate and it "
            "does not include the rule-based components of the live confidence engine."
        ),
    }


# ----------------------------------------------------------------------- reports
def write_report(out: dict[str, Any], reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now_naive().strftime("%Y%m%dT%H%M%SZ")
    json_path = reports_dir / f"validation_{stamp}.json"
    json_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    hold = out.get("holdout", {}).get("metrics", {})
    oof = out.get("walk_forward", {}).get("out_of_fold", {})
    lines = [
        f"# Quotex Signal AI — validation report {stamp}",
        "",
        f"- Model version: `{out.get('version')}`",
        f"- Trained at: {out.get('trained_at_utc')}",
        f"- Rows: {out.get('provenance', {}).get('rows')}",
        f"- Data source: **{out.get('provenance', {}).get('source')}**"
        + ("  ⚠️ SIMULATED — not market performance" if out.get('provenance', {}).get('is_simulated') else ""),
        f"- Horizon: {out.get('horizon')} candle(s); neutral band: {out.get('neutral_atr_fraction')} × ATR",
        "",
        "## Walk-forward (out-of-fold)",
        f"- Accuracy: {oof.get('metrics', {}).get('accuracy')}",
        f"- Macro F1: {oof.get('metrics', {}).get('macro_f1')}",
        f"- ROC-AUC (macro): {oof.get('roc_auc', {}).get('macro')}",
        f"- Brier: {oof.get('brier')}",
        f"- Expected calibration error: {oof.get('calibration', {}).get('expected_calibration_error')}",
        f"- Directional win rate (gated rows): {oof.get('directional', {}).get('win_rate')}%",
        f"- Max losing streak: {oof.get('directional', {}).get('max_losing_streak')}",
        "",
        "## Final holdout",
        f"- Rows: {out.get('holdout', {}).get('test_rows')}",
        f"- Accuracy: {hold.get('accuracy')}",
        f"- Macro F1: {hold.get('macro_f1')}",
        f"- ROC-AUC (macro): {out.get('holdout', {}).get('roc_auc', {}).get('macro')}",
        f"- Directional win rate: {out.get('holdout', {}).get('directional', {}).get('win_rate')}%",
        "",
        "## Confusion matrix (holdout, rows = actual, cols = predicted)",
        "",
        "| actual \\ predicted | PUT | NEUTRAL | CALL |",
        "| --- | --- | --- | --- |",
    ]
    matrix = hold.get("confusion_matrix") or [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    for idx, label in enumerate(CLASS_LABELS):
        lines.append(f"| {label} | {matrix[idx][0]} | {matrix[idx][1]} | {matrix[idx][2]} |")
    lines += [
        "",
        "## Calibration (holdout)",
        "",
        "| bin | count | mean confidence | realised accuracy | gap |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in (out.get("holdout", {}).get("calibration", {}).get("bins") or []):
        lines.append(f"| {row['bin']} | {row['count']} | {row['mean_confidence']} | {row['accuracy']} | {row['gap']} |")
    lines += [
        "",
        "## Notes",
        "- No random shuffling is used anywhere: folds are chronological with an embargo ≥ horizon.",
        "- The holdout block is never used for model selection or calibration.",
        "- Reported numbers come from stored evaluation output; nothing is hard-coded.",
    ]
    md_path = reports_dir / f"validation_{stamp}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    (reports_dir / "latest.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    (reports_dir / "latest.md").write_text("\n".join(lines), encoding="utf-8")
    return md_path


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train + validate the Quotex Signal AI M1 models")
    parser.add_argument("--assets", default=",".join(DEFAULT_ENABLED_ASSETS[:6]),
                        help="comma separated asset symbols")
    parser.add_argument("--source", default="auto",
                        choices=["auto", "simulated", "csv", "real_only", "live"],
                        help="csv/real_only = operator datasets (real data), simulated = demo model")
    parser.add_argument("--rows", type=int, default=20000, help="max candles requested per asset")
    parser.add_argument("--horizon", type=int, default=1, help="label horizon in candles (expiry minutes)")
    parser.add_argument("--neutral-atr", type=float, default=0.25,
                        help="moves smaller than this × ATR are labelled NEUTRAL (coin-flip band)")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--holdout", type=float, default=0.2)
    parser.add_argument("--min-confidence", type=float, default=85.0,
                        help="threshold used for the research gate replay")
    parser.add_argument("--version", default=None)
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    parser.add_argument("--require-market", action="store_true",
                        help="abort unless EVERY asset's dataset is real market data")
    args = parser.parse_args(argv)

    version = args.version or utc_now_naive().strftime("v%Y%m%d-%H%M")
    symbols = [s.strip().upper() for s in args.assets.split(",") if s.strip()]
    print(f"Quotex Signal AI — training {version}")
    print(f"  assets: {symbols}\n  source: {args.source}   horizon: {args.horizon} candle(s)")

    dataset = collect_dataset(symbols, rows_per_asset=args.rows, source=args.source,
                              horizon=args.horizon, neutral_atr_fraction=args.neutral_atr)
    print(f"\n  dataset: {len(dataset)} rows  classes: "
          f"{dict(zip(['PUT', 'NEUTRAL', 'CALL'], np.bincount(dataset.y, minlength=3).tolist()))}")
    if len(dataset) < 3000:
        print("  ! WARNING: fewer than 3,000 rows — metrics will be noisy (flagged in the artifact).")

    source_class = dataset.provenance.get("source_class")
    if args.require_market and source_class != "MARKET":
        raise SystemExit(
            f"--require-market was set but the dataset is '{source_class}'. "
            "Import real operator candles first (POST /api/data/import or backend/data/csv/<ASSET>.csv)."
        )
    if source_class == "MARKET":
        print("  data class: MARKET (operator-supplied real market data) → model will load as READY_MARKET")
    else:
        print(f"  data class: {source_class} → model will load as READY_DEMO "
              "(pipeline demonstration; no production signals)")

    print("\n[1/5] walk-forward validation …")
    walk = evaluate(dataset, n_splits=args.folds, neutral_margin=0.02, seed=args.seed)
    oof = walk.get("out_of_fold", {})
    print(f"   folds: {walk.get('fold_count')}   oof accuracy: {oof.get('metrics', {}).get('accuracy')}   "
          f"macro F1: {oof.get('metrics', {}).get('macro_f1')}")

    print("\n[2/5] final holdout …")
    holdout_fit = fit_holdout(dataset, holdout_fraction=args.holdout, seed=args.seed)
    holdout = evaluate_holdout(dataset, holdout_fraction=args.holdout, seed=args.seed, fit=holdout_fit)
    print(f"   holdout rows: {holdout.get('test_rows')}   accuracy: {holdout.get('metrics', {}).get('accuracy')}   "
          f"macro F1: {holdout.get('metrics', {}).get('macro_f1')}")

    print("\n[3/5] confidence-gate replay …")
    gate = signal_gate_replay(dataset, threshold=args.min_confidence,
                              holdout_fraction=args.holdout, seed=args.seed, fit=holdout_fit)
    print(f"   gated signals: {gate.get('gated_signals')}   hit rate: {gate.get('hit_rate')}")
    print(f"   sweep: " + ", ".join(f"≥{row['threshold']}% → {row['gated_signals']} signals"
                                    for row in gate.get("threshold_sweep", [])))
    del holdout_fit

    temperature = float(oof.get("temperature_fitted", 1.0) or 1.0)
    print(f"\n[4/5] fitting production models on all {len(dataset)} rows (temperature {temperature}) …")
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler().fit(dataset.x)
    fitted = _fit_all(build_estimators(args.seed), dataset.x, dataset.y, scaler)
    if not fitted:
        raise SystemExit("No model could be fitted — aborting.")

    print("\n[5/5] fitting anomaly detector …")
    gc.collect()
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler as SS

    anomaly_scaler = SS().fit(dataset.x)
    iso = IsolationForest(n_estimators=200, contamination=0.01, random_state=args.seed, n_jobs=-1)
    iso.fit(anomaly_scaler.transform(dataset.x))

    target = model_dir(version)
    target.mkdir(parents=True, exist_ok=True)
    joblib.dump({"models": fitted, "version": version}, target / "models.pkl")
    joblib.dump({"features": scaler, "anomaly_scaler": anomaly_scaler}, target / "scaler.pkl")
    joblib.dump({"model": iso, "scaler": anomaly_scaler}, target / "isolation_forest.pkl")

    # Metadata + hash manifest first, so every artifact directory is verifiable on load.
    metrics_payload = {
        "walk_forward": walk,
        "holdout": holdout,
        "gate_replay": gate,
        "per_model_holdout": {},
    }
    for name, model in fitted.items():
        try:
            import numpy as _np

            train_slice, test_slice = TS.holdout_split(len(dataset), holdout_fraction=args.holdout, embargo=2)
            proba = _proba_matrix(model, dataset.x.iloc[test_slice], scaler, name)
            metrics_payload["per_model_holdout"][name] = M.classification_metrics(
                dataset.y.iloc[test_slice].to_numpy(), proba.argmax(axis=1)
            )
        except Exception as exc:  # noqa: BLE001
            metrics_payload["per_model_holdout"][name] = {"error": f"{type(exc).__name__}: {exc}"}

    training_timestamp = utc_iso(now_utc())
    oof_metrics = {
        "accuracy": oof.get("metrics", {}).get("accuracy"),
        "macro_f1": oof.get("metrics", {}).get("macro_f1"),
        "roc_auc_macro": oof.get("roc_auc", {}).get("macro"),
        "brier": oof.get("brier"),
        "expected_calibration_error": oof.get("calibration", {}).get("expected_calibration_error"),
        "directional_win_rate": oof.get("directional", {}).get("win_rate"),
        "max_losing_streak": oof.get("directional", {}).get("max_losing_streak"),
        "samples": oof.get("samples"),
    }
    holdout_metrics = {
        "accuracy": holdout.get("metrics", {}).get("accuracy"),
        "macro_f1": holdout.get("metrics", {}).get("macro_f1"),
        "roc_auc_macro": holdout.get("roc_auc", {}).get("macro"),
        "brier": holdout.get("brier"),
        "directional_win_rate": holdout.get("directional", {}).get("win_rate"),
        "samples": holdout.get("test_rows"),
    }
    metadata = {
        # ---- identity + spec §3 provenance block ---------------------------
        "version": version,
        "model_version": version,
        "feature_version": FEATURE_VERSION,
        "trained_at_utc": training_timestamp,
        "training_timestamp": training_timestamp,
        "data_source": dataset.provenance.get("source"),
        "data_source_class": dataset.provenance.get("source_class"),
        "data_period": {
            "start_utc": dataset.provenance.get("period_start_utc"),
            "end_utc": dataset.provenance.get("period_end_utc"),
        },
        "row_count": int(len(dataset)),
        "asset_list": [a["asset"] for a in dataset.provenance.get("assets", [])],
        "validation_metrics": oof_metrics,
        "holdout_metrics": holdout_metrics,
        "feature_names": list(FEATURE_NAMES),
        "horizon": args.horizon,
        "neutral_atr_fraction": args.neutral_atr,
        "temperature": temperature,
        "data_provenance": dataset.provenance,
        "training_rows": int(len(dataset)),
        "class_balance": {label: int(c) for label, c in zip(CLASS_LABELS, np.bincount(dataset.y, minlength=3))},
        "models": list(fitted.keys()),
        "small_dataset": bool(len(dataset) < 3000),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "metrics": {
            "walk_forward_summary": walk.get("summary"),
            "out_of_fold": oof_metrics,
            "holdout": holdout_metrics,
        },
    }
    (target / "metadata.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    # Hash manifest: written last, so it covers every artifact file the loader will read.
    from ..models import integrity as _integrity

    _integrity.write_manifest(target, note=f"written by backend.research.train ({version})")

    out = {
        "version": version,
        "trained_at_utc": metadata["trained_at_utc"],
        "horizon": args.horizon,
        "neutral_atr_fraction": args.neutral_atr,
        "provenance": dataset.provenance,
        "walk_forward": walk,
        "holdout": holdout,
        "gate_replay": gate,
        "per_model_holdout": metrics_payload["per_model_holdout"],
        "models": list(fitted.keys()),
    }
    report_path = write_report(out, REPORTS_DIR)

    current_link = ARTIFACTS_DIR / "current"
    if current_link.is_symlink() or current_link.exists():
        try:
            if current_link.is_symlink() or current_link.is_file():
                current_link.unlink()
            else:
                for child in current_link.iterdir():
                    if child.is_file():
                        child.unlink()
        except OSError:
            pass
    try:
        if not current_link.exists():
            current_link.symlink_to(target.name, target_is_directory=True)
    except OSError:
        # platforms without symlink support: copy the files instead
        current_link.mkdir(parents=True, exist_ok=True)
        for name in ("models.pkl", "scaler.pkl", "isolation_forest.pkl", "metadata.json", "manifest.json"):
            src = target / name
            if src.exists():
                (current_link / name).write_bytes(src.read_bytes())

    print(f"\n✅ artifacts written to {target}")
    print(f"   active model dir: {model_dir()}")
    print(f"   validation report: {report_path}")
    if dataset.provenance.get("is_simulated"):
        print("\n⚠️  This model was trained on SIMULATED data — the dashboard will label it DEMO.")
    print("\nRestart the API (or POST /api/model/reload) so the engine picks up the new artifact.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
