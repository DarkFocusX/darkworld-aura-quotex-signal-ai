"""Test helpers shared across the suite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from backend.data.base import FeedMeta, make_frame
from backend.timeutil import utc_iso


def synthetic_market_frame(
    asset: str = "EURUSD",
    minutes: int = 220,
    *,
    last_candle_close_offset_seconds: float = 5.0,
    simulated: bool = False,
    realtime: bool = False,
    seed: int = 11,
    min_candles: int = 50,
):
    """Deterministic OHLC frame whose newest candle closed `offset` seconds ago."""
    now = datetime.now(tz=timezone.utc)
    last_close = now - timedelta(seconds=last_candle_close_offset_seconds)
    end = last_close.replace(second=0, microsecond=0)              # candle start of the last closed candle
    index = pd.date_range(end=end, periods=minutes, freq="1min", tz="UTC")
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.normal(0, 1.2e-4, minutes))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 4e-5, minutes))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 4e-5, minutes))
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 100.0}, index=index)
    meta = FeedMeta(
        asset=asset,
        provider="test_feed",
        source_label="TEST FEED",
        mode="LIVE" if realtime else "REPLAY",
        realtime=realtime,
        is_simulated=simulated,
        delayed_seconds=last_candle_close_offset_seconds,
        retrieved_at_utc=utc_iso(now),
    )
    frame = make_frame(asset, df, meta, min_candles=min_candles)
    frame.meta.latest_candle_close_utc = utc_iso(last_close)
    frame.meta.latest_candle_open_utc = utc_iso(end)
    return frame
