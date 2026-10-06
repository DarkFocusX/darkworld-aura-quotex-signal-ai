"""Operator CSV ingestion — the primary REAL-data path.

Two ways to get data in:

  1. Drop a file at `backend/data/csv/<ASSET>.csv` (exported from your platform).
  2. POST the CSV text to `/api/data/import` — it is parsed, validated, normalised
     and only written to disk when it passes validation.

Accepted headers (case-insensitive):
    time|datetime|date|timestamp|epoch|open_time, open, high, low, close[, volume]
Timestamps may be ISO-8601 (offset optional, UTC assumed) or epoch seconds/ms.

Safety properties:
  * the asset must be a known symbol — the file name is derived from the registry,
    never from user input, so path traversal is impossible;
  * the payload is parsed in memory and row/size-capped before anything is written;
  * writes are atomic (temp file + replace) so a crashed import cannot leave a
    half-written dataset behind;
  * the file is only written after validation, and the validation report is stored
    as provenance.

A CSV is historical input by definition: frames served from here are REPLAY,
never LIVE (spec §1).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from ...config import CSV_DIR
from ...constants import AssetSpec, canonical_symbol
from ...timeutil import utc_iso, utc_now_naive
from ..base import (
    MarketDataAdapter,
    CandleFrame,
    FeedMeta,
    MarketDataUnavailable,
    SOURCE_CLASS_MARKET,
    SOURCE_CLASS_SIMULATED,
    SOURCE_CLASS_FIXTURE,
    make_frame,
)
from .. import provenance as prov
from ..validation import csv_text_to_frame, validate_candles

_TIME_KEYS = ("time", "datetime", "date", "timestamp", "epoch", "open_time")

_SOURCE_CLASS_BY_NAME = {
    "market": SOURCE_CLASS_MARKET,
    "real": SOURCE_CLASS_MARKET,
    "csv": SOURCE_CLASS_MARKET,
    "simulated": SOURCE_CLASS_SIMULATED,
    "demo": SOURCE_CLASS_SIMULATED,
    "fixture": SOURCE_CLASS_FIXTURE,
    "test": SOURCE_CLASS_FIXTURE,
}


class CsvProvider(MarketDataAdapter):
    name = "csv"
    label = "Operator CSV dataset (replay)"
    label = "Operator CSV dataset"
    realtime = False
    is_simulated = False

    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or CSV_DIR)

    # ------------------------------------------------------------------ paths
    def path_for(self, asset: AssetSpec) -> Path:
        """Canonical file for an asset. The name comes from the registry only."""
        return self.directory / f"{asset.symbol}.csv"

    def available(self, asset: AssetSpec) -> bool:
        return self.path_for(asset).exists()

    # ------------------------------------------------------------------ write
    def write_dataset(self, asset: AssetSpec, frame: pd.DataFrame, *, filename: str,
                      source_class: str = SOURCE_CLASS_MARKET, operator_label: str = "",
                      validation: dict | None = None, notes: str = "") -> dict:
        """Persist a validated frame as the asset's CSV dataset (atomic)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        # The stored path always derives from the validated asset symbol; the
        # display name is reduced to a plain file name so it can never be a path.
        filename = Path(str(filename)).name.replace("..", "_") or f"{asset.symbol}.csv"
        path = self.path_for(asset)
        out = frame.copy()
        out.index.name = "time"
        text = out.to_csv(date_format="%Y-%m-%dT%H:%M:%SZ", float_format="%.8g")
        tmp = path.with_suffix(".csv.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        validation = validation or {}
        record = prov.record_import(
            asset.symbol,
            source_class=source_class,
            provider=self.name,
            source_label=f"Operator CSV: {filename}",
            filename=filename,
            rows=int(len(frame)),
            first_candle_utc=validation.get("first_candle_utc") or (
                utc_iso(frame.index[0].to_pydatetime()) if len(frame) else None
            ),
            last_candle_utc=validation.get("last_candle_utc") or (
                utc_iso(frame.index[-1].to_pydatetime()) if len(frame) else None
            ),
            span_hours=validation.get("span_hours"),
            has_volume=bool(frame["volume"].notna().any()) if "volume" in frame else False,
            sha256=prov.file_sha256(path),
            validation=validation,
            notes=notes,
            operator_label=operator_label,
            imported_at=utc_now_naive(),
        )
        return {"path": str(path), "rows": int(len(frame)), "provenance": record.as_dict()}

    # ------------------------------------------------------------------- fetch
    # ---------------------------------------------------- adapter interface (spec §11)
    def connect(self) -> dict:
        """A CSV dataset is always 'connected' — it is on disk, or it is not."""
        return self.health()

    def health(self) -> dict:
        files = sorted(p.name for p in self.directory.glob("*.csv")) if self.directory.exists() else []
        return {
            "provider": self.name,
            "label": self.label,
            "connected": bool(files),
            "status": "REPLAY_ONLY" if files else "UNAVAILABLE",
            "detail": (
                f"{len(files)} dataset file(s) in {self.directory} — operator snapshots, not a live feed; "
                "signals need candles newer than the freshness budget"
                if files else f"no CSV datasets in {self.directory}"
            ),
            "realtime_capable": False,
            "supports_subscribe": False,
            "is_simulated": False,
            "files": files[:20],
            "last_update_utc": None,
            "checked_at_utc": utc_iso(),
        }

    def subscribe(self, asset: AssetSpec, callback: object | None = None) -> dict:
        return {
            "provider": self.name,
            "asset": asset.symbol,
            "supported": False,
            "mode": "pull_only",
            "detail": ("a CSV snapshot cannot push updates — re-export the file, use POST /api/data/import, "
                       "or wire a streaming provider for live signalling"),
        }

    def fetch(self, asset: AssetSpec, limit: int = 300, now: datetime | None = None) -> CandleFrame:
        path = self.path_for(asset)
        if not path.exists():
            raise MarketDataUnavailable(
                asset.symbol, "No operator dataset found", self.name,
                f"expected a CSV at {path} (or POST /api/data/import)",
            )
        try:
            raw = pd.read_csv(path)
        except Exception as exc:  # noqa: BLE001 — surfaced verbatim to the UI
            raise MarketDataUnavailable(asset.symbol, "CSV could not be parsed", self.name,
                                        str(exc)[:180]) from exc

        cols = {str(c).lower().strip(): c for c in raw.columns}
        time_col = next((cols[k] for k in _TIME_KEYS if k in cols), None)
        if time_col is None:
            raise MarketDataUnavailable(asset.symbol, "CSV missing a time column", self.name,
                                        f"columns={list(raw.columns)[:8]}")
        for needed in ("open", "high", "low", "close"):
            if needed not in cols:
                raise MarketDataUnavailable(asset.symbol, f"CSV missing '{needed}' column", self.name)

        df = pd.DataFrame({
            "open": pd.to_numeric(raw[cols["open"]], errors="coerce"),
            "high": pd.to_numeric(raw[cols["high"]], errors="coerce"),
            "low": pd.to_numeric(raw[cols["low"]], errors="coerce"),
            "close": pd.to_numeric(raw[cols["close"]], errors="coerce"),
            "volume": pd.to_numeric(raw[cols["volume"]], errors="coerce") if "volume" in cols else None,
        })
        from ...timeutil import parse_any_ts

        index = []
        for value in raw[time_col].tolist():
            try:
                index.append(pd.Timestamp(parse_any_ts(value)))
            except Exception:  # noqa: BLE001
                index.append(pd.NaT)
        df.index = pd.DatetimeIndex(index)
        df = df[df.index.notna()]

        record = prov.get(asset.symbol) or {}
        source_class = record.get("source_class", SOURCE_CLASS_MARKET)
        is_simulated = source_class == SOURCE_CLASS_SIMULATED
        label = record.get("source_label") or f"Operator CSV ({path.name})"
        notes = record.get("notes") or "Operator-supplied dataset. File data is historical/replay input."
        if source_class == SOURCE_CLASS_FIXTURE:
            label = f"FIXTURE dataset ({path.name}) — QA only, not tradable"
            notes = "FIXTURE data: synthetic test input. Never treat as market data."
        elif source_class == SOURCE_CLASS_SIMULATED:
            label = f"SIMULATED dataset ({path.name}) — demo, not market data"
            notes = "SIMULATED data: pipeline demonstration only."

        meta = FeedMeta(
            asset=asset.symbol,
            provider=self.name,
            source_label=label,
            mode="REPLAY",
            realtime=False,
            is_simulated=is_simulated,
            is_replay=True,
            source_class=source_class,
            delayed_seconds=0.0,
            retrieved_at_utc=utc_iso(utc_now_naive()),
            notes=notes,
        )
        frame = make_frame(asset.symbol, df.tail(max(limit, 1)), meta, min_candles=30, now=now)

        # Validate the WHOLE file, not just the requested tail: a dataset that fails
        # validation must never reach the model, even if the last N rows look fine.
        whole = make_frame(asset.symbol, df, meta, min_candles=1, now=now, drop_forming=False)
        report = validate_candles(whole.df, check_freshness=True, now=now)
        frame.meta.source_status = report.status
        frame.meta.status = "OK" if report.ok else "INVALID"
        frame.meta.notes = f"{frame.meta.notes} Dataset validation: {report.status}.".strip()
        frame.meta.validation = report.as_dict()  # type: ignore[attr-defined]
        if not report.ok:
            raise MarketDataUnavailable(
                asset.symbol,
                "Dataset failed validation",
                self.name,
                "; ".join(report.errors[:3]),
            )
        if report.warnings:
            frame.meta.notes = (
                f"{frame.meta.notes} " + "; ".join(report.warnings[:2])
            ).strip()
        return frame


def import_csv_text(
    asset_symbol: str,
    csv_text: str,
    *,
    source_class: str = SOURCE_CLASS_MARKET,
    operator_label: str = "",
    filename: str | None = None,
    dry_run: bool = False,
    max_rows: int = 300_000,
    directory: Path | None = None,
) -> dict:
    """Validate + (optionally) persist operator CSV text. Never raises on bad data."""
    canonical = canonical_symbol(asset_symbol)
    if canonical is None:
        return {
            "status": "REJECTED",
            "asset": asset_symbol,
            "reason": f"Unknown asset '{asset_symbol}'. Use one of the configured symbols.",
            "validation": None,
        }

    from ...constants import ASSET_MAP

    spec = ASSET_MAP[canonical]
    requested_class = (source_class or SOURCE_CLASS_MARKET).upper()
    if requested_class not in _SOURCE_CLASS_BY_NAME and requested_class not in {
        SOURCE_CLASS_MARKET, SOURCE_CLASS_SIMULATED, SOURCE_CLASS_FIXTURE
    }:
        return {
            "status": "REJECTED",
            "asset": canonical,
            "reason": "source_class must be one of MARKET | SIMULATED | FIXTURE",
            "validation": None,
        }
    declared_class = _SOURCE_CLASS_BY_NAME.get(requested_class.lower(), requested_class)

    try:
        frame, parse_meta = csv_text_to_frame(csv_text, max_rows=max_rows)
    except ValueError as exc:
        return {"status": "REJECTED", "asset": canonical, "reason": str(exc), "validation": None}

    if parse_meta["rows_unparsed_timestamp"]:
        frame = frame[frame.index.notna()]

    # Drop the in-progress candle before validating/storing: an unfinished candle
    # is never part of a dataset the model may learn from.
    from ..base import drop_forming_candle

    frame, dropped = drop_forming_candle(frame)

    report = validate_candles(frame, check_freshness=True)
    payload: dict = {
        "asset": canonical,
        "source_class": declared_class,
        "validation": report.as_dict(),
        "parse": parse_meta,
        "rows_dropped_unclosed": dropped,
        "dry_run": bool(dry_run),
    }
    if not report.ok:
        payload.update({"status": "REJECTED", "reason": "; ".join(report.errors[:4])})
        return payload

    if dry_run:
        payload.update({"status": "VALIDATED", "reason": None})
        return payload

    provider = CsvProvider(directory)
    written = provider.write_dataset(
        spec,
        frame,
        filename=(filename or f"{canonical}.csv").strip() or f"{canonical}.csv",
        source_class=declared_class,
        operator_label=operator_label,
        validation=report.as_dict(),
        notes=(
            f"Imported via /api/data/import ({declared_class}). "
            + ("Warnings: " + "; ".join(report.warnings[:3]) if report.warnings else "No validation warnings.")
        ),
    )
    payload.update({
        "status": "IMPORTED",
        "reason": None,
        "path": written["path"],
        "rows": written["rows"],
        "provenance": written["provenance"],
        "warnings": report.warnings,
    })
    return payload
