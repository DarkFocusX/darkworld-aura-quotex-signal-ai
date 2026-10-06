"""Broker (Quotex-compatible) feed adapter.

Quotex publishes no official public market-data API. OTC pairs in particular are
broker-internal instruments: there is no independent public source for their quotes.
This adapter therefore never invents a URL, a session or a candle. It is the
*integration point* for a feed the operator is authorised to use:

1. **Out-of-process bridge (supported path).** `tools/start_live_feed.py` holds the
   credentials, connects, parses messages and builds closed M1 candles, then delivers
   them to the API (`POST /api/data/ingest`) or straight into the rolling store. Those
   rows carry the provider label the operator declares (default `operator_bridge`) and
   `realtime=true`, so the engine treats them as a realtime market series.
   `tools/probe_live_feed.py` reports CONNECTED / NOT_CONFIGURED / AUTH_FAILED /
   NO_DATA / INVALID_DATA for that path.

2. **In-process adapter (this class).** When the operator attaches their own tick
   handler inside the API process (calling `push_tick` / `push_candle` from their
   socket loop), the same state machine, health payload and store reads below apply,
   and `IngestionService.start_broker()` can drive it.

Both paths share one rule: with no verified stream this adapter reports
`NOT_CONFIGURED` (or `AWAITING_TICK_STREAM`) and `fetch()` raises
`MarketDataUnavailable`, so the engine falls through to the next provider and the UI
shows DATA UNAVAILABLE. Nothing is fabricated to fill the gap.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ...constants import AssetSpec
from ...timeutil import utc_iso
from .. import store
from ..base import (ADAPTER_NOT_CONFIGURED, ADAPTER_STREAMING, CandleFrame,
                    MarketDataAdapter, MarketDataUnavailable)
from ..feedsource import (STATE_AUTH_FAILED, STATE_CONNECTED, STATE_DISCONNECTED, STATE_ERROR,
                          STATE_NOT_CONFIGURED, register_secret, redact)
from ..feed import validate_candle

MAX_BUFFERED_TICKS_PER_ASSET = 5000


class BrokerFeedProvider(MarketDataAdapter):
    name = "broker_ws"
    realtime = True
    label = "Broker feed (Quotex-compatible adapter)"
    is_simulated = False

    def __init__(self, endpoint: str | None = None, credentials: str | None = None):
        self.endpoint = (endpoint or os.environ.get("QUOTEX_WS_URL", "")).strip()
        self.credentials = (credentials or os.environ.get("QUOTEX_SSID", "")).strip()
        register_secret(self.credentials)
        self._buffer: dict[str, list[dict]] = {}
        self._state = STATE_NOT_CONFIGURED if not self.endpoint else STATE_DISCONNECTED
        self._state_since = utc_iso()
        self._last_tick_utc: str | None = None
        self._last_candle_utc: str | None = None
        self._last_message_utc: str | None = None
        self._ticks = 0
        self._candles = 0
        self._rejected = 0
        self._ignored = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------------ configuration
    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    @property
    def endpoint_set(self) -> bool:
        return bool(self.endpoint)

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._state_since = utc_iso()

    def status(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "configured": self.configured,
            "endpoint_set": bool(self.endpoint),
            "credentials_set": bool(self.credentials),
            "adapter_state": "AWAITING_TICK_STREAM" if self.configured else "NOT_CONFIGURED",
            "detail": self._detail(),
        }

    def _detail(self) -> str:
        if not self.configured:
            return ("Set QUOTEX_WS_URL / QUOTEX_SSID (or run tools/start_live_feed.py) to stream real "
                    "candles; without a verified stream this adapter stays NOT_CONFIGURED.")
        if self._state == STATE_CONNECTED:
            return f"verified tick stream attached ({self._ticks} tick(s), {self._candles} candle(s) stored)"
        if self._state == STATE_AUTH_FAILED:
            return f"the feed rejected the session: {self._last_error}"
        if self._state == STATE_ERROR:
            return f"transport error: {self._last_error}"
        return "endpoint set, waiting for a verified tick/candle stream"

    # ---------------------------------------------------- adapter interface (spec §11)
    def connect(self) -> dict:
        """Verify the adapter is wired; never invents a session."""
        if self.configured and self._state == STATE_NOT_CONFIGURED:
            self._set_state(STATE_DISCONNECTED)
        return self.health()

    def disconnect(self) -> None:
        with self._lock():  # noqa: SIM117 - explicit for readability
            self._buffer.clear()
        self._set_state(STATE_DISCONNECTED if self.configured else STATE_NOT_CONFIGURED)

    def health(self) -> dict:
        streaming = self._state == STATE_CONNECTED and bool(self._ticks or self._candles)
        return {
            "provider": self.name,
            "label": self.label,
            "connected": bool(streaming),
            "state": self._state,
            "state_since_utc": self._state_since,
            "status": ADAPTER_STREAMING if streaming else (
                "AWAITING_TICK_STREAM" if self.configured else ADAPTER_NOT_CONFIGURED),
            "detail": self._detail(),
            "realtime_capable": True,
            "supports_subscribe": True,
            "is_simulated": False,
            "buffered_assets": sorted(self._buffer),
            "ticks_received": self._ticks,
            "candles_stored": self._candles,
            "rejected_messages": self._rejected,
            "ignored_messages": self._ignored,
            "last_tick_utc": self._last_tick_utc,
            "last_candle_utc": self._last_candle_utc,
            "last_message_utc": self._last_message_utc,
            "last_update_utc": self._last_tick_utc or self._last_candle_utc,
            "last_error": self._last_error,
            "checked_at_utc": utc_iso(),
        }

    def stats(self) -> dict:
        """Counters for the bridge / probe tools and the tests."""
        return {
            "state": self._state,
            "ticks_received": self._ticks,
            "candles_stored": self._candles,
            "rejected_messages": self._rejected,
            "ignored_messages": self._ignored,
            "last_tick_utc": self._last_tick_utc,
            "last_candle_utc": self._last_candle_utc,
            "last_message_utc": self._last_message_utc,
            "last_error": self._last_error,
            "state_since_utc": self._state_since,
        }

    def subscribe(self, asset: AssetSpec, callback: object | None = None) -> dict:
        if not self.configured:
            return {"provider": self.name, "asset": asset.symbol, "supported": False,
                    "mode": "unconfigured", "detail": "QUOTEX_WS_URL is not set"}
        self._buffer.setdefault(asset.symbol, [])
        return {"provider": self.name, "asset": asset.symbol, "supported": True,
                "mode": "tick_stream", "detail": "symbol registered for streaming once ticks arrive"}

    # ---------------------------------------------------------------- ingestion hooks
    def _lock(self):
        import threading

        lock = getattr(self, "_mutex", None)
        if lock is None:
            lock = threading.RLock()
            self._mutex = lock
        return lock

    def note_ignored(self, count: int = 1) -> None:
        self._ignored += int(count)

    def note_rejected(self, reason: str) -> None:
        self._rejected += 1
        self._last_error = redact(reason)[:200]

    def note_message(self, when: str | None = None) -> None:
        self._last_message_utc = when or utc_iso()

    def note_auth_failure(self, reason: str = "") -> None:
        self._set_state(STATE_AUTH_FAILED)
        self._last_error = redact(reason or "the feed rejected the session")[:200]

    def note_connected(self) -> None:
        self._set_state(STATE_CONNECTED)

    def note_disconnected(self, reason: str = "") -> None:
        if reason:
            self._last_error = redact(reason)[:200]
        self._set_state(STATE_DISCONNECTED if self.configured else STATE_NOT_CONFIGURED)

    def push_tick(self, asset_symbol: str, tick: dict) -> dict:
        """Integration point: a verified broker tick for `asset_symbol`."""
        symbol = str(asset_symbol).strip().upper()
        with self._lock():
            buffer = self._buffer.setdefault(symbol, [])
            buffer.append(dict(tick))
            if len(buffer) > MAX_BUFFERED_TICKS_PER_ASSET:
                del buffer[:-MAX_BUFFERED_TICKS_PER_ASSET]
            self._ticks += 1
            stamp = tick.get("time_utc")
            if stamp:
                self._last_tick_utc = str(stamp)
            self._last_message_utc = self._last_tick_utc or utc_iso()
            if self._state in {STATE_NOT_CONFIGURED, STATE_DISCONNECTED, STATE_ERROR}:
                self._set_state(STATE_CONNECTED)      # verified ticks are arriving
        return {"asset": symbol, "buffered": len(buffer), "state": self._state}

    def drain_ticks(self, asset_symbol: str) -> list[dict]:
        """Hand the buffered ticks to the continuous feed (`ContinuousFeed.pull_once`)."""
        symbol = str(asset_symbol).strip().upper()
        with self._lock():
            ticks = self._buffer.get(symbol) or []
            self._buffer[symbol] = []
        return ticks

    def push_candle(self, asset_symbol: str, row: dict, *, store_row: bool = True) -> dict:
        """Integration point: one *closed* candle from the authorised feed.

        Validated with the same rules the API applies (`feed.validate_candle`), then
        persisted with its provenance. An invalid candle is rejected, never repaired.
        """
        symbol = str(asset_symbol).strip().upper()
        payload = dict(row)
        payload["asset"] = symbol
        payload.setdefault("provider", self.name)
        payload.setdefault("source_label", "Broker feed (operator-authorised)")
        payload.setdefault("source_class", "MARKET")
        payload.setdefault("is_simulated", False)
        payload.setdefault("is_replay", False)
        payload.setdefault("realtime", True)
        payload.setdefault("resolution_minutes", 1.0)
        ok, reason = validate_candle(payload)
        if not ok:
            self.note_rejected(str(reason))
            return {"asset": symbol, "accepted": False, "reason": reason}
        receipt = {"inserted": 0, "updated": 0, "skipped": 0}
        if store_row:
            receipt = store.upsert_candles([payload])
        self._candles += 1
        self._last_candle_utc = str(payload.get("ts_utc"))
        self._last_message_utc = self._last_candle_utc
        if self._state in {STATE_NOT_CONFIGURED, STATE_DISCONNECTED, STATE_ERROR}:
            self._set_state(STATE_CONNECTED)
        return {"asset": symbol, "accepted": True, "stored": store_row, "receipt": receipt,
                "ts_utc": payload.get("ts_utc")}

    # ------------------------------------------------------------------- reads (§11)
    def _last_buffered_tick(self) -> str | None:
        latest = None
        for ticks in self._buffer.values():
            for tick in ticks:
                stamp = tick.get("time_utc") if isinstance(tick, dict) else None
                if stamp and (latest is None or str(stamp) > latest):
                    latest = str(stamp)
        return latest

    def latest_tick(self, asset: AssetSpec) -> dict | None:
        """Newest verified tick for the asset, falling back to the newest stored candle."""
        symbol = str(getattr(asset, "symbol", asset)).upper()
        with self._lock():
            ticks = self._buffer.get(symbol) or []
            if ticks:
                tick = ticks[-1]
                return {"asset": symbol, "price": tick.get("price"), "time_utc": tick.get("time_utc"),
                        "provider": self.name, "is_simulated": False, "is_replay": False,
                        "source": "tick_buffer"}
        row = store.latest_row(symbol, provider=self.name)
        if row:
            return {"asset": symbol, "price": row["close"], "time_utc": row["ts_utc"],
                    "provider": self.name, "is_simulated": bool(row["is_simulated"]),
                    "is_replay": bool(row["is_replay"]), "source": "store"}
        return None

    def latest_candles(self, asset: AssetSpec, limit: int = 300,
                       now: datetime | None = None) -> CandleFrame:
        """Closed candles for the asset from the store rows this adapter produced."""
        from .live_store import live_store

        symbol = str(getattr(asset, "symbol", asset)).upper()
        row = store.latest_row(symbol, provider=self.name)
        if row is None:
            raise MarketDataUnavailable(
                symbol,
                "Broker feed has not delivered verified candles for this instrument",
                self.name,
                f"state={self._state}; run tools/probe_live_feed.py to check the connection",
            )
        return live_store.fetch(asset, limit=limit, now=now)

    def fetch(self, asset: AssetSpec, limit: int = 300, now: datetime | None = None) -> CandleFrame:
        if not self.configured and store.latest_row(asset.symbol, provider=self.name) is None:
            raise MarketDataUnavailable(
                asset.symbol,
                "Broker feed not configured — no public data source exists for this instrument",
                self.name,
                "set QUOTEX_WS_URL and run tools/start_live_feed.py (or wire an in-process tick handler)",
            )
        return self.latest_candles(asset, limit=limit, now=now)

    # ------------------------------------------------------------------ diagnostics
    def freshness_budget_seconds(self) -> float:
        from ...constants import GATES

        return float(GATES["max_data_lag_seconds"])

    def age_seconds(self, asset_symbol: str, *, now: datetime | None = None) -> float | None:
        row = store.latest_row(str(asset_symbol).upper(), provider=self.name)
        if not row:
            return None
        moment = now or datetime.now(tz=timezone.utc)
        close_at = self._close_time(row)
        return round((moment - close_at).total_seconds(), 3)

    @staticmethod
    def _close_time(row: dict) -> datetime:
        from datetime import timedelta

        opened = datetime.fromisoformat(str(row["ts_utc"]).replace("Z", "+00:00"))
        return opened + timedelta(minutes=float(row.get("resolution_minutes") or 1.0))


broker_ws = BrokerFeedProvider()
