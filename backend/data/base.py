"""Market-data contracts.

Every frame returned by a provider carries `FeedMeta` provenance. The signal
engine refuses to trade without it, and the UI renders the provenance verbatim.
A frame is NEVER relabelled: simulated data can never present itself as live.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from ..timeutil import to_ist, to_utc, utc_iso, ist_iso
from datetime import UTC

OHLC_COLUMNS = ("open", "high", "low", "close", "volume")

FEED_STATUS_OK = "OK"
FEED_STATUS_UNAVAILABLE = "UNAVAILABLE"
FEED_STATUS_DEGRADED = "DEGRADED"

# What the operator sees, in order of trust. Every frame declares exactly one.
SOURCE_CLASS_MARKET = "MARKET"          # real quotes (live or replay/delayed)
SOURCE_CLASS_SIMULATED = "SIMULATED"    # synthetic demo series
SOURCE_CLASS_FIXTURE = "FIXTURE"        # test/QA data — never tradable

# UI badges (spec §1)
DISPLAY_LIVE = "LIVE"
DISPLAY_REPLAY = "REPLAY"
DISPLAY_SIMULATED = "SIMULATED"
DISPLAY_UNAVAILABLE = "DATA UNAVAILABLE"


class MarketDataUnavailable(RuntimeError):
    """Raised when a provider cannot supply verified candles for an asset."""

    def __init__(self, asset: str, reason: str, provider: str = "unknown", detail: str | None = None):
        super().__init__(f"{asset}: {reason}" + (f" ({detail})" if detail else ""))
        self.asset = asset
        self.reason = reason
        self.provider = provider
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "reason": self.reason,
            "provider": self.provider,
            "detail": self.detail,
        }


@dataclass
class FeedMeta:
    asset: str
    provider: str                    # broker_ws | yahoo | csv | simulated | fixture
    source_label: str                # human label shown in the UI
    mode: str                        # LIVE | REPLAY
    realtime: bool                   # True only for a genuinely realtime feed
    is_simulated: bool
    delayed_seconds: float
    is_replay: bool = False          # file/historical input — never presented as live
    source_class: str = SOURCE_CLASS_MARKET
    source_status: str = FEED_STATUS_OK
    timeframe: str = "M1"
    last_update_utc: str | None = None
    latest_candle_open_utc: str | None = None
    latest_candle_close_utc: str | None = None
    retrieved_at_utc: str | None = None
    candles: int = 0
    has_volume: bool = False
    status: str = FEED_STATUS_OK
    error: str | None = None
    notes: str = ""
    fallback_chain: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """Canonical feed payload used by every API response (single source of truth)."""
        return {
            "provider": self.provider,
            "asset": self.asset,
            "timeframe": self.timeframe,
            "source_label": self.source_label,
            "mode": self.mode,
            "badge": self.badge,
            "display_status": self.display_status,
            "source_class": self.source_class,
            "source_status": self.source_status,
            "realtime": self.realtime,
            "simulated": self.is_simulated,
            "is_simulated": self.is_simulated,
            "is_replay": self.is_replay,
            "last_update_utc": self.last_update_utc,
            "delayed_seconds": self.delayed_seconds,
            "candles": self.candles,
            "has_volume": self.has_volume,
            "latest_candle_open_utc": self.latest_candle_open_utc,
            "latest_candle_close_utc": self.latest_candle_close_utc,
            "retrieved_at_utc": self.retrieved_at_utc,
            "status": self.status,
            "error": self.error,
            "notes": self.notes,
            "fallback_chain": self.fallback_chain,
        }

    @property
    def badge(self) -> str:
        """Four-state badge required by the UI: LIVE | REPLAY | SIMULATED | DATA UNAVAILABLE."""
        if self.status == FEED_STATUS_UNAVAILABLE:
            return DISPLAY_UNAVAILABLE
        if self.source_class == SOURCE_CLASS_SIMULATED or self.is_simulated:
            return DISPLAY_SIMULATED
        if self.realtime and not self.is_replay:
            return DISPLAY_LIVE
        return DISPLAY_REPLAY

    @property
    def display_status(self) -> str:
        return self.badge

    @property
    def is_market_data(self) -> bool:
        """True only for real quotes. Fixtures and simulations are never market data."""
        return (
            not self.is_simulated
            and self.source_class == SOURCE_CLASS_MARKET
            and self.status != FEED_STATUS_UNAVAILABLE
        )


@dataclass
class CandleFrame:
    asset: str
    df: pd.DataFrame                # index = UTC candle-open timestamps, columns = OHLC_COLUMNS
    meta: FeedMeta
    live_price: float | None = None  # last traded price on an in-progress candle (display only)


def drop_forming_candle(df: pd.DataFrame, now: datetime | None = None,
                        tolerance_seconds: float = 2.0) -> tuple[pd.DataFrame, int]:
    """Remove the in-progress candle.

    A candle is only usable for prediction once it has CLOSED (spec §8). Candle
    closed_at = open + timeframe; anything closing after `now` is still forming and
    is dropped. Returns (closed_only_frame, dropped_rows).
    """
    if df is None or df.empty:
        return df, 0
    reference = to_utc(now or datetime.now(tz=UTC))
    cutoff = reference - pd.Timedelta(seconds=tolerance_seconds)
    closed = df[(df.index + pd.Timedelta(minutes=1)) <= cutoff]
    return closed, int(len(df) - len(closed))


# --------------------------------------------------------------- adapter layer
# Spec §11 — every provider exposes the same small interface so a real feed can be
# dropped in without touching the engine:
#
#     connect()  disconnect()  health()  latest_tick()  latest_candles()  subscribe()
#
# A provider that cannot do something says so instead of pretending: replay
# sources report `supports_subscribe=False`, and an unconfigured broker feed
# reports NOT_CONFIGURED rather than inventing candles.

ADAPTER_READY = "READY"
ADAPTER_REPLAY_ONLY = "REPLAY_ONLY"
ADAPTER_NOT_CONFIGURED = "NOT_CONFIGURED"
ADAPTER_UNAVAILABLE = "UNAVAILABLE"
ADAPTER_DEGRADED = "DEGRADED"
ADAPTER_STREAMING = "STREAMING"


class MarketDataAdapter:
    """Base class: providers override what they can actually do."""

    name: str = "adapter"
    label: str = "Adapter"
    realtime: bool = False
    is_simulated: bool = False

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> dict[str, Any]:
        """Establish/verify the connection. Cheap and side-effect free by default."""
        return self.health()

    def disconnect(self) -> None:
        return None

    # -- health ------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "label": self.label,
            "connected": False,
            "status": ADAPTER_NOT_CONFIGURED,
            "detail": "adapter does not implement health()",
            "realtime_capable": bool(self.realtime),
            "supports_subscribe": False,
            "is_simulated": bool(self.is_simulated),
            "last_update_utc": None,
            "checked_at_utc": utc_iso(),
        }

    def latest_candles(self, asset: "AssetSpec", limit: int = 300,
                       now: datetime | None = None) -> "CandleFrame":
        """Alias of fetch() — the interface name used by the adapter layer."""
        return self.fetch(asset, limit=limit, now=now)  # type: ignore[attr-defined]

    def latest_tick(self, asset: "AssetSpec") -> dict[str, Any] | None:
        """Newest closed candle as a tick, or None when unavailable/stale."""
        try:
            frame = self.latest_candles(asset, limit=5)
        except Exception:  # noqa: BLE001 - health reporting must never raise
            return None
        if not len(frame.df):
            return None
        row = frame.df.iloc[-1]
        return {
            "asset": frame.meta.asset,
            "time_utc": utc_iso(frame.df.index[-1].to_pydatetime()),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "provider": frame.meta.provider,
            "badge": frame.meta.display_status,
            "is_simulated": bool(frame.meta.is_simulated),
            "is_replay": bool(frame.meta.is_replay),
        }

    def subscribe(self, asset: "AssetSpec", callback: Any | None = None) -> dict[str, Any]:
        """Register interest in push updates. Replay sources cannot do this."""
        return {
            "provider": self.name,
            "asset": getattr(asset, "symbol", str(asset)),
            "supported": False,
            "mode": "pull_only",
            "detail": f"{self.label} is a pull/replay source — no push updates are available",
        }


def make_frame(
    asset: str,
    df: pd.DataFrame,
    meta: FeedMeta,
    live_price: float | None = None,
    min_candles: int = 1,
    now: datetime | None = None,
    drop_forming: bool = True,
) -> CandleFrame:
    """Normalise + sanity-check provider output. Raises on malformed data."""
    if df is None or df.empty:
        raise MarketDataUnavailable(asset, "Provider returned no candles", meta.provider)
    frame = df.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    missing = [c for c in OHLC_COLUMNS[:4] if c not in frame.columns]
    if missing:
        raise MarketDataUnavailable(asset, f"Malformed candles, missing columns: {missing}", meta.provider)
    if "volume" not in frame.columns:
        frame["volume"] = np.nan
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, utc=True)
    elif frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    frame.index = frame.index.tz_convert("UTC")
    frame = frame[list(OHLC_COLUMNS)].astype(float)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    bad = frame[(frame["high"] < frame["low"]) | frame[["open", "high", "low", "close"]].isna().any(axis=1)]
    if len(bad) > 0:
        frame = frame.drop(index=bad.index)
    if drop_forming:
        frame, dropped = drop_forming_candle(frame, now)
        if dropped:
            meta.notes = (
                f"{meta.notes} In-progress candle excluded ({dropped} row(s) not yet closed)."
            ).strip()
    if len(frame) == 0:
        raise MarketDataUnavailable(
            asset, "No CLOSED candles available for this asset", meta.provider,
            "the feed only returned candles that are still forming",
        )
    if len(frame) < min_candles:
        raise MarketDataUnavailable(
            asset, f"Insufficient candles ({len(frame)} < {min_candles})", meta.provider
        )
    meta.candles = int(len(frame))
    meta.has_volume = bool(frame["volume"].notna().any())
    last_open = frame.index[-1].to_pydatetime()
    meta.latest_candle_open_utc = utc_iso(last_open)
    meta.latest_candle_close_utc = utc_iso(last_open + pd.Timedelta(minutes=1))
    meta.last_update_utc = meta.latest_candle_close_utc
    meta.asset = asset
    if meta.source_class == SOURCE_CLASS_SIMULATED:
        meta.is_simulated = True
    if meta.is_simulated:
        meta.source_class = SOURCE_CLASS_SIMULATED
    if not meta.source_status:
        meta.source_status = meta.status
    return CandleFrame(asset=asset, df=frame, meta=meta, live_price=live_price)


def data_lag_seconds(meta: FeedMeta, now_utc: datetime | None = None) -> float:
    """Seconds since the close of the newest candle in the frame."""
    if not meta.latest_candle_close_utc:
        return float("inf")
    close_dt = datetime.fromisoformat(meta.latest_candle_close_utc.replace("Z", "+00:00"))
    return (to_utc(now_utc or datetime.now(tz=close_dt.tzinfo)) - close_dt).total_seconds()


def frame_to_records(frame: CandleFrame, limit: int | None = None) -> list[dict[str, Any]]:
    df = frame.df if limit is None else frame.df.tail(limit)
    out: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        t = ts.to_pydatetime()
        out.append(
            {
                "time_utc": utc_iso(t),
                "time_ist": ist_iso(to_ist(t)),
                "epoch": int(t.timestamp()),
                "open": round(float(row["open"]), 6),
                "high": round(float(row["high"]), 6),
                "low": round(float(row["low"]), 6),
                "close": round(float(row["close"]), 6),
                "volume": (None if pd.isna(row["volume"]) else round(float(row["volume"]), 4)),
            }
        )
    return out
