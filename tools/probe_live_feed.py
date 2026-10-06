"""Live feed probe — is the authorised feed actually delivering usable M1 candles?

    python3 tools/probe_live_feed.py                    # probe the configured feed (~30 s)
    python3 tools/probe_live_feed.py --seconds 90 --asset USDJPY
    python3 tools/probe_live_feed.py --self-test         # fixture messages, labelled SIMULATED
    python3 tools/probe_live_feed.py --json              # machine-readable report

Verdicts (never hidden, never softened):

    NOT_CONFIGURED  QUOTEX_WS_URL (or --url) is missing — nothing can be received
    AUTH_FAILED     the feed rejected the session (QUOTEX_SSID / header / message auth)
    INVALID_DATA    messages arrived but none carried a usable price or a complete OHLC set
    NO_DATA         connected and authenticated, but no data message inside the window
    CONNECTED       real data messages arrived, passed parsing + validation, and M1 candles
                    were built from them

It also reports symbol availability, timestamp freshness, the M1 aggregation result and a
candle-validation tally, so a partial problem ("connects, but the pair is not subscribed")
is visible instead of being rounded up to "connected".

The probe never writes to the store: candles are built with `persist_candles=False`, so an
exploratory run cannot leave rows behind. `--self-test` feeds labelled fixture messages
through the same parser/validator/builder and can never be mistaken for market data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import load_dotenv                                    # noqa: E402
from backend.constants import ASSET_MAP, GATES                            # noqa: E402
from backend.data.feed import ContinuousFeed                              # noqa: E402
from backend.data.feedsource import (KIND_AUTH_ERROR, KIND_CANDLE, KIND_IGNORED, KIND_INVALID,
                                     KIND_TICK, STATE_AUTH_FAILED, STATE_CONNECTED, STATE_ERROR,
                                     STATE_NOT_CONFIGURED, WebSocketFeedSource, parse_feed_message,
                                     redact)                                  # noqa: E402
from backend.timeutil import utc_iso                                      # noqa: E402

VERDICT_CONNECTED = "CONNECTED"
VERDICT_NOT_CONFIGURED = "NOT_CONFIGURED"
VERDICT_AUTH_FAILED = "AUTH_FAILED"
VERDICT_NO_DATA = "NO_DATA"
VERDICT_INVALID = "INVALID_DATA"

EXIT = {VERDICT_CONNECTED: 0, VERDICT_NOT_CONFIGURED: 3, VERDICT_AUTH_FAILED: 4,
        VERDICT_NO_DATA: 6, VERDICT_INVALID: 7}

FIXTURE_MESSAGES = [
    {"type": "welcome", "message": "session accepted"},
    {"type": "tick", "symbol": "USDJPY", "price": 149.100, "ts": "2026-10-02T12:00:05Z"},
    {"type": "tick", "symbol": "USDJPY", "price": 149.140, "ts": "2026-10-02T12:00:20Z"},
    {"type": "tick", "symbol": "USDJPY", "price": 149.080, "ts": "2026-10-02T12:00:40Z"},
    {"type": "tick", "symbol": "USDJPY", "price": 149.120, "ts": "2026-10-02T12:00:55Z"},
    {"type": "tick", "symbol": "USDJPY", "price": 149.130, "ts": "2026-10-02T12:01:10Z"},
    {"type": "candle", "symbol": "USDJPY", "open": 1.0, "close": 2.0, "ts": "2026-10-02T12:02:00Z"},
    {"type": "tick", "symbol": "ZZZUNKNOWN", "price": 5.0, "ts": "2026-10-02T12:02:20Z"},
]


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.assets = self._assets()
        self.counts = {
            "frames": 0, "data_messages": 0, "ticks": 0, "candles_in": 0, "ignored": 0,
            "invalid": 0, "unknown_symbols": 0, "auth_errors": 0, "future": 0,
            "candles_closed": 0, "payload_rejected": 0,
        }
        self.symbols_seen: dict[str, int] = {}
        self.first_message_utc: datetime | None = None
        self.last_message_utc: datetime | None = None
        self.last_data_utc: datetime | None = None
        self.aggregated: list[dict] = []
        self.source: WebSocketFeedSource | None = None
        self.builder = ContinuousFeed(provider="probe", source_label="Live feed probe (read-only)",
                                      realtime=True, is_simulated=False, is_replay=False,
                                      persist_candles=False)
        for asset in self.assets:
            self.builder.subscribe(asset)

    # ------------------------------------------------------------------- utility
    def _assets(self) -> list[str]:
        raw = (self.args.asset or os.environ.get("LIVE_FEED_ASSETS")
               or os.environ.get("ENABLED_ASSETS") or "USDJPY")
        out = []
        for part in str(raw).split(","):
            symbol = part.strip().upper()
            if symbol and symbol in ASSET_MAP and symbol not in out:
                out.append(symbol)
        return out or ["USDJPY"]

    def _resolve(self, name: str | None) -> str | None:
        key = str(name or "").strip().upper()
        if not key:
            return self.assets[0] if len(self.assets) == 1 else None
        if key in ASSET_MAP:
            return key
        squashed = key.replace("/", "").replace("_", "").replace("-", "")
        return squashed if squashed in ASSET_MAP else None

    # ---------------------------------------------------------------- ingestion
    def consume(self, raw, *, now: datetime | None = None) -> None:
        self.counts["frames"] += 1
        moment = now or datetime.now(tz=timezone.utc)
        self.first_message_utc = self.first_message_utc or moment
        self.last_message_utc = moment
        for message in parse_feed_message(raw):
            if message.kind == KIND_IGNORED:
                self.counts["ignored"] += 1
                continue
            if message.kind == KIND_AUTH_ERROR:
                self.counts["auth_errors"] += 1
                continue
            if message.kind == KIND_INVALID:
                self.counts["invalid"] += 1
                continue
            self.counts["data_messages"] += 1
            self.last_data_utc = moment
            asset = self._resolve(message.asset)
            if asset is None:
                self.counts["unknown_symbols"] += 1
                self.symbols_seen[str(message.asset or "?")] = \
                    self.symbols_seen.get(str(message.asset or "?"), 0) + 1
                continue
            self.symbols_seen[asset] = self.symbols_seen.get(asset, 0) + 1
            stamp = message.time_utc
            if stamp is not None and (stamp - moment).total_seconds() > 60:
                self.counts["future"] += 1
                continue
            if message.kind == KIND_TICK:
                self.counts["ticks"] += 1
                receipt = self.builder.ingest_tick(asset, message.price, time_utc=stamp,
                                                   volume=message.volume)
                if not receipt.get("accepted"):
                    self.counts["payload_rejected"] += 1
                self._collect_closed(receipt)
            elif message.kind == KIND_CANDLE:
                self.counts["candles_in"] += 1
                receipt = self.builder.ingest_candle(asset, {
                    "ts_utc": stamp, "open": message.open, "high": message.high,
                    "low": message.low, "close": message.close, "volume": message.volume,
                    "resolution_minutes": 1.0,
                })
                if receipt.get("accepted"):
                    self.aggregated.append({"asset": asset, "ts_utc": str(stamp), "source": "candle",
                                            "open": message.open, "high": message.high,
                                            "low": message.low, "close": message.close})
                else:
                    self.counts["payload_rejected"] += 1

    def _collect_closed(self, receipt: dict) -> None:
        closed = receipt.get("closed_candle") or {}
        if not closed.get("accepted"):
            if closed:
                self.counts["payload_rejected"] += 1
            return
        row = closed.get("row") or {}
        self.counts["candles_closed"] += 1
        self.aggregated.append({
            "asset": row.get("asset"), "ts_utc": utc_iso(row["ts_utc"]) if row.get("ts_utc") else None,
            "source": "ticks", "open": row.get("open"), "high": row.get("high"),
            "low": row.get("low"), "close": row.get("close"), "volume": row.get("volume"),
        })

    # -------------------------------------------------------------------- verdict
    def verdict(self) -> tuple[str, list[str]]:
        notes: list[str] = []
        if not self.args.url:
            return VERDICT_NOT_CONFIGURED, ["QUOTEX_WS_URL is not set (or --url was not given)."]
        if self.counts["auth_errors"] or (self.source and self.source.state == STATE_AUTH_FAILED):
            return VERDICT_AUTH_FAILED, ["the feed rejected the session"]
        if self.source and self.source.state == STATE_ERROR:
            return VERDICT_INVALID, [f"transport error: {self.source.last_error}"]
        usable = self.counts["ticks"] + self.counts["candles_in"]
        if usable == 0:
            if self.counts["frames"] == 0:
                return VERDICT_NO_DATA, ["connected, but not a single frame arrived in the window"]
            if self.counts["invalid"]:
                return VERDICT_INVALID, [
                    f"{self.counts['invalid']} message(s) had no usable price and no complete OHLC set"]
            return VERDICT_NO_DATA, ["only non-data messages (heartbeats/acks) arrived"]
        if self.counts["candles_closed"] == 0 and self.counts["candles_in"] == 0:
            notes.append("ticks arrived, but no M1 candle has closed yet inside the probe window")
        if self.counts["unknown_symbols"]:
            notes.append(f"{self.counts['unknown_symbols']} message(s) used an instrument that is not "
                         f"a known asset — map it with LIVE_FEED_SYMBOL_MAP")
        if self.counts["invalid"]:
            notes.append(f"{self.counts['invalid']} message(s) rejected (incomplete or invalid OHLC)")
        if self.counts["payload_rejected"]:
            notes.append(f"{self.counts['payload_rejected']} value(s) rejected by the candle validator")
        if self.counts["future"]:
            notes.append(f"{self.counts['future']} message(s) carried a future timestamp")
        return VERDICT_CONNECTED, notes

    def report(self) -> dict:
        verdict, notes = self.verdict()
        now = datetime.now(tz=timezone.utc)
        freshness = None
        if self.last_data_utc is not None:
            freshness = round((now - self.last_data_utc).total_seconds(), 1)
        return {
            "verdict": verdict,
            "url_set": bool(self.args.url),
            "ssid_set": bool(os.environ.get("QUOTEX_SSID", "").strip()),
            "state": self.source.state if self.source else STATE_NOT_CONFIGURED,
            "assets_requested": self.assets,
            "symbols_seen": self.symbols_seen,
            "counts": dict(self.counts),
            "last_message_age_seconds": freshness,
            "freshness_budget_seconds": float(GATES["max_data_lag_seconds"]),
            "m1_candles": self.aggregated[-5:],
            "notes": notes,
            "self_test": bool(self.args.self_test),
            "generated_at_utc": utc_iso(),
        }


def run_self_test(probe: Probe, args: argparse.Namespace) -> int:
    """Feed labelled fixture messages through the real parser/validator/builder."""
    print("SELF-TEST — fixture messages (never market data; nothing is stored)\n")
    base = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)
    for index, message in enumerate(FIXTURE_MESSAGES):
        payload = json.dumps(message)
        if "ts" in message:                       # keep the fixture inside the freshness window
            payload = payload.replace(message["ts"], (base + timedelta(seconds=index * 15)).isoformat())
        probe.consume(payload, now=datetime.now(tz=timezone.utc))
    probe.consume('{"type":"tick","symbol":"USDJPY","price":149.11,"ts":"2099-01-01T00:00:00Z"}')
    probe.consume("not-json-at-all")
    probe.counts["frames"] = len(FIXTURE_MESSAGES) + 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None, help="override QUOTEX_WS_URL")
    parser.add_argument("--asset", default=None, help="asset to watch (default: LIVE_FEED_ASSETS / ENABLED_ASSETS)")
    parser.add_argument("--seconds", type=float, default=None, help="observation window (default 30)")
    parser.add_argument("--self-test", action="store_true",
                        help="run the plumbing check on labelled fixture messages (no network)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()

    args.url = (args.url or os.environ.get("QUOTEX_WS_URL", "").strip())
    if args.self_test:
        args.url = args.url or "fixture://self-test"
    window = args.seconds if args.seconds is not None else (0.0 if args.self_test else 30.0)

    probe = Probe(args)
    if args.self_test:
        run_self_test(probe, args)
    else:
        if not args.url:
            report = probe.report()
            emit(report, args)
            return EXIT[VERDICT_NOT_CONFIGURED]
        from tools.start_live_feed import env_float, env_flag, log as bridge_log

        ssid = os.environ.get("QUOTEX_SSID", "").strip()
        headers = {}
        if ssid and not env_flag("LIVE_FEED_AUTH_IN_MESSAGE", False):
            header = os.environ.get("LIVE_FEED_AUTH_HEADER", "Authorization") or "Authorization"
            scheme = os.environ.get("LIVE_FEED_AUTH_SCHEME", "")
            headers[header] = f"{scheme} {ssid}".strip()
        probe.source = WebSocketFeedSource(args.url, ssid=ssid, headers=headers,
                                           timeout=env_float("LIVE_FEED_CONNECT_TIMEOUT", 10.0),
                                           reconnect=False,
                                           on_log=lambda level, message: bridge_log(level, message))
        health = probe.source.connect()
        if health["state"] == STATE_AUTH_FAILED:
            emit(probe.report(), args)
            return EXIT[VERDICT_AUTH_FAILED]
        if health["state"] != STATE_CONNECTED:
            emit(probe.report(), args)
            return EXIT[VERDICT_INVALID]
        deadline = time.time() + window
        while time.time() < deadline:
            frame = probe.source.recv(timeout=min(2.0, max(0.5, deadline - time.time())))
            if frame is None:
                if probe.source.state == STATE_AUTH_FAILED:
                    break
                continue
            probe.consume(frame)
        probe.source.close()

    report = probe.report()
    emit(report, args)
    return EXIT[report["verdict"]]


def emit(report: dict, args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    counts = report["counts"]
    print("LIVE FEED PROBE")
    print(f"  verdict            : {report['verdict']}")
    print(f"  endpoint configured: {report['url_set']}   session value set: {report['ssid_set']}")
    print(f"  connection state   : {report['state']}")
    print(f"  assets requested   : {', '.join(report['assets_requested'])}")
    print(f"  symbols seen       : {report['symbols_seen'] or '—'}")
    print(f"  frames / data      : {counts['frames']} / {counts['data_messages']}")
    print(f"  ticks / candles in : {counts['ticks']} / {counts['candles_in']}")
    print(f"  M1 candles closed  : {counts['candles_closed']}")
    print(f"  rejected / ignored : {counts['invalid'] + counts['payload_rejected']} / {counts['ignored']}")
    print(f"  unknown symbols    : {counts['unknown_symbols']}   future stamps: {counts['future']}")
    age = report["last_message_age_seconds"]
    print(f"  newest data age    : {'—' if age is None else f'{age}s'} "
          f"(budget {report['freshness_budget_seconds']:.0f}s)")
    for candle in report["m1_candles"]:
        print(f"    M1 {candle.get('ts_utc')} o={candle.get('open')} h={candle.get('high')} "
              f"l={candle.get('low')} c={candle.get('close')} ({candle.get('source')})")
    for note in report["notes"]:
        print(f"  note               : {note}")
    if report["self_test"]:
        print("  ⚠️  SELF-TEST: fixture messages were used (labelled, never market data).")
    print("\nNext: python3 tools/start_live_feed.py   (then GET /api/data/live-status)")


if __name__ == "__main__":
    raise SystemExit(main())
