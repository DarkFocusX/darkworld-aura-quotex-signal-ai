"""Twelve Data realtime standard-FX bridge — vendor quotes → the existing M1 pipeline.

    python3 tools/start_twelve_data_feed.py                  # connect, stream, build M1, deliver
    python3 tools/start_twelve_data_feed.py --target store   # write straight into the local store
    python3 tools/start_twelve_data_feed.py --dry-run        # connect + log, deliver nothing
    python3 tools/start_twelve_data_feed.py --seconds 900    # stop after 15 minutes
    python3 tools/start_twelve_data_feed.py --show-config    # print the resolved (redacted) setup

It is the *same* bridge as `tools/start_live_feed.py` — the parsing, the tick validation,
the `ContinuousFeed` M1 builder, the delivery, the heartbeats, the reconnect/backoff and
the redaction are all reused, never re-implemented. Only the vendor specifics differ:

* the endpoint carries the key in its query string:
  ``wss://ws.twelvedata.com/v1/quotes/price?apikey=<TWELVE_DATA_API_KEY>``;
* there is no header session, so `Authorization` is never sent;
* the subscription is one frame for the whole instrument set:
  ``{"action": "subscribe", "params": {"symbols": "EUR/USD,GBP/USD,..."}}``;
* instruments are translated with the explicit `EUR/USD → EURUSD` map, and an instrument
  that maps to nothing (or to an OTC pair, which this source does not carry) is **not**
  subscribed — nothing is guessed and standard FX is never presented as OTC.

Security: the key comes from `TWELVE_DATA_API_KEY` only. It is never printed, never
logged, never stored in SQLite or the config table, never returned by the API and never
placed in an exception message — every log line goes through the shared redactor, which
masks ``apikey=`` and every query string.

The pipeline it feeds is unchanged: a Twelve Data tick → validate → `ContinuousFeed`
(closed M1 only) → the rolling store → `live_store` → the *same* `SignalEngine`.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import load_dotenv                                        # noqa: E402
from backend.data.providers import twelve_data as td                          # noqa: E402
from tools.start_live_feed import (DEFAULT_LOG_SECONDS, DEFAULT_RECV_TIMEOUT,  # noqa: E402
                                   EXIT_NOT_CONFIGURED, EXIT_OK, LiveFeedBridge, env,
                                   env_float, log, symbol_map)


class TwelveDataFeedBridge(LiveFeedBridge):
    """The shared bridge specialised for the Twelve Data realtime standard-FX stream."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._subscription_sent = False
        super().__init__(args)
        # every row this bridge writes carries the vendor as its provider label, and the
        # label names the source (never the endpoint's query string)
        self.builder.source_label = f"Twelve Data realtime standard FX ({self._host()})"
        # the vendor instrument vocabulary, with the operator's own overrides winning
        self.mapping = {**td.SYMBOL_MAP, **symbol_map()}

    # ------------------------------------------------------------------ configuration
    def _endpoint(self) -> str:
        """Endpoint *with* the key in the `apikey=` query parameter (never logged verbatim)."""
        return td.connect_url(self.args.url)

    def _session(self) -> str:
        """Twelve Data authenticates in the URL — no session value belongs in a header."""
        return ""

    def _headers(self) -> dict[str, str]:
        return {}

    def _assets(self) -> list[str]:
        """Configured instruments → project assets (OTC and unmapped names are refused)."""
        raw = (self.args.assets or env(td.ENV_SYMBOLS) or td.DEFAULT_SYMBOLS)
        assets: list[str] = []
        refused: list[str] = []
        for part in str(raw).replace(" ", "").split(","):
            symbol = part.strip().upper()
            if not symbol:
                continue
            asset = td.canonical_asset(symbol)
            if asset is None:
                if symbol not in refused:
                    refused.append(symbol)
            elif asset not in assets:
                assets.append(asset)
        if refused:
            log("WARNING", f"not subscribed — no project asset behind {', '.join(refused)} "
                           f"(standard FX is never substituted for an OTC instrument)")
        if not assets:
            log("ERROR", f"no supported standard-FX instrument configured — set {td.ENV_SYMBOLS} "
                         f"(supported: {', '.join(sorted(td.SYMBOL_MAP))})")
        return assets

    # -------------------------------------------------------------------- subscribing
    def _subscribe(self, *, reconnect: bool = False) -> None:
        """One Twelve Data subscribe frame for the whole instrument set."""
        payload = td.subscription_payload(self.assets)
        if payload is None:
            log("WARNING", "nothing to subscribe to — no supported instrument is configured")
            return
        if self.source.send_json(payload):
            self._subscription_sent = True
            log("INFO", f"subscribed to {payload['params']['symbols']}"
                        + (" (after reconnect)" if reconnect else ""))
        else:
            log("WARNING", "the Twelve Data subscribe frame could not be sent")

    def _subscribe_payload(self, asset: str) -> dict | None:
        return td.subscription_payload([asset])

    # ------------------------------------------------------------------------ messages
    def handle_message(self, raw) -> str | None:
        """Twelve Data control frames first, then the shared tick/candle path."""
        control = td.control_frame(raw)
        if control is not None:
            kind, detail = control
            self._last_message_utc = datetime.now(tz=timezone.utc)
            if kind == td.EVENT_AUTH_FAILED:
                self.source.note_auth_failure(detail)
                log("ERROR", f"Twelve Data refused the session: {detail}")
                return "auth_failed"
            if kind == td.EVENT_REJECTED:
                self.counts["rejected_messages"] += 1
                log("WARNING", f"Twelve Data rejected a frame: {detail}")
                return None
            if kind == td.EVENT_ACK:
                log("INFO", f"Twelve Data: {detail}", quiet=self.args.quiet)
            self.counts["ignored_messages"] += 1
            return None
        return super().handle_message(raw)


# ------------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None,
                        help=f"override the endpoint only ({td.ENV_WS_URL}) — the key still comes "
                             f"from {td.ENV_API_KEY}")
    parser.add_argument("--assets", default=None,
                        help=f"comma-separated instruments (default: {td.ENV_SYMBOLS})")
    parser.add_argument("--target", choices=["api", "store"], default=env("LIVE_FEED_TARGET", "api"),
                        help="deliver candles through the API (default) or straight into the store")
    parser.add_argument("--api-url", default=env("LIVE_FEED_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--provider-label", default=env("LIVE_FEED_PROVIDER_LABEL", td.PROVIDER_NAME),
                        help="provider label stored on every row (provenance)")
    parser.add_argument("--seconds", type=float, default=env_float("LIVE_FEED_MAX_SECONDS", 0.0),
                        help="stop after N seconds (0 = run until Ctrl+C)")
    parser.add_argument("--recv-timeout", type=float,
                        default=env_float("LIVE_FEED_RECV_TIMEOUT", DEFAULT_RECV_TIMEOUT))
    parser.add_argument("--log-seconds", type=float,
                        default=env_float("LIVE_FEED_LOG_SECONDS", DEFAULT_LOG_SECONDS))
    parser.add_argument("--no-reconnect", action="store_true", help="exit instead of reconnecting")
    parser.add_argument("--dry-run", action="store_true", help="parse and log, deliver nothing")
    parser.add_argument("--quiet", action="store_true", help="only log warnings/errors and the summary")
    parser.add_argument("--show-config", action="store_true",
                        help="print the resolved configuration (redacted) and exit")
    return parser


def resolved_config(args: argparse.Namespace) -> dict:
    """Everything an operator needs to check — with the key reduced to a boolean."""
    return {
        "provider": td.PROVIDER_NAME,
        "endpoint": td.safe_endpoint(args.url),
        "api_key_set": bool(td.api_key()),
        "configured": td.configured(),
        "symbols": td.symbols(),
        "instruments": td.instruments(),
        "unmapped_symbols": td.unmapped_symbols(),
        "subscribe": td.subscription_payload(td.symbols()),
        "target": args.target,
        "provider_label": args.provider_label,
        "freshness_budget_seconds": 120.0,
    }


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    args.url = (args.url or "").strip() or None      # the key is added by connect_url()

    if args.show_config:
        print(json.dumps(resolved_config(args), indent=2))
        return EXIT_OK

    if not td.configured():
        print("TWELVE DATA NOT CONFIGURED", flush=True)
        print(
            "\nNo Twelve Data API key is configured, so no quote can be received.\n"
            "Nothing is invented and no other feed is substituted for yours.\n\n"
            "  1. copy the template:      cp .env.example .env      (Windows: copy .env.example .env)\n"
            f"  2. put your key there:     {td.ENV_API_KEY}=<your-twelve-data-api-key>\n"
            f"  3. (optional) instruments: {td.ENV_SYMBOLS}={td.DEFAULT_SYMBOLS}\n"
            "  4. verify:                 python3 tools/probe_live_feed.py --provider twelve_data\n"
            "  5. start the bridge:       python3 tools/start_twelve_data_feed.py\n\n"
            "The key is read from the environment only: it is never printed, logged, stored in\n"
            "the database or returned by the API. See LIVE_FEED_SETUP.md.",
            flush=True)
        return EXIT_NOT_CONFIGURED

    if not td.symbols():
        print("TWELVE DATA NOT CONFIGURED — no supported standard-FX instrument", flush=True)
        print(f"\n{td.ENV_SYMBOLS} names nothing this project can trade. "
              f"Supported: {', '.join(sorted(td.SYMBOL_MAP))}.\n"
              f"OTC pairs have no source here and are never substituted.", flush=True)
        return EXIT_NOT_CONFIGURED

    bridge = TwelveDataFeedBridge(args)

    signal.signal(signal.SIGINT, bridge.stop)
    try:
        signal.signal(signal.SIGTERM, bridge.stop)
    except (AttributeError, ValueError):  # pragma: no cover - platforms without SIGTERM
        pass

    log("INFO", f"Twelve Data bridge · endpoint {td.safe_endpoint(args.url)} · "
                f"instruments {', '.join(td.instruments()) or '—'} · key set {bool(td.api_key())}",
        quiet=args.quiet)
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
