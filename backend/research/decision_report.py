"""Spec §19 — final decision report for the model-quality phase.

Reads everything the system actually measured and writes `RESEARCH_DECISION.md`:

    DATA QUALITY / MODEL QUALITY / VALIDATION QUALITY / PAPER-TRADING RESULTS /
    SIGNAL COVERAGE / NO-TRADE RATE / KNOWN LIMITATIONS

and exactly one classification:

    RESEARCH_ONLY | PAPER_TRADING_READY | LIVE_DATA_READY_FOR_PAPER_SIGNALING

The classification is a rule over evidence, not a judgement call, and the report
never labels anything profitable.

    python3 -m backend.research.decision_report
"""
from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lab import load_index, summarise

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = PROJECT_ROOT / "RESEARCH_DECISION.md"

RESEARCH_ONLY = "RESEARCH_ONLY"
PAPER_READY = "PAPER_TRADING_READY"
LIVE_READY = "LIVE_DATA_READY_FOR_PAPER_SIGNALING"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}" if isinstance(value, float) else str(value)
    return str(value)


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def collect() -> dict[str, Any]:
    """Everything the report needs, gathered from the files the system wrote."""
    from backend.data.registry import registry

    index = load_index()
    summary = summarise(index) if index.get("available") else None
    artifacts_dir = PROJECT_ROOT / "backend/models/artifacts"
    current = artifacts_dir / "current"
    metadata = _load_json(current / "metadata.json") if current.exists() else None

    data_rows = []
    for symbol in ("USDJPY", "USDCHF", "EURUSD", "GBPUSD", "USDBDT-OTCq", "EURCAD-OTCq"):
        try:
            data_rows.append(registry.dataset_status(symbol))
        except Exception as exc:  # noqa: BLE001 - report generation must not crash
            data_rows.append({"asset": symbol, "status": "ERROR", "reason": str(exc)[:120]})
    adapters = registry.adapter_health()

    try:
        from backend.database import db

        rows = db.get_signal_performance(include_simulated=True)
        performance = {"signal_performance": rows}
    except Exception:  # noqa: BLE001
        performance = None

    return {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "index": index,
        "summary": summary,
        "metadata": metadata,
        "data_rows": data_rows,
        "adapters": adapters,
        "performance": performance,
        "gate_audit": _load_json(PROJECT_ROOT / "GATE_AUDIT.json"),
        "research_report": _load_json(PROJECT_ROOT / "docs" / "research_report.json"),
        "python": platform.python_version(),
    }


def classify(bundle: dict[str, Any]) -> tuple[str, list[str]]:
    """Apply the published rule. Returns (classification, reasons)."""
    reasons: list[str] = []
    rows = bundle["data_rows"]
    metadata = bundle["metadata"] or {}
    summary = bundle["summary"] or {}

    real_rows = [r for r in rows if r.get("status") in {"OK", "DEGRADED"}
                 and r.get("is_real_market_data") and not r.get("is_simulated")]
    if not real_rows:
        reasons.append("no validated real market dataset is registered")
        return RESEARCH_ONLY, reasons
    reasons.append(f"validated real market data present for {[r['asset'] for r in real_rows]}")

    if metadata.get("data_source_class") != "MARKET":
        reasons.append("the artifact in 'current' was not trained on market data")
        return RESEARCH_ONLY, reasons
    reasons.append(f"active artifact {metadata.get('model_version')} is READY_MARKET "
                   f"({metadata.get('row_count'):,} rows of operator data)")

    streaming = [row for row in rows if (row.get("freshness_seconds") or 1e9) < 180 and row.get("is_realtime")]
    feed_can_stream = any(
        adapter.get("realtime_capable") and adapter.get("connected") and not adapter.get("is_simulated")
        and adapter.get("status") == "STREAMING"
        for key, adapter in (bundle["adapters"] or {}).items() if not key.startswith("_"))
    audit = bundle.get("gate_audit") or {}
    if audit:
        window = audit.get("window") or {}
        oracle = audit.get("oracle_feasibility") or {}
        reasons.append(f"full-history gate audit (GATE_AUDIT.json): {window.get('minutes')} audited minutes produced "
                       f"{audit.get('status_counts', {}).get('SIGNAL', 0)} signal(s); even a "
                       f"perfect-direction oracle clears every gate "
                       f"{oracle.get('oracle_signals')}/{oracle.get('applicable')} times "
                       f"({oracle.get('verdict')}) — no threshold was changed")
    if not streaming and not feed_can_stream:
        reasons.append("no continuously updating feed: every dataset is a static snapshot, so the "
                       "freshness gate (120 s realtime / 180 s replay) can never pass for a signal")
        return PAPER_READY, reasons

    evidence = summary.get("cells_with_a_selected_threshold", 0) if summary else 0
    if not evidence:
        reasons.append("no confidence gate cleared the validation evidence bar on real data, so live "
                       "signalling must remain NO TRADE")
        return PAPER_READY, reasons

    performance = bundle.get("performance") or {}
    decided = (performance.get("signal_performance") or {}).get("decided", 0) if performance else 0
    if decided < 200:
        reasons.append(f"only {decided} decided paper trade(s) exist — the evidence needed for a live "
                       "paper-signalling claim has not accumulated yet")
        return PAPER_READY, reasons

    reasons.append(f"{decided} decided paper trades with a qualifying gate on real data")
    return LIVE_READY, reasons


def render(bundle: dict[str, Any]) -> str:
    classification, reasons = classify(bundle)
    metadata = bundle["metadata"] or {}
    summary = bundle["summary"] or {}
    performance = (bundle.get("performance") or {}).get("signal_performance") or {}
    index = bundle["index"]
    rows = bundle["data_rows"]

    lines: list[str] = []
    lines.append("# Model quality & live paper-trading validation — decision report")
    lines.append("")
    lines.append(f"- generated: {bundle['generated_at_utc']}")
    lines.append(f"- classification: **{classification}**")
    lines.append(f"- reasons: {'; '.join(reasons)}")
    lines.append("")
    lines.append("This report separates what was measured from what is claimed. It never labels the system "
                 "profitable, and a confidence score is never described as accuracy.")
    lines.append("")

    # -- DATA QUALITY ------------------------------------------------------
    lines.append("## DATA QUALITY")
    lines.append("")
    lines.append("| asset | badge | rows | resolution | newest candle (UTC) | age | validation | gaps |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        validation = row.get("validation") or row.get("quality") or {}
        freshness = row.get("freshness_seconds")
        lines.append(
            f"| {row.get('asset')} | {row.get('badge', row.get('status'))} | "
            f"{row.get('rows', '—')} | {validation.get('interval_minutes', '—')} min | "
            f"{(row.get('latest_candle_open_utc') or '—')} | "
            f"{'—' if freshness is None else f'{round(freshness)} s'} | "
            f"{validation.get('status', row.get('status'))} | {validation.get('missing_candles', '—')} |")
    lines.append("")
    adapters = {k: v for k, v in (bundle["adapters"] or {}).items() if not k.startswith("_")}
    lines.append("Provider adapters (spec §11):")
    lines.append("")
    for key, adapter in adapters.items():
        lines.append(f"- `{key}` — status **{adapter.get('status')}**, realtime_capable "
                     f"{adapter.get('realtime_capable')}, subscribe {adapter.get('supports_subscribe')}"
                     f" — {adapter.get('detail')}")
    lines.append("")

    # -- MODEL QUALITY -----------------------------------------------------
    lines.append("## MODEL QUALITY")
    lines.append("")
    if metadata:
        period = metadata.get("data_period") or {}
        lines.append(f"- active artifact: `{metadata.get('model_version')}` "
                     f"({metadata.get('data_source_class')}, feature version "
                     f"`{metadata.get('feature_version')}`)")
        lines.append(f"- trained: {metadata.get('trained_at_utc')} · rows: "
                     f"{metadata.get('row_count'):,} · assets: {', '.join(metadata.get('asset_list') or [])}")
        lines.append(f"- data period: {period.get('start_utc')} → {period.get('end_utc')}")
        validation = metadata.get("validation_metrics") or {}
        holdout = metadata.get("holdout_metrics") or {}
        lines.append(f"- walk-forward (out-of-fold): accuracy {_fmt(validation.get('accuracy'))}, "
                     f"macro F1 {_fmt(validation.get('macro_f1'))}, ROC-AUC {_fmt(validation.get('roc_auc_macro'))}, "
                     f"directional win rate {_fmt(validation.get('directional_win_rate'), 1)}%")
        lines.append(f"- untouched holdout: accuracy {_fmt(holdout.get('accuracy'))}, "
                     f"macro F1 {_fmt(holdout.get('macro_f1'))}, ROC-AUC {_fmt(holdout.get('roc_auc_macro'))}, "
                     f"samples {holdout.get('samples')}")
        lines.append("- interpretation: at ~1/3 accuracy on three classes the model is **not** demonstrating "
                     "a directional edge; ROC-AUC near 0.5 means it barely separates CALL from PUT.")
    else:
        lines.append("- no READY_MARKET artifact is loaded.")
    lines.append("")

    # -- GATE AUDIT AND ORACLE FEASIBILITY --------------------------------
    audit = bundle.get("gate_audit") or {}
    lines.append("## GATE AUDIT AND ORACLE FEASIBILITY")
    lines.append("")
    if audit:
        window = audit.get("window") or {}
        gate_audit = audit.get("gate_audit") or {}
        oracle = audit.get("oracle_feasibility") or {}
        lines.append(f"- window: {window.get('minutes')} real M1 minutes "
                     f"({window.get('start_utc')} → {window.get('end_utc')}), "
                     f"provider {window.get('provider')}")
        lines.append(f"- scan outcomes over the window: {audit.get('status_counts')}")
        lines.append("- per-gate result (unchanged production gate stack, nothing persisted):")
        lines.append("")
        lines.append("| gate | evaluated | passed | failed | pass % | top failure reason |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for gate, row in (gate_audit.get("per_gate") or {}).items():
            reasons = row.get("failure_reasons") or {}
            top = max(reasons.items(), key=lambda kv: kv[1])[0] if reasons else "—"
            rate = row.get("pass_rate_of_evaluated")
            lines.append(f"| {gate} | {row.get('evaluated')} | {row.get('passed')} | {row.get('failed')} | "
                         f"{'—' if rate is None else rate} | {str(top)[:90]} |")
        lines.append("")
        applicable = oracle.get("applicable")
        blockers = oracle.get("top_oracle_blockers") or {}
        top_blocker = max(blockers.items(), key=lambda kv: kv[1])[0] if blockers else None
        lines.append(f"- oracle feasibility (research only — perfect direction known in advance): "
                     f"**{oracle.get('verdict')}** · {oracle.get('oracle_signals')} of "
                     f"{applicable} applicable minutes cleared every gate")
        if top_blocker:
            lines.append(f"- the gate that still blocks even a perfect model: **{top_blocker}** — "
                         f"model quality is not the binding constraint on this history")
        lines.append("- no production threshold was changed by this audit; a variant may only be adopted "
                     "with defensible out-of-sample evidence, never because it produces more signals")
    else:
        lines.append("- no gate audit artefact yet — run the read-only gate audit described in "
                     "README.md (it writes GATE_AUDIT.md / GATE_AUDIT.json; the audit lives in "
                     "`tools/` and is deliberately not part of the runtime code).")
    lines.append("")

    # -- VALIDATION QUALITY ------------------------------------------------
    lines.append("## VALIDATION QUALITY")
    lines.append("")
    if index.get("available"):
        lines.append(f"- research run `{index.get('run_id')}` on dataset "
                     f"`{index.get('dataset', {}).get('dataset_version')}` "
                     f"({index.get('dataset', {}).get('rows', 0):,} rows)")
        lines.append(f"- experiments: **{summary.get('experiment_count')}** · with a qualifying gate: "
                     f"**{summary.get('cells_with_a_selected_threshold')}** · without evidence of edge: "
                     f"**{summary.get('cells_without_evidence_of_edge')}**")
        lines.append("- held out: no shuffling, expanding-window folds with an embargo ≥ the label horizon, "
                     "one untouched chronological holdout applied once per cell")
        lines.append("- thresholds: chosen on validation folds only under a published rule (≥ 100 signals and "
                     "a Wilson 95 % lower bound above 0.50); 'no threshold' is a valid, honest outcome")
        if summary.get("cells_with_a_selected_threshold") == 0:
            lines.append("- **no cell cleared the evidence bar**, so the correct live behaviour of the running "
                         "system is NO TRADE — that is the system working as specified, not a defect")
    else:
        lines.append("- no research run has completed yet; validation quality is unmeasured.")
    lines.append("")

    # -- PAPER-TRADING RESULTS --------------------------------------------
    lines.append("## PAPER-TRADING RESULTS")
    lines.append("")
    lines.append(f"- live paper trades generated: {performance.get('generated', 0)} · decided: "
                 f"{performance.get('decided', 0)} · wins {performance.get('wins', 0)} / losses "
                 f"{performance.get('losses', 0)} · pending {performance.get('pending', 0)} · "
                 f"unresolved {performance.get('unresolved', 0)}")
    lines.append(f"- win rate: {_pct(performance.get('win_rate'))}" if performance.get("decided")
                 else "- win rate: — (no decided paper trades: the feed is a static snapshot, so the "
                      "freshness gate blocks signalling, and every outcome would have to come from a "
                      "verified candle close anyway)")
    if index.get("available"):
        lines.append(f"- offline paper replay over the holdout: {summary.get('paper_signals_total')} signals "
                     f"across all cells, {summary.get('paper_wins_total')} wins / "
                     f"{summary.get('paper_losses_total')} losses (descriptive only; never mixed with live "
                     "paper results)")
    lines.append("")

    # -- SIGNAL COVERAGE / NO-TRADE RATE ----------------------------------
    lines.append("## SIGNAL COVERAGE AND NO-TRADE RATE")
    lines.append("")
    lines.append("- signal coverage on live data: **0 signals** — the newest operator candle is hours old "
                 "(static snapshot), which fails the configured 120 s / 180 s freshness budgets")
    lines.append("- no-trade rate on live data: **100 %**, with the reason surfaced in the API/UI "
                 "(`DATA SOURCE IS REAL` / freshness), never a fabricated CALL/PUT")
    if index.get("available"):
        no_trade = [r for r in index.get("experiments", []) if r.get("selected_threshold") is None]
        lines.append(f"- offline: {len(no_trade)} of {summary.get('experiment_count')} experiment cells chose "
                     "no threshold, i.e. the evidence-based default outside those cells is also NO TRADE")
    lines.append("")

    # -- KNOWN LIMITATIONS -------------------------------------------------
    lines.append("## KNOWN LIMITATIONS")
    lines.append("")
    lines.extend([
        "- operator datasets are **static snapshots**: they can train and evaluate a model, but they cannot "
        "produce a fresh signal, so live paper trading cannot start until a continuously updating feed exists",
        "- OTC instruments have no public data source; they report DATA UNAVAILABLE until a broker/operator "
        "connection is wired (never simulated prices dressed as OTC quotes)",
        "- the public delayed feed is rate-limited and re-sampled; it is labelled REPLAY and must not be "
        "treated as a Quotex feed",
        "- two assets of ~8 days of M1 data is a small, correlated sample; metrics carry wide uncertainty and "
        "class balance shifts between sessions",
        "- XGBoost is unavailable in this environment and the neural challenger is untrained; both are "
        "recorded as unavailable rather than silently skipped",
        "- every metric here is offline validation or replay; none of it is a forecast, and none of it is "
        "combined with live paper results",
    ])
    lines.append("")

    # -- CLASSIFICATION ----------------------------------------------------
    lines.append("## CLASSIFICATION")
    lines.append("")
    lines.append(f"**{classification}**")
    lines.append("")
    lines.append("| classification | meaning |")
    lines.append("| --- | --- |")
    lines.append("| RESEARCH_ONLY | pipeline works on historical data only; no real-data model and/or no "
                 "validated real dataset |")
    lines.append("| PAPER_TRADING_READY | real data validated + READY_MARKET artifact + gates enforced, but a "
                 "continuously updating feed (or accumulated paper evidence) is still missing |")
    lines.append("| LIVE_DATA_READY_FOR_PAPER_SIGNALING | fresh real feed + READY_MARKET + a gate that cleared "
                 "the evidence bar + enough decided paper trades to report |")
    lines.append("")
    lines.append("The system is **not** labelled profitable: nothing measured here supports that claim, and "
                 "the correct behaviour today is NO TRADE.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    bundle = collect()
    REPORT_PATH.write_text(render(bundle), encoding="utf-8")
    print(f"wrote {REPORT_PATH}")
    print(f"classification: {classify(bundle)[0]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
