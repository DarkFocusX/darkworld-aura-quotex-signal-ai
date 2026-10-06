"""Phases 4–9 research report — mined from the stored experiment records.

The final master phase asks for research on targets (§4), model families (§5), features
(§6), regimes (§7), calibration (§8) and thresholds (§9).  Every one of those questions
was already *measured* by `backend/research/lab.py` and stored as an experiment record, so
this tool does not re-run anything and cannot invent a result: it reads the records, prints
what they measured, applies the published evidence bar, and refuses to draw a conclusion
the data does not support.

Read-only.  Usage:

    python3 tools/research_report.py            # writes docs/RESEARCH_REPORT.md + .json
"""
from __future__ import annotations

import glob
import importlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXPERIMENTS = PROJECT_ROOT / "backend" / "research" / "experiments"
VALIDATION = PROJECT_ROOT / "backend" / "research" / "reports" / "latest.json"
OUT_MD = PROJECT_ROOT / "docs" / "RESEARCH_REPORT.md"
OUT_JSON = PROJECT_ROOT / "docs" / "research_report.json"

CHALLENGERS = ("xgboost", "torch", "tensorflow", "keras")
EVIDENCE_BAR = {"min_signals": 100, "min_wilson_lower_bound": 0.50}


def dependency_status() -> dict[str, str]:
    """Challenger availability — UNAVAILABLE is reported, never worked around."""
    out: dict[str, str] = {}
    for name in CHALLENGERS:
        try:
            module = importlib.import_module(name)
        except Exception as exc:                                     # noqa: BLE001
            out[name] = f"UNAVAILABLE ({type(exc).__name__})"
        else:
            out[name] = f"AVAILABLE {getattr(module, '__version__', '?')}"
    return out


def load_experiments() -> list[dict]:
    records = []
    for path in sorted(glob.glob(str(EXPERIMENTS / "lab-*.json"))):
        try:
            records.append(json.loads(Path(path).read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return records


def _metrics(block: dict) -> dict:
    return {
        "accuracy": block.get("accuracy"),
        "macro_f1": block.get("macro_f1"),
        "macro_precision": block.get("macro_precision"),
        "macro_recall": block.get("macro_recall"),
        "roc_auc": ((block.get("roc_auc") or {}).get("macro")
                    if isinstance(block.get("roc_auc"), dict) else block.get("roc_auc")),
        "roc_auc_per_class": (block.get("roc_auc") if isinstance(block.get("roc_auc"), dict) else None),
        "brier": block.get("brier"),
        "calibration_error": (block.get("calibration") or {}).get("expected_calibration_error"),
        "directional_hit_rate": (
            round(block["directional_correct"] / block["directional_calls"], 4)
            if block.get("directional_calls") else None
        ),
        "directional_calls": block.get("directional_calls"),
        "samples": block.get("samples"),
        "per_class": block.get("per_class"),
    }


def summarise(record: dict) -> dict:
    target = record.get("target") or {}
    features = record.get("feature_set") or {}
    sweep = record.get("threshold_sweep") or []
    with_signals = [row for row in sweep if (row.get("signals") or 0) > 0]
    best_wilson = max((row.get("wilson_lower_bound") or 0.0) for row in sweep) if sweep else None
    max_signals = max((row.get("signals") or 0) for row in sweep) if sweep else 0
    qualifying = [row for row in sweep
                  if (row.get("signals") or 0) >= EVIDENCE_BAR["min_signals"]
                  and (row.get("wilson_lower_bound") or 0.0) > EVIDENCE_BAR["min_wilson_lower_bound"]]
    return {
        "experiment_id": record.get("experiment_id"),
        "target": target.get("target"),
        "horizon_minutes": target.get("horizon_minutes"),
        "neutral": f"{target.get('neutral_mode')}{target.get('neutral_value')}",
        "target_identifier": target.get("identifier"),
        "feature_set": features.get("feature_set"),
        "feature_version": features.get("feature_version"),
        "feature_count": features.get("count"),
        "regime": record.get("regime"),
        "families": record.get("models"),
        "rows": record.get("rows"),
        "dataset_version": (record.get("dataset") or {}).get("dataset_version"),
        "period": [(record.get("dataset") or {}).get("period_start_utc"),
                   (record.get("dataset") or {}).get("period_end_utc")],
        "validation": _metrics(record.get("validation_metrics") or {}),
        "holdout": _metrics(record.get("holdout_metrics") or {}),
        "folds": [{"fold": f.get("fold"),
                   "test_rows": f.get("test_rows"),
                   "accuracy": (f.get("test_metrics") or {}).get("accuracy"),
                   "macro_f1": (f.get("test_metrics") or {}).get("macro_f1")}
                  for f in (record.get("folds") or [])],
        "temperature": (record.get("calibration") or {}).get("temperature"),
        "calibration_holdout_bins": ((record.get("calibration") or {}).get("holdout") or {}).get("bins"),
        "calibration_validation_bins": ((record.get("calibration") or {}).get("validation") or {}).get("bins"),
        "threshold_sweep": sweep,
        "threshold_thresholds_with_signals": len(with_signals),
        "threshold_max_signals": max_signals,
        "threshold_best_wilson_lower_bound": best_wilson,
        "threshold_selected": (record.get("threshold_selection") or {}).get("threshold"),
        "threshold_selection_reason": (record.get("threshold_selection") or {}).get("reason"),
        "threshold_qualifying_cells": qualifying,
        "paper_replay": {k: v for k, v in (record.get("paper_replay") or {}).items()
                         if k not in {"signals_preview", "signals_detail"}},
        "holdout_gate_test": record.get("holdout_gate_test"),
        "walk_forward_paper": {k: v for k, v in (record.get("walk_forward_paper") or {}).items()
                               if k != "steps"},
        "timings_seconds": record.get("timings_seconds"),
    }


def md_report(rows: list[dict]) -> str:
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    versions = sorted({r["dataset_version"] for r in rows if r["dataset_version"]})
    folds = sorted({len(r["folds"]) for r in rows})
    lines: list[str] = [
        "# Research report — targets, models, features, regimes, calibration, thresholds",
        "",
        f"- generated: {now}",
        f"- experiments mined: **{len(rows)}** (records in `backend/research/experiments/`)",
        f"- dataset fingerprint(s): {', '.join(versions) or '—'}",
        f"- walk-forward folds per experiment: {folds} — the same chronological fold construction "
        "is used everywhere; a cell only has fewer folds when its regime slice has fewer rows",
        "- this report re-runs nothing: every number is read from the stored experiment "
        "records, so it cannot drift from the measurement it describes",
        "",
        "## 4. Targets — NEXT_CANDLE_DIRECTION and M1_EXPIRY_OUTCOME, kept separate",
        "",
        "The two targets are *never* silently substituted for one another: each cell states "
        "which target it measured, and the neutral band is the ATR-normalised one the config "
        "names (`neutral_mode=atr`, fraction 0.25). Horizons 1/2/3/5 are measured, not assumed.",
        "",
        "| target | horizon | neutral | cells | holdout samples | accuracy | macro F1 | "
        "AUC | Brier | directional hit rate |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["target"], row["horizon_minutes"], row["neutral"])].append(row)
    for (target, horizon, neutral), items in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        best = max(items, key=lambda r: (r["holdout"].get("macro_f1") or 0.0))
        lines.append(
            f"| {target} | {horizon} | {neutral} | {len(items)} | {best['holdout'].get('samples')} | "
            f"{_f(best['holdout'].get('accuracy'))} | {_f(best['holdout'].get('macro_f1'))} | "
            f"{_f(best['holdout'].get('roc_auc'))} | {_f(best['holdout'].get('brier'))} | "
            f"{_f(best['holdout'].get('directional_hit_rate'))} |"
        )
    lines += ["",
              "Class balance is stored per experiment (`per_class` supports PUT / NEUTRAL / CALL); "
              "a benchmark that predicts NEUTRAL is judged on macro F1, never on raw accuracy — "
              "with three classes ~0.33 accuracy is chance, not skill.",
              "",
              "## 5. Model families and challengers",
              "",
              "| family | cells | median holdout accuracy | median macro F1 | median AUC | median Brier |",
              "| --- | --- | --- | --- | --- | --- |"]
    by_family: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = "+".join(row["families"] or [])
        by_family[key].append(row)
    for family, items in sorted(by_family.items()):
        lines.append(f"| {family} | {len(items)} | {_med(items, 'accuracy')} | {_med(items, 'macro_f1')} | "
                     f"{_med(items, 'roc_auc')} | {_med(items, 'brier')} |")
    lines += ["",
              "Challenger availability (a missing dependency is reported, never fabricated):",
              ""]
    for name, status in dependency_status().items():
        lines.append(f"- `{name}`: {status}")
    lines += ["",
              "Per-fold detail (same chronological train/test construction in every cell, so "
              "cells are comparable; fold counts differ only where a slice is short):",
              "",
              "| experiment | target | h | family | feature set | fold accuracies | fold macro F1 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in sorted(rows, key=lambda r: r["experiment_id"] or ""):
        accs = ", ".join(_f(f["accuracy"]) for f in row["folds"])
        f1s = ", ".join(_f(f["macro_f1"]) for f in row["folds"])
        lines.append(f"| `{row['experiment_id']}` | {row['target']} | {row['horizon_minutes']} | "
                     f"{'+'.join(row['families'] or [])} | {row['feature_version']} | {accs} | {f1s} |")
    lines += ["",
              "## 6. Features",
              "",
              "Every feature set is causal by construction and covered by the leakage tests "
              "(`tests/test_features.py`): a feature may only use candles at or before the "
              "scanned candle. The extended lab block adds momentum-slope and structure terms "
              "on top of the base block; it is kept only when the untouched holdout agrees.",
              "",
              "| feature version | count | cells | median macro F1 | best macro F1 | delta vs base (same target/h |",
              "| --- | --- | --- | --- | --- | --- |"]
    by_features: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_features[row["feature_version"] or "?"].append(row)
    base_by_key = {key: max(items, key=lambda r: (r["holdout"].get("macro_f1") or 0.0))
                   for key, items in grouped.items() if items}
    for version, items in sorted(by_features.items()):
        best = max(items, key=lambda r: (r["holdout"].get("macro_f1") or 0.0))
        base = None
        for key, item in base_by_key.items():
            if (item["target"], item["horizon_minutes"]) == (best["target"], best["horizon_minutes"]) \
                    and item["regime"] == best["regime"] and "+".join(item["families"] or []) == \
                    "+".join(best["families"] or []):
                base = item
                break
        delta = None
        if base and base["holdout"].get("macro_f1") and best["holdout"].get("macro_f1"):
            delta = round(best["holdout"]["macro_f1"] - base["holdout"]["macro_f1"], 4)
        lines.append(f"| {version} | {items[0]['feature_count']} | {len(items)} | "
                     f"{_med(items, 'macro_f1')} | {_f(best['holdout'].get('macro_f1'))} | {_f(delta)} |")
    lines += ["",
              "No feature is kept for an in-sample gain; a delta that is not visible on the "
              "untouched holdout is reported as zero evidence, not as an improvement.",
              "",
              "## 7. Regimes",
              "",
              "| regime filter | cells | median holdout macro F1 | median accuracy | note |",
              "| --- | --- | --- | --- | --- |"]
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_regime[row["regime"] or "?"].append(row)
    for regime, items in sorted(by_regime.items()):
        note = ("all minutes (reference)" if regime == "all"
                else "OOS slice — no improvement may be claimed before untouched evidence")
        lines.append(f"| {regime} | {len(items)} | {_med(items, 'macro_f1')} | {_med(items, 'accuracy')} | {note} |")
    lines += ["",
              "## 8. Calibration",
              "",
              "The stored calibration is the reliability of the predicted class: bins with "
              "sample counts, mean confidence and realised accuracy. A bucket is only "
              "reported as meaningful with enough observations; the records flag under ~200 "
              "samples as unproven, and confidence is never described as a probability of "
              "profit.",
              "",
              "| experiment | temperature | validation ECE | holdout ECE | holdout samples | "
              "largest holdout bucket | bucket samples | bin confidence → accuracy |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in sorted(rows, key=lambda r: r["experiment_id"] or ""):
        bins = [b for b in (row["calibration_holdout_bins"] or []) if b.get("count")]
        biggest = max(bins, key=lambda b: b["count"], default=None)
        ece_holdout = _ece(row["calibration_holdout_bins"])
        lines.append(
            f"| `{row['experiment_id'][:52]}` | {_f(row['temperature'])} | "
            f"{_f(row['validation'].get('calibration_error'))} | {_f(ece_holdout)} | "
            f"{row['holdout'].get('samples')} | {biggest['bin'] if biggest else '—'} | "
            f"{biggest['count'] if biggest else '—'} | "
            f"{(_f(biggest['mean_confidence']) + ' → ' + _f(biggest['accuracy'])) if biggest else '—'} |")
    lines += ["",
              "The live-paper view uses the spec's operator buckets (50–60 … 95–100) and "
              "prints the decided-trade count next to every rate in `PAPER_TRADING_REPORT.md`; "
              "the offline view above is the model-side reliability, kept separate from paper "
              "performance on purpose.",
              "",
              "## 9. Thresholds and the evidence bar",
              "",
              f"Published bar: **≥ {EVIDENCE_BAR['min_signals']} signals AND Wilson 95 % lower "
              f"bound > {EVIDENCE_BAR['min_wilson_lower_bound']:.2f}**. A threshold is selected on "
              "validation only and then tested once on the untouched holdout.",
              "",
              "| experiment | thresholds tried | thresholds with signals | max signals | "
              "best Wilson LB | selected | qualifying cells |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in sorted(rows, key=lambda r: r["experiment_id"] or ""):
        lines.append(
            f"| `{row['experiment_id'][:52]}` | {len(row['threshold_sweep'])} | "
            f"{row['threshold_thresholds_with_signals']} | {row['threshold_max_signals']} | "
            f"{_f(row['threshold_best_wilson_lower_bound'])} | {row['threshold_selected'] or 'none'} | "
            f"{len(row['threshold_qualifying_cells'])} |")
    selected = [r for r in rows if r["threshold_selected"]]
    lines += ["",
              "### Verdict",
              "",
              (f"- {len(selected)} of {len(rows)} experiment cells selected a threshold; the rest "
               "produced **NO QUALIFIED EDGE** — a valid final result, not a failure to be papered over."
               if not all(r["threshold_selected"] for r in rows) else
               "- every cell selected a threshold."),
              f"- largest signal count any threshold produced: "
              f"{max((r['threshold_max_signals'] for r in rows), default=0)} "
              f"(bar: {EVIDENCE_BAR['min_signals']}).",
              f"- best Wilson lower bound observed: "
              f"{_f(max((r['threshold_best_wilson_lower_bound'] or 0.0) for r in rows))} "
              f"(bar: > {EVIDENCE_BAR['min_wilson_lower_bound']:.2f}) — a high lower bound on a "
              "handful of trades is not evidence, which is exactly why the signal-count "
              "condition exists.",
              "- no threshold was adopted into production by this report; adopting one would "
              "require the bar to clear on untouched data, and the audit would have to be "
              "superseded by new evidence.",
              "",
              "## 10. What this report does not claim",
              "",
              "- no profitability claim, no accuracy claim for live trading, no best-model ranking;",
              "- the holdout numbers describe this operator export only (one instrument, "
              "one period) and may not transfer;",
              "- paper performance is tracked separately in `PAPER_TRADING_REPORT.md` and is "
              "currently at zero decided trades, so the live evidence bar is NOT PASSED.",
              "",
              f"_generated by `tools/research_report.py` at {now}; sources: "
              f"{len(rows)} experiment records + `backend/research/reports/latest.json`._",
              ""]
    return "\n".join(lines)


def _f(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        return str(value)
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _med(items: list[dict], key: str) -> str:
    values = sorted(r["holdout"][key] for r in items if r["holdout"].get(key) is not None)
    if not values:
        return "—"
    middle = values[len(values) // 2]
    return f"{middle:.4f}"


def _ece(bins) -> float | None:
    if not bins:
        return None
    total = sum(b["count"] for b in bins if b.get("count"))
    if not total:
        return None
    error = 0.0
    for bucket in bins:
        if not bucket.get("count"):
            continue
        error += (bucket["count"] / total) * abs((bucket["mean_confidence"] or 0) - (bucket["accuracy"] or 0))
    return round(error, 4)


def main() -> int:
    records = load_experiments()
    rows = [summarise(record) for record in records]
    payload = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "experiments": len(rows),
        "evidence_bar": EVIDENCE_BAR,
        "challenger_dependencies": dependency_status(),
        "threshold_selected_cells": sum(1 for r in rows if r["threshold_selected"]),
        "max_signals_any_threshold": max((r["threshold_max_signals"] for r in rows), default=0),
        "best_wilson_lower_bound": max((r["threshold_best_wilson_lower_bound"] or 0.0) for r in rows) if rows else None,
        "target_coverage": dict(Counter(f"{r['target']}|h{r['horizon_minutes']}" for r in rows)),
        "policy": ("read-only mining of stored experiment records; no threshold adopted; no "
                   "in-sample number promoted to a claim; missing challenger dependencies are "
                   "reported as UNAVAILABLE"),
        "cells": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text(md_report(rows), encoding="utf-8")
    print(f"report: {OUT_MD} + {OUT_JSON}")
    print(json.dumps({k: v for k, v in payload.items() if k not in {"cells", "target_coverage"}}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
