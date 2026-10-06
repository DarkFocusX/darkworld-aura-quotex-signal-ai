"""CLI for the model-quality lab (spec §1–§9).

    python -m backend.research.run_experiments --assets USDJPY,USDCHF --source csv --rows 40000
    python -m backend.research.run_experiments --quick          # smoke-sized grid
"""
from __future__ import annotations

import argparse

from .lab import (FL, LabConfig, MODEL_LGBM, MODEL_LOGREG, MODEL_RF, run_lab)  # noqa: F401


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Model quality + paper-trading research lab")
    parser.add_argument("--assets", default="USDJPY,USDCHF", help="comma-separated symbols")
    parser.add_argument("--source", default="csv",
                        choices=["auto", "simulated", "csv", "real_only", "live"])
    parser.add_argument("--rows", type=int, default=40_000, help="max candles per asset")
    parser.add_argument("--horizons", default="1,2,3,5", help="expiry horizons in minutes")
    parser.add_argument("--neutral-atr", type=float, default=0.25, help="neutral band as a fraction of ATR")
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--holdout", type=float, default=0.2)
    parser.add_argument("--models", default="logistic_regression,random_forest,lightgbm",
                        help="model families benchmarked on the headline horizon")
    parser.add_argument("--core-models", default="lightgbm",
                        help="families measured on the other horizons (keeps the grid affordable)")
    parser.add_argument("--no-regime", action="store_true", help="skip the regime study")
    parser.add_argument("--no-ensemble", action="store_true", help="skip the pooled ensemble cells")
    parser.add_argument("--headline-horizon", type=int, default=1)
    parser.add_argument("--quick", action="store_true",
                        help="tiny grid: horizon 1, base features, logreg only (smoke test)")
    args = parser.parse_args(argv)

    if args.quick:
        config = LabConfig(assets=tuple(args.assets.split(",")), rows_per_asset=min(args.rows, 8_000),
                           source=args.source, horizons=(1,), feature_sets=(FL.FEATURE_SET_BASE,),
                           targets=(__import__("backend.research.targets", fromlist=["TARGET_M1_EXPIRY"]).TARGET_M1_EXPIRY,),
                           models=(MODEL_LOGREG,), folds=2)
        return 0 if run_lab(config, regime=False, ensemble=False) else 1

    config = LabConfig(
        assets=tuple(a.strip() for a in args.assets.split(",") if a.strip()),
        rows_per_asset=args.rows,
        source=args.source,
        horizons=tuple(int(h) for h in args.horizons.split(",") if h.strip()),
        neutral_value=args.neutral_atr,
        folds=args.folds,
        holdout_fraction=args.holdout,
        models=tuple(m.strip() for m in args.models.split(",") if m.strip()),
        core_models=tuple(m.strip() for m in args.core_models.split(",") if m.strip()),
    )
    run_lab(config, headline_horizon=args.headline_horizon,
            regime=not args.no_regime, ensemble=not args.no_ensemble)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
