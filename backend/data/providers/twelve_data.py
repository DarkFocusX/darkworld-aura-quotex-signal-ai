# ---------------------------------------------------------------------------
# Twelve Data REST time-series backfill.
#
# The live WebSocket feed (tools/start_twelve_data_feed.py) uses the official WebSocket.
# The *historical* path uses Twelve Data's official REST time-series endpoint to pull
# REAL past 1-minute OHLC candles and store them through the existing ingestion path.
# Backfill rows are `realtime=false`, so they can never make the live feed report
# FRESH; the live bridge resumes from the newest stored close.
#
# Security: the key is read from the environment only, masked by `redact()` in every
# URL/URL-encoded form, and never printed, logged, returned or stored.
#
# Idempotent: pages that overlap an already-stored candle are overwritten by the store's
# existing dedup (no duplicates, no double counting); a re-run is a re-check, never a
# recreation.

# --------------------------------------------------------------------------- errors

# --------------------------------------------------------------------------- errors
class TwelveDataRESTError(RuntimeError):
    """The REST call failed: timeout, network, bad status code or bad response."""


class MissingTwelveDataKey(RuntimeError):
    """The API key is not configured."""


import argparse
import json
import os
import socket
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from typing import Any

# --------------------------------------------------------------------------- config
def feed_config() -> dict[str, Any]:
    """Backfill provenance baked into every stored row."""
    return {
        "provider": PROVIDER_LABEL,
        "realtime": REALTIME,
        "simulated": SIMULATED,
        "replay": REPLAY,
        "source_class": SOURCE_CLASS,
    }


BACKFILL_TIMEOUT = float(os.environ.get("TWELVE_DATA_BACKFILL_TIMEOUT", "45"))

# ======================================================================== backfill
# Backfill *past* real candles from Twelve Data's REST time-series endpoint and store
# them through the existing ingestion path. It is not a feed and never becomes one:
# historical rows carry `realtime=false` so they cannot make the feed report FRESH,
# and the live WebSocket bridge resumes from the newest stored close.

PROVIDER_LABEL = "twelve_data"
REALTIME = False
SIMULATED = False
REPLAY = False
SOURCE_CLASS = "MARKET"

REST_URL = "https://api.twelvedata.com/time_series"
REST_DEFAULT_SYMBOL = "EUR/USD"
REST_DEFAULT_INTERVAL = "1min"
REST_DEFAULT_OUTPUTSIZE = 200
REST_MAX_OUTPUTSIZE = 200          # Twelve Data's documented hard ceiling for time_series

#: Errors raised by the backfill (kept separate from the live WebSocket feed's errors).


class TwelveDataRESTError(RuntimeError):
    """The REST call failed: timeout, network, bad status code or bad response."""


class MissingTwelveDataKey(RuntimeError):
    """The API key is not configured."""


class BackfillError(RuntimeError):
    """The backfill run failed for any other reason."""


BACKFILL_TIMEOUT = float(os.environ.get("TWELVE_DATA_BACKFILL_TIMEOUT", "45"))


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def api_key() -> str:
    return _env(ENV_API_KEY)


def rest_url() -> str:
    return _env(ENV_REST_URL) or REST_URL


def default_symbol() -> str:
    return _env(ENV_REST_SYMBOL) or REST_DEFAULT_SYMBOL


def default_interval() -> str:
    return _env(ENV_REST_INTERVAL) or REST_DEFAULT_INTERVAL


def raw_outputsize() -> int:
    try:
        return int(_env(ENV_REST_OUTPUTSIZE, str(REST_DEFAULT_OUTPUTSIZE)))
    except ValueError:
        return REST_DEFAULT_OUTPUTSIZE


def rest_outputsize() -> int:
    """The Twelve Data ceiling for the REST time-series endpoint (deduplicated)."""
    return min(max(int(_env(ENV_REST_OUTPUTSIZE) or REST_DEFAULT_OUTPUTSIZE), 1),
               REST_MAX_OUTPUTSIZE)


def _rest_request(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """A single Twelve Data REST time-series call. Never prints or returns the key:
    the key travels in `apikey=` and `redact()` masks the whole query string."""
    key = api_key()
    register_secret(key)
    if not key:
        raise MissingTwelveDataKey(
            f"{ENV_API_KEY} is not set — {PROVIDER_LABEL} backfill needs your REAL key "
            f"from {ENV_WS_URL if False else '.env'} (never hard-coded, never quoted)")
    params = dict(params)
    params["apikey"] = quote(key, safe="")
    url = f"{rest_url()}{path}?{urlencode({k: v for k, v in params.items() if v is not None})}"
    req = urllib.request.Request(url, headers={"User-Agent": "quotex-signal-ai/twelve-data-backfill"})
    with urllib.request.urlopen(req, timeout=BACKFILL_TIMEOUT) as response:
        raw = response.read().decode("utf-8", "replace")
    if response.status != 200:
        raise TwelveDataRESTError(raw[:2000] or f"HTTP {response.status}")
    return json.loads(raw)


# ----------------------------------------------------------------------- backfill
def fetch_history(asset: str, *, symbol: str | None = None, interval: str = "1min",
                  minutes: int = 200) -> list[dict[str, Any]]:
    """Retrieve and deduplicate the *closed* 1-minute OHLC rows the REST API owns."""
    symbol = canonical_asset_for(asset, symbol)
    if minutes < 1:
        return []
    minutes = max(minutes, REST_MAX_OUTPUTSIZE)   # request the ceiling once
    out: list[dict[str, Any]] = []
    page = None
    while len(out) < minutes:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "outputsize": REST_MAX_OUTPUTSIZE}
        if page is not None:
            params["page"] = page
        data = _rest_request("/", params)
        page += 1
        values = data.get("values")
        if not isinstance(values, list) or not values:
            # Twelve Data happens to place the newest bar on the first page, so a page
            # with no values means we exhausted the series; handle missing/empty here
            if len(out) == 0:
                raise TwelveDataRESTError("200 OK but no values in the response")
            break
        batch = [row_for(v) for v in values]
        out.extend(batch)
        if len(out) >= minutes:
            break
        if len(values) == 0:
            break
    # the REST API returns *newest first*: reverse into chronological order so the
    # existing M1 builder sees strictly increasing timestamps
    rows = list(reversed(out[:minutes]))
    dedupe(rows)          # page boundaries can overlap (outputsize/2 edges overlap)
    return rows[:minutes]


def parse_rows(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    """A twelve-data REST response -> parsed candles (closed-candle filter included)."""
    values = payload.get("values")
    if not isinstance(values, list):
        raise TwelveDataRESTError("200 OK but no 'values' array in the response")
    rows = []
    for raw in values:
        if not isinstance(raw, dict):
            continue
        ts = str(raw.get("datetime") or raw.get("ts") or "").strip()
        try:
            dt = parse_twelve_datetime(ts)
        except Exception:
            dt = None
        if dt is None:
            continue                     # malformed row: never stored
        candle = {
            "asset": symbol,
            "provider": PROVIDER_LABEL,
            "ts_utc": dt.astimezone(timezone.utc).isoformat(),
            "open": float(raw.get("open")),
            "high": float(raw.get("high")),
            "low": float(raw.get("low")),
            "close": float(raw.get("close")),
            "realtime": REALTIME,
            "simulated": SIMULATED,
            "replay": REPLAY,
            "source_class": SOURCE_CLASS,
            "source_label": f"Twelve Data historical backfill ({symbol})",
            "ingested_at_utc": utc_iso(),
        }
        if candle["open"] <= 0 or candle["high"] <= 0 or candle["low"] <= 0 or candle["close"] <= 0:
            continue                     # only *closed* candles: a missing/live bar is
        if candle["high"] < max(candle["open"], candle["close"]) or candle["low"] > min(
                candle["open"], candle["close"]):
            continue                     # never store an invalid geometry
        if len(rows) and dt <= datetime.fromisoformat(rows[-1]["ts_utc"]):
            continue                     # strictly increasing timestamps only
        rows.append(candle)
    return rows


def backfill(asset: str, *, minutes: int = 200, symbol: str | None = None,
             target: str = "api", label: str = PROVIDER_LABEL) -> dict[str, Any]:
    """One backfill run. Deduplicated against what is already stored, so it is
    idempotent: pages that overlap an already-stored candle update rather than
    duplicate it."""
    symbol = canonical_asset_for(asset, symbol)
    if not td.configured():
        raise MissingTwelveDataKey(f"{ENV_API_KEY} is not set — set it in .env and retry")
    minutes = max(minutes, REST_MAX_OUTPUTSIZE)
    existing = store.candles(symbol, provider=label)
    latest = store.latest_row(symbol, provider=label)
    latest_ts = latest["ts_utc"] if latest else None
    rows: list[dict[str, Any]] = []
    cursor = latest_ts if latest_ts else None
    page = 0
    while len(rows) < minutes:
        params: dict[str, Any] = {"symbol": symbol, "interval": "1min",
                                  "outputsize": REST_MAX_OUTPUTSIZE}
        if cursor:
            params["cursor"] = cursor
        data = _rest_request("/", params)
        values = data.get("values")
        if not values:
            break
        batch = parse_rows({"values": values}, symbol)
        if not batch:
            break
        # Twelve Data pages are newest-first; the newest row of the batch is the
        # freshest data available — skip anything at or before the newest stored row
        batch = [r for r in batch if r["ts_utc"] > latest_ts] if latest_ts else batch
        batch = dedupe_rows(batch)
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1]["ts_utc"]
        page += 1
        if page > REST_MAX_OUTPUTSIZE // 2 + 5:
            raise TwelveDataRESTError("too many pages; the symbol may have been delisted or "
                                       "restricted by the account")
    if not rows:
        return {"asset": symbol, "provider": label, "found": 0, "stored": 0,
                "duplicates": 0, "skipped": 0, "realtime": False,
                "note": "no new closed candles from Twelve Data REST"}
    storage = store.upsert_candles(rows, retention_minutes=DEFAULT_RETENTION_MINUTES)
    return {"asset": symbol, "provider": label, "found": len(rows), "stored": storage["inserted"],
            "duplicates": storage["updated"], "skipped": storage["skipped"],
            "realtime": False, "note": f"backfill {len(rows)} rows -> stored {storage['inserted']} "
                                        f"(updated {storage['updated']}, skipped {storage['skipped']})"}


# --------------------------------------------------------------------- re-fetch
def fetch_rows(asset: str, *, symbol: str | None = None, minutes: int = 200) -> list[dict]:
    symbol = canonical_asset_for(asset, symbol)
    minutes = max(minutes, REST_MAX_OUTPUTSIZE)
    rows: list[dict] = []
    page = 0
    while len(rows) < minutes:
        params = {"symbol": symbol, "interval": "1min", "outputsize": REST_MAX_OUTPUTSIZE,
                  "page": page}
        data = _rest_request("/", params)
        values = data.get("values")
        if not values:
            break
        batch = parse_rows({"values": values}, symbol)
        rows.extend(dedupe_rows(batch))
        page += 1
        if page > REST_MAX_OUTPUTSIZE // 2 + 5:
            raise TwelveDataRESTError("too many pages")
    return rows[:minutes]


# --------------------------------------------------------------------- retry / backoff
class BackfillError(RuntimeError):
    pass


class TwelveDataRESTError(BackfillError):
    pass


class MissingTwelveDataKey(BackfillError):
    pass


_Backoff = 1.0


def _retry(f, *, tries: int = 5, base: float = 2.0, jitter: float = 0.5, timeout: float = 30.0):
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return f()
        except (urllib.error.URLError, socket.timeout, socket.gaierror,
                TwelveDataRESTError, OSError) as exc:
            last = exc
            if attempt == tries - 1:
                raise BackfillError(f"Twelve Data REST call failed after {tries} attempts: "
                                     f"{type(exc).__name__}: {exc}") from exc
            _Backoff = min(base, _Backoff * jitter)
            time.sleep(_Backoff)
    raise BackfillError(f"Twelve Data REST call failed: {last}")


# --------------------------------------------------------------------- storage
def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row["asset"], row["ts_utc"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def dedupe(store_rows, new_rows):
    keys = {(r["asset"], r["ts_utc"]) for r in store_rows}
    return [r for r in new_rows if (r["asset"], r["ts_utc"]) not in keys], len(new_rows) - len(
        [r for r in new_rows if (r["asset"], r["ts_utc"]) in keys])


# --------------------------------------------------------------------- main CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--asset", default="EURUSD",
                        help="asset to backfill (default EURUSD)")
    parser.add_argument("--symbol", default=None,
                        help="Twelve Data instrument (default inferred from the asset map)")
    parser.add_argument("--minutes", type=int, default=200,
                        help="buffer of closed M1 candles to accumulate (default 200)")
    parser.add_argument("--target", choices=["api", "store"], default="api",
                        help="deliver through the API (default) or write straight into the store")
    parser.add_argument("--provider-label", default=PROVIDER_LABEL,
                        help="provenance label written on every stored row")
    parser.add_argument("--url", default=None,
                        help="override REST endpoint (default: TWELVE_DATA_REST_URL or official)")
    parser.add_argument("--outputsize", type=int, default=None,
                        help="page size for the REST time-series endpoint")
    parser.add_argument("--quiet", action="store_true",
                        help="only print the summary plus warnings and errors")
    parser.add_argument("--no-api", action="store_true",
                        help="write into the store and do not POST to the API")
    return parser


def main() -> int:
    from backend.config import load_dotenv

    load_dotenv()
    args = build_parser().parse_args()
    if args.no_api:
        args.target = "store"
    if args.quiet:
        args.target = "store"

    if not td.configured():
        print("TWELVE DATA NOT CONFIGURED", flush=True)
        print("\nTWELVE_DATA_API_KEY is not set in .env. Nothing is manufactured — "
              "every stored row keeps the REAL key's provenance and the feed stays "
              "unavailable until a real clock lands.\n", flush=True)
        return EXIT_NOT_CONFIGURED

    try:
        result = backfill(args.asset, minutes=args.minutes, symbol=args.symbol,
                          target=args.target, label=args.provider_label)
    except MissingTwelveDataKey as exc:
        print("TWELVE DATA NOT CONFIGURED", flush=True)
        print(f"\n{exc}\n", flush=True)
        return EXIT_NOT_CONFIGURED
    except TwelveDataRESTError as exc:
        print("REST ERROR", flush=True)
        print(f"\n{twelve_data.rest_url()}: {exc}\n", flush=True)
        return EXIT_REST_ERROR
    except BackfillError as exc:
        print("BACKFILL ERROR", flush=True)
        print(f"\n{exc}\n", flush=True)
        return EXIT_BACKFILL_ERROR

    print(f"Backfill {result['asset']} -> provider {result['provider']} | "
          f"found {result['found']} | stored {result['stored']} | "
          f"duplicates {result['duplicates']} | skipped {result['skipped']} | "
          f"realtime={result['realtime']}", flush=True)
    if result["note"]:
        print(f"note: {result['note']}", flush=True)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
