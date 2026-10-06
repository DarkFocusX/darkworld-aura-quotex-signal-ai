"""Ingestion service (spec §1, §11 — continuous paper-trading collection).

Owns the process-wide feed and paper scanner, exposes their lifecycle over HTTP,
and stays OFF until explicitly enabled:

    ENABLE_INGESTION=true        start the continuous feed + paper scanner (API lifespan)
    PAPER_COLLECTION=true        record every scan (qualified and rejected) for research
    INGESTION_TICK_SECONDS=1     loop cadence

It also provides the one supported way to feed *verified* data into the store when
no broker WebSocket is wired: `POST /api/data/ingest` with real ticks/candles that
the operator's own process already holds. In development, a clearly-labelled
simulated feed can be started so the whole path is exercisable — it can never back
a production signal, because every gate (real data, READY_MARKET, freshness) still
applies to it unchanged.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

from ..data import store
from ..data.feed import ContinuousFeed, build_simulated_feed, simulated_tick
from ..timeutil import ist_clock, utc_iso, utc_now_naive
from . import paper as paper_state
from .scanner import PaperScanner, paper_collection_enabled
from . import ledger

EMPTY = "EMPTY"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class IngestionService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._feed: ContinuousFeed | None = None
        self._scanner: PaperScanner | None = None
        self.started_at_utc: str | None = None
        self.last_error: str | None = None
        self.mode: str | None = None                 # "simulated" | "external" | "broker"

    # ------------------------------------------------------------------ control
    def start(self, *, feed: ContinuousFeed | None = None, scanner: PaperScanner | None = None,
              mode: str | None = None) -> dict[str, Any]:
        with self._lock:
            if feed is not None:
                self._feed = feed
                self.mode = mode or "external"
            if scanner is not None:
                self._scanner = scanner
            if self._feed is None:
                self._feed = ContinuousFeed(tick_seconds=_env_float("INGESTION_TICK_SECONDS", 1.0))
                self.mode = self.mode or "external"
            if self._scanner is None:
                self._scanner = PaperScanner(self._feed,
                                             tick_seconds=_env_float("INGESTION_TICK_SECONDS", 1.0))
            # the feed owns the tick loop; the scanner is driven by it
            if not self._feed.running:
                self._scanner.feed = self._feed
                self._feed.on_candle_closed(self._on_candle)
                self._feed.start()
            if not self._scanner.running:
                self._scanner.start()
            self.started_at_utc = self.started_at_utc or utc_iso()
        return self.status()

    def start_simulated(self, *, realtime: bool = True) -> dict[str, Any]:
        """DEVELOPMENT helper: a clearly-labelled simulated continuous feed."""
        feed = build_simulated_feed(realtime=realtime)
        for symbol in _default_assets():
            feed.subscribe(symbol)
        return self.start(feed=feed, mode="simulated")

    def start_broker(self) -> dict[str, Any]:
        """Attach the broker adapter when it is configured and streaming."""
        from ..data.registry import registry

        adapter = registry.broker
        health = adapter.health()
        if not health.get("connected"):
            raise RuntimeError(f"broker feed is not connected (state={health.get('status')}); "
                               "set QUOTEX_WS_URL and wire the tick handler first")
        feed = ContinuousFeed(provider="broker_ws", source_label="Broker tick stream (operator feed)",
                              realtime=True, is_simulated=False, is_replay=False,
                              tick_seconds=_env_float("INGESTION_TICK_SECONDS", 1.0))
        assets = _default_assets()
        feed.subscribe_adapter(adapter, assets)
        return self.start(feed=feed, mode="broker")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._scanner:
                self._scanner.stop()
            if self._feed:
                self._feed.stop()
        return self.status()

    def ingest_market_tick(self, *, now: datetime | None = None) -> int:
        """DEV/simulated driver: push the deterministic simulated price for every
        subscribed asset. Only used when the feed is the simulated one."""
        with self._lock:
            feed = self._feed
        if feed is None or feed.provider != "simulated" or not feed.connected:
            return 0
        moment = now or datetime.now(tz=timezone.utc)
        pushed = 0
        for symbol in feed.health().get("subscribed_assets", []):
            feed.ingest_tick(symbol, simulated_tick(symbol, moment), time_utc=moment)
            pushed += 1
        return pushed

    def _on_candle(self, event: dict[str, Any]) -> None:
        """A candle closed → hand straight to the scanner (scanner also polls)."""
        with self._lock:
            scanner = self._scanner
        if scanner is None or not scanner.running:
            return
        # the scanner's own loop already evaluates on its next tick; this hook just
        # shortens the latency between a candle closing and the scan.
        try:
            scanner.tick()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------- state
    @property
    def feed(self) -> ContinuousFeed | None:
        with self._lock:
            return self._feed

    @property
    def scanner(self) -> PaperScanner | None:
        with self._lock:
            return self._scanner

    def status(self) -> dict[str, Any]:
        with self._lock:
            feed, scanner = self._feed, self._scanner
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "started_at_utc": self.started_at_utc,
            "last_error": self.last_error,
            "feed": feed.health() if feed else None,
            "scanner": scanner.status() if scanner else None,
            "paper_collection": paper_collection_enabled(),
            "assets_in_store": store.assets(),
            "note": ("Ingestion is independent of the production scheduler: starting it never changes the "
                     "gates, the confidence threshold or the asset policy."),
        }

    @property
    def enabled(self) -> bool:
        return bool(self._env_enabled or (self._feed is not None and self._feed.connected))

    @property
    def _env_enabled(self) -> bool:
        return _env_flag("ENABLE_INGESTION", False)

    def live_status(self, assets: list[str] | None = None, *, now: datetime | None = None) -> dict[str, Any]:
        """Spec §3 — `GET /api/data/live-status` payload, per asset."""
        payload = paper_state.live_status(assets, now=now)
        payload["ingestion"] = {
            "enabled": self.enabled,
            "mode": self.mode,
            "started_at_utc": self.started_at_utc,
            "paper_collection": paper_collection_enabled(),
        }
        return payload

    def current_ist(self, *, now: datetime | None = None) -> str:
        return ist_clock(now or utc_now_naive())


def _default_assets() -> list[str]:
    from ..settings_service import get_settings

    settings = get_settings()
    assets = [a for a in settings.enabled_assets if a]
    return assets or ["USDJPY"]


ingestion = IngestionService()
