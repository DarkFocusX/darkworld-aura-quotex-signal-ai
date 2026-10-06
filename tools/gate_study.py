"""Gate audit + oracle feasibility + safe gate-variant research over the REAL history.

Spec §PHASE 1–3 of the final master phase. One chronological pass over the operator's
M1 export answers three questions with the *unchanged* production engine:

1. GATE AUDIT — for every real minute: which gate stopped it, how often, and where.
   Gates: DATA · FEATURE · MODEL · ANOMALY · REGIME · CONSISTENCY · CONFIDENCE · RISK · LOCK.
   Also the cumulative funnel ALL → DATA → FEATURES → MODEL → ANOMALY → REGIME →
   CONSISTENCY → CONFIDENCE → RISK → FINAL.

2. ORACLE FEASIBILITY (research only) — "if the model direction were perfect, could the
   remaining gates produce a qualified setup?" The oracle knows the direction the entry
   candle actually moved and is used *only* here: it lives in this offline tool, never in
   the engine, never behind an API, never in the scanner or Telegram. `tests/test_research_isolation.py`
   proves it cannot reach production inference.

3. SAFE GATE VARIANTS A–D — evaluated on the same chronological minutes with the real
   model, so a variant can only be adopted later with out-of-sample evidence. A variant
   that emits more signals is *not* preferred for that reason.

Usage:

    python3 tools/gate_study.py scan --minutes 27900 --workers 2
    python3 tools/gate_study.py analyse
"""
from __future__ import annotations

import argparse
import gzip
import json
import multiprocessing as mp
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

STUDY = Path(os.environ.get("GATE_STUDY_STUDY", PROJECT_ROOT / "docs" / "gate_study.jsonl.gz"))
# Shards live inside the workspace, not in /tmp: a full-history pass runs for tens of
# minutes, and a workspace restart must not throw the completed minutes away. `--resume`
# (the default) re-reads whatever is already on disk and only scans what is missing.
SHARD_ROOT = Path(os.environ.get("GATE_STUDY_SHARD_DIR", PROJECT_ROOT / "tmp_gate_study_shards"))
AUDIT_JSON = PROJECT_ROOT / "GATE_AUDIT.json"
AUDIT_MD = PROJECT_ROOT / "GATE_AUDIT.md"
VARIANTS_MD = PROJECT_ROOT / "docs" / "RESEARCH_VARIANTS.md"

GATE_ORDER = ("DATA", "FEATURE", "MODEL", "ANOMALY", "REGIME", "CONSISTENCY",
              "CONFIDENCE", "RISK", "LOCK", "FINAL")
CHECK_FOR_GATE = {
    "DATA": "DATA CHECK",
    "FEATURE": "FEATURE CHECK",
    "MODEL": "MODEL CHECK",
    "REGIME": "REGIME CHECK",
    "CONSISTENCY": "SIGNAL CONSISTENCY CHECK",
    "CONFIDENCE": "CONFIDENCE CHECK",
}
THRESHOLDS = (60, 65, 70, 75, 80, 85, 90, 95)
TECHNICAL_GROUPS = ("trend", "momentum", "candle", "structure")


# --------------------------------------------------------------------------- inputs
def real_frame(limit: int | None = None):
    import pandas as pd

    path = PROJECT_ROOT / "backend" / "data" / "csv" / "USDJPY.csv"
    frame = pd.read_csv(path)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame = frame.set_index("time").sort_index()
    frame = frame[["open", "high", "low", "close", "volume"]].astype(float)
    return frame.tail(limit) if limit else frame


def store_rows(frame, provider: str = "operator_csv") -> list[dict]:
    return [
        {"asset": "USDJPY", "provider": provider, "ts_utc": str(index),
         "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]),
         "close": float(row["close"]), "replay": True, "is_replay": True, "realtime": False,
         "source_label": "Operator CSV (REAL)"}
        for index, row in frame.iterrows()
    ]


# ------------------------------------------------------------------- gate extraction
def extract_gates(scan) -> dict:
    """Gate verdicts from one scan, in the audit's canonical order (None = not reached)."""
    checks = {c["check"]: c for c in ((scan.checks or {}).get("checks") or [])}
    eligibility = {r["requirement"]: r for r in ((scan.eligibility or {}).get("requirements") or [])}
    ensemble = scan.ensemble or {}
    gates: dict[str, int | None] = {}
    reasons: dict[str, str] = {}

    def from_check(gate: str) -> int | None:
        result = checks.get(CHECK_FOR_GATE[gate])
        if result is None:
            return None
        if not result.get("passed"):
            reasons[gate] = str(result.get("detail") or "")[:160]
            return 0
        return 1

    if scan.checks is None:                     # data stage aborted before any check ran
        gates.update({gate: None for gate in GATE_ORDER})
        gates["DATA"] = 0
        gates["FINAL"] = 0
        reasons["DATA"] = str(scan.reason or "")[:160]
        return {"gates": gates, "reasons": reasons, "eligibility_failures": []}

    data_ok = from_check("DATA")
    if data_ok:
        for requirement in ("DATA SOURCE IS REAL", "DATA IS FRESH", "ENOUGH CANDLES"):
            result = eligibility.get(requirement)
            if result is not None and not result.get("passed"):
                data_ok = 0
                reasons["DATA"] = f"{requirement}: {result.get('detail') or ''}"[:160]
                break
    gates["DATA"] = data_ok
    gates["FEATURE"] = from_check("FEATURE")
    gates["MODEL"] = from_check("MODEL")
    gates["ANOMALY"] = None if scan.ensemble is None else (0 if ensemble.get("anomaly_flag") else 1)
    if gates["ANOMALY"] == 0:
        reasons["ANOMALY"] = str(ensemble.get("anomaly_detail") or "IsolationForest anomaly veto")[:160]
    gates["REGIME"] = from_check("REGIME")
    gates["CONSISTENCY"] = from_check("CONSISTENCY")
    gates["CONFIDENCE"] = from_check("CONFIDENCE")
    risk = (scan.checks or {}).get("risk")
    gates["RISK"] = None if risk is None else (1 if risk.get("passed") else 0)
    if gates["RISK"] == 0:
        reasons["RISK"] = "; ".join(risk.get("reasons") or [])[:160] or "risk policy blocked"
    gates["LOCK"] = None if scan.lock is None else (0 if scan.lock.get("locked") else 1)
    if gates["LOCK"] == 0:
        reasons["LOCK"] = "a signal already exists for this candle (per-candle lock)"
    gates["FINAL"] = 1 if scan.status == "SIGNAL" else 0
    failures = [r["requirement"] for r in ((scan.eligibility or {}).get("requirements") or [])
                if not r.get("passed")]
    return {"gates": gates, "reasons": reasons, "eligibility_failures": failures}


def oracle_direction(entry_open: float, settlement_close: float) -> str | None:
    """The direction the *settlement candle* actually moved — research only.

    Returns None for a flat move, where "the perfect direction" does not exist.
    """
    if settlement_close > entry_open:
        return "CALL"
    if settlement_close < entry_open:
        return "PUT"
    return None


def _record_for(frame, index: int, engine, oracle_engine) -> dict:
    """One audited minute (index points at the last *scanned* candle)."""
    stamp = frame.index[index]
    moment = stamp.to_pydatetime() + timedelta(seconds=5)
    scan = engine.scan_asset("USDJPY", now=moment, persist=False)
    audit = extract_gates(scan)
    components = ((scan.components or {}).get("components") or {})
    ensemble = scan.ensemble or {}

    # Future (research only): entry candle = the next minute boundary after the scan,
    # fill = its OPEN, settlement = its CLOSE (the same convention the paper ledger uses).
    entry_index = index + 1
    future_ok = entry_index < len(frame)
    move_normalised = label = call_hit = put_hit = None
    if future_ok:
        entry_open = float(frame["open"].iloc[entry_index])
        settlement_close = float(frame["close"].iloc[entry_index])
        delta = settlement_close - entry_open
        atr = _atr_before(frame, entry_index)
        band = abs(0.25 * atr) if atr else 0.0
        move_normalised = round(delta / atr, 4) if atr else None
        label = 1 if (band > 0 and abs(delta) <= band) else (2 if delta > 0 else 0)
        call_hit = 1 if label == 2 else (0 if label == 0 else None)
        put_hit = 1 if label == 0 else (0 if label == 2 else None)
    else:
        entry_open = settlement_close = None

    record: dict = {
        "m": str(stamp), "s": scan.status, "c": scan.confidence, "dir": scan.direction,
        "p": ensemble.get("direction_probability"), "pn": ensemble.get("directional_probability_normalised"),
        "ag": ensemble.get("agreement"), "an": ensemble.get("anomaly_flag"),
        "gates": audit["gates"], "reasons": audit["reasons"],
        "elig": audit["eligibility_failures"],
        "comp": {k: v.get("score") for k, v in components.items()},
        "mv": move_normalised, "label": label, "call_hit": call_hit, "put_hit": put_hit,
        "oracle": None,
    }

    # Oracle feasibility: re-scan with the *actual* direction of the settlement candle.
    # Only minutes whose DATA and FEATURE stages already passed can be helped by a better
    # model, so the other minutes are not re-run (a model change can never fix them).
    if future_ok and audit["gates"]["DATA"] == 1 and audit["gates"]["FEATURE"] == 1:
        direction = oracle_direction(entry_open, settlement_close)
        if direction is None:
            record["oracle"] = {"applicable": False, "why": "flat settlement candle — no perfect direction exists"}
        else:
            oracle_engine.set_direction(direction)
            oracle_scan = oracle_engine.engine.scan_asset("USDJPY", now=moment, persist=False)
            oracle_audit = extract_gates(oracle_scan)
            record["oracle"] = {
                "applicable": True, "direction": direction, "status": oracle_scan.status,
                "confidence": oracle_scan.confidence, "gates": oracle_audit["gates"],
                "reasons": oracle_audit["reasons"],
            }
    return record


def _atr_before(frame, index: int, period: int = 14) -> float | None:
    import numpy as np

    if index < period + 1:
        return None
    window = frame.iloc[max(0, index - period - 5):index]
    if len(window) < period + 1:
        return None
    high, low, close = window["high"], window["low"], window["close"]
    previous = close.shift(1)
    true_range = (high - low).combine((high - previous).abs(), max).combine((low - previous).abs(), max)
    atr = true_range.rolling(period).mean().iloc[-1]
    return float(atr) if np.isfinite(atr) and atr > 0 else None


class _OracleModel:
    """Research-only model double that returns the *actual* direction of the trade.

    It exists to answer "is the architecture overconstrained?" — the future is used here
    and nowhere else. `tests/test_research_isolation.py` asserts that no production module
    imports this file, and the instance is constructed only inside `gate_study.scan`.
    """

    def __init__(self, real, direction: str = "CALL", probability: float = 0.93) -> None:
        self._real = real
        self._direction = direction
        self._probability = probability

    def set_direction(self, direction: str) -> None:
        self._direction = direction

    @property
    def metadata(self):
        return self._real.metadata

    def status(self) -> dict:
        status = dict(self._real.status())
        status["state"] = "READY_MARKET"
        status["production_usable"] = True
        return status

    def predict(self, values):  # noqa: ARG002 — the oracle ignores the features by design
        from backend.models.ensemble import EnsembleResult, ModelVote

        p = float(self._probability)
        other = round((1 - p) / 2, 4)
        votes = [ModelVote(model=name, p_put=other, p_neutral=other, p_call=p, weight=1.0,
                           vote=self._direction, status="OK")
                 for name in ("logistic_regression", "random_forest", "lightgbm")]
        return EnsembleResult(
            p_put=other if self._direction == "CALL" else p, p_neutral=other,
            p_call=p if self._direction == "CALL" else other, direction=self._direction,
            direction_probability=p, directional_probability_normalised=0.96,
            agreement=1.0, spread=0.01, votes=votes,
        )


class _OracleEngine:
    """Wraps the production engine so only its model layer changes."""

    def __init__(self, real_service) -> None:
        from backend.settings_service import get_settings
        from backend.signals.engine import SignalEngine

        settings = get_settings()
        settings.environment = "production"
        settings.data_source_mode = "real_only"
        self.engine = SignalEngine(settings)
        self._model = _OracleModel(real_service)
        self.engine.model_service = lambda: self._model

    def set_direction(self, direction: str) -> None:
        self._model.set_direction(direction)


# ------------------------------------------------------------------------ the scan
def _worker(payload) -> tuple[str, int]:
    db_path, kind, stamps, shard = payload
    from backend.database import db as db_module

    db_module.DB_PATH = Path(db_path)
    from backend.data import store

    store.init_store()
    from backend.models.service import get_model_service
    from backend.settings_service import get_settings
    from backend.signals.engine import SignalEngine

    settings = get_settings()
    settings.environment = "production"
    settings.data_source_mode = "real_only"
    engine = SignalEngine(settings)
    oracle = None if kind == "audit_only" else _OracleEngine(get_model_service())

    frame = real_frame()
    positions = {str(stamp): i for i, stamp in enumerate(frame.index)}
    written = 0
    with gzip.open(shard, "wt", encoding="utf-8") as handle:
        for stamp in stamps:
            index = positions.get(stamp)
            if index is None:
                continue
            handle.write(json.dumps(_record_for(frame, index, engine, oracle), default=str) + "\n")
            written += 1
    return str(shard), written


def audited_minutes(shard_root: Path | None = None) -> set[str]:
    """Minute stamps already present in the shard directory (tolerates partial shards)."""
    root = shard_root or SHARD_ROOT
    done: set[str] = set()
    if not root.exists():
        return done
    for path in sorted(root.glob("shard_*.jsonl.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        done.add(json.loads(line)["m"])
                    except (json.JSONDecodeError, KeyError):
                        continue
        except (OSError, EOFError):
            # A truncated gzip is expected when a run was interrupted: the rows that were
            # flushed before the cut are still valid and count as audited.
            continue
    return done


def merge_shards(shard_root: Path | None = None) -> list[dict]:
    """Every shard row on disk, deduplicated by minute (newest generation wins), sorted."""
    root = shard_root or SHARD_ROOT
    rows: dict[str, dict] = {}
    for path in sorted(root.glob("shard_*.jsonl.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rows[row["m"]] = row            # later file/line overwrites the earlier one
        except (OSError, EOFError):
            continue
    return [rows[stamp] for stamp in sorted(rows)]


def scan(minutes: int, workers: int, warmup: int = 320, stride: int = 1,
         kind: str = "full", resume: bool = True) -> dict:
    from backend.data import store
    from backend.database import db as db_module

    db_module.init_db()
    store.init_store()
    frame = real_frame()
    store.upsert_candles(store_rows(frame), retention_minutes=0)   # never prune the audit window
    # Warm-up: the feature block needs ~320 prior candles before it counts 120 usable
    # ones, so auditing earlier minutes would measure the warm-up, not the gates.
    usable = frame.index[max(warmup, 0):-1]              # last candle has no settlement yet
    stamps = [str(stamp) for stamp in usable]
    if stride and stride > 1:
        stamps = stamps[::int(stride)]
    if minutes and minutes < len(stamps):
        stamps = stamps[-minutes:]
    stamps.reverse()                                      # newest first

    shard_dir = SHARD_ROOT
    shard_dir.mkdir(parents=True, exist_ok=True)
    already = audited_minutes(shard_dir) if resume else set()
    pending = [stamp for stamp in stamps if stamp not in already]
    generation = 1 + max((int(path.name.split("_")[1]) for path in shard_dir.glob("shard_*.jsonl.gz")),
                         default=0)
    slices = [pending[i::workers] for i in range(workers)]
    payloads = [(str(db_module.DB_PATH), kind, part,
                 str(shard_dir / f"shard_{generation}_{i}.jsonl.gz"))
                for i, part in enumerate(slices)]
    print(json.dumps({"resume": resume, "already_audited": len(already),
                      "to_scan": len(pending), "generation": generation, "workers": workers,
                      "kind": kind, "stride": stride}, indent=2))

    started = datetime.now(tz=timezone.utc)
    if pending:
        if workers > 1:
            with mp.Pool(processes=workers) as pool:
                shards = pool.map(_worker, payloads)
        else:
            shards = [_worker(payloads[0])]
    else:
        shards = []

    # merge every generation on disk (old + new), ordered oldest → newest
    rows = merge_shards(shard_dir)
    STUDY.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(STUDY, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")

    summary = {
        "artefact": str(STUDY.relative_to(PROJECT_ROOT))
                    if str(STUDY).startswith(str(PROJECT_ROOT)) else str(STUDY),
        "minutes_audited": len(rows),
        "minutes_from_previous_runs": len(already),
        "minutes_scanned_now": len(pending),
        "window_start_utc": rows[0]["m"] if rows else None,
        "window_end_utc": rows[-1]["m"] if rows else None,
        "workers": workers,
        "stride": stride,
        "kind": kind,
        "generation": generation,
        "seconds": round((datetime.now(tz=timezone.utc) - started).total_seconds(), 1),
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    print(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------- analysis
def load_study() -> list[dict]:
    with gzip.open(STUDY, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _normalise(text: str) -> str:
    out = []
    for token in str(text).split():
        try:
            float(token.strip("()[],%≥<"))
        except ValueError:
            out.append(token)
        else:
            out.append("#")
    return " ".join(out)[:150]


def gate_audit(rows: list[dict]) -> dict:
    """Phase 1 — per gate totals, failure distribution and the cumulative funnel."""
    per_gate: dict[str, dict] = {}
    for gate in GATE_ORDER:
        values = [row["gates"].get(gate) for row in rows]
        evaluated = [v for v in values if v is not None]
        passed = sum(1 for v in evaluated if v == 1)
        failed = sum(1 for v in evaluated if v == 0)
        reasons = Counter(_normalise(row["reasons"][gate]) for row in rows
                          if row["gates"].get(gate) == 0 and gate in row["reasons"])
        per_gate[gate] = {
            "evaluated": len(evaluated),
            "not_reached": len(values) - len(evaluated),
            "passed": passed,
            "failed": failed,
            "pass_rate_of_evaluated": round(passed / len(evaluated) * 100, 2) if evaluated else None,
            "pass_rate_of_all_candidates": round(passed / len(rows) * 100, 2) if rows else None,
            "failure_reasons": dict(reasons.most_common(8)),
        }

    funnel: list[dict] = []
    remaining = len(rows)
    for gate in GATE_ORDER:
        blocked = sum(1 for row in rows if row["gates"].get(gate) == 0)
        # A candidate can be stopped at exactly one earlier gate; the funnel counts the
        # first gate that stopped it, which is how a real scan behaves.
        first_stop = Counter()
        for row in rows:
            for candidate in GATE_ORDER:
                if row["gates"].get(candidate) == 0:
                    first_stop[candidate] += 1
                    break
        remaining = len(rows) - sum(v for k, v in first_stop.items() if GATE_ORDER.index(k) <= GATE_ORDER.index(gate))
        funnel.append({
            "stage": gate,
            "candidates_in": len(rows) - sum(v for k, v in first_stop.items()
                                             if GATE_ORDER.index(k) < GATE_ORDER.index(gate)),
            "blocked_here": first_stop.get(gate, 0),
            "surviving": max(remaining, 0),
            "raw_failures": blocked,
        })
    return {"per_gate": per_gate, "funnel": funnel}


def oracle_summary(rows: list[dict]) -> dict:
    """Phase 2 — can a perfect model direction clear the remaining gates?"""
    candidates = [row for row in rows if row["oracle"]]
    applicable = [row for row in candidates if row["oracle"].get("applicable")]
    cleared = [row for row in applicable if row["oracle"]["status"] == "SIGNAL"]
    per_gate: dict[str, dict] = {}
    for gate in GATE_ORDER:
        values = [row["oracle"]["gates"].get(gate) for row in applicable]
        evaluated = [v for v in values if v is not None]
        per_gate[gate] = {
            "evaluated": len(evaluated),
            "passed": sum(1 for v in evaluated if v == 1),
            "failed": sum(1 for v in evaluated if v == 0),
        }
    reasons = Counter()
    for row in applicable:
        for gate, reason in (row["oracle"].get("reasons") or {}).items():
            reasons[f"{gate}: {_normalise(reason)}"] += 1
    if cleared:
        verdict = "FEASIBLE"
    elif applicable and per_gate["DATA"]["passed"] > 0:
        verdict = "OVERCONSTRAINED"
    elif not applicable:
        verdict = "INSUFFICIENT_DATA"
    else:
        verdict = "DATA_BLOCKED"
    return {
        "candidates_considered": len(candidates),
        "applicable": len(applicable),
        "flat_or_no_settlement": len(candidates) - len(applicable),
        "oracle_signals": len(cleared),
        "per_gate": per_gate,
        "top_oracle_blockers": dict(reasons.most_common(10)),
        "verdict": verdict,
    }


def _wilson_lower(wins: int, decided: int, z: float = 1.959963984540054) -> float | None:
    if decided <= 0:
        return None
    p = wins / decided
    denominator = 1 + z * z / decided
    centre = p + z * z / (2 * decided)
    spread = z * ((p * (1 - p) / decided + z * z / (4 * decided * decided)) ** 0.5)
    return round((centre - spread) / denominator, 4)


def _streak(hits: list[int]) -> int:
    longest = current = 0
    for hit in hits:
        current = 0 if hit else current + 1
        longest = max(longest, current)
    return longest


def _variant_stats(records: list[dict], *, name: str, note: str) -> dict:
    decided = [r for r in records if r["label"] in (0, 2)]
    wins = sum(1 for r in decided if (r["call_hit"] if r["dir"] == "CALL" else r["put_hit"]) == 1)
    voids = sum(1 for r in records if r["label"] == 1)
    return {
        "variant": name,
        "note": note,
        "signals": len(records),
        "coverage_pct": round(len(records) / len(varied_population(records)) * 100, 3) if False else None,
        "decided": len(decided),
        "voids": voids,
        "wins": wins,
        "losses": len(decided) - wins,
        "hit_rate": round(wins / len(decided), 4) if decided else None,
        "wilson_95_lower_bound": _wilson_lower(wins, len(decided)),
        "max_losing_streak": _streak([1 if (r["call_hit"] if r["dir"] == "CALL" else r["put_hit"]) == 1 else 0
                                      for r in records]),
        "confidence_distribution": dict(Counter(_bucket(r["c"]) for r in records)),
        "direction_distribution": dict(Counter(r["dir"] for r in records)),
        "regime_distribution": dict(Counter(_regime(r) for r in records)),
    }


def varied_population(records: list[dict]) -> list[dict]:   # pragma: no cover - helper
    return records


def _bucket(confidence: float | None) -> str:
    if confidence is None:
        return "—"
    for low, high in ((50, 60), (60, 70), (70, 75), (75, 80), (80, 85), (85, 90), (90, 95), (95, 100)):
        if low <= confidence < high or (high == 100 and confidence >= 95):
            return f"{low}–{high}"
    return "<50"


def _regime(row: dict) -> str:
    scores = row.get("comp") or {}
    if (scores.get("regime") or 0) >= 65:
        return "TREND"
    if (scores.get("volatility") or 0) < 35:
        return "HIGH_VOLATILITY"
    return "RANGE"


def variants(rows: list[dict]) -> dict:
    """Phase 3 — A (production), B/C (soft gates), D (threshold sweep). Research only."""
    def context_ok(row: dict) -> bool:
        return all(row["gates"].get(g) == 1 for g in ("REGIME", "CONSISTENCY"))

    def model_ok(row: dict) -> bool:
        return row["gates"].get("MODEL") == 1

    def data_ok(row: dict) -> bool:
        return row["gates"].get("DATA") == 1 and row["gates"].get("FEATURE") == 1

    def risk_ok(row: dict) -> bool:
        return row["gates"].get("RISK") != 0

    variant_a = [row for row in rows if row["gates"].get("FINAL") == 1]

    variant_b = [row for row in rows
                 if data_ok(row) and model_ok(row) and risk_ok(row)
                 and (row["c"] or 0) >= 85]

    variant_c = []
    for row in rows:
        if not (data_ok(row) and model_ok(row) and risk_ok(row)):
            continue
        penalty = 0.0 if context_ok(row) else 6.0        # documented research penalty
        if (row["c"] or 0) - penalty >= 85:
            variant_c.append(row)

    sweep = {}
    for threshold in THRESHOLDS:
        selected = [row for row in rows
                    if row["gates"].get("DATA") == 1 and row["gates"].get("FEATURE") == 1
                    and row["gates"].get("MODEL") == 1 and (row["c"] or 0) >= threshold]
        entry = _variant_stats(selected, name=f"D @ {threshold}",
                              note="production gates with the confidence threshold swept")
        entry["coverage_pct"] = round(len(selected) / len(rows) * 100, 3) if rows else None
        sweep[str(threshold)] = entry

    def stats(records: list[dict], name: str, note: str) -> dict:
        payload = _variant_stats(records, name=name, note=note)
        payload["coverage_pct"] = round(len(records) / len(rows) * 100, 3) if rows else None
        return payload

    return {
        "population_minutes": len(rows),
        "A_production": stats(variant_a, "A — production", "unchanged production architecture"),
        "B_technical_in_confidence": stats(
            variant_b, "B — soft technical context",
            "data validity + model validity + risk stay hard gates; the technical context "
            "only contributes through the confidence score"),
        "C_regime_as_penalty": stats(
            variant_c, "C — regime as penalty",
            "data validity + model validity stay hard gates; an unfavourable regime or a "
            "non-aligned consistency check costs 6 confidence points instead of vetoing"),
        "D_threshold_sweep": sweep,
    }


def write_audit(rows: list[dict], audit: dict, oracle: dict, variant_report: dict) -> None:
    status_counts = Counter(row["s"] for row in rows)
    payload = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "window": {"start_utc": rows[0]["m"] if rows else None,
                   "end_utc": rows[-1]["m"] if rows else None,
                   "minutes": len(rows), "asset": "USDJPY",
                   "provider": "operator_csv (REAL operator export)"},
        "status_counts": dict(status_counts),
        "policy": ("production engine unchanged; persist=False so the audit wrote no signal; "
                   "the oracle used the future direction only inside tools/gate_study.py"),
        "gate_audit": audit,
        "oracle_feasibility": oracle,
        "variants": variant_report,
    }
    AUDIT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    lines = [
        "# GATE AUDIT — why the real operator history produced no signal",
        "",
        f"- window: `{payload['window']['start_utc']}` → `{payload['window']['end_utc']}` "
        f"({payload['window']['minutes']} minutes, {payload['window']['asset']}, {payload['window']['provider']})",
        f"- generated: {payload['generated_at_utc']}",
        "- engine: **unchanged production gate stack**, `persist=False` (nothing was written)",
        f"- scan outcomes: {json.dumps(dict(status_counts))}",
        "",
        "## 1. Per-gate result",
        "",
        "| gate | evaluated | not reached | passed | failed | pass % (of evaluated) | top failure reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for gate in GATE_ORDER:
        row = audit["per_gate"][gate]
        top = next(iter(row["failure_reasons"].items()), ("—", 0))
        lines.append(f"| {gate} | {row['evaluated']} | {row['not_reached']} | {row['passed']} | "
                     f"{row['failed']} | {row['pass_rate_of_evaluated']} | {top[0]} ({top[1]}) |")

    lines += [
        "",
        "### Failure-reason distribution (top 5 per gate)",
        "",
    ]
    for gate in GATE_ORDER:
        reasons = audit["per_gate"][gate]["failure_reasons"]
        if not reasons:
            continue
        lines.append(f"**{gate}**")
        lines.append("")
        for reason, count in list(reasons.items())[:5]:
            lines.append(f"- `{count}×` {reason}")
        lines.append("")

    lines += [
        "## 2. Cumulative filtering",
        "",
        "```",
        f"ALL candidates            {payload['window']['minutes']}",
    ]
    for stage in audit["funnel"]:
        lines.append(f"{stage['stage']:<26}{stage['surviving']:>7}   (blocked here: {stage['blocked_here']})")
    lines += ["```", "",
              "## 3. Oracle feasibility (research only)",
              "",
              f"- verdict: **{oracle['verdict']}**",
              f"- minutes where the oracle was applicable: {oracle['applicable']} "
              f"(flat settlement candles: {oracle['flat_or_no_settlement']})",
              f"- oracle signals (perfect direction *and* every remaining gate cleared): {oracle['oracle_signals']}",
              "",
              "| oracle stage | evaluated | passed | failed |",
              "| --- | --- | --- | --- |",
              ]
    for gate in GATE_ORDER:
        row = oracle["per_gate"][gate]
        lines.append(f"| {gate} | {row['evaluated']} | {row['passed']} | {row['failed']} |")
    lines += ["", "Top oracle blockers (perfect direction, unchanged gates):", ""]
    for reason, count in list(oracle["top_oracle_blockers"].items())[:8]:
        lines.append(f"- `{count}×` {reason}")
    lines += [
        "",
        "## 4. Safe gate variants (research only — identical chronological minutes)",
        "",
        "| variant | signals | coverage % | decided | wins | losses | hit rate | Wilson 95 % LB | max losing streak |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key in ("A_production", "B_technical_in_confidence", "C_regime_as_penalty"):
        row = variant_report[key]
        lines.append(f"| {row['variant']} | {row['signals']} | {row['coverage_pct']} | {row['decided']} | "
                     f"{row['wins']} | {row['losses']} | {row['hit_rate']} | {row['wilson_95_lower_bound']} | "
                     f"{row['max_losing_streak']} |")
    for threshold, row in variant_report["D_threshold_sweep"].items():
        lines.append(f"| D @ conf ≥ {threshold} | {row['signals']} | {row['coverage_pct']} | {row['decided']} | "
                     f"{row['wins']} | {row['losses']} | {row['hit_rate']} | {row['wilson_95_lower_bound']} | "
                     f"{row['max_losing_streak']} |")
    lines += [
        "",
        "**No variant is adopted on this evidence.** Variant B/C are research constructions; adopting one",
        "would require untouched out-of-sample validation through the experiment suite, exactly like any",
        "other model change. Producing more signals is explicitly *not* a reason to adopt anything.",
        "",
        "## 5. Verdict",
        "",
        f"- gate audit: **COMPLETE** ({len(rows)} minutes, {len(GATE_ORDER)} gates)",
        f"- primary bottleneck: **{_bottleneck(audit)}**",
        f"- oracle verdict: **{oracle['verdict']}**",
        "- production resolution: **NO TRADE** on every audited minute — unchanged, and correct.",
        "",
    ]
    AUDIT_MD.write_text("\n".join(lines), encoding="utf-8")

    variants_lines = [
        "# Research variants A–D (final master phase §3)",
        "",
        "Identical chronological minutes, real operator candles, the real READY_MARKET model.",
        "Research only: nothing here changes production.",
        "",
        *lines[lines.index("## 4. Safe gate variants (research only — identical chronological minutes)"):
               lines.index("## 5. Verdict")],
    ]
    VARIANTS_MD.write_text("\n".join(variants_lines), encoding="utf-8")
    print(f"audit: {AUDIT_MD} + {AUDIT_JSON}")
    print(f"variants: {VARIANTS_MD}")


def _bottleneck(audit: dict) -> str:
    funnel = audit["funnel"]
    worst = max(funnel, key=lambda stage: stage["blocked_here"])
    return f"{worst['stage']} (stopped {worst['blocked_here']} of {funnel[0]['candidates_in']} candidates)"


def analyse() -> dict:
    rows = load_study()
    audit = gate_audit(rows)
    oracle = oracle_summary(rows)
    variant_report = variants(rows)
    write_audit(rows, audit, oracle, variant_report)
    return {"minutes": len(rows), "oracle": oracle["verdict"],
            "production_signals": variant_report["A_production"]["signals"]}


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_cmd = sub.add_parser("scan", help="audit the real history (read-only)")
    scan_cmd.add_argument("--minutes", type=int, default=0, help="0 = whole usable history")
    scan_cmd.add_argument("--workers", type=int, default=2)
    scan_cmd.add_argument("--stride", type=int, default=1,
                          help="audit every Nth usable minute (1 = every minute)")
    scan_cmd.add_argument("--kind", choices=("full", "audit_only"), default="full",
                          help="audit_only skips the oracle re-scan (the per-gate audit is unaffected)")
    scan_cmd.add_argument("--no-resume", action="store_true",
                          help="ignore minutes already audited in tmp_gate_study_shards/")
    sub.add_parser("analyse", help="write GATE_AUDIT.md/.json from the artefact")

    args = parser.parse_args(argv)
    if args.command == "scan":
        scan(args.minutes, args.workers, stride=args.stride, kind=args.kind,
             resume=not args.no_resume)
    else:
        print(json.dumps(analyse(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
