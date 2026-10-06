"""Controlled model retraining (spec §13 of the paper-trading phase).

Hard rule: **the production artifact is never replaced just because a new model
scores better on the data it was trained on.** A candidate must beat the artifact
that is currently in service *on the same untouched holdout*, by a configurable
margin, before anything is promoted — and promotion is always an explicit,
recorded decision, never a background side effect of collecting candles.

    RETRAIN_INTERVAL_DAYS   how often a retrain may run (default 7)
    RETRAIN_MIN_NEW_ROWS    new rows required since the last retrain (default 500)
    RETRAIN_MIN_IMPROVEMENT required holdout gain to promote (default 0.01 accuracy)

The job is split in two, so nothing heavy happens implicitly:

    python -m backend.research.retrain --check          # is a retrain due?
    python -m backend.research.retrain --evaluate       # run the candidate
    python -m backend.research.retrain --promote        # only if the report says PROMOTE

`--evaluate` builds the dataset from the configured real sources, runs the
production target/horizon/feature set through the research `evaluate_combo`
machinery (identical folds and embargo as every other experiment), writes a
verdict report to `backend/research/reports/retrain_<stamp>.json`, and stops.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, Settings
from ..constants import DEFAULT_ENABLED_ASSETS
from ..database import db
from ..models.service import get_model_service
from ..settings_service import get_settings
from ..timeutil import utc_iso
from . import feature_lab as FL
from . import lab as LAB
from . import targets as T

REPORTS = Path(__file__).resolve().parent / "reports"
STATE_KEY = "retrain_state"


@dataclass
class RetrainPolicy:
    interval_days: int = 7
    min_new_rows: int = 500
    min_improvement: float = 0.01
    holdout_fraction: float = 0.2
    folds: int = 3
    assets: tuple[str, ...] = tuple(DEFAULT_ENABLED_ASSETS[:2])
    target: str = T.TARGET_M1_EXPIRY
    horizon: int = 1
    feature_set: str = FL.FEATURE_SET_BASE
    notes: str = ("promotion requires a strict out-of-sample improvement; training accuracy is never a "
                  "promotion criterion")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_env(cls) -> "RetrainPolicy":
        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        raw_assets = os.environ.get("RETRAIN_ASSETS", "").replace(" ", "")
        assets = tuple(a for a in raw_assets.split(",") if a) or cls.assets
        return cls(interval_days=_int("RETRAIN_INTERVAL_DAYS", cls.interval_days),
                   min_new_rows=_int("RETRAIN_MIN_NEW_ROWS", cls.min_new_rows),
                   min_improvement=_float("RETRAIN_MIN_IMPROVEMENT", cls.min_improvement),
                   assets=assets)


def should_promote(current: dict[str, Any], candidate: dict[str, Any], policy: RetrainPolicy) -> bool:
    """Strict out-of-sample comparison. Training accuracy plays no part in this.

    `current` and `candidate` are metric dicts carrying at least `holdout_accuracy`.
    """
    baseline = current.get("holdout_accuracy")
    challenger = candidate.get("holdout_accuracy")
    if baseline is None or challenger is None:
        return False
    try:
        baseline = float(baseline)
        challenger = float(challenger)
    except (TypeError, ValueError):
        return False
    return (challenger - baseline) >= float(policy.min_improvement)


# ------------------------------------------------------------------ due check
def state() -> dict[str, Any]:
    with db.connection() as conn:
        row = conn.execute("SELECT value FROM config_kv WHERE key=?", (STATE_KEY,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return {}


def _write_state(payload: dict[str, Any]) -> None:
    db.set_config({STATE_KEY: payload})


def due(policy: RetrainPolicy | None = None, *, now_utc: str | None = None) -> dict[str, Any]:
    policy = policy or RetrainPolicy.from_env()
    status = get_model_service().status()
    last = state().get("last_retrain_utc")
    rows_now = _current_rows()
    rows_then = state().get("rows_at_last_retrain")
    now_utc = now_utc or utc_iso()
    from datetime import datetime, timedelta

    reason: list[str] = []
    is_due = True
    if last:
        elapsed = (datetime.fromisoformat(now_utc) - datetime.fromisoformat(last)).total_seconds() / 86400.0
        if elapsed < policy.interval_days:
            is_due = False
            reason.append(f"only {elapsed:.2f} day(s) since the last retrain (interval {policy.interval_days})")
    if rows_then is not None and rows_now - int(rows_then) < policy.min_new_rows:
        is_due = False
        reason.append(f"only {rows_now - int(rows_then)} new row(s) since the last retrain "
                      f"(minimum {policy.min_new_rows})")
    return {
        "due": is_due,
        "checked_at_utc": now_utc,
        "policy": policy.as_dict(),
        "current_model": {"version": status.get("version"), "state": status.get("state"),
                          "production_usable": status.get("production_usable"),
                          "holdout_metrics": (status.get("holdout_metrics") or {})},
        "rows_now": rows_now,
        "rows_at_last_retrain": rows_then,
        "last_retrain_utc": last,
        "reasons": reason or ["interval and new-data requirements are satisfied"],
        "note": ("Retraining is never triggered by a single new candle, and promotion always requires an "
                 "out-of-sample improvement on the untouched holdout."),
    }


def _current_rows() -> int:
    from ..data import store

    rows = 0
    for asset in store.assets():
        rows += store.count(asset)
    if rows:
        return rows
    status = get_model_service().status()
    try:
        return int((status.get("training") or {}).get("rows") or status.get("rows") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- evaluation
def evaluate(policy: RetrainPolicy | None = None, *, rows_per_asset: int = 40000,
             source: str = "csv", verbose: bool = True) -> dict[str, Any]:
    """Run the candidate through the same fold/embargo machinery and record a verdict."""
    policy = policy or RetrainPolicy.from_env()
    settings: Settings = get_settings()
    config = LAB.LabConfig(assets=policy.assets, rows_per_asset=rows_per_asset, source=source,
                           horizons=(policy.horizon,), folds=policy.folds,
                           holdout_fraction=policy.holdout_fraction,
                           min_train_rows=6000, core_models=("lightgbm",))
    data = LAB.load_market_data(config)
    spec = T.TargetSpec(target=policy.target, horizon=policy.horizon, neutral_mode="atr",
                        neutral_value=config.neutral_value, reference="theoretical")
    fingerprints = LAB.dataset_fingerprint(data, config)
    result = LAB.evaluate_combo(data, config, spec, policy.feature_set, fingerprints,
                                families=("lightgbm",), wf_family="lightgbm", verbose=False)
    record = result["record"]
    candidate = {"holdout_accuracy": (record.get("holdout") or {}).get("accuracy"),
                 "holdout_macro_f1": (record.get("holdout") or {}).get("macro_f1"),
                 "holdout_roc_auc": (record.get("holdout") or {}).get("roc_auc"),
                 "validation_accuracy": (record.get("validation") or {}).get("accuracy")}
    status = get_model_service().status()
    current_metrics = status.get("holdout_metrics") or {}
    baseline = {"holdout_accuracy": current_metrics.get("accuracy"),
                "holdout_macro_f1": current_metrics.get("macro_f1"),
                "holdout_roc_auc": current_metrics.get("roc_auc")}
    promote = should_promote(baseline, candidate, policy)
    verdict = {
        "generated_at_utc": utc_iso(),
        "policy": policy.as_dict(),
        "dataset": {"version": fingerprints.get("dataset_version"), "rows": fingerprints.get("rows"),
                    "assets": list(policy.assets), "source": source},
        "experiment_id": record.get("experiment_id"),
        "candidate": candidate,
        "incumbent": {"version": status.get("version"), **baseline},
        "verdict": "PROMOTE" if promote else "RETAIN",
        "reason": (
            "candidate holdout accuracy beats the incumbent by the configured margin"
            if promote else
            "candidate did not show a sufficient out-of-sample improvement — the incumbent artifact is kept"
        ),
        "training_accuracy_is_not_a_criterion": True,
        "promotion_command": ("python -m backend.research.retrain --promote" if promote else None),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"retrain_{utc_iso().replace(':', '').replace('-', '')}.json"
    path.write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")
    verdict["report_path"] = str(path)
    if verbose:
        print(f"retrain verdict: {verdict['verdict']} — {verdict['reason']}")
        print(f"  report: {path}")
    return verdict


def promote(verdict: dict[str, Any] | None = None, *, version: str | None = None) -> dict[str, Any]:
    """Promote a candidate **only** when its own report says PROMOTE.

    Promotion retrains through the existing production trainer (which writes a new
    artifact version and repoints `current`), then records the decision.
    """
    verdict = verdict or _latest_report()
    if not verdict:
        return {"promoted": False, "reason": "no retrain report found — run --evaluate first"}
    if verdict.get("verdict") != "PROMOTE":
        return {"promoted": False, "reason": f"report verdict is {verdict.get('verdict')}, not PROMOTE"}
    from . import train as train_cli

    target_version = version or f"v{utc_iso()[:10].replace('-', '')}-retrain"
    argv = ["--assets", ",".join(verdict["dataset"]["assets"]), "--source", verdict["dataset"]["source"],
            "--rows", str(40000), "--horizon", str(verdict["policy"]["horizon"]),
            "--folds", str(verdict["policy"]["folds"]), "--version", target_version,
            "--require-market"]
    code = train_cli.main(argv)
    if code != 0:
        return {"promoted": False, "reason": f"training CLI exited with {code}"}
    _write_state({"last_retrain_utc": utc_iso(), "rows_at_last_retrain": _current_rows(),
                  "version": target_version, "report": verdict.get("report_path")})
    db.log_event("RETRAIN", f"promoted {target_version} after out-of-sample improvement",
                 meta={"report": verdict.get("report_path"), "candidate": verdict.get("candidate")})
    return {"promoted": True, "version": target_version, "report": verdict.get("report_path")}


def _latest_report() -> dict[str, Any] | None:
    if not REPORTS.exists():
        return None
    files = sorted(REPORTS.glob("retrain_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI glue
    parser = argparse.ArgumentParser(description="Controlled model retraining (spec §13)")
    parser.add_argument("--check", action="store_true", help="report whether a retrain is due")
    parser.add_argument("--evaluate", action="store_true", help="run the candidate and write a verdict")
    parser.add_argument("--promote", action="store_true", help="promote only if the verdict says PROMOTE")
    parser.add_argument("--source", default="csv")
    parser.add_argument("--rows", type=int, default=40000)
    args = parser.parse_args(argv)
    policy = RetrainPolicy.from_env()
    if args.check or not (args.evaluate or args.promote):
        print(json.dumps(due(policy), indent=2, default=str))
    if args.evaluate:
        print(json.dumps(evaluate(policy, rows_per_asset=args.rows, source=args.source), indent=2, default=str))
    if args.promote:
        print(json.dumps(promote(), indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
