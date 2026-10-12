"""Provider registry.

Resolves an asset to a verified `CandleFrame` using a documented priority chain
and records every attempt in `FeedMeta.fallback_chain` so the UI can show the
user exactly where the numbers came from.

data_source_mode
----------------
auto        csv (if present) → broker feed (if configured) → public delayed feed
            → SIMULATED (clearly labelled; blocked in production by policy)
real_only   csv → broker feed → public delayed feed. NO synthetic fallback.
live_only   broker feed → public delayed feed (no local CSV). NO synthetic fallback.
simulated   simulated only
csv         local CSV only

A provider failure never silently upgrades data quality: a simulated frame is
always `is_simulated=True` and carries a warning note.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime
from typing import Any

from ..constants import ASSET_MAP, GATES, AssetSpec, canonical_symbol
from .base import (
    CandleFrame,
    FeedMeta,
    MarketDataUnavailable,
    SOURCE_CLASS_FIXTURE,
    SOURCE_CLASS_MARKET,
    SOURCE_CLASS_SIMULATED,
    data_lag_seconds,
)
from . import provenance as prov
from .providers import (BrokerFeedProvider, CsvProvider, SimulatedProvider, TwelveDataFeedProvider,
                        YahooProvider)
from .providers.live_store import live_store
from .providers.csv_provider import import_csv_text
from ..timeutil import utc_iso
from .validation import (adapter_view, data_quality_block,
                         unavailable_report, validate_candles)

_CACHE_TTL = 3.0


class MarketDataRegistry:
    def __init__(self) -> None:
        self.broker = BrokerFeedProvider()
        #: Twelve Data realtime standard FX. It does not join the fetch chain below: the
        #: candles it produces are already served by `live_store` with their own provider
        #: label, so the engine path is unchanged. It is registered here for health/status
        #: visibility and for the OTC refusal it documents.
        self.twelve_data = TwelveDataFeedProvider()
        self.yahoo = YahooProvider()
        self.csv = CsvProvider()
        self.simulated = SimulatedProvider()
        self._cache: dict[str, tuple[float, CandleFrame]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    def asset(self, symbol: str) -> AssetSpec:
        canonical = canonical_symbol(symbol)
        spec = ASSET_MAP.get(canonical) if canonical else None
        if spec is None:
            raise MarketDataUnavailable(symbol, "Unknown asset symbol", "registry")
        return spec

    def provider_report(self) -> list[dict[str, Any]]:
        return [
            self.broker.status(),
            self.twelve_data.status(),
            {"provider": "yahoo", "configured": True, "adapter_state": "READY",
             "detail": "Public delayed feed for the non-OTC FX pairs; commonly rate-limited."},
            {"provider": "csv", "configured": True, "adapter_state": "READY",
             "detail": "Primary REAL-data path: drop backend/data/csv/<ASSET>.csv or POST /api/data/import."},
            {"provider": "simulated", "configured": True, "adapter_state": "READY",
             "detail": "Deterministic demo series. Always flagged is_simulated=true."},
        ]

    def _providers_for(self, spec: AssetSpec, mode: str) -> list[tuple[str, Any]]:
        if mode == "simulated":
            return [("simulated", self.simulated)]
        if mode == "csv":
            return [("csv", self.csv)]
        # Continuous ingestion first: if the rolling store holds candles for this asset
        # they are the newest verified rows available, and the adapter keeps each row's
        # own provider/simulated/replay labels (it falls through when the store is empty
        # for the asset, so nothing about the existing chains changes when no feed runs).
        if spec.kind == "otc":
            chain: list[tuple[str, Any]] = [("live_store", live_store), ("broker_ws", self.broker),
                                            ("csv", self.csv)]
            if mode == "auto":
                chain.append(("simulated", self.simulated))
            return chain
        # forex: ingested feed → operator CSV → public delayed feed
        chain = [("live_store", live_store), ("csv", self.csv), ("yahoo", self.yahoo)]
        if mode == "live_only":
            chain = [("live_store", live_store), ("broker_ws", self.broker), ("yahoo", self.yahoo)]
        if mode == "auto":
            chain.append(("simulated", self.simulated))
        # real_only: ingested → csv → broker → public delayed. NEVER simulated.
        return chain

    # -------------------------------------------------------------------- fetch
    def get_frame(
        self,
        symbol: str,
        limit: int = 300,
        data_source_mode: str = "auto",
        use_cache: bool = True,
        now: datetime | None = None,
        _skip_autoregister: bool = False,
    ) -> CandleFrame:
        spec = self.asset(symbol)
        if not _skip_autoregister and prov.source_class_for(symbol) is None:
            # First read of an operator file: validate it once and record provenance
            # so the DATA STATUS panel never shows an unverified dataset.
            if self.csv.available(spec):
                try:
                    self.register_file(symbol)
                except Exception:  # noqa: BLE001 — never blocking a read on bookkeeping
                    pass
        cache_key = f"{symbol}:{limit}:{data_source_mode}"
        if use_cache and data_source_mode != "csv":
            with self._lock:
                hit = self._cache.get(cache_key)
                if hit and (time.time() - hit[0]) < _CACHE_TTL:
                    return hit[1]

        chain: list[dict[str, Any]] = []
        last_error: MarketDataUnavailable | None = None
        for name, provider in self._providers_for(spec, data_source_mode):
            try:
                frame = provider.fetch(spec, limit=limit, now=now)
            except MarketDataUnavailable as exc:
                chain.append({"provider": name, "status": "FAILED", "reason": exc.reason,
                              **({"detail": exc.detail} if exc.detail else {})})
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001 — provider bugs must not look like market data
                chain.append({"provider": name, "status": "ERROR",
                              "reason": f"{type(exc).__name__}: {str(exc)[:160]}"})
                last_error = MarketDataUnavailable(symbol, f"Provider error in {name}", name, str(exc)[:160])
                continue
            chain.append({
                "provider": name,
                "status": "OK",
                "source_label": frame.meta.source_label,
                "is_simulated": frame.meta.is_simulated,
                "mode": frame.meta.mode,
            })
            frame.meta.fallback_chain = chain
            if len(chain) > 1 and not frame.meta.is_simulated:
                frame.meta.notes = (
                    f"{frame.meta.notes} Earlier providers failed: "
                    + ", ".join(f"{c['provider']} ({c.get('reason', 'failed')})" for c in chain[:-1])
                ).strip()
            with self._lock:
                self._cache[cache_key] = (time.time(), frame)
            return frame

        attempted = ", ".join(step["provider"] for step in chain) or "none"
        raise MarketDataUnavailable(
            symbol,
            f"No verified market data available (tried: {attempted})",
            last_error.provider if last_error else "registry",
            (
                " | ".join(f"{step['provider']}: {step.get('reason', 'failed')}" for step in chain)
                or (last_error.detail if last_error else None)
            ),
        )

    def probe(self, symbol: str, data_source_mode: str = "auto") -> dict[str, Any]:
        """Non-raising availability probe used by /api/assets and AUTO SELECT."""
        try:
            frame = self.get_frame(symbol, limit=200, data_source_mode=data_source_mode, use_cache=False)
        except MarketDataUnavailable as exc:
            return {"asset": symbol, "available": False, "reason": exc.reason,
                    "provider": exc.provider, "detail": exc.detail}
        return {
            "asset": symbol,
            "available": True,
            "provider": frame.meta.provider,
            "source_label": frame.meta.source_label,
            "mode": frame.meta.mode,
            "simulated": frame.meta.is_simulated,
            "candles": frame.meta.candles,
            "latest_candle_utc": frame.meta.latest_candle_open_utc,
            "live_price": frame.live_price,
        }

    # ------------------------------------------------------- dataset management
    def import_csv(self, asset_symbol: str, csv_text: str, **kwargs: Any) -> dict[str, Any]:
        """Validate + persist an operator CSV, then invalidate the frame cache."""
        result = import_csv_text(asset_symbol, csv_text, directory=self.csv.directory, **kwargs)
        if result.get("status") == "IMPORTED":
            self.clear_cache()
        return result

    def register_file(self, asset_symbol: str, *, source_class: str = SOURCE_CLASS_MARKET,
                      operator_label: str = "", force: bool = False) -> dict[str, Any]:
        """Validate + register a CSV that already sits in the dataset folder.

        Used by `POST /api/data/import` when the operator copied a file in
        manually, and automatically the first time an unregistered file is read.
        """
        canonical = canonical_symbol(asset_symbol)
        if canonical is None:
            return {"status": "REJECTED", "asset": asset_symbol,
                    "reason": f"Unknown asset '{asset_symbol}'"}
        spec = ASSET_MAP[canonical]
        path = self.csv.path_for(spec)
        if not path.exists():
            return {"status": "REJECTED", "asset": canonical,
                    "reason": f"No dataset file at {path.name}",
                    "expected_path": str(path)}
        existing = prov.get(canonical)
        if existing and not force:
            return {"status": "ALREADY_REGISTERED", "asset": canonical, "provenance": existing}

        try:
            # validate the WHOLE dataset, not just the tail used for inference
            frame = self.get_frame(canonical, limit=1_000_000, data_source_mode="csv",
                                   use_cache=False, _skip_autoregister=True)
        except MarketDataUnavailable as exc:
            return {"status": "REJECTED", "asset": canonical, "reason": exc.reason, "detail": exc.detail}

        report = validate_candles(frame.df, check_freshness=True)
        if not report.ok:
            return {"status": "REJECTED", "asset": canonical, "validation": report.as_dict(),
                    "reason": "; ".join(report.errors[:4])}

        record = prov.record_import(
            canonical,
            source_class=source_class,
            provider="csv",
            source_label=f"Operator CSV: {path.name}",
            filename=path.name,
            rows=int(len(frame.df)),
            first_candle_utc=report.first_candle_utc,
            last_candle_utc=report.last_candle_utc,
            span_hours=report.span_hours,
            has_volume=report.has_volume,
            sha256=prov.file_sha256(path),
            validation=report.as_dict(),
            notes="Registered from a local dataset file after validation.",
            operator_label=operator_label,
        )
        self.clear_cache()
        return {"status": "REGISTERED", "asset": canonical, "rows": int(len(frame.df)),
                "validation": report.as_dict(), "provenance": record.as_dict()}

    def adapter_health(self, *, probe: bool = False) -> dict[str, Any]:
        """Health of every provider behind the same adapter interface (spec §11)."""
        providers = {
            "csv": self.csv,
            "broker_ws": self.broker,
            "twelve_data": self.twelve_data,
            "public_feed": self.yahoo,
            "simulated": self.simulated,
        }
        out: dict[str, Any] = {}
        for key, provider in providers.items():
            try:
                report = provider.health()
            except Exception as exc:  # noqa: BLE001 - health must never break the API
                report = {"provider": getattr(provider, "name", key), "connected": False,
                          "status": "UNKNOWN", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
            report["key"] = key
            out[key] = report
        live_capable = [k for k, v in out.items() if v.get("realtime_capable") and v.get("connected")]
        streaming = [k for k, v in out.items() if v.get("status") == "STREAMING"]
        out["_summary"] = {
            "providers": len(providers),
            "connected": sum(1 for k, v in out.items() if k != "_summary" and v.get("connected")),
            "realtime_capable_connected": live_capable,
            "streaming": streaming,
            "note": (
                "a continuously updating M1 provider is required for live paper signalling; without one the "
                "engine reports DATA UNAVAILABLE rather than analysing a snapshot"
            ),
            "checked_at_utc": utc_iso(),
        }
        return out

    def dataset_status(self, symbol: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Full DATA STATUS record for one asset (spec §2).

        Always returns a record — unavailable assets report DATA UNAVAILABLE with
        the provider chain that was tried, never a fabricated row count.
        """
        canonical = canonical_symbol(symbol)
        if canonical is None:
            return unavailable_report(symbol, "Unknown asset symbol", "not in the configured universe", "registry")

        spec = ASSET_MAP[canonical]
        record = prov.get(canonical) or {}
        path = self.csv.path_for(spec)
        file_present = path.exists()
        chain_note = [
            {"provider": "csv", "status": "OK" if file_present else "MISSING",
             "detail": "operator dataset" if file_present else f"no file at {path}"},
            {"provider": "broker_ws", "status": "OK" if self.broker.configured else "NOT_CONFIGURED",
             "detail": self.broker.status()["detail"]},
            {"provider": "public_feed",
             "status": "AVAILABLE" if spec.yahoo_symbol else "NOT_APPLICABLE",
             "detail": (f"delayed public feed ({spec.yahoo_symbol})" if spec.yahoo_symbol
                        else "no public source for this instrument")},
        ]

        if not file_present:
            return unavailable_report(
                canonical,
                "No operator dataset for this asset",
                (
                    f"expected {path.name} in the dataset folder or POST /api/data/import"
                    + ("" if spec.yahoo_symbol else " — this instrument has no public data source")
                ),
                "csv",
                chain_note,
                quality=data_quality_block(None, rows=0),
                adapter=adapter_view(type("M", (), {"provider": "csv", "display_status": "DATA UNAVAILABLE",
                                                    "is_replay": False, "is_market_data": False})(),
                                     self.adapter_health()),
            )

        try:
            frame = self.get_frame(canonical, limit=1_000_000, data_source_mode="csv",
                                   use_cache=False, now=now, _skip_autoregister=True)
        except MarketDataUnavailable as exc:
            failed = getattr(exc, "validation", None)
            return unavailable_report(
                canonical, exc.reason, exc.detail, exc.provider, chain_note,
                quality=(data_quality_block(failed) if failed else data_quality_block(None, rows=0)),
                adapter=adapter_view(type("M", (), {"provider": exc.provider,
                                                    "display_status": "DATA UNAVAILABLE",
                                                    "is_replay": False, "is_market_data": False})(),
                                     self.adapter_health()),
            )

        report = validate_candles(frame.df, check_freshness=True, now=now)
        lag = data_lag_seconds(frame.meta, now)
        freshness = None if not math.isfinite(lag) else round(float(lag), 1)
        meta = frame.meta
        return {
            "asset": canonical,
            "status": "OK" if report.ok else "DEGRADED",
            "badge": meta.badge,
            "display_status": meta.display_status,
            "provider": meta.provider,
            "source_label": meta.source_label,
            "source_class": meta.source_class,
            "source_status": meta.source_status,
            "timeframe": meta.timeframe,
            "rows": int(len(frame.df)),
            "candles_available": int(len(frame.df)),
            "latest_candle_open_utc": meta.latest_candle_open_utc,
            "latest_candle_close_utc": meta.latest_candle_close_utc,
            "last_update_utc": meta.last_update_utc,
            "freshness_seconds": freshness,
            "is_realtime": meta.realtime,
            "is_replay": meta.is_replay,
            "is_simulated": meta.is_simulated,
            "is_real_market_data": meta.is_market_data,
            "has_volume": meta.has_volume,
            "validation": report.as_dict(),
            "provenance": record or None,
            "providers_tried": chain_note,
            "notes": meta.notes,
            "adapter": adapter_view(meta, self.adapter_health()),
            "quality": data_quality_block(report, rows=int(len(frame.df)),
                                          latest_candle_utc=meta.latest_candle_open_utc,
                                          interval_minutes=report.interval_minutes,
                                          stale_seconds=freshness),
            "as_of_utc": utc_iso(),
        }

    def dataset_status_all(self, symbols: list[str] | None = None, *,
                           now: datetime | None = None) -> dict[str, Any]:
        assets = symbols or [a.symbol for a in ASSET_MAP.values()]
        records = [self.dataset_status(symbol, now=now) for symbol in assets]
        available = [r for r in records if r.get("status") in {"OK", "DEGRADED"}]
        market = [r for r in available if r.get("is_real_market_data")]
        return {
            "assets": records,
            "adapter_health": self.adapter_health(),
            "counts": {
                "total": len(records),
                "available": len(available),
                "unavailable": len(records) - len(available),
                "real_market": len(market),
                "simulated": len([r for r in available if r.get("is_simulated")]),
                "replay": len([r for r in available if r.get("is_replay")]),
                "stale": len([r for r in available
                              if (r.get("freshness_seconds") or 0) > GATES["max_replay_lag_seconds"]]),
            },
            "data_source_mode": None,
            "as_of_utc": utc_iso(),
        }

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


registry = MarketDataRegistry()
