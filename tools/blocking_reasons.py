"""Why did the engine say NO TRADE? — a read-only diagnostic over REAL minutes.

The §18 sweep (`tools/find_qualified_minute.py`) answers "did any minute clear every
gate?".  This tool answers the follow-up question honestly: *which* gate stopped each
minute, and was the model layer the only remaining blocker?

Nothing here writes a signal: every scan runs with `persist=False` and the production
gate stack unchanged.  Output → `docs/blocking_reasons.json`, which the §18 harness
reads so scenario C/E/F can point at a real minute instead of an invented one.

    python3 tools/blocking_reasons.py --minutes 240
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT = PROJECT_ROOT / "docs" / "blocking_reasons.json"
STUDY = PROJECT_ROOT / "docs" / "gate_study.jsonl.gz"
DIAGNOSTICS = PROJECT_ROOT / "docs" / "gate_diagnostics.json"
AUDIT_MD = PROJECT_ROOT / "GATE_AUDIT.md"
NEAR_MISS_MARKER = "## 5. Near-miss mining — the minutes that came closest"

from tools.harness_model import QualifiedModelLayer  # noqa: E402

# Blocker families.  A minute is "model-only" when every reason it produced belongs to
# the model layer — i.e. real market data, freshness, indicators, regime and anomaly
# checks all passed, and only the learned model's probability/agreement was too weak.
MODEL_KEYWORDS = (
    "directional probability",
    "confidence",
    "model agreement",
    "model not ready",
    "model inference",
    "conflicting indicators",
)
DATA_KEYWORDS = (
    "fresh",
    "stale",
    "candle",
    "gap",
    "anomal",
    "volatil",
    "atr",
    "spread",
    "regime",
    "trend",
    "session",
    "risk",
    "lock",
)


def classify(reason: str) -> str:
    low = reason.lower()
    if any(word in low for word in DATA_KEYWORDS):
        return "data/regime"
    if any(word in low for word in MODEL_KEYWORDS):
        return "model"
    return "other"


def scan_minutes(minutes: int) -> list[dict]:
    from backend.data import store
    from backend.database import db as db_module
    from backend.settings_service import get_settings
    from backend.signals.engine import SignalEngine

    db_module.init_db()
    store.init_store()

    # Ingest more history than we scan: the engine needs >= 120 prior closed candles,
    # so a window scanned without warm-up would report "Insufficient candles" instead of
    # the real gate — an artefact, not a finding.
    # The feature warm-up consumes ~163 rows before the engine counts 120 usable candles,
    # so the ingest window must be wider than the scanned window or the earliest minutes
    # would report a warm-up artefact ("Insufficient candles") instead of the real gate.
    frame = _real_frame(minutes + 320)
    store.upsert_candles(_rows(frame, "operator_csv"), retention_minutes=0)   # keep the whole window

    settings = get_settings()
    settings.environment = "production"
    settings.data_source_mode = "real_only"
    engine = SignalEngine(settings)
    from backend.models.service import get_model_service

    real_service = get_model_service()
    hypothetical = SignalEngine(settings)
    hypothetical.model_service = lambda: QualifiedModelLayer(real_service)

    rows: list[dict] = []
    for stamp in frame.index[-minutes:]:
        moment = stamp.to_pydatetime() + timedelta(seconds=5)
        scan = engine.scan_asset("USDJPY", now=moment, persist=False)
        checks = scan.checks or {}
        reasons = [str(r) for r in (checks.get("reasons") or [])]
        payload = scan.components or {}
        components = payload.get("components") or {}
        # Exact (measured, not estimated) counterfactual: the same real candles, features,
        # rules, gates and confidence arithmetic, with only the learned model layer
        # replaced by a confident unanimous ensemble.  Separation of concerns: this says
        # "the market context would have carried a signal if the model were strong",
        # never "the model was strong".
        with_model = hypothetical.scan_asset("USDJPY", now=moment, persist=False)
        rows.append({
            "minute_utc": str(stamp),
            "status": scan.status,
            "confidence": scan.confidence,
            "direction": scan.direction,
            "reasons": reasons,
            "families": sorted({classify(r) for r in reasons}),
            "components": components,
            "with_qualified_model_layer": {
                "status": with_model.status,
                "confidence": with_model.confidence,
                "gate_reasons": [str(r) for r in ((with_model.checks or {}).get("reasons") or [])],
            },
        })
    return rows


def analyse(rows: list[dict]) -> dict:
    histogram = Counter()
    for row in rows:
        for reason in row["reasons"] or ["<none>"]:
            histogram[_normalise(reason)] += 1
    model_only = [r for r in rows if r["families"] == ["model"]]
    best = sorted(
        model_only or [r for r in rows if r["status"] == "NO_TRADE"],
        key=lambda r: (r["confidence"] or 0.0),
        reverse=True,
    )[:5]
    hypothetical = [r for r in rows if r.get("with_qualified_model_layer")]
    by_ceiling = sorted(
        (r for r in hypothetical if r["with_qualified_model_layer"]["confidence"] is not None),
        key=lambda r: r["with_qualified_model_layer"]["confidence"], reverse=True)[:5]
    return {
        "minutes": len(rows),
        "best_observed_confidence": max((r["confidence"] or 0.0) for r in rows) if rows else None,
        "best_confidence_with_a_qualified_model_view": max(
            (r["with_qualified_model_layer"]["confidence"] for r in hypothetical
             if r["with_qualified_model_layer"]["confidence"] is not None), default=None),
        "minutes_that_would_clear_every_gate_with_a_qualified_model_view": sum(
            1 for r in hypothetical if r["with_qualified_model_layer"]["status"] == "SIGNAL"
        ),
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "reason_histogram": dict(histogram.most_common(20)),
        "model_only_blocker_minutes": len(model_only),
        # Which REAL minutes would qualify if the learned model layer were strong enough?
        # Ranked by measured counterfactual confidence; §18 scenario E looks here first.
        "best_minutes_with_a_qualified_model_view": [
            {"minute_utc": r["minute_utc"], "status": r["status"], "confidence": r["confidence"],
             "with_qualified_model_layer": r["with_qualified_model_layer"],
             "reasons": r["reasons"]}
            for r in by_ceiling
        ],
        "closest_minutes": [
            {
                "minute_utc": r["minute_utc"],
                "status": r["status"],
                "confidence": r["confidence"],
                "reasons": r["reasons"],
                "families": r["families"],
            }
            for r in best
        ],
    }


# --------------------------------------------------------------- full-history mining
def mine_study(path: Path = STUDY, detail: int = 15) -> dict:
    """Read the completed full-history gate audit and rank the *near misses*.

    This is the strongest available answer to "was anything close?": every real minute of
    the operator export is already scored there, so the ranking needs no new scans and no
    invented setup.  A minute is a *model-only* miss when every gate apart from MODEL and
    CONFIDENCE passed — real data, freshness, candles, anomaly, regime, indicator
    consistency, risk and lock all fine, and only the learned model was too weak.
    """
    import gzip

    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    non_model = ("DATA", "FEATURE", "ANOMALY", "REGIME", "CONSISTENCY", "RISK", "LOCK")
    model_only, one_gate_away, oracle_clear = [], {}, []
    for row in rows:
        gates = row.get("gates") or {}
        failed = [gate for gate in non_model if gates.get(gate) == 0]
        if not failed:
            model_only.append(row)
        elif len(failed) == 1:
            one_gate_away.setdefault(failed[0], []).append(row)
        oracle = row.get("oracle") or {}
        if oracle.get("applicable") and oracle.get("status") == "SIGNAL":
            oracle_clear.append(row)

    def rank(rows_):
        return sorted(rows_, key=lambda r: (r.get("c") or 0.0), reverse=True)[:detail]

    def brief(row):
        gates = row.get("gates") or {}
        return {
            "minute_utc": row.get("m"),
            "status": row.get("s"),
            "confidence": row.get("c"),
            "failing_gates": [gate for gate in gates if gates.get(gate) == 0],
            "reasons": row.get("reasons") or {},
            "direction_probability": row.get("p"),
            "agreement": row.get("ag"),
            "oracle": (row.get("oracle") or {}).get("status"),
        }

    summary = {
        "minutes": len(rows),
        "status_counts": dict(Counter(row.get("s") for row in rows)),
        "model_only_blocker_minutes": len(model_only),
        "best_model_only_minutes": [brief(row) for row in rank(model_only)],
        "minutes_one_gate_away": {gate: len(items) for gate, items in sorted(one_gate_away.items())},
        "best_one_gate_away": [
            {"blocked_by": gate, **brief(row)}
            for gate, items in sorted(one_gate_away.items())
            for row in rank(items)[:3]
        ],
        "oracle_clears_every_gate": len(oracle_clear),
        "oracle_cleared_minutes": [brief(row) for row in rank(oracle_clear)],
        "source": str(path.name),
        "policy": ("mined from the completed read-only gate audit — no new scans, no threshold change, "
                   "no production effect; the oracle block uses future outcomes and is research-only, "
                   "isolated from every runtime path (tests/test_research_isolation.py)."),
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    return summary


def _append_near_miss_section(summary: dict) -> None:
    """Keep GATE_AUDIT.md self-contained: replace (never duplicate) the near-miss section."""
    if not AUDIT_MD.exists():
        return
    text = AUDIT_MD.read_text(encoding="utf-8")
    index = text.find(NEAR_MISS_MARKER)
    if index != -1:
        text = text[:index].rstrip() + "\n"
    lines = [text.rstrip(), "", NEAR_MISS_MARKER + "\n",
             f"Mined from all {summary['minutes']} audited minutes ({summary['source']}). No threshold was ",
             "changed and no scan was re-run to produce this table — it is a property of the audit data.\n",
             f"- minutes where **only the model layer** (MODEL/CONFIDENCE) failed: "
             f"**{summary['model_only_blocker_minutes']}**",
             f"- minutes where the oracle (perfect direction, research only) cleared every gate: "
             f"**{summary['oracle_clears_every_gate']}**",
             "- minutes with exactly one non-model gate failing: "
             + (", ".join(f"{gate} {count}" for gate, count in summary["minutes_one_gate_away"].items())
                or "none"),
             "",
             "| # | minute (UTC) | confidence | failing gates | blocking reason |",
             "| --- | --- | --- | --- | --- |"]
    for i, row in enumerate(summary["best_model_only_minutes"], start=1):
        reason = next(iter((row["reasons"] or {}).values()), "—")
        lines.append(f"| {i} | {row['minute_utc']} | {row['confidence']} | "
                     f"{', '.join(row['failing_gates']) or '—'} | {str(reason)[:110]} |")
    if not summary["best_model_only_minutes"]:
        lines.append("| — | — | — | — | no minute reached this state |")
    lines.append("")
    AUDIT_MD.write_text("\n".join(lines), encoding="utf-8")


def _normalise(reason: str) -> str:
    """Collapse numbers so the histogram groups the same gate together."""
    out = []
    for token in reason.split():
        cleaned = token.strip("()[],%≥<")
        try:
            float(cleaned)
        except ValueError:
            out.append(token)
        else:
            out.append("#")
    return " ".join(out)[:120]


def _real_frame(limit: int):
    import pandas as pd

    path = PROJECT_ROOT / "backend" / "data" / "csv" / "USDJPY.csv"
    frame = pd.read_csv(path)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame = frame.set_index("time").sort_index()
    return frame[["open", "high", "low", "close", "volume"]].astype(float).tail(limit)


def _rows(frame, provider: str) -> list[dict]:
    return [
        {"asset": "USDJPY", "provider": provider, "ts_utc": str(index),
         "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]),
         "close": float(row["close"]), "replay": True, "is_replay": True, "realtime": False,
         "source_label": "Operator CSV (REAL)"}
        for index, row in frame.iterrows()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=240)
    parser.add_argument("--from-study", action="store_true",
                        help="mine the completed full-history gate audit instead of scanning")
    parser.add_argument("--detail", type=int, default=12, help="per-minute rows kept in the artefact")
    args = parser.parse_args(argv)

    if args.from_study:
        summary = mine_study()
        DIAGNOSTICS.parent.mkdir(parents=True, exist_ok=True)
        DIAGNOSTICS.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        _append_near_miss_section(summary)
        print(json.dumps({k: v for k, v in summary.items()
                          if k not in {"best_model_only_minutes", "best_one_gate_away",
                                       "oracle_cleared_minutes"}}, indent=2, default=str))
        print(f"artifact: {DIAGNOSTICS}")
        return 0

    rows = scan_minutes(args.minutes)
    summary = analyse(rows)
    payload = {
        **summary,
        "window_start_utc": rows[0]["minute_utc"] if rows else None,
        "window_end_utc": rows[-1]["minute_utc"] if rows else None,
        "per_minute": rows[-args.detail:],
        "policy": ("production gate stack unchanged; every scan ran with persist=False, so nothing "
                   "was written to the signal ledger while diagnosing. Numbers under "
                   "'with_qualified_model_layer' substitute the learned model layer with the harness "
                   "double described in this file and are labelled hypothetical everywhere."),
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k not in {"per_minute", "closest_minutes"}},
                     indent=2, default=str))
    for row in summary["closest_minutes"]:
        print("  closest:", row["minute_utc"], row["status"], row["confidence"], row["reasons"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
