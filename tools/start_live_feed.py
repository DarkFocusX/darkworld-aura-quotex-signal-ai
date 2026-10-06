"""Live M1 feed bridge — the operator's authorised feed → the existing rolling store.

    python3 tools/start_live_feed.py                     # connect, stream, build M1, deliver
    python3 tools/start_live_feed.py --target store      # write straight into the store
    python3 tools/start_live_feed.py --assets USDJPY,USDCHF --seconds 900
    python3 tools/start_live_feed.py --dry-run           # connect + log, deliver nothing

What it does (and does not do):

* connects to `QUOTEX_WS_URL` with the session value from `QUOTEX_SSID` (environment
  only — nothing is ever hard-coded, guessed or printed);
* parses incoming messages with `backend.data.feedsource.parse_feed_message` — ticks
  are aggregated into M1 candles by the *same* `ContinuousFeed` builder the API uses
  (`open` = first tick, `high`/`low` = running extremes, `close` = last tick, volume
  only when the feed supplies it);
* delivers each **closed** candle to the API (`POST /api/data/ingest`, default) or
  straight into the rolling store (`--target store`);
* reconnects with exponential backoff, logs connection health, and stops cleanly on
  Ctrl+C;
* **never** invents a candle, a price, a volume or a fallback feed. If the feed sends
  a partial bar, the message is rejected and counted. If nothing is configured the
  tool prints `LIVE FEED NOT CONFIGURED` and exits.

The API's paper scanner then picks the new candle up through the rolling store and
runs the *same* `SignalEngine` the GENERATE SIGNAL button uses.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import load_dotenv                      # noqa: E402
from backend.constants import ASSET_MAP, canonical_symbol    # noqa: E402
from backend.data import store                               # noqa: E402
from backend.data.feed import ContinuousFeed                 # noqa: E402
from backend.data.feedsource import (KIND_AUTH_ERROR, KIND_CANDLE, KIND_IGNORED, KIND_INVALID,
                                     KIND_TICK, STATE_AUTH_FAILED, STATE_CONNECTED,
                                     WebSocketFeedSource, parse_feed_message, redact)  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_CONFIGURED = 3
EXIT_AUTH_FAILED = 4
EXIT_TRANSPORT = 5

DEFAULT_RECV_TIMEOUT = 5.0
DEFAULT_LOG_SECONDS = 30.0
IDLE_WARN_SECONDS = 90.0


# --------------------------------------------------------------------------- helpers
def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_flag(name: str, default: bool = False) -> bool:
    raw = env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:
        return default


def log(level: str, message: str, *, quiet: bool = False) -> None:
    stamp = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    if not quiet:
        print(f"[{stamp}Z] {level:7s} {redact(message)}", flush=True)
    try:                                        # mirror into the API event log when possible
        from backend.database import db

        if level in {"ERROR", "WARNING"}:
            db.log_event("FEED", redact(message)[:400], level=level)
    except Exception:  # noqa: BLE001 - logging must never break the bridge
        pass


def symbol_map() -> dict[str, str]:
    """`LIVE_FEED_SYMBOL_MAP=US30=US30,XAU/USD=XAUUSD` — what the feed calls an instrument."""
    raw = env("LIVE_FEED_SYMBOL_MAP")
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        left, _, right = pair.partition("=")
        left, right = left.strip().upper(), right.strip().upper()
        if left and right:
            mapping[left] = right
    return mapping


def resolve_symbol(name: str, mapping: dict[str, str]) -> str | None:
    """Map a feed symbol onto a known asset — or return None (never guess a mapping)."""
    key = str(name or "").strip().upper()
    if not key:
        return None
    if key in mapping:
        return mapping[key] if mapping[key] in ASSET_MAP else None
    canonical = canonical_symbol(key)
    if canonical and canonical in ASSET_MAP:
        return canonical
    squashed = key.replace("/", "").replace("_", "").replace("-", "")
    canonical = canonical_symbol(squashed)
    return canonical if canonical and canonical in ASSET_MAP else None


def normalise_assets(raw: str | None) -> list[str]:
    if not raw:
        from backend.settings_service import get_settings

        assets = [a for a in get_settings().enabled_assets if a]
    else:
        assets = [part.strip().upper() for part in raw.split(",") if part.strip()]
    out: list[str] = []
    for asset in assets:
        canonical = canonical_symbol(asset) or asset
        if canonical in ASSET_MAP and canonical not in out:
            out.append(canonical)
    if not env_flag("LIVE_FEED_INCLUDE_OTC", False):
        out = [a for a in out if not a.upper().endswith("OTCQ")]
    return out


def api_post(api_url: str, payload: dict, *, timeout: float = 10.0) -> tuple[bool, str]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(f"{api_url.rstrip('/')}/api/data/ingest", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read() or b"{}")
            return True, f"http {response.status} accepted={data.get('accepted')} rejected={data.get('rejected')}"
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace")
        return False, f"http {exc.code}: {redact(detail)}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {redact(exc)}"


# ----------------------------------------------------------------------------- bridge
class LiveFeedBridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.source = WebSocketFeedSource(
            args.url,
            ssid=env("QUOTEX_SSID"),
            headers=self._headers(),
            timeout=env_float("LIVE_FEED_CONNECT_TIMEOUT", 10.0),
            reconnect=not args.no_reconnect,
            on_log=lambda level, message: log(level, message, quiet=args.quiet),
        )
        self.assets = self._assets()
        self.mapping = symbol_map()
        self.builder = ContinuousFeed(
            provider=args.provider_label,
            source_label=f"Operator-authorised live feed ({self._host()})",
            realtime=True, is_simulated=False, is_replay=False,
            retention_minutes=int(env_float("LIVE_FEED_RETENTION_MINUTES", 7 * 24 * 60)),
            # `--target api`: the API owns the store, so the closure of a candle is
            # delivered over HTTP. `--target store`: this process writes it directly.
            persist_candles=(args.target == "store"),
        )
        for asset in self.assets:
            self.builder.subscribe(asset)
        self.stopped = threading.Event()
        self.counts = {
            "messages": 0, "ticks": 0, "candles": 0, "delivered": 0, "delivery_failures": 0,
            "rejected_messages": 0, "ignored_messages": 0, "unknown_symbols": 0,
            "future_messages": 0, "duplicate_candles": 0,
        }
        self._unknown_seen: set[str] = set()
        self._last_log = 0.0
        self._last_message_utc: datetime | None = None
        self._stopping = False

    # ------------------------------------------------------------------ configuration
    def _host(self) -> str:
        from urllib.parse import urlparse

        try:
            parsed = urlparse(self.args.url)
            return parsed.netloc or "feed"
        except Exception:  # noqa: BLE001
            return "feed"

    def _headers(self) -> dict[str, str]:
        ssid = env("QUOTEX_SSID")
        if not ssid or env_flag("LIVE_FEED_AUTH_IN_MESSAGE", False):
            return {}
        header = env("LIVE_FEED_AUTH_HEADER", "Authorization") or "Authorization"
        scheme = env("LIVE_FEED_AUTH_SCHEME")
        return {header: f"{scheme} {ssid}".strip()}

    def _assets(self) -> list[str]:
        assets = normalise_assets(self.args.assets or env("LIVE_FEED_ASSETS") or None)
        if not assets:
            log("ERROR", "no valid asset configured — set LIVE_FEED_ASSETS or ENABLED_ASSETS")
        return assets

    def _subscribe_payload(self, asset: str) -> dict | None:
        template = env("LIVE_FEED_SUBSCRIBE_TEMPLATE")
        if not template:
            return None
        try:
            rendered = template.format(asset=asset, ssid=env("QUOTEX_SSID"), symbol=asset)
        except (KeyError, IndexError) as exc:
            log("ERROR", f"LIVE_FEED_SUBSCRIBE_TEMPLATE is malformed: {type(exc).__name__}")
            return None
        try:
            return json.loads(rendered)
        except json.JSONDecodeError:
            log("ERROR", "LIVE_FEED_SUBSCRIBE_TEMPLATE must be JSON after substitution — nothing "
                         "was sent (a guessed subscribe message could subscribe to the wrong feed)")
            return None

    # ------------------------------------------------------------------------ delivery
    def deliver_candle(self, asset: str, row: dict, *, receipt: dict | None = None) -> bool:
        """Deliver one closed candle: over HTTP (--target api) or already stored (--target store)."""
        stamp = row.get("ts_utc")
        if isinstance(stamp, datetime):
            stamp = stamp.astimezone(timezone.utc).isoformat()
        # Only the fields the ingest API's candle schema accepts: `asset` belongs to the
        # request envelope, and the whole payload here is validated with extra="forbid"
        # (the live run caught a 422 when the row's own `asset` leaked in).
        payload = {
            "ts_utc": stamp,
            "open": row.get("open"), "high": row.get("high"),
            "low": row.get("low"), "close": row.get("close"),
            "resolution_minutes": float(row.get("resolution_minutes") or 1.0),
        }
        if row.get("volume") is not None:
            payload["volume"] = float(row["volume"])       # only when the feed supplied it
        if self.args.dry_run:
            self.counts["delivered"] += 1
            return True
        if self.args.target == "store":
            # the ContinuousFeed builder already wrote this row with its provenance
            if (receipt or {}).get("updated"):
                self.counts["duplicate_candles"] += 1
            self.counts["delivered"] += 1
            return True
        ok, detail = api_post(self.args.api_url, {
            "asset": asset,
            "candles": [payload],
            "provider": self.args.provider_label,
            "source_label": self.builder.source_label,
            "realtime": True, "simulated": False, "replay": False,
        })
        if ok:
            self.counts["delivered"] += 1
            log("INFO", f"candle stored {asset} {payload['ts_utc']} → {detail}", quiet=self.args.quiet)
        else:
            self.counts["delivery_failures"] += 1
            log("ERROR", f"delivery failed for {asset} {payload['ts_utc']}: {detail}")
        return ok

    # ----------------------------------------------------------------------------- run
    def handle_message(self, raw) -> str | None:
        """Returns 'auth_failed' when the feed refused the session, else None."""
        messages = parse_feed_message(raw)
        self._last_message_utc = datetime.now(tz=timezone.utc)
        if not messages:
            self.counts["ignored_messages"] += 1
            return None
        for message in messages:
            if message.kind == KIND_IGNORED:
                self.counts["ignored_messages"] += 1
                continue
            if message.kind == KIND_AUTH_ERROR:
                self.source.note_auth_failure(message.reason or "feed refused the session")
                return "auth_failed"
            if message.kind == KIND_INVALID:
                self.counts["rejected_messages"] += 1
                log("WARNING", f"message rejected: {message.reason}")
                continue

            stamp = message.time_utc
            now = datetime.now(tz=timezone.utc)
            if stamp is not None and (stamp - now).total_seconds() > 60:
                self.counts["future_messages"] += 1
                log("WARNING", f"message rejected: timestamp {stamp.isoformat()} is in the future")
                continue

            asset = resolve_symbol(message.asset or self._single_asset(), self.mapping)
            if not asset:
                self.counts["unknown_symbols"] += 1
                key = str(message.asset)
                if key not in self._unknown_seen:
                    self._unknown_seen.add(key)
                    log("WARNING", f"unknown instrument {key!r} ignored — add it to LIVE_FEED_SYMBOL_MAP "
                                   f"by mapping it onto a known asset")
                continue

            if message.kind == KIND_TICK:
                self.counts["ticks"] += 1
                receipt = self.builder.ingest_tick(asset, message.price, time_utc=stamp,
                                                   volume=message.volume)
                if not receipt.get("accepted"):
                    self.counts["rejected_messages"] += 1
                    log("WARNING", f"tick rejected for {asset}: {receipt.get('reason')}")
                    continue
                closed = receipt.get("closed_candle") or {}
                row = closed.get("row") or (closed.get("candle") or {}).get("row")
                if closed.get("accepted") and row:
                    self.counts["candles"] += 1
                    log("INFO", f"M1 candle closed {asset} {row.get('ts_utc')} "
                                f"o={row.get('open')} h={row.get('high')} "
                                f"l={row.get('low')} c={row.get('close')}", quiet=self.args.quiet)
                    self.deliver_candle(asset, row, receipt=closed.get("receipt"))
                elif closed.get("accepted") is False:
                    self.counts["rejected_messages"] += 1
                    log("WARNING", f"candle rejected for {asset}: {closed.get('reason')}")
            elif message.kind == KIND_CANDLE:
                self.counts["candles"] += 1
                row = {
                    "ts_utc": stamp, "open": message.open, "high": message.high,
                    "low": message.low, "close": message.close,
                    "volume": message.volume, "resolution_minutes": 1.0,
                }
                stored = self.builder.ingest_candle(asset, dict(row))
                if not stored.get("accepted"):
                    self.counts["rejected_messages"] += 1
                    log("WARNING", f"candle rejected for {asset}: {stored.get('reason')}")
                    continue
                if stored.get("status") == "DUPLICATE_UPDATED":
                    self.counts["duplicate_candles"] += 1
                log("INFO", f"closed candle {asset} {stamp.isoformat() if stamp else '—'}",
                    quiet=self.args.quiet)
                self.deliver_candle(asset, row, receipt=stored.get("receipt"))
        return None

    def _single_asset(self) -> str | None:
        return self.assets[0] if len(self.assets) == 1 else None

    def heartbeat(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_log) < self.args.log_seconds:
            return
        self._last_log = now
        health = self.source.health()
        idle = health.get("idle_seconds")
        rows = {asset: store.count(asset, provider=self.args.provider_label) for asset in self.assets}
        log("INFO", f"health state={health['state']} messages={self.counts['messages']} "
                    f"ticks={self.counts['ticks']} candles={self.counts['candles']} "
                    f"delivered={self.counts['delivered']} rejected={self.counts['rejected_messages']} "
                    f"unknown={self.counts['unknown_symbols']} reconnects={health['reconnects']} "
                    f"idle={idle}s rows={rows}")
        if idle is not None and idle > IDLE_WARN_SECONDS:
            log("WARNING", f"no message for {idle:.0f}s — check the instrument/subscription with "
                           f"tools/probe_live_feed.py")

    def stop(self, *_args) -> None:
        if self._stopping:
            return
        self._stopping = True
        log("INFO", "stop requested — closing the feed …")
        self.stopped.set()

    def run(self) -> int:
        log("INFO", f"starting live feed bridge → {self.args.target} "
                    f"(assets: {', '.join(self.assets) or '—'}, provider label "
                    f"{self.args.provider_label})")
        health = self.source.connect()
        if health["state"] == STATE_AUTH_FAILED:
            log("ERROR", f"LIVE FEED AUTH_FAILED — {health.get('last_error')}")
            return EXIT_AUTH_FAILED
        if health["state"] != STATE_CONNECTED:
            log("ERROR", f"LIVE FEED NOT CONNECTED — state={health['state']} "
                         f"{health.get('last_error') or ''}")
            return EXIT_TRANSPORT
        self._subscribe()
        deadline = time.time() + self.args.seconds if self.args.seconds else None
        empty_polls = 0
        try:
            while not self.stopped.is_set():
                if deadline and time.time() >= deadline:
                    log("INFO", f"reached the time limit ({self.args.seconds:.0f}s) — stopping")
                    break
                frame = self.source.recv(timeout=self.args.recv_timeout)
                if frame is None:
                    empty_polls += 1
                    if self.source.state == STATE_AUTH_FAILED:
                        log("ERROR", "LIVE FEED AUTH_FAILED — the feed rejected the session")
                        return EXIT_AUTH_FAILED
                    if self.source.state != STATE_CONNECTED:
                        if self.args.no_reconnect:
                            log("ERROR", "connection lost and reconnection is disabled — exiting")
                            return EXIT_TRANSPORT
                        self.source.ensure_connected()
                        self._subscribe(reconnect=True)
                    elif empty_polls % 12 == 0:
                        self.heartbeat()
                    continue
                empty_polls = 0
                self.counts["messages"] += 1
                if not self.args.quiet:
                    self.heartbeat()
                outcome = self.handle_message(frame)
                if outcome == "auth_failed":
                    log("ERROR", "LIVE FEED AUTH_FAILED — the feed rejected the session; check "
                                 "QUOTEX_SSID (nothing is guessed or retried forever)")
                    return EXIT_AUTH_FAILED
        except KeyboardInterrupt:            # pragma: no cover - interactive
            log("INFO", "interrupted (Ctrl+C)")
        finally:
            self.source.close()
            self.heartbeat(force=True)
            log("INFO", f"stopped · delivered {self.counts['delivered']} candle(s) · "
                        f"rejected {self.counts['rejected_messages']} message(s) · "
                        f"failures {self.counts['delivery_failures']}")
        return EXIT_OK

    def _subscribe(self, *, reconnect: bool = False) -> None:
        for asset in self.assets:
            payload = self._subscribe_payload(asset)
            if payload is None:
                continue
            if self.source.send_json(payload):
                log("INFO", f"subscription sent for {asset}" + (" (after reconnect)" if reconnect else ""))
            else:
                log("WARNING", f"subscription for {asset} could not be sent")


# ------------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None, help="override QUOTEX_WS_URL")
    parser.add_argument("--assets", default=None, help="comma-separated assets (default: LIVE_FEED_ASSETS / ENABLED_ASSETS)")
    parser.add_argument("--target", choices=["api", "store"], default=env("LIVE_FEED_TARGET", "api"),
                        help="deliver candles through the API (default) or straight into the store")
    parser.add_argument("--api-url", default=env("LIVE_FEED_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--provider-label", default=env("LIVE_FEED_PROVIDER_LABEL", "operator_bridge"),
                        help="provider label stored on every row (provenance)")
    parser.add_argument("--seconds", type=float, default=env_float("LIVE_FEED_MAX_SECONDS", 0.0),
                        help="stop after N seconds (0 = run until Ctrl+C)")
    parser.add_argument("--recv-timeout", type=float, default=env_float("LIVE_FEED_RECV_TIMEOUT", DEFAULT_RECV_TIMEOUT))
    parser.add_argument("--log-seconds", type=float, default=env_float("LIVE_FEED_LOG_SECONDS", DEFAULT_LOG_SECONDS))
    parser.add_argument("--no-reconnect", action="store_true", help="exit instead of reconnecting")
    parser.add_argument("--dry-run", action="store_true", help="parse and log, deliver nothing")
    parser.add_argument("--quiet", action="store_true", help="only log warnings/errors and the summary")
    return parser


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    args.url = (args.url or env("QUOTEX_WS_URL"))

    ok, reason = (bool(args.url), "QUOTEX_WS_URL")
    if not ok:
        print("LIVE FEED NOT CONFIGURED", flush=True)
        print(
            "\nNo authorised feed endpoint is configured, so no candle can be received.\n"
            "Nothing is invented and no public feed is substituted for yours.\n\n"
            "  1. copy the template:      cp .env.example .env      (Windows: copy .env.example .env)\n"
            "  2. put your feed endpoint: QUOTEX_WS_URL=wss://<your-feed-host>/<path>\n"
            "  3. put your session value: QUOTEX_SSID=<your-session-or-token>\n"
            "  4. verify:                 python3 tools/probe_live_feed.py\n"
            "  5. start the bridge:       python3 tools/start_live_feed.py\n\n"
            "See LIVE_FEED_SETUP.md for the step-by-step version.",
            flush=True)
        return EXIT_NOT_CONFIGURED

    bridge = LiveFeedBridge(args)
    signal.signal(signal.SIGINT, bridge.stop)
    try:
        signal.signal(signal.SIGTERM, bridge.stop)
    except (AttributeError, ValueError):  # pragma: no cover - platforms without SIGTERM
        pass
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
