"""Operator utility — export real M1 candles into the CSV format this system ingests.

Why this exists
---------------
The dashboard is only as honest as its data. This script pulls **real** 1-minute
candles from a public source and writes them to `backend/data/csv/<ASSET>.csv` in
the exact format `/api/data/import` accepts. It does not simulate, smooth or
extrapolate anything: whatever the source returns is what gets written (and it is
labelled REPLAY everywhere, because a downloaded file is historical input).

Sources
-------
* `yahoo` — the public Yahoo Finance chart API. Real quotes for the non-OTC FX
  pairs, roughly a 1-minute delay, frequently rate-limited, may contain gaps.
  It CANNOT provide Quotex OTC instruments: nobody publishes those.
* For OTC pairs, feed `backend/data/csv/<ASSET>.csv` from your own broker export
  (or wire `QUOTEX_WS_URL` and use the tick adapter).

Usage
-----
    python -m tools.fetch_market_csv --assets EURUSD,GBPUSD --days 20
    python -m tools.fetch_market_csv --assets EURUSD --days 7 --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.constants import ASSET_MAP, canonical_symbol  # noqa: E402

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; QuotexSignalAI/1.0; operator data export)"}
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def fetch_chunk(yahoo_symbol: str, start: datetime, end: datetime, *, retries: int = 3) -> pd.DataFrame:
    params = {
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": "1m",
        "includePrePost": "false",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(CHART_URL.format(symbol=yahoo_symbol), params=params,
                                    headers=HEADERS, timeout=25)
            if response.status_code == 429:
                last_error = RuntimeError("HTTP 429 — public feed rate-limited")
                time.sleep(4 * (attempt + 1))
                continue
            response.raise_for_status()
            payload = response.json()
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                raise RuntimeError((payload.get("chart") or {}).get("error") or "empty chart result")
            block = result[0]
            stamps = block.get("timestamp") or []
            quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
            frame = pd.DataFrame({
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "close": quote.get("close"),
                "volume": quote.get("volume"),
            })
            frame.index = pd.DatetimeIndex(pd.to_datetime(stamps, unit="s", utc=True))
            return frame.dropna(subset=["open", "high", "low", "close"])
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {yahoo_symbol}: {last_error}")


def collect(yahoo_symbol: str, days: int) -> pd.DataFrame:
    """Walk backwards in 7-day windows (the 1m endpoint limit per request)."""
    end = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    frames: list[pd.DataFrame] = []
    remaining = days
    while remaining > 0:
        window = min(7, remaining)
        start = end - timedelta(days=window)
        try:
            frames.append(fetch_chunk(yahoo_symbol, start, end))
        except Exception as exc:  # noqa: BLE001 — a missing window is a gap, not a fake candle
            print(f"    ! window {start:%Y-%m-%d}→{end:%Y-%m-%d} failed: {exc}")
        end = start
        remaining -= window
        time.sleep(1.2)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    # keep only fully closed candles
    out = out[(out.index + pd.Timedelta(minutes=1)) <= datetime.now(tz=UTC)]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Export real M1 candles to the operator CSV folder.")
    parser.add_argument("--assets", default="EURUSD,GBPUSD,USDJPY,AUDUSD,USDCHF")
    parser.add_argument("--days", type=int, default=20, help="calendar days of M1 history per asset")
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parents[1] / "backend" / "data" / "csv"))
    parser.add_argument("--dry-run", action="store_true", help="fetch but do not write files")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    for raw in args.assets.split(","):
        symbol = canonical_symbol(raw.strip())
        if symbol is None:
            print(f"  ! unknown asset '{raw.strip()}' — skipped")
            failures.append(raw.strip())
            continue
        spec = ASSET_MAP[symbol]
        if not spec.yahoo_symbol:
            print(f"  ! {symbol}: no public source (OTC instrument) — supply your own broker CSV")
            failures.append(symbol)
            continue
        print(f"  · {symbol}: fetching {args.days}d of M1 from the public feed …")
        frame = collect(spec.yahoo_symbol, args.days)
        if frame.empty:
            print(f"    ! no data returned for {symbol}")
            failures.append(symbol)
            continue
        path = out_dir / f"{symbol}.csv"
        if args.dry_run:
            print(f"    dry-run: {len(frame)} rows ({frame.index[0]} → {frame.index[-1]})")
            continue
        out = frame.copy()
        out.index.name = "time"
        out.to_csv(path, date_format="%Y-%m-%dT%H:%M:%SZ", float_format="%.8g")
        print(f"    wrote {len(frame)} rows → {path.name} ({frame.index[0]:%Y-%m-%d %H:%M} → {frame.index[-1]:%Y-%m-%d %H:%M} UTC)")

    if failures:
        print(f"\n  not exported: {', '.join(failures)}")
    print("\n  Import state is recorded by the API (POST /api/data/import overwrites provenance).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
