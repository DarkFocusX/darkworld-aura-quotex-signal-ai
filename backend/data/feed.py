"""Continuous M1 ingestion (spec §1–§3 of the paper-trading phase).

One service, one job: take whatever a provider really gives us — ticks or closed
candles — build verified M1 candles, and put them in the rolling store.

What this module refuses to do (by design, and by test):

* it never invents a candle for a minute that had no ticks — the minute is simply
  missing and `gaps()` reports it;
* it never re-labels data: a simulated tick stays `is_simulated=True` forever, a
  replayed file stays `is_replay=True`, and a provider name is never overwritten;
* it never accepts a candle that fails OHLC validation, an out-of-order
  timestamp, a duplicate tick, or a non-finite price;
* it never claims a feed is fresh when it is not — `freshness` is derived from the
  newest *verified* candle close time against the published budgets (120 s for a
  realtime feed, 180 s for a replay feed).
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ..constants import GATES
from ..timeutil import utc_iso
from .base import SOURCE_CLASS_MARKET, SOURCE_CLASS_SIMULATED
from . import store

FRESH = "FRESH"
STALE = "STALE"
INVALID = "INVALID"
UNAVAILABLE = "UNAVAILABLE"

MAX_FUTURE_SKEW_SECONDS = 5.0


@dataclass
class FormingCandle:
    """The M1 candle currently being built from ticks. It is *not* a candle until
    the minute rolls over — a partially formed candle is never persisted."""

    asset: str
    minute_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ticks: int = 1
    first_tick_utc: datetime | None = None
    last_tick_utc: datetime | None = None
    source_label: str | None = None      # per-producer provenance, overrides the feed default

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "minute_utc": utc_iso(self.minute_utc),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "ticks": self.ticks,
            "last_tick_utc": utc_iso(self.last_tick_utc) if self.last_tick_utc else None,
            "state": "FORMING",
        }


def tick_minute(ts_utc: datetime) -> datetime:
    return ts_utc.astimezone(timezone.utc).replace(second=0, microsecond=0)


def validate_candle(row: dict[str, Any]) -> tuple[bool, str | None]:
    """Structural validation. Returns (ok, reason). A rejected candle is counted and
    dropped — never repaired."""
    values = {}
    for key in ("open", "high", "low", "close"):
        value = row.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False, f"non-numeric {key}"
        if not math.isfinite(value) or value <= 0:
            return False, f"invalid {key}={row.get(key)!r}"
        values[key] = value
    if values["high"] < max(values["open"], values["close"], values["low"]):
        return False, "high is below open/close/low"
    if values["low"] > min(values["open"], values["close"], values["high"]):
        return False, "low is above open/close/high"
    return True, None


class ContinuousFeed:
    """Provider-agnostic ingestion service.

    ``ingest_tick`` builds M1 candles from a tick stream; ``ingest_candle`` accepts
    closed candles from providers that only publish bars. Both paths validate,
    persist and update the per-asset live status.
    """

    name = "continuous"

    def __init__(self, *, provider: str = "broker_ws", source_label: str | None = None,
                 realtime: bool = True, is_simulated: bool = False, is_replay: bool = False,
                 source_class: str | None = None, tick_seconds: float = 1.0,
                 retention_minutes: int = store.DEFAULT_RETENTION_MINUTES,
                 persist_candles: bool = True) -> None:
        self.provider = provider
        self.source_label = source_label or f"Continuous feed ({provider})"
        self.realtime = bool(realtime)
        self.is_simulated = bool(is_simulated)
        self.is_replay = bool(is_replay)
        self.source_class = source_class or (SOURCE_CLASS_SIMULATED if is_simulated else SOURCE_CLASS_MARKET)
        self.tick_seconds = float(tick_seconds)
        self.retention_minutes = int(retention_minutes)
        # When False the feed still builds and validates every closed candle, but leaves
        # persistence to the caller — that is how the out-of-process live-feed bridge
        # delivers candles to a remote API instead of writing to its own local store.
        self.persist_candles = bool(persist_candles)

        self._lock = threading.RLock()
        self._forming: dict[str, FormingCandle] = {}
        self._last_tick: dict[str, dict[str, Any]] = {}
        self._last_tick_signature: dict[str, tuple[Any, ...]] = {}
        self._subscribed: set[str] = set()
        self._connected = False
        self._stats: dict[str, dict[str, Any]] = {}
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._adapters: list[tuple[Any, list[str]]] = []
        self._started_at_utc: str | None = None
        self.last_error: str | None = None
        self.last_candle_at_utc: str | None = None

    # ------------------------------------------------------------- lifecycle (§11)
    def connect(self) -> dict[str, Any]:
        with self._lock:
            self._connected = True
            if self._started_at_utc is None:
                self._started_at_utc = utc_iso()
        return self.health()

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._forming.clear()
        self.stop()

    @property
    def connected(self) -> bool:
        return self._connected

    def health(self) -> dict[str, Any]:
        with self._lock:
            assets = sorted(self._subscribed)
            return {
                "provider": self.provider,
                "label": self.source_label,
                "connected": self._connected,
                "status": "CONNECTED" if self._connected else ("DISCONNECTED" if self._started_at_utc else "NOT_STARTED"),
                "running": self.running,
                "realtime_capable": self.realtime,
                "supports_subscribe": True,
                "is_simulated": self.is_simulated,
                "is_replay": self.is_replay,
                "subscribed_assets": assets,
                "assets_with_candles": [a for a in assets if store.count(a, provider=self.provider) > 0],
                "last_candle_at_utc": self.last_candle_at_utc,
                "last_error": self.last_error,
                "started_at_utc": self._started_at_utc,
                "checked_at_utc": utc_iso(),
            }

    # -------------------------------------------------------------- subscribing
    def subscribe(self, asset: str, callback: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        symbol = str(asset).upper()
        with self._lock:
            self._subscribed.add(symbol)
            if callback:
                self._callbacks.append(callback)
            self._stats.setdefault(symbol, _empty_stats())
        return {"provider": self.provider, "asset": symbol, "supported": True, "mode": "tick_stream",
                "detail": "M1 candles are built from validated ticks and persisted to the rolling store"}

    def subscribe_adapter(self, adapter: Any, assets: Iterable[str]) -> None:
        """Attach a live adapter that exposes ``drain_ticks`` (broker feed etc.)."""
        symbols = [str(a).upper() for a in assets]
        self._adapters.append((adapter, symbols))
        for symbol in symbols:
            self.subscribe(symbol)

    def latest_tick(self, asset: str) -> dict[str, Any] | None:
        with self._lock:
            return self._last_tick.get(str(asset).upper())

    def latest_candles(self, asset: str, limit: int = 300) -> list[dict[str, Any]]:
        return store.candles(asset, provider=self.provider, limit=limit)

    def on_candle_closed(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._callbacks.append(callback)

    # -------------------------------------------------------------- ingestion
    def ingest_tick(self, asset: str, price: Any, *, time_utc: datetime | None = None,
                    volume: float | None = None, provider: str | None = None,
                    source_label: str | None = None) -> dict[str, Any]:
        """Feed one tick. Returns a receipt describing what happened to it."""
        symbol = str(asset).upper()
        ts = time_utc or datetime.now(tz=timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            stats = self._stats.setdefault(symbol, _empty_stats())
            stats["ticks_received"] += 1
            try:
                value = float(price)
            except (TypeError, ValueError):
                stats["rejected_ticks"] += 1
                return {"asset": symbol, "accepted": False, "reason": "non-numeric price", "status": "INVALID"}
            if not math.isfinite(value) or value <= 0:
                stats["rejected_ticks"] += 1
                return {"asset": symbol, "accepted": False, "reason": f"invalid price {price!r}", "status": "INVALID"}
            if (ts - now).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
                stats["rejected_ticks"] += 1
                return {"asset": symbol, "accepted": False,
                        "reason": "tick timestamp is in the future", "status": "INVALID"}
            signature = (ts.replace(microsecond=0).isoformat(), round(value, 10))
            if self._last_tick_signature.get(symbol) == signature:
                stats["duplicate_ticks"] += 1
                return {"asset": symbol, "accepted": False, "reason": "duplicate tick", "status": "DUPLICATE"}

            minute = tick_minute(ts)
            forming = self._forming.get(symbol)
            closed_receipt: dict[str, Any] | None = None
            if forming is not None and minute > forming.minute_utc:
                closed_receipt = self._finalise(symbol, forming)
                forming = None
            if forming is None:
                forming = FormingCandle(asset=symbol, minute_utc=minute, open=value, high=value, low=value,
                                        close=value, volume=float(volume or 0.0), ticks=1,
                                        first_tick_utc=ts, last_tick_utc=ts,
                                        source_label=source_label)
                self._forming[symbol] = forming
            else:
                forming.high = max(forming.high, value)
                forming.low = min(forming.low, value)
                forming.close = value
                forming.volume += float(volume or 0.0)
                forming.ticks += 1
                forming.last_tick_utc = ts
            self._last_tick_signature[symbol] = signature
            self._last_tick[symbol] = {
                "asset": symbol, "price": value, "time_utc": utc_iso(ts), "provider": provider or self.provider,
                "is_simulated": self.is_simulated, "minute_utc": utc_iso(minute), "ticks_in_candle": forming.ticks,
            }
            stats["ticks_accepted"] += 1
            stats["last_tick_utc"] = utc_iso(ts)
            receipt = {
                "asset": symbol, "accepted": True, "price": value, "time_utc": utc_iso(ts),
                "minute_utc": utc_iso(minute), "status": "FORMING",
                "forming": forming.as_dict(), "closed_candle": closed_receipt,
            }
            return receipt

    def ingest_candle(self, asset: str, row: dict[str, Any], *, provider: str | None = None) -> dict[str, Any]:
        """Feed one *closed* candle from a candle-only provider."""
        symbol = str(asset).upper()
        payload = dict(row)
        payload["asset"] = symbol
        payload.setdefault("provider", provider or self.provider)
        payload.setdefault("source_label", self.source_label)
        payload.setdefault("source_class", self.source_class)
        payload.setdefault("is_simulated", self.is_simulated)
        payload.setdefault("is_replay", self.is_replay)
        payload.setdefault("realtime", self.realtime)
        payload.setdefault("resolution_minutes", 1.0)
        with self._lock:
            stats = self._stats.setdefault(symbol, _empty_stats())
            ok, reason = validate_candle(payload)
            if not ok:
                stats["rejected_candles"] += 1
                return {"asset": symbol, "accepted": False, "reason": reason, "status": INVALID}
            if self.persist_candles:
                receipt = store.upsert_candles([payload], retention_minutes=self.retention_minutes)
            else:
                receipt = {"inserted": 0, "updated": 0, "skipped": 0, "persisted": False}
            stats["candles_ingested"] += 1
            stats["last_candle_utc"] = payload.get("ts_utc")
            if receipt["updated"]:
                stats["duplicate_candles"] += 1
            self.last_candle_at_utc = payload.get("ts_utc") or self.last_candle_at_utc
            status = "DUPLICATE_UPDATED" if receipt["updated"] else "CLOSED"
            return {"asset": symbol, "accepted": True, "status": status, "receipt": receipt,
                    "ts_utc": payload.get("ts_utc")}

    def _finalise(self, symbol: str, forming: FormingCandle) -> dict[str, Any]:
        """Persist the completed M1 candle built from ticks."""
        row = {
            "asset": symbol,
            "provider": self.provider,
            # ISO-8601 UTC in the row itself: the same value the store persists and the
            # bridge delivers, so no consumer has to guess whether it holds a datetime.
            "ts_utc": utc_iso(forming.minute_utc),
            "resolution_minutes": 1.0,
            "open": forming.open, "high": forming.high, "low": forming.low, "close": forming.close,
            "volume": forming.volume if forming.volume else None,
            "is_simulated": self.is_simulated, "is_replay": self.is_replay,
            "source_class": self.source_class,
            "source_label": forming.source_label or self.source_label,
            "realtime": self.realtime, "ingested_at_utc": utc_iso(),
        }
        ok, reason = validate_candle(row)
        stats = self._stats.setdefault(symbol, _empty_stats())
        if not ok:
            stats["rejected_candles"] += 1
            return {"asset": symbol, "accepted": False, "reason": reason, "status": INVALID}
        if self.persist_candles:
            receipt = store.upsert_candles([row], retention_minutes=self.retention_minutes)
        else:
            receipt = {"inserted": 0, "updated": 0, "skipped": 0, "persisted": False}
        stats["candles_closed"] += 1
        stats["last_candle_utc"] = row["ts_utc"]
        self.last_candle_at_utc = row["ts_utc"]
        event = {
            "asset": symbol, "provider": self.provider, "ts_utc": utc_iso(forming.minute_utc),
            "close": forming.close, "ticks": forming.ticks, "is_simulated": self.is_simulated,
            "is_replay": self.is_replay, "receipt": receipt,
        }
        for callback in list(self._callbacks):
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001 — a bad subscriber must not stop ingestion
                self.last_error = f"callback error: {type(exc).__name__}: {exc}"
        return {"asset": symbol, "accepted": True, "status": "CLOSED", "candle": event,
                "row": row, "persisted": bool(self.persist_candles), "receipt": receipt}

    # --------------------------------------------------------------- driver loop
    def pull_once(self, now: datetime | None = None) -> dict[str, Any]:
        """One ingestion pass: drain any attached adapters (verified ticks only)."""
        now = now or datetime.now(tz=timezone.utc)
        pulled = 0
        with self._lock:
            adapters = list(self._adapters)
        for adapter, symbols in adapters:
            drain = getattr(adapter, "drain_ticks", None)
            if not callable(drain):
                continue
            for symbol in symbols:
                try:
                    ticks = drain(symbol) or []
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    continue
                for tick in ticks:
                    price = tick.get("price") if isinstance(tick, dict) else None
                    stamp = tick.get("time_utc") if isinstance(tick, dict) else None
                    when = None
                    if stamp:
                        try:
                            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                        except ValueError:
                            when = None
                    self.ingest_tick(symbol, price, time_utc=when)
                    pulled += 1
        return {"pulled": pulled, "at_utc": utc_iso(now)}

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"feed-{self.provider}", daemon=True)
        self._thread.start()
        if not self._connected:
            self.connect()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:  # pragma: no cover - exercised through tick()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.tick_seconds)

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        return self.pull_once(now)

    # ------------------------------------------------------------------- status
    def stats(self, asset: str | None = None) -> dict[str, Any]:
        with self._lock:
            if asset:
                return dict(self._stats.get(str(asset).upper(), _empty_stats()))
            return {symbol: dict(values) for symbol, values in self._stats.items()}

    def budgets(self, asset: str | None = None) -> dict[str, float]:
        row = store.latest_row(asset) if asset else None
        realtime = bool(row.get("realtime")) if row else self.realtime
        return {
            "realtime": float(GATES["max_data_lag_seconds"]),
            "replay": float(GATES["max_replay_lag_seconds"]),
            "applied": float(GATES["max_data_lag_seconds"] if realtime else GATES["max_replay_lag_seconds"]),
        }

    def live_status(self, asset: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Spec §3 payload: provider, latest candle, age, resolution, rows, gaps,
        flags and the derived freshness_status (shared implementation)."""
        now = now or datetime.now(tz=timezone.utc)
        payload = asset_live_status(asset, now=now, provider=self.provider,
                                    realtime_hint=self.realtime, simulated_hint=self.is_simulated,
                                    replay_hint=self.is_replay)
        with self._lock:
            stats = dict(self._stats.get(str(asset).upper(), _empty_stats()))
            forming = self._forming.get(str(asset).upper())
        payload["connected"] = self._connected
        payload["stats"] = stats
        if forming:
            payload["forming_candle"] = forming.as_dict()
        return payload

    @staticmethod
    def freshness_from_age(age_seconds: float | None, *, realtime: bool, has_rows: bool,
                           invalid: bool = False) -> str:
        if invalid:
            return INVALID
        if not has_rows or age_seconds is None:
            return UNAVAILABLE
        budget = float(GATES["max_data_lag_seconds"] if realtime else GATES["max_replay_lag_seconds"])
        return FRESH if age_seconds <= budget else STALE

    def live_status_all(self, assets: Iterable[str] | None = None, *, now: datetime | None = None) -> dict[str, Any]:
        symbols = [str(a).upper() for a in (assets or sorted(self._subscribed) or store.assets())]
        rows = [self.live_status(symbol, now=now) for symbol in symbols]
        counts: dict[str, int] = {FRESH: 0, STALE: 0, INVALID: 0, UNAVAILABLE: 0}
        for row in rows:
            counts[row["freshness_status"]] = counts.get(row["freshness_status"], 0) + 1
        return {
            "provider": self.provider,
            "connected": self._connected,
            "running": self.running,
            "is_simulated": self.is_simulated,
            "is_replay": self.is_replay,
            "budgets_seconds": {"realtime": float(GATES["max_data_lag_seconds"]),
                                "replay": float(GATES["max_replay_lag_seconds"])},
            "counts": counts,
            "generated_at_utc": utc_iso(now or datetime.now(tz=timezone.utc)),
            "assets": rows,
            "notes": [
                "FRESH means the newest verified candle closed within the published budget for its feed class "
                f"({GATES['max_data_lag_seconds']:.0f}s realtime / {GATES['max_replay_lag_seconds']:.0f}s replay).",
                "A missing minute is reported as a gap and is never filled with a fabricated candle.",
                "Simulated rows stay labelled SIMULATED; they can never back a production signal.",
            ],
        }


def asset_live_status(asset: str, *, now: datetime, provider: str | None = None,
                      realtime_hint: bool = True, simulated_hint: bool = False,
                      replay_hint: bool = False) -> dict[str, Any]:
    """Spec §3 per-asset verdict, computed from the rolling store.

    Single implementation used by both the feed and the dashboard, so the API and
    the UI can never disagree about whether a feed is fresh.
    """
    symbol = str(asset).upper()
    row = store.latest_row(symbol)
    if row is None:
        return {
            "asset": symbol,
            "provider": provider,
            "latest_candle": None,
            "age_seconds": None,
            "resolution_minutes": None,
            "rows": 0,
            "gaps": None,
            "is_simulated": bool(simulated_hint),
            "is_replay": bool(replay_hint),
            "realtime": bool(realtime_hint),
            "freshness_status": UNAVAILABLE,
            "budget_seconds": None,
            "detail": "no verified candle has been ingested for this asset yet",
        }
    resolution = float(row.get("resolution_minutes") or 1.0)
    opened = row["ts_utc"]
    if isinstance(opened, datetime):
        opened_dt = opened if opened.tzinfo else opened.replace(tzinfo=timezone.utc)
    else:
        opened_dt = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
    opened_dt = opened_dt.astimezone(timezone.utc)
    closed_dt = opened_dt + timedelta(minutes=resolution)
    age = (now - closed_dt).total_seconds()
    realtime = bool(row.get("realtime"))
    budget = float(GATES["max_data_lag_seconds"] if realtime else GATES["max_replay_lag_seconds"])
    ok, reason = validate_candle({k: row.get(k) for k in ("open", "high", "low", "close")})
    invalid = (not ok) or opened_dt > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS) or resolution <= 0
    if invalid:
        status = INVALID
        detail = reason or "candle timestamp or resolution is not valid"
    elif age <= budget:
        status = FRESH
        detail = None
    else:
        status = STALE
        detail = (f"newest verified candle closed {age:.0f}s ago — outside the {budget:.0f}s "
                  f"{'realtime' if realtime else 'replay'} budget")
    return {
        "asset": symbol,
        "provider": row["provider"],
        "latest_candle": {
            "open_time_utc": row["ts_utc"],
            "close_time_utc": utc_iso(closed_dt),
            "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"],
            "volume": row["volume"],
            "received_at_utc": row["ingested_at_utc"],
        },
        "age_seconds": round(float(age), 1),
        "resolution_minutes": resolution,
        "rows": store.count(symbol, provider=row["provider"]),
        "gaps": store.gaps(symbol, provider=row["provider"], limit=500),
        "is_simulated": bool(row["is_simulated"]),
        "is_replay": bool(row["is_replay"]),
        "realtime": realtime,
        "freshness_status": status,
        "budget_seconds": budget,
        "detail": detail,
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "ticks_received": 0, "ticks_accepted": 0, "duplicate_ticks": 0, "rejected_ticks": 0,
        "candles_closed": 0, "candles_ingested": 0, "duplicate_candles": 0, "rejected_candles": 0,
        "last_tick_utc": None, "last_candle_utc": None,
    }


# --------------------------------------------------------------------- drivers
def simulated_tick(asset: str, now: datetime | None = None) -> float:
    """Deterministic *simulated* price for the development feed.

    This is explicitly synthetic: smooth sinusoidal regimes plus a small hash-based
    jitter. It exists so the ingestion → store → engine → scanner path can be
    exercised without a broker connection, and it is labelled SIMULATED everywhere.
    It is never a Quotex/OTC quote and never market data.
    """
    from ..constants import ASSET_MAP

    spec = ASSET_MAP.get(str(asset).upper())
    base = float(getattr(spec, "simulated_base", 1.0) or 1.0)
    pip = float(getattr(spec, "simulated_pip", 0.0001) or 0.0001)
    now = now or datetime.now(tz=timezone.utc)
    t = now.timestamp()
    phase = int(hashlib.sha256(str(asset).upper().encode()).hexdigest()[:6], 16) % 3600
    slow = 0.9 * math.sin(2 * math.pi * (t + phase) / 5400.0)
    fast = 0.35 * math.sin(2 * math.pi * (t + phase) / 420.0)
    seed = int(hashlib.sha256(f"{asset}:{int(t * 4)}".encode()).hexdigest()[:8], 16)
    jitter = ((seed % 2000) / 1000.0 - 1.0) * 1.5
    price = base + (slow + fast + jitter) * pip
    if price <= 0:
        price = base
    return round(price, 6)


def build_simulated_feed(*, tick_seconds: float = 1.0, realtime: bool = True) -> ContinuousFeed:
    """A continuous feed driven by the clearly-labelled simulated series."""
    return ContinuousFeed(provider="simulated", source_label="SIMULATED FEED (dev — never market data)",
                          realtime=realtime, is_simulated=True, is_replay=False,
                          source_class=SOURCE_CLASS_SIMULATED, tick_seconds=tick_seconds)
