"""Public delayed feed (Yahoo Finance chart API, no API key required).

Notes on honesty:
  * Yahoo's 1-minute FX series is delayed and frequently rate-limited (HTTP 429).
    Frames are therefore marked `realtime=False, mode="REPLAY"`, and the engine
    treats them as replay data — signals derived from a replay feed are paper /
    study signals, never presented as executable live signals.
  * On any failure we raise MarketDataUnavailable with the real HTTP status.
    We never substitute synthetic prices for a live asset.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import requests

from ...constants import AssetSpec, TIMEFRAME
from ...timeutil import utc_iso, utc_now_naive
from ..base import (SOURCE_CLASS_MARKET, CandleFrame, FeedMeta, MarketDataUnavailable,
                     MarketDataAdapter, make_frame)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
_FAILURE_COOLDOWN_SECONDS = 90.0


class YahooProvider(MarketDataAdapter):
    name = "yahoo"
    label = "Public delayed feed (not a Quotex/OTC source)"
    label = "Yahoo Finance (delayed public feed)"
    realtime = False
    is_simulated = False

    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout
        self._cooldown: dict[str, tuple[float, str]] = {}

    def _blocked(self, symbol: str) -> str | None:
        entry = self._cooldown.get(symbol)
        if not entry:
            return None
        until, reason = entry
        if time.time() < until:
            return reason
        self._cooldown.pop(symbol, None)
        return None

    # ---------------------------------------------------- adapter interface (spec §11)
    def connect(self) -> dict:
        """Report reachability without pretending the series is real-time."""
        return self.health()

    def health(self) -> dict:
        return {
            "provider": self.name,
            "label": self.label,
            "connected": True,
            "status": "REPLAY_ONLY",
            "detail": ("public delayed data — treated as REPLAY, never as a live Quotex/OTC feed; "
                       "M1 bars may be re-sampled or delayed by the upstream service"),
            "realtime_capable": False,
            "supports_subscribe": False,
            "is_simulated": False,
            "rate_limited": True,
            "last_update_utc": None,
            "checked_at_utc": utc_iso(),
        }

    def subscribe(self, asset: AssetSpec, callback: object | None = None) -> dict:
        return {
            "provider": self.name,
            "asset": asset.symbol,
            "supported": False,
            "mode": "poll_only",
            "detail": ("the public feed is polled on request and rate-limited; it cannot stream ticks and "
                       "must not be presented as a broker feed"),
        }

    def fetch(self, asset: AssetSpec, limit: int = 300, now: datetime | None = None) -> CandleFrame:
        if not asset.yahoo_symbol:
            raise MarketDataUnavailable(
                asset.symbol, "No public data feed mapped for this asset", self.name,
                "OTC pairs require a broker feed",
            )
        blocked = self._blocked(asset.yahoo_symbol)
        if blocked:
            raise MarketDataUnavailable(asset.symbol, blocked, self.name, "cooldown active")

        params = {"range": "1d", "interval": "1m", "includePrePost": "false"}
        try:
            resp = requests.get(
                CHART_URL.format(symbol=asset.yahoo_symbol),
                params=params,
                headers=_HEADERS,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            reason = f"Network error contacting feed ({type(exc).__name__})"
            self._cooldown[asset.yahoo_symbol] = (time.time() + _FAILURE_COOLDOWN_SECONDS, reason)
            raise MarketDataUnavailable(asset.symbol, reason, self.name, str(exc)[:180]) from exc

        if resp.status_code != 200:
            reason = f"Feed returned HTTP {resp.status_code}"
            if resp.status_code in (429, 403, 401):
                reason = f"Feed rate-limited/blocked (HTTP {resp.status_code})"
            self._cooldown[asset.yahoo_symbol] = (time.time() + _FAILURE_COOLDOWN_SECONDS, reason)
            raise MarketDataUnavailable(asset.symbol, reason, self.name)

        try:
            payload = resp.json()
            result = payload["chart"]["result"][0]
            stamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self._cooldown[asset.yahoo_symbol] = (time.time() + _FAILURE_COOLDOWN_SECONDS, "Malformed feed payload")
            raise MarketDataUnavailable(asset.symbol, "Malformed feed payload", self.name, str(exc)[:180]) from exc

        df = pd.DataFrame(
            {
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "close": quote.get("close"),
                "volume": quote.get("volume"),
            },
            index=pd.to_datetime(stamps, unit="s", utc=True),
        )
        df = df.dropna(subset=["open", "high", "low", "close"])
        if df.empty:
            raise MarketDataUnavailable(asset.symbol, "Feed returned an empty candle set (market closed or no data)", self.name)

        meta = FeedMeta(
            asset=asset.symbol,
            provider=self.name,
            source_label=self.label,
            mode="REPLAY",
            realtime=False,
            is_simulated=False,
            is_replay=True,
            source_class=SOURCE_CLASS_MARKET,
            delayed_seconds=60.0,
            retrieved_at_utc=utc_iso(utc_now_naive()),
            notes=(
                "Public delayed feed. 1-minute FX data from this source lags the market and may "
                "contain gaps — treated as replay data, not an executable live feed."
            ),
        )
        return make_frame(asset.symbol, df.tail(limit), meta, min_candles=30)
