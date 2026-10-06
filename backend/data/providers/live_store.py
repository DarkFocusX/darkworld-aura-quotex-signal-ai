"""Provider adapter over the rolling M1 store (continuous-ingestion phase).

This adapter is how a continuously ingested feed becomes tradable without touching
the engine: it returns the *same* `CandleFrame` every other provider returns, and
it keeps the honest provenance of each stored row (provider name, simulated /
replay flags, realtime capability). If the store has no rows for an asset it
raises `MarketDataUnavailable`, so the registry falls through to the next provider
exactly as it does today.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ...timeutil import utc_iso
from ..base import (
    CandleFrame,
    FeedMeta,
    MarketDataUnavailable,
    SOURCE_CLASS_MARKET,
    SOURCE_CLASS_SIMULATED,
    make_frame,
)
from .. import store

ADAPTER_NAME = "live_store"


class LiveStoreProvider:
    name = ADAPTER_NAME
    label = "Continuous ingestion store"
    realtime = True
    is_simulated = False
    source_class = SOURCE_CLASS_MARKET

    # ------------------------------------------------- adapter interface (spec §11)
    def connect(self) -> dict[str, Any]:
        return self.health()

    def disconnect(self) -> None:      # the store has no connection to close
        return None

    def health(self) -> dict[str, Any]:
        try:
            symbols = store.assets()
        except Exception as exc:  # noqa: BLE001 — health() must never raise
            return {"provider": ADAPTER_NAME, "label": self.label, "connected": False, "status": "ERROR",
                    "detail": f"{type(exc).__name__}: {exc}", "realtime_capable": True,
                    "supports_subscribe": False, "is_simulated": False, "last_update_utc": None,
                    "checked_at_utc": utc_iso()}
        newest = None
        rows = 0
        for symbol in symbols:
            row = store.latest_row(symbol)
            if not row:
                continue
            rows += store.count(symbol, provider=row["provider"])
            stamp = row["ingested_at_utc"]
            if newest is None or stamp > newest:
                newest = stamp
        return {
            "provider": ADAPTER_NAME,
            "label": self.label,
            "connected": True,
            "status": "READY" if symbols else "EMPTY",
            "detail": ("candles written by the continuous ingestion service; each row keeps its provider "
                       "label and simulated/replay flags"),
            "realtime_capable": True,
            "supports_subscribe": False,
            "is_simulated": False,
            "assets": symbols,
            "rows": rows,
            "last_update_utc": newest,
            "checked_at_utc": utc_iso(),
        }

    def latest_tick(self, asset: Any) -> dict[str, Any] | None:
        symbol = getattr(asset, "symbol", asset)
        row = store.latest_row(str(symbol))
        if not row:
            return None
        return {"asset": str(symbol).upper(), "price": row["close"], "time_utc": row["ts_utc"],
                "provider": row["provider"], "is_simulated": row["is_simulated"]}

    def subscribe(self, asset: Any, callback: Any | None = None) -> dict[str, Any]:
        return {"provider": ADAPTER_NAME, "asset": getattr(asset, "symbol", asset), "supported": False,
                "mode": "pull", "detail": "reads from the rolling store; use the ingestion service to write"}

    def fetch(self, asset: Any, limit: int = 300, now: datetime | None = None) -> CandleFrame:
        symbol = str(getattr(asset, "symbol", asset)).upper()
        # Respect the caller's clock at the *query* level as well: asking for the newest
        # N rows of a store that holds later rows would return nothing once the caller's
        # cutoff is applied.  Live scans (now ≈ newest) are unaffected.
        rows = store.candles(symbol, limit=limit, until=now)
        if now is not None:
            cutoff = now
            if cutoff.tzinfo is None:
                from datetime import timezone as _tz

                cutoff = cutoff.replace(tzinfo=_tz.utc)
            visible = []
            for row in rows:
                close_at = pd.Timestamp(row["ts_utc"]) + pd.Timedelta(
                    minutes=float(row.get("resolution_minutes") or 1.0))
                if close_at.to_pydatetime() <= cutoff:
                    visible.append(row)
            rows = visible
        if not rows:
            raise MarketDataUnavailable(symbol, "No continuously ingested candles stored for this asset",
                                        ADAPTER_NAME, "start the ingestion service or POST candles/ticks")
        newest = rows[-1]
        provider = newest["provider"]
        # Serve the contiguous series from the provider that owns the newest row, so a
        # frame never silently splices two different feeds together.
        series = [row for row in rows if row["provider"] == provider]
        df = pd.DataFrame(
            {
                "open": [r["open"] for r in series],
                "high": [r["high"] for r in series],
                "low": [r["low"] for r in series],
                "close": [r["close"] for r in series],
                "volume": [r["volume"] for r in series],
            },
            index=pd.DatetimeIndex([pd.Timestamp(r["ts_utc"]) for r in series], tz="UTC"),
        )
        close_at = pd.Timestamp(newest["ts_utc"]) + pd.Timedelta(minutes=float(newest["resolution_minutes"]))
        meta = FeedMeta(
            asset=symbol,
            provider=provider,
            source_label=newest.get("source_label") or self.label,
            mode="REALTIME" if newest["realtime"] else "REPLAY",
            realtime=bool(newest["realtime"]),
            is_simulated=bool(newest["is_simulated"]),
            is_replay=bool(newest["is_replay"]),
            delayed_seconds=0.0,
            source_class=newest.get("source_class") or (
                SOURCE_CLASS_SIMULATED if newest["is_simulated"] else SOURCE_CLASS_MARKET),
            retrieved_at_utc=utc_iso(),
            notes=("Candles served from the continuous ingestion store "
                   f"({store.count(symbol, provider=provider)} stored rows for provider '{provider}')."),
        )
        # pass a low floor: the engine's own DATA CHECK decides whether the stored
        # series is long enough (>= 120 candles), so the rejection reason is exact
        frame = make_frame(symbol, df, meta, min_candles=1)
        frame.meta.latest_candle_open_utc = newest["ts_utc"]
        frame.meta.latest_candle_close_utc = utc_iso(close_at.to_pydatetime())
        frame.meta.last_update_utc = utc_iso(close_at.to_pydatetime())
        return frame


live_store = LiveStoreProvider()
