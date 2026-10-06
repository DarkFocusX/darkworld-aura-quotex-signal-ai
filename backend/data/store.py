"""Rolling M1 candle store (spec §2 — continuous paper-trading phase).

Continuous ingestion writes verified M1 candles here; the engine reads them back
through the `live_store` provider. Rules enforced by this module:

* every row carries its provenance: `provider`, `is_simulated`, `is_replay`,
  `source_class` and the ingestion timestamp — a row never loses its label;
* timestamps are stored as UTC ISO strings (the UI renders IST, this layer never
  hides the UTC origin);
* duplicates are impossible: `(asset, provider, ts_utc)` is the primary key, so a
  re-delivered candle overwrites the same row instead of creating a second one;
* nothing is fabricated — a missing minute simply has no row (gap detection
  reports it, and the outcome engine refuses to settle from thin air).

The store is deliberately small and boring: a bounded window of minutes per
asset, purged by age on write so the SQLite file cannot grow without limit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import pandas as pd

from ..config import DB_PATH
from ..timeutil import to_utc, utc_iso
from .base import CandleFrame, FeedMeta, MarketDataUnavailable, make_frame

TABLE = "candles"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    asset            TEXT NOT NULL,
    provider         TEXT NOT NULL,
    ts_utc           TEXT NOT NULL,          -- candle OPEN time, UTC ISO
    resolution_minutes REAL NOT NULL DEFAULT 1.0,
    open             REAL NOT NULL,
    high             REAL NOT NULL,
    low              REAL NOT NULL,
    close            REAL NOT NULL,
    volume           REAL,
    is_simulated     INTEGER NOT NULL DEFAULT 0,
    is_replay        INTEGER NOT NULL DEFAULT 0,
    source_class     TEXT,
    source_label     TEXT,
    realtime         INTEGER NOT NULL DEFAULT 0,
    ingested_at_utc  TEXT NOT NULL,
    PRIMARY KEY (asset, provider, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_candles_asset_ts ON {TABLE} (asset, ts_utc DESC);
"""

DEFAULT_RETENTION_MINUTES = 7 * 24 * 60          # one week of M1 candles per provider


def _connect():
    """Open the shared application database (same file as signals/scans)."""
    from ..database import db as _db

    return _db._connect()


def init_store() -> None:
    from ..database import db as _db

    _db.init_db()                     # the shared application schema (signals/scans/config)
    with _connect() as conn:
        conn.executescript(DDL)
        conn.commit()


# ------------------------------------------------------------------ ingestion
def upsert_candles(rows: Iterable[dict[str, Any]], *,
                   retention_minutes: int = DEFAULT_RETENTION_MINUTES) -> dict[str, Any]:
    """Persist M1 candles. Input rows carry ``asset, ts_utc, open, high, low, close``
    plus provenance. Returns a small receipt (inserted/updated/skipped)."""
    init_store()
    inserted = updated = skipped = 0
    with _connect() as conn:
        for row in rows:
            asset = str(row.get("asset") or "").strip().upper()
            provider = str(row.get("provider") or "unknown")
            ts = _iso_minute(row.get("ts_utc") or row.get("timestamp"))
            ohlc = [row.get(k) for k in ("open", "high", "low", "close")]
            if not asset or not ts or any(v is None for v in ohlc):
                skipped += 1
                continue
            try:
                values = [float(v) for v in ohlc]
            except (TypeError, ValueError):
                skipped += 1
                continue
            if len({round(v, 10) for v in values}) == 0:      # pragma: no cover - defensive
                skipped += 1
                continue
            volume = row.get("volume")
            payload = (
                asset, provider, ts, float(row.get("resolution_minutes") or 1.0),
                values[0], values[1], values[2], values[3],
                None if volume is None else float(volume),
                1 if row.get("is_simulated") else 0,
                1 if row.get("is_replay") else 0,
                row.get("source_class"), row.get("source_label"),
                1 if row.get("realtime") else 0,
                str(row.get("ingested_at_utc") or utc_iso()),
            )
            exists = conn.execute(
                f"SELECT 1 FROM {TABLE} WHERE asset=? AND provider=? AND ts_utc=?",
                (asset, provider, ts),
            ).fetchone()
            conn.execute(
                f"INSERT OR REPLACE INTO {TABLE} (asset, provider, ts_utc, resolution_minutes, open, high, "
                "low, close, volume, is_simulated, is_replay, source_class, source_label, realtime, "
                "ingested_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
            if exists:
                updated += 1
            else:
                inserted += 1
        if retention_minutes > 0:
            cutoff = utc_iso(datetime.now(tz=timezone.utc) - timedelta(minutes=retention_minutes))
            conn.execute(f"DELETE FROM {TABLE} WHERE ts_utc < ?", (cutoff,))
        conn.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


# --------------------------------------------------------------------- reads
def _row_to_dict(row) -> dict[str, Any]:
    keys = row.keys()
    data = {k: row[k] for k in keys}
    data["is_simulated"] = bool(data.get("is_simulated"))
    data["is_replay"] = bool(data.get("is_replay"))
    data["realtime"] = bool(data.get("realtime"))
    return data


def latest_row(asset: str, provider: str | None = None) -> dict[str, Any] | None:
    init_store()
    symbol = str(asset).upper()
    with _connect() as conn:
        if provider:
            cur = conn.execute(
                f"SELECT * FROM {TABLE} WHERE asset=? AND provider=? ORDER BY ts_utc DESC LIMIT 1",
                (symbol, provider),
            )
        else:
            cur = conn.execute(
                f"SELECT * FROM {TABLE} WHERE asset=? ORDER BY ts_utc DESC LIMIT 1", (symbol,)
            )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def latest_provider(asset: str) -> str | None:
    row = latest_row(asset)
    return row["provider"] if row else None


def candles(asset: str, *, provider: str | None = None, limit: int = 300,
            until: datetime | None = None) -> list[dict[str, Any]]:
    """Newest `limit` candles (optionally only those opened at or before `until`).

    `until` is what keeps a historical replay honest: a scan clocked at an earlier
    moment must not see later rows, even when the store already holds them.  Live
    callers pass no `until`, so production behaviour is unchanged.
    """
    init_store()
    symbol = str(asset).upper()
    clauses = ["asset=?"]
    params: list[Any] = [symbol]
    if provider:
        clauses.append("provider=?")
        params.append(provider)
    if until is not None:
        cutoff = utc_iso(to_utc(until))
        clauses.append("ts_utc<=?")
        params.append(cutoff)
    params.append(int(limit))
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM {TABLE} WHERE {' AND '.join(clauses)} ORDER BY ts_utc DESC LIMIT ?",
            tuple(params),
        )
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    return list(reversed(rows))


def count(asset: str, *, provider: str | None = None) -> int:
    init_store()
    symbol = str(asset).upper()
    with _connect() as conn:
        if provider:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE asset=? AND provider=?", (symbol, provider)
            ).fetchone()
        else:
            row = conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE asset=?", (symbol,)).fetchone()
    return int(row[0]) if row else 0


def assets() -> list[str]:
    init_store()
    with _connect() as conn:
        rows = conn.execute(f"SELECT DISTINCT asset FROM {TABLE} ORDER BY asset").fetchall()
    return [r[0] for r in rows]


def gaps(asset: str, *, provider: str | None = None, limit: int = 500,
         tolerance_minutes: int = 1) -> dict[str, Any]:
    """Detect missing minutes in the stored series. A gap is a *fact about the feed*,
    never patched or interpolated."""
    rows = candles(asset, provider=provider, limit=limit)
    stamps = [datetime.fromisoformat(r["ts_utc"].replace("Z", "+00:00")) for r in rows]
    missing: list[str] = []
    for prev, nxt in zip(stamps, stamps[1:]):
        delta = (nxt - prev).total_seconds() / 60.0
        step = int(round(delta))
        if step > tolerance_minutes:
            for i in range(1, step):
                missing.append(utc_iso(prev + timedelta(minutes=i)))
    return {
        "asset": str(asset).upper(),
        "provider": provider or (rows[-1]["provider"] if rows else None),
        "window_rows": len(rows),
        "missing_minutes": len(missing),
        "first_missing_utc": missing[0] if missing else None,
        "last_missing_utc": missing[-1] if missing else None,
        "sample": missing[:10],
    }


def frame_for(asset: str, *, provider: str | None = None, limit: int = 300) -> CandleFrame:
    """Build a `CandleFrame` from stored candles — the same object type every other
    provider returns, so the engine cannot tell the difference (except that the
    provenance is honest about who supplied the rows)."""
    rows = candles(asset, provider=provider, limit=limit)
    if not rows:
        raise MarketDataUnavailable(str(asset).upper(), "No stored candles for this asset", "live_store",
                                    "ingest ticks or candles first")
    newest = rows[-1]
    resolved_provider = provider or newest["provider"]
    df = pd.DataFrame(
        {
            "open": [r["open"] for r in rows],
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r["volume"] for r in rows],
        },
        index=pd.DatetimeIndex([pd.Timestamp(r["ts_utc"]) for r in rows], tz="UTC"),
    )
    close_utc = pd.Timestamp(newest["ts_utc"]) + pd.Timedelta(minutes=float(newest["resolution_minutes"]))
    meta = FeedMeta(
        asset=str(asset).upper(),
        provider=resolved_provider,
        source_label=newest.get("source_label") or f"Rolling M1 store ({resolved_provider})",
        mode="REALTIME" if newest.get("realtime") else "REPLAY",
        realtime=bool(newest.get("realtime")),
        is_simulated=bool(newest.get("is_simulated")),
        is_replay=bool(newest.get("is_replay")),
        delayed_seconds=0.0,
        source_class=newest.get("source_class"),
        retrieved_at_utc=utc_iso(),
        notes=(
            "Candles served from the continuous ingestion store; each row keeps the provider "
            "label it arrived with."
        ),
    )
    frame = make_frame(str(asset).upper(), df, meta, min_candles=1)
    frame.meta.latest_candle_open_utc = newest["ts_utc"]
    frame.meta.latest_candle_close_utc = utc_iso(close_utc.to_pydatetime())
    return frame


def verified_price(asset: str, at_utc: datetime, *, field: str = "close",
                   provider: str | None = None) -> float | None:
    """Exact lookup used by the outcome engine: the price of the candle that
    *closed* at ``at_utc`` (equivalently: the candle opening at ``at_utc`` minus the
    resolution). Returns None when the candle is not stored — callers must treat
    None as UNRESOLVED, never as a reason to guess."""
    stamp = _iso_minute(at_utc)
    if stamp is None:
        return None
    init_store()
    symbol = str(asset).upper()
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM {TABLE} WHERE asset=? AND ts_utc=? "
            + ("AND provider=? " if provider else "")
            + "ORDER BY ingested_at_utc DESC LIMIT 1",
            (symbol, stamp, provider) if provider else (symbol, stamp),
        )
        row = cur.fetchone()
    if not row:
        return None
    data = _row_to_dict(row)
    if field not in data:
        return None
    value = data.get(field)
    return None if value is None else float(value)


def purge(older_than_minutes: int) -> int:
    init_store()
    cutoff = utc_iso(datetime.now(tz=timezone.utc) - timedelta(minutes=int(older_than_minutes)))
    with _connect() as conn:
        cur = conn.execute(f"DELETE FROM {TABLE} WHERE ts_utc < ?", (cutoff,))
        conn.commit()
        return int(cur.rowcount or 0)


def _iso_minute(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return utc_iso(dt.astimezone(timezone.utc).replace(second=0, microsecond=0))
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return utc_iso(parsed.astimezone(timezone.utc).replace(second=0, microsecond=0))


def rows_as_records(asset: str, *, provider: str | None = None, limit: int = 500) -> Sequence[dict[str, Any]]:
    return candles(asset, provider=provider, limit=limit)
