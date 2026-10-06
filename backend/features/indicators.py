"""Technical indicators — pure pandas/numpy, causal (no look-ahead).

Every function uses only information available up to and including the last row,
so the same code is safe for both live inference and walk-forward training.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [(df["high"] - df["low"]), (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, window)
    std = close.rolling(window, min_periods=window).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + num_std * std, "lower": mid - num_std * std,
                         "width": (2 * num_std * std)})


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder ADX / +DI / -DI."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(df)
    atr_ = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False,
                                                          min_periods=period).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False,
                                                             min_periods=period).mean() / atr_.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame({"adx": dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean(),
                         "plus_di": plus_di, "minus_di": minus_di})


def efficiency_ratio(close: pd.Series, window: int = 10) -> pd.Series:
    """Kaufman efficiency ratio: |net move| / sum(|moves|). 1 = clean trend, 0 = chop."""
    net = (close - close.shift(window)).abs()
    path = close.diff().abs().rolling(window, min_periods=window).sum()
    return (net / path.replace(0, np.nan)).clip(0.0, 1.0)


def zscore(series: pd.Series, window: int = 20) -> pd.Series:
    mean = series.rolling(window, min_periods=window).mean()
    std = series.rolling(window, min_periods=window).std(ddof=0)
    return (series - mean) / std.replace(0, np.nan)


def candle_anatomy(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    rng = (h - l).replace(0, np.nan)
    body = (c - o)
    return pd.DataFrame({
        "body": body,
        "body_abs": body.abs(),
        "range": h - l,
        "body_ratio": (body.abs() / rng).clip(0.0, 1.0),
        "upper_wick_ratio": ((h - np.maximum(o, c)) / rng).clip(0.0, 1.0),
        "lower_wick_ratio": ((np.minimum(o, c) - l) / rng).clip(0.0, 1.0),
        "close_pos": ((c - l) / rng).clip(0.0, 1.0),
        "bullish": (c > o).astype(float),
    })


def swing_levels(df: pd.DataFrame, lookback: int, left: int = 2, right: int = 2) -> tuple[list[float], list[float]]:
    """Confirmed pivot highs/lows (right-neighbour confirmation avoids look-ahead)."""
    highs, lows = [], []
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    start = max(left, n - lookback)
    for i in range(start, n - right):
        window = slice(i - left, i + right + 1)
        if h[i] == h[window].max():
            highs.append(float(h[i]))
        if l[i] == l[window].min():
            lows.append(float(l[i]))
    return highs, lows


def support_resistance(df: pd.DataFrame, price: float, lookback: int = 90) -> dict:
    """Nearest confirmed support/resistance + position inside the recent range."""
    recent = df.tail(lookback)
    highs, lows = swing_levels(recent, lookback=lookback)
    fallback_high = float(recent["high"].max()) if len(recent) else price
    fallback_low = float(recent["low"].min()) if len(recent) else price
    resistances = sorted([h for h in highs if h > price]) or [fallback_high]
    supports = sorted([s for s in lows if s < price], reverse=True) or [fallback_low]
    hi = float(recent["high"].max()) if len(recent) else price
    lo = float(recent["low"].min()) if len(recent) else price
    span = (hi - lo) if hi > lo else max(price * 1e-6, 1e-12)
    return {
        "nearest_support": supports[0],
        "nearest_resistance": resistances[0],
        "range_high": hi,
        "range_low": lo,
        "range_position": float(np.clip((price - lo) / span, 0.0, 1.0)),
        "range_span_pct": float(span / price) if price else 0.0,
        "pivot_highs": highs[-8:],
        "pivot_lows": lows[-8:],
    }


def donchian(df: pd.DataFrame, window: int) -> tuple[float, float]:
    return float(df["high"].tail(window).max()), float(df["low"].tail(window).min())


def rolling_volume_z(df: pd.DataFrame, window: int = 20) -> float:
    vol = df["volume"]
    if vol.isna().all() or len(vol.dropna()) < window:
        return 0.0
    z = zscore(vol, window).iloc[-1]
    return 0.0 if pd.isna(z) else float(np.clip(z, -4, 4))


def slope_norm(series: pd.Series, window: int = 10, scale: float = 1.0) -> float:
    """Least-squares slope over the window, normalised by `scale` (usually ATR)."""
    if len(series.dropna()) < window:
        return 0.0
    y = series.tail(window).to_numpy(dtype=float)
    x = np.arange(window, dtype=float)
    denom = ((x - x.mean()) ** 2).sum()
    if denom == 0 or scale <= 0:
        return 0.0
    return float(((x - x.mean()) * (y - y.mean())).sum() / denom / scale)
