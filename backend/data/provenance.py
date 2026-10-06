"""Dataset provenance store.

Every candle dataset the engine can read has a *provenance record*: where it came
from, when it was imported, how many rows it had, the validation outcome and a
content hash. The DATA PROVENANCE panel and `/api/data/status` read from here, and
the signal policy uses it to decide whether a frame is real market data.

Design rules:
  * the operator declares the data class (`MARKET` | `SIMULATED` | `FIXTURE`);
  * the declaration is stored verbatim and shown in the UI — nothing is upgraded
    silently;
  * a record for an asset that no longer has a file is reported as MISSING.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import DATA_DIR
from ..timeutil import utc_iso

PROVENANCE_PATH = DATA_DIR / "provenance.json"

SOURCE_MARKET = "MARKET"
SOURCE_SIMULATED = "SIMULATED"
SOURCE_FIXTURE = "FIXTURE"
VALID_SOURCE_CLASSES = (SOURCE_MARKET, SOURCE_SIMULATED, SOURCE_FIXTURE)

_LOCK = threading.RLock()


@dataclass
class ProvenanceRecord:
    asset: str
    source_class: str = SOURCE_MARKET
    provider: str = "csv"
    source_label: str = ""
    filename: str | None = None
    imported_at_utc: str | None = None
    rows: int = 0
    first_candle_utc: str | None = None
    last_candle_utc: str | None = None
    span_hours: float | None = None
    timeframe: str = "M1"
    has_volume: bool = False
    sha256: str | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    operator_label: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["is_real_market_data"] = self.source_class == SOURCE_MARKET
        return payload


def _load() -> dict[str, Any]:
    if not PROVENANCE_PATH.exists():
        return {}
    try:
        data = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a corrupt file must not take the API down
        return {}


def _save(data: dict[str, Any]) -> None:
    PROVENANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROVENANCE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(PROVENANCE_PATH)


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def record_import(
    asset: str,
    *,
    source_class: str,
    provider: str = "csv",
    source_label: str = "",
    filename: str | None = None,
    rows: int = 0,
    first_candle_utc: str | None = None,
    last_candle_utc: str | None = None,
    span_hours: float | None = None,
    timeframe: str = "M1",
    has_volume: bool = False,
    sha256: str | None = None,
    validation: dict[str, Any] | None = None,
    notes: str = "",
    operator_label: str = "",
    imported_at: datetime | None = None,
) -> ProvenanceRecord:
    source_class = (source_class or SOURCE_MARKET).upper()
    if source_class not in VALID_SOURCE_CLASSES:
        source_class = SOURCE_MARKET
    record = ProvenanceRecord(
        asset=asset,
        source_class=source_class,
        provider=provider,
        source_label=source_label,
        filename=filename,
        imported_at_utc=utc_iso(imported_at) if imported_at else utc_iso(),
        rows=int(rows),
        first_candle_utc=first_candle_utc,
        last_candle_utc=last_candle_utc,
        span_hours=span_hours,
        timeframe=timeframe,
        has_volume=bool(has_volume),
        sha256=sha256,
        validation=validation or {},
        notes=notes,
        operator_label=operator_label,
    )
    with _LOCK:
        data = _load()
        data[asset] = record.as_dict()
        _save(data)
    return record


def get(asset: str) -> dict[str, Any] | None:
    with _LOCK:
        return _load().get(asset)


def all_records() -> dict[str, Any]:
    with _LOCK:
        return _load()


def clear(asset: str | None = None) -> None:
    with _LOCK:
        data = _load()
        if asset is None:
            data = {}
        else:
            data.pop(asset, None)
        _save(data)


def source_class_for(asset: str) -> str | None:
    """Declared class for an asset, or None when nothing was ever imported."""
    record = get(asset)
    return (record or {}).get("source_class")


# --------------------------------------------------------------- serving-window version
def window_version(meta, *, rows: int | None = None, first_open_utc: str | None = None) -> str:
    """Deterministic id for the *exact* data window a decision was served from.

    Stored on every paper-ledger row next to the model and feature versions, so a trade can
    be traced to the prices it actually saw. It is a fingerprint of what was served — it is
    never a claim about data quality, and it is deliberately a different id space from the
    research `ds-…` dataset fingerprints (different inputs, different purpose).
    """
    import hashlib

    parts = [
        str(getattr(meta, "asset", "") or ""),
        str(getattr(meta, "provider", "") or ""),
        str(getattr(meta, "source_class", "") or ""),
        str(getattr(meta, "badge", "") or ""),
        str(getattr(meta, "latest_candle_open_utc", "") or ""),
        str(getattr(meta, "latest_candle_close_utc", "") or ""),
        str(first_open_utc or ""),
        str(int(rows) if rows else ""),
        "sim" if getattr(meta, "is_simulated", False) else "real",
        "replay" if getattr(meta, "is_replay", False) else ("live" if getattr(meta, "realtime", False) else "static"),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"win-{digest}"
