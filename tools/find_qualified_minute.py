"""
NOTE (superseded): this sweep predates the clock-aware store query used by the gate audit. It
now ingests with `retention_minutes=0`, but the authoritative full-history measurement is
`tools/gate_study.py` → `GATE_AUDIT.md`/`.json`, which records every gate, every minute and the
oracle counterfactual. Use this tool only for a quick yes/no re-check.
Find a minute in the REAL operator history where the engine genuinely emits a signal.

This is not a search for a flattering number and not a threshold tweak: every scan runs
the production gate stack unchanged (real data, READY_MARKET, freshness budget against
the *replayed* clock, ≥120 candles, model probability, agreement, anomaly veto,
consistency, confidence ≥ the configured threshold). `persist=False` keeps the search
read-only — nothing is written to the ledger while looking.

Result (or the honest absence of one) is written to `docs/qualified_minute.json` as an
optional note for the operator. It is *not* an input to any production path: the §18 harness
(`tools/verify_scenarios.py`) searches the same real history in-process, and the authoritative
measurement stays `GATE_AUDIT.json`.

    python3 tools/find_qualified_minute.py --minutes 3000 --workers 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT = PROJECT_ROOT / "docs" / "qualified_minute.json"


def _worker(args: tuple[str, str, list[dict], list[str]]) -> list[dict]:
    """Scan a slice of minutes in a child process (read-only)."""
    db_path, mode, stored, stamps = args
    from backend.database import db as db_module

    db_module.DB_PATH = Path(db_path)
    from backend.data import store  # noqa: F401  (schema is created by the parent)

    store.init_store()
    from backend.settings_service import get_settings
    from backend.signals.engine import STATUS_SIGNAL, SignalEngine

    settings = get_settings()
    settings.environment = "production"
    settings.data_source_mode = "real_only"
    engine = SignalEngine(settings)
    if mode == "qualified_model_layer":
        # §18 E/F mode: only the *learned model layer* is replaced (harness double); every
        # other gate is production code on real candles. Results are labelled hypothetical.
        from backend.models.service import get_model_service

        from tools.harness_model import QualifiedModelLayer

        engine.model_service = lambda: QualifiedModelLayer(get_model_service())
    found: list[dict] = []
    for stamp in stamps:
        moment = datetime.fromisoformat(stamp) + timedelta(seconds=5)
        try:
            scan = engine.scan_asset("USDJPY", now=moment, persist=False)
        except Exception as exc:  # noqa: BLE001
            found.append({"minute_utc": stamp, "error": f"{type(exc).__name__}: {exc}"[:120]})
            continue
        if scan.status == STATUS_SIGNAL:
            found.append({"minute_utc": stamp, "mode": mode, "confidence": scan.confidence,
                          "direction": scan.direction,
                          "checks": (scan.checks or {}).get("reasons", [])})
    return found


def search(minutes: int, workers: int, mode: str = "real_model") -> dict:
    """Sweep the newest `minutes` real candles, newest first."""
    from backend.data import store
    from backend.database import db as db_module
    from backend.signals import ledger

    # Reuse the configured database (the sweep is read-only for the ledger).
    db_module.init_db()
    store.init_store()
    ledger.ensure_schema()
    frame = _real_frame(minutes + 5)
    store.upsert_candles(_rows(frame, "operator_csv"), retention_minutes=0)   # keep the whole window
    stamps = [str(idx) for idx in frame.index[-minutes:]]
    stamps.reverse()                      # newest first: the freshest qualifying minute wins

    slices = [stamps[i::workers] for i in range(workers)]
    started = time.time()
    found: list[dict] = []
    if workers > 1:
        import multiprocessing as mp

        payload = [(str(db_module.DB_PATH), mode, [], part) for part in slices]
        with mp.Pool(processes=workers) as pool:
            for result in pool.imap_unordered(_worker, payload):
                found.extend(result)
    else:
        found = _worker((str(db_module.DB_PATH), mode, [], stamps))

    signals = [row for row in found if "error" not in row]
    errors = [row for row in found if "error" in row]
    payload = {
        "mode": mode,
        "searched_minutes": len(stamps),
        "window_start_utc": stamps[-1] if stamps else None,
        "window_end_utc": stamps[0] if stamps else None,
        "qualified_minutes": sorted(signals, key=lambda r: r["confidence"], reverse=True)[:10],
        "errors": errors[:5],
        "seconds": round(time.time() - started, 1),
        "policy": ("every scan ran the production gate stack unchanged; the sweep only reads "
                   "(persist=False) and never lowers a threshold"
                   + ("; the ONLY substitution is the learned model layer (tools/harness_model.py) — "
                      "every result in this file is hypothetical, not a result of the real model"
                      if mode == "qualified_model_layer" else "")),
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "qualified_minutes"}, indent=2))
    for row in payload["qualified_minutes"]:
        print("  QUALIFIED:", row)
    if not signals:
        print("  no minute in the searched window cleared every gate — that is a valid result")
    return payload


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
    parser.add_argument("--minutes", type=int, default=1500)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--qualified-model-layer", action="store_true",
                        help="replace the learned model layer with the harness double (hypothetical)")
    parser.add_argument("--out", type=Path, default=None, help="artefact path override")
    args = parser.parse_args(argv)
    global OUTPUT
    if args.out is not None:
        OUTPUT = args.out
    search(args.minutes, args.workers,
           "qualified_model_layer" if args.qualified_model_layer else "real_model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
