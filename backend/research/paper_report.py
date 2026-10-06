"""Paper-trading research report (spec §15).

Writes `PAPER_TRADING_REPORT.md`: what the *live paper ledger* actually collected,
kept strictly apart from the offline validation numbers (which live in their own
section and are never merged into a win rate).

The report refuses to overstate:

* a win rate without a decided sample is printed as `—`, never as `0 %`;
* the evidence bar is reported as PASSED / NOT PASSED with the exact shortfall;
* regime and confidence distributions come from the ledger rows, not from the
  model's predictions.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..data import store
from ..data.registry import registry
from ..database import db
from ..models.service import get_model_service
from ..signals import ledger, paper as paper_state
from ..timeutil import utc_iso

REPORT_PATH = PROJECT_ROOT / "PAPER_TRADING_REPORT.md"


def _confidence_distribution(rows: list[dict[str, Any]]) -> list[tuple[str, int, int, float | None]]:
    """Bucket → (label, signals, decided, win rate).

    Buckets are the ones the final master spec names: 50–60 … 95–100.  A bucket is only
    reported as meaningful with enough decided trades; the report prints the sample count
    next to every rate so a thin bucket can never be mistaken for evidence.
    """
    buckets = [(50.0, 60.0), (60.0, 70.0), (70.0, 75.0), (75.0, 80.0),
               (80.0, 85.0), (85.0, 90.0), (90.0, 95.0), (95.0, 100.01)]
    out: list[tuple[str, int, int, float | None]] = []
    for low, high in buckets:
        selected = [r for r in rows if r["confidence"] is not None and low <= float(r["confidence"]) < high]
        decided = [r for r in selected if r["status"] in (ledger.WIN, ledger.LOSS)]
        wins = sum(1 for r in decided if r["status"] == ledger.WIN)
        label = f"{low:.0f}–{min(high, 100.0):.0f} %"
        out.append((label, len(selected), len(decided), (wins / len(decided)) if decided else None))
    return out


def _regime_distribution(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Regimes observed at scan time, taken from the recorded scan evidence."""
    counts: dict[str, int] = {}
    for row in rows:
        checks = row.get("checks") or {}
        regime = None
        if isinstance(checks, dict):
            regime = (checks.get("regime") or {}).get("label") if isinstance(checks.get("regime"), dict) else None
        key = str(regime or "NOT RECORDED")
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def collect() -> dict[str, Any]:
    ledger.ensure_schema()
    rows = ledger.rows(limit=1000)
    evidence = ledger.evidence()
    summary = ledger.summary()
    live = paper_state.live_status()
    model = get_model_service().status()
    providers = registry.provider_report()
    return {
        "generated_at_utc": utc_iso(),
        "ledger_rows": rows,
        "evidence": evidence,
        "summary": summary,
        "live_status": live,
        "model": model,
        "providers": providers,
        "store": {asset: {"rows": store.count(asset), "gaps": store.gaps(asset)["missing_minutes"]}
                  for asset in store.assets()},
        "scans": db.list_scans(limit=1),
    }


def render(payload: dict[str, Any]) -> str:
    rows = payload["ledger_rows"]
    evidence = payload["evidence"]
    live = payload["live_status"]
    model = payload["model"]
    period = sorted({r["entry_date_ist"] for r in rows if r.get("entry_date_ist")})
    decided = evidence["total_decided_trades"]
    win_rate = evidence["win_rate"]
    lb = evidence["wilson_95_lower_bound"]

    lines: list[str] = []
    add = lines.append
    add("# Paper-trading report")
    add("")
    add(f"- generated: {payload['generated_at_utc']}")
    add(f"- mode: **PAPER** (no live execution exists in this build)")
    add(f"- collection period: {(period[0] + ' → ' + period[-1]) if period else 'no signals collected yet'}")
    add(f"- assets in the ledger: {', '.join(evidence['assets']) or '—'}")
    add(f"- data providers feeding the ledger: "
        f"{', '.join(sorted({str(r['feed_provider']) for r in rows if r.get('feed_provider')})) or '—'}")
    add("")
    add("> Confidence is a model-derived score, never a guarantee of profit. Offline validation metrics and "
        "live paper results are reported in separate sections and are never combined.")
    add("")
    add("## 1. Live paper results")
    add("")
    add("| metric | value |")
    add("| --- | --- |")
    add(f"| signals issued | {evidence['qualified_signals']} |")
    add(f"| decided trades | {decided} |")
    add(f"| wins | {evidence['wins']} |")
    add(f"| losses | {evidence['losses']} |")
    add(f"| unresolved | {evidence['unresolved_trades']} |")
    add(f"| pending | {evidence['pending_trades']} |")
    add(f"| win rate | {'—' if win_rate is None else f'{win_rate * 100:.1f} %'} |")
    add(f"| Wilson 95 % lower bound | {'—' if lb is None else f'{lb:.4f}'} |")
    add(f"| maximum losing streak | {evidence['max_losing_streak']} |")
    add(f"| scans recorded | {evidence['scans_recorded']} |")
    coverage = evidence["coverage"]
    add("| coverage (signals / scans) | "
        + ("—" if coverage is None else f"{coverage * 100:.2f} %") + " |")
    # One definition, used everywhere: NO_TRADE scans / all recorded scans. `1 - coverage`
    # would silently fold DATA_UNAVAILABLE scans (no trading decision was ever made) into the
    # rejection rate, which would misreport the engine's behaviour.
    no_trade_rate = evidence.get("no_trade_rate")
    add("| no-trade rate (NO_TRADE scans / scans) | "
        + ("—" if no_trade_rate is None else f"{no_trade_rate * 100:.2f} %") + " |")
    counts = evidence.get("scan_status_counts") or {}
    if counts:
        add("| scans by status | "
            + " · ".join(f"{name} {value}" for name, value in sorted(counts.items())) + " |")
    add(f"| locked duplicate attempts | {evidence['locked_attempts']} |")
    add(f"| distinct days | {evidence['days']} |")
    add(f"| distinct assets | {len(evidence['assets'])} |")
    add("")
    add(f"**Evidence bar:** {evidence['evidence_bar']} — "
        f"rule: ≥ {evidence['evidence_rule']['minimum_qualifying_signals']} decided signals and a Wilson 95 % "
        f"lower bound above {evidence['evidence_rule']['wilson_95_lower_bound_above']:.2f}.")
    if evidence["reasons"]:
        add("")
        add("Shortfall: " + "; ".join(evidence["reasons"]) + ".")
    add("")
    add("This report does **not** label the system profitable: nothing here meets the published bar.")
    add("")
    add("## 2. Confidence distribution (ledger rows)")
    add("")
    add("| bucket | signals | decided | win rate |")
    add("| --- | --- | --- | --- |")
    for bucket, count, decided_count, rate in _confidence_distribution(rows):
        add(f"| {bucket} | {count} | {decided_count} | {'—' if rate is None else f'{rate * 100:.1f} %'} |")
    add("")
    add("## 3. Regime distribution (scan evidence)")
    add("")
    regimes = _regime_distribution(rows)
    if regimes:
        add("| regime | signals |")
        add("| --- | --- |")
        for name, count in regimes:
            add(f"| {name} | {count} |")
    else:
        add("No signals were issued, so no regime distribution exists yet.")
    add("")
    add("## 4. Data quality and feed state")
    add("")
    add("| asset | freshness | provider | rows | gaps | simulated | replay |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for row in live["assets"]:
        gaps = row.get("gaps") or {}
        add(f"| {row['asset']} | {row['freshness_status']} | {row.get('provider') or '—'} | "
            f"{row.get('rows', 0)} | {gaps.get('missing_minutes', '—')} | "
            f"{'yes' if row.get('is_simulated') else 'no'} | {'yes' if row.get('is_replay') else 'no'} |")
    add("")
    add("Provider adapters:")
    add("")
    for prov in payload["providers"]:
        add(f"- `{prov['provider']}` — {prov.get('status')}"
            + (f" · {prov.get('detail')}" if prov.get("detail") else ""))
    add("")
    add("## 5. Offline validation (separate from the numbers above)")
    add("")
    add(f"- active artifact: `{model.get('version')}` — {model.get('state')} "
        f"(production usable: {model.get('production_usable')})")
    metrics = (model.get("metrics") or {})
    if metrics:
        add(f"- holdout: accuracy {metrics.get('accuracy')}, macro F1 {metrics.get('macro_f1')}, "
            f"ROC-AUC {metrics.get('roc_auc')}")
    add("- these figures come from the research grid / training report and are **never** mixed into the live "
        "paper win rate above")
    add("")
    add("## 6. Known limitations")
    add("")
    add("- paper outcomes require a verified closing candle; anything else stays UNRESOLVED rather than guessed")
    add("- a simulated feed can exercise the pipeline but can never back a production signal")
    add("- a stale or absent feed produces NO TRADE / DATA UNAVAILABLE — never a fabricated CALL/PUT")
    add("- the model is not validated for profitability; no profit claim is made anywhere in this report")
    add("")
    return "\n".join(lines)


def write_report(path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    payload = collect()
    text = render(payload)
    target = Path(path or REPORT_PATH)
    target.write_text(text, encoding="utf-8")
    return target, payload
