"""SIMULATED market feed — pipeline testing only.

This provider generates a deterministic, clearly-labelled synthetic M1 price
path. It exists so the feature/model/validation pipeline can be exercised
end-to-end without a broker feed.

Hard rules (enforced by the callers, not just documented):
  * `is_simulated = True`, `realtime = False`, `mode = "REPLAY"` on every frame.
  * Signals derived from it are stored with `is_simulated = 1` and the API
    surfaces `"feed": {"simulated": true}` so the UI can display a warning.
  * Telegram delivery is blocked for simulated signals unless the operator
    explicitly enables TELEGRAM_ALLOW_SIMULATED.

It is NEVER used to fill a missing candle for a live asset, and it is never
described to the user as market data.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from threading import RLock

import numpy as np
import pandas as pd

from ...constants import AssetSpec
from ...timeutil import floor_minute, utc_iso
from ..base import SOURCE_CLASS_SIMULATED, CandleFrame, FeedMeta, MarketDataAdapter, make_frame

EPOCH = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_LOCK = RLock()
_CACHE: dict[str, tuple[int, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}


def _regime_series(rng: np.random.Generator, n: int, asset: AssetSpec) -> tuple[np.ndarray, np.ndarray]:
    """Piecewise drift/vol regimes: quiet ranges, trends and volatility bursts."""
    drift = np.zeros(n, dtype=float)
    vol_mult = np.ones(n, dtype=float)
    base_sigma = asset.simulated_vol_bps / 10000.0
    i = 0
    while i < n:
        span = int(rng.integers(40, 260))
        end = min(n, i + span)
        kind = rng.random()
        if kind < 0.42:          # range / mean-reverting
            mu, mult = 0.0, float(rng.uniform(0.6, 1.15))
        elif kind < 0.80:        # trend
            mu, mult = float(rng.normal(0, 0.10)) * base_sigma, float(rng.uniform(0.9, 1.7))
        else:                    # volatility burst
            mu, mult = 0.0, float(rng.uniform(1.8, 3.2))
        drift[i:end] = mu
        vol_mult[i:end] = mult
        i = end
    return drift, vol_mult


def _path(asset: AssetSpec, n_minutes: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Deterministic OHLC(V) arrays for `n_minutes` minutes from EPOCH.

    The generator consumes its random stream in a fixed order, so requesting a
    longer horizon reproduces the same prefix — which keeps the series stable
    across calls (and identical for training and serving).
    """
    with _LOCK:
        cached = _CACHE.get(asset.symbol)
        if cached and cached[0] == n_minutes:
            return cached[1], cached[2], cached[3], cached[4], cached[5]

    rng = np.random.default_rng(asset.seed)
    sigma = asset.simulated_vol_bps / 10000.0
    drift, vol_mult = _regime_series(rng, n_minutes, asset)
    z = rng.standard_normal(n_minutes)
    noise = sigma * vol_mult * z

    # Synthetic micro-structure: an AR(1) term plus volatility clustering.
    #
    # IMPORTANT: real 1-minute FX has only ~0.01–0.05 lag-1 return persistence. The
    # persistence below is deliberately STRONGER than reality so that the end-to-end
    # pipeline (features → models → confidence gates) has something to learn on a
    # series that is not a pure random walk; otherwise the demo could never emit a
    # signal and the whole flow would be untestable.
    #
    # This is still synthetic data. Models trained on it are labelled DEMO in the
    # UI and their metrics must never be read as market performance.
    from scipy.signal import lfilter

    rho = 0.26 + (asset.seed % 7) * 0.01      # ~0.26–0.32 (demo only — far above real FX)
    clustered = noise * (0.75 + 0.5 * np.abs(np.roll(z, 1)) / np.maximum(np.std(z), 1e-9))
    rets = lfilter([1.0], [1.0, -rho], drift + clustered).astype(float)

    spike_mask = rng.random(n_minutes) < 0.0025
    rets = np.where(spike_mask, rets * float(rng.uniform(3.0, 6.0)), rets)

    p0 = 1.0 + (asset.seed % 97) / 1000.0
    closes = p0 * np.exp(np.cumsum(rets))
    opens = np.empty(n_minutes, dtype=float)
    opens[0] = p0
    opens[1:] = closes[:-1]

    wick_up = np.abs(rng.normal(0, 0.55, n_minutes)) * sigma * vol_mult
    wick_dn = np.abs(rng.normal(0, 0.55, n_minutes)) * sigma * vol_mult
    highs = np.maximum(opens, closes) * (1.0 + wick_up)
    lows = np.minimum(opens, closes) * (1.0 - wick_dn)

    scale = asset.simulated_pip
    opens = np.round(opens / scale) * scale
    closes = np.round(closes / scale) * scale
    highs = np.round(highs / scale) * scale
    lows = np.round(lows / scale) * scale

    # volume: deterministic, correlated with absolute return
    vrng = np.random.default_rng(asset.seed + 7)
    base = 120.0 + (asset.seed % 60)
    noise = vrng.lognormal(mean=0.0, sigma=0.35, size=n_minutes)
    activity = 1.0 + np.minimum(6.0, np.abs(rets) / max(sigma, 1e-9))
    volume = np.round(base * noise * activity, 2)

    with _LOCK:
        _CACHE[asset.symbol] = (n_minutes, opens, highs, lows, closes, volume)
    return opens, highs, lows, closes, volume


class SimulatedProvider(MarketDataAdapter):
    """Deterministic simulated feed (see module docstring)."""

    name = "simulated"
    label = "Simulated demo feed (synthetic)"
    label = "SIMULATED FEED (demo — not market data)"
    realtime = False
    is_simulated = True

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    # ---------------------------------------------------- adapter interface (spec §11)
    def connect(self) -> dict:
        return self.health()

    def health(self) -> dict:
        return {
            "provider": self.name,
            "label": self.label,
            "connected": True,
            "status": "READY",
            "detail": ("synthetic feed for pipeline demonstration — never market data, never a production "
                       "signal source"),
            "realtime_capable": True,
            "supports_subscribe": True,
            "is_simulated": True,
            "last_update_utc": None,
            "checked_at_utc": utc_iso(),
        }

    def subscribe(self, asset: AssetSpec, callback: object | None = None) -> dict:
        return {"provider": self.name, "asset": asset.symbol, "supported": True, "mode": "generated",
                "detail": "candles are generated on demand and labelled SIMULATED"}

    def fetch(self, asset: AssetSpec, limit: int = 300, now: datetime | None = None) -> CandleFrame:
        if not self.enabled:
            from ..base import MarketDataUnavailable

            raise MarketDataUnavailable(asset.symbol, "Simulated feed disabled by configuration", self.name)

        now_utc = floor_minute(now or datetime.now(tz=timezone.utc))
        completed = int((now_utc - EPOCH).total_seconds() // 60)      # candles fully closed
        in_progress_index = completed                                # the candle forming right now
        total = completed + 1                                         # +1 to model the live candle
        if completed < limit + 10:
            completed = limit + 10
            total = completed + 1

        opens, highs, lows, closes, volume = _path(asset, total)

        index = pd.date_range(start=EPOCH, periods=total, freq="1min", tz="UTC")
        df = pd.DataFrame(
            {
                "open": opens[:-1],
                "high": highs[:-1],
                "low": lows[:-1],
                "close": closes[:-1],
                "volume": volume[:-1],
            },
            index=index[:-1],
        ).tail(limit)

        # the in-progress candle is only exposed as a live price (never as a closed candle)
        elapsed = (datetime.now(tz=timezone.utc) - now_utc).total_seconds()
        frac = float(np.clip(elapsed / 60.0, 0.0, 1.0))
        live_open = float(closes[in_progress_index - 1]) if in_progress_index >= 1 else float(opens[0])
        step = math.log(max(closes[in_progress_index], 1e-12) / max(opens[in_progress_index], 1e-12))
        live_price = live_open * math.exp(step * frac)
        live_price = float(np.round(live_price / asset.simulated_pip) * asset.simulated_pip)

        meta = FeedMeta(
            asset=asset.symbol,
            provider=self.name,
            source_label=self.label,
            mode="REPLAY",
            realtime=False,
            is_simulated=True,
            is_replay=True,
            source_class=SOURCE_CLASS_SIMULATED,
            delayed_seconds=0.0,
            retrieved_at_utc=utc_iso(datetime.now(tz=timezone.utc)),
            notes=(
                "Deterministic simulated series produced locally for pipeline testing. "
                "NOT market data. Never labelled as live."
            ),
        )
        frame = make_frame(asset.symbol, df, meta, live_price=live_price, min_candles=10)
        # newest completed candle closes exactly at the top of the current minute
        frame.meta.latest_candle_close_utc = utc_iso(now_utc)
        frame.meta.latest_candle_open_utc = utc_iso(now_utc - timedelta(minutes=1))
        return frame
