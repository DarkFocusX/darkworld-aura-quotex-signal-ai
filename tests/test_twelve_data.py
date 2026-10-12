"""Twelve Data realtime **standard FX** — configuration, protocol, pipeline, security.

What this file proves, in the order the data travels:

* the API key comes from the environment only; it is registered with the *shared* redactor,
  masked inside the ``apikey=`` query parameter, masked in URL form and in bare form, and it
  never appears in a health payload, a status payload, a log line or an exception;
* the subscription frame is exactly the one Twelve Data documents, built from an explicit
  instrument map — an unmapped instrument is reported and **not** subscribed, and OTC
  instruments are never subscribed at all;
* a Twelve Data price event is parsed by the *shared* parser and aggregated by the *shared*
  ``ContinuousFeed`` builder into a validated closed M1 candle whose OHLC comes from real
  ticks (no invented value, no invented volume);
* malformed, incomplete, invalid-geometry, non-finite, negative and future values are
  rejected, duplicates are dropped, and an unfinished candle is never persisted;
* the stored row keeps ``provider=twelve_data``, ``source_class=MARKET``, ``realtime=True``,
  ``is_simulated=False``, ``is_replay=False``, and ``live_store`` serves it to the engine
  with that provenance intact;
* FRESH requires a **stored closed candle inside the 120 s realtime budget**: a socket, a
  heartbeat, a subscribe acknowledgement and an unclosed candle are all *not* data;
* disconnect / reconnect / silent-socket behaviour is the shared transport's, and a rejected
  session is reported as AUTH_FAILED — never as "no data";
* OTC protection: ``USDBDT-OTCq`` / ``EURCAD-OTCq`` cannot be subscribed and cannot be
  fetched from this source, even if a row were somehow present;
* AUTO SELECT is untouched: Twelve Data rows simply flow through the existing availability
  probe and the existing engine chain.

Fixtures are labelled and use a fake key. No real credential, no network call and no
production database is involved.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.constants import ASSET_MAP                                        # noqa: E402
from backend.data import store                                                 # noqa: E402
from backend.data.feedsource import (KIND_IGNORED, KIND_INVALID, KIND_TICK,    # noqa: E402
                                     STATE_AUTH_FAILED, STATE_CONNECTED, STATE_DISCONNECTED,
                                     STATE_NOT_CONFIGURED, WebSocketFeedSource, parse_feed_message,
                                     redact, register_secret)
from backend.data.providers import twelve_data as td                           # noqa: E402

#: A fake credential: it is never a real key, and every assertion below is about it being
#: kept out of logs/payloads/exceptions.
FAKE_KEY = "td-fake-0123456789abcdef"
NOW = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)

OTC_SYMBOLS = ("USDBDT-OTCq", "EURCAD-OTCq")


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def price_frame(symbol: str, price: float, when: datetime) -> str:
    """A real Twelve Data realtime-quotes price event."""
    return json.dumps({"event": "price", "symbol": symbol, "currency_base": "X",
                       "currency_quote": "Y", "exchange": "forex", "type": "Forex",
                       "timestamp": int(when.timestamp()), "price": price})


# --------------------------------------------------------------------------- fixtures
@pytest.fixture()
def twelve_env(monkeypatch):
    """A configured Twelve Data environment with a fake key and no operator feed."""
    monkeypatch.setenv(td.ENV_API_KEY, FAKE_KEY)
    monkeypatch.delenv(td.ENV_WS_URL, raising=False)
    monkeypatch.delenv(td.ENV_SYMBOLS, raising=False)
    monkeypatch.delenv("QUOTEX_WS_URL", raising=False)
    monkeypatch.delenv("QUOTEX_SSID", raising=False)
    return td.TwelveDataFeedProvider()


@pytest.fixture()
def client(temp_db, twelve_env, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("TRADING_ENV", "production")
    monkeypatch.setenv("DATA_SOURCE_MODE", "auto")
    from backend.main import app

    return TestClient(app)


@pytest.fixture()
def bridge(monkeypatch, twelve_env, temp_db):
    """The Twelve Data bridge with the delivery call stubbed (no network, no HTTP)."""
    from tools import start_live_feed as bridge_module
    from tools import start_twelve_data_feed as td_bridge

    delivered: list[dict] = []
    monkeypatch.setattr(bridge_module, "api_post",
                        lambda url, payload, **kw: (delivered.append(payload), (True, "stub"))[1])

    def build(assets: str = "EUR/USD", target: str = "api"):
        args = td_bridge.build_parser().parse_args(["--assets", assets, "--target", target])
        return td_bridge.TwelveDataFeedBridge(args)

    build.delivered = delivered          # type: ignore[attr-defined]
    return build


def _store_series(asset: str, *, candles: int = 150, minutes_ago: float = 1.0,
                  provider: str = td.PROVIDER_NAME,
                  now: datetime | None = None) -> None:
    """Store a realtime real closed M1 series the way the bridge would."""
    reference = now or datetime.now(tz=timezone.utc)
    end = (reference - timedelta(minutes=minutes_ago)).replace(second=0, microsecond=0)
    rows = []
    for step in range(candles):
        opened = end - timedelta(minutes=candles - 1 - step)
        base = 1.10 + step * 0.00001
        rows.append({
            "asset": asset, "provider": provider, "ts_utc": iso(opened),
            "open": base, "high": base + 0.0004, "low": base - 0.0004, "close": base + 0.0002,
            "realtime": True, "is_simulated": False, "is_replay": False,
            "source_class": "MARKET", "source_label": "Twelve Data realtime standard FX",
        })
    store.upsert_candles(rows, retention_minutes=0)


# ---------------------------------------------------- §security: key loading + redaction
def test_api_key_is_read_from_the_environment_only(monkeypatch):
    monkeypatch.delenv(td.ENV_API_KEY, raising=False)
    assert td.api_key() == ""
    assert td.configured() is False

    monkeypatch.setenv(td.ENV_API_KEY, FAKE_KEY)
    assert td.api_key() == FAKE_KEY
    assert td.configured() is True
    # the public endpoint has a default, but the key is what makes a session
    assert td.ws_url() == td.DEFAULT_WS_URL

    monkeypatch.setenv(td.ENV_WS_URL, "wss://example.invalid/quote")
    assert td.ws_url() == "wss://example.invalid/quote"


def test_provider_without_a_key_is_not_configured(monkeypatch, temp_db):
    monkeypatch.delenv(td.ENV_API_KEY, raising=False)
    provider = td.TwelveDataFeedProvider()
    health = provider.health()
    assert provider.configured is False
    assert health["state"] == STATE_NOT_CONFIGURED
    assert health["connected"] is False
    assert health["api_key_set"] is False
    assert td.ENV_API_KEY in health["detail"]                 # tells the operator what to set


def test_apikey_query_parameter_is_masked_even_for_an_unregistered_value():
    """The `apikey=` rule is a pattern, not just a registry lookup."""
    secret = "unregistered-9f8e7d6c5b4a"
    masked = redact(f"apikey={secret}")
    assert secret not in masked
    assert "apikey=" in masked                      # the parameter survives, the value does not
    assert secret not in redact(f"connecting with apikey={secret}&limit=5")
    assert secret not in redact(f"https://api.example.com/v1?apikey={secret}")


def test_apikey_query_parameter_is_masked_everywhere():
    register_secret(FAKE_KEY)
    masked = redact(f"wss://ws.twelvedata.com/v1/quotes/price?apikey={FAKE_KEY}")
    assert FAKE_KEY not in masked
    assert masked.endswith("?***")                  # the whole query string is masked
    # ... whatever the parameter is called
    assert FAKE_KEY not in redact(f"wss://host/ws?key={FAKE_KEY}&x=1")
    # ... and the bare value is masked too, wherever it turns up
    assert FAKE_KEY not in redact(f"connect failed for {FAKE_KEY}")
    # ... including inside a JSON error body
    assert FAKE_KEY not in redact(json.dumps({"error": f"bad apikey={FAKE_KEY}"}))


def test_connect_url_carries_the_key_but_safe_endpoint_never_shows_it(monkeypatch):
    monkeypatch.setenv(td.ENV_API_KEY, FAKE_KEY)
    monkeypatch.delenv(td.ENV_WS_URL, raising=False)
    url = td.connect_url()
    assert url == f"{td.DEFAULT_WS_URL}?apikey={FAKE_KEY}"     # the socket needs it
    safe = td.safe_endpoint()
    assert FAKE_KEY not in safe
    assert "twelvedata.com/v1/quotes/price" in safe            # still identifiable


def test_no_payload_ever_returns_the_key(twelve_env):
    provider = twelve_env
    provider.note_auth_failure(f"twelve data rejected {FAKE_KEY}")
    payload = json.dumps({"status": provider.status(), "health": provider.health(),
                          "stats": provider.stats()})
    assert FAKE_KEY not in payload
    assert "***" in payload
    assert provider.health()["api_key_set"] is True            # a boolean, never the value


def test_key_is_not_leaked_in_the_connect_url_inside_the_transport(monkeypatch):
    from tests.test_live_feed import _FakeSocket, _install_fake_websocket

    monkeypatch.setenv(td.ENV_API_KEY, FAKE_KEY)
    captured: dict[str, object] = {}

    def factory(url, **kwargs):
        captured["url"] = url
        return _FakeSocket()

    _install_fake_websocket(monkeypatch, factory)
    source = WebSocketFeedSource(td.connect_url(), ssid="")
    health = source.connect()
    assert FAKE_KEY in str(captured["url"])                    # the socket really got the key
    assert FAKE_KEY not in json.dumps(health)                  # the report never does
    assert health["endpoint"].endswith("?***")


def test_bridge_prints_not_configured_without_a_key(monkeypatch, capsys, temp_db):
    from tools import start_twelve_data_feed as td_bridge

    monkeypatch.delenv(td.ENV_API_KEY, raising=False)
    # `main()` loads `.env`. On an operator's machine that file *does* contain their real
    # key, so this test must neutralise the loader: the point is "no key configured", not
    # "connect to Twelve Data for real".
    monkeypatch.setattr(td_bridge, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["start_twelve_data_feed.py"])
    exit_code = td_bridge.main()
    out = capsys.readouterr().out
    assert exit_code == td_bridge.EXIT_NOT_CONFIGURED
    assert "TWELVE DATA NOT CONFIGURED" in out
    assert td.ENV_API_KEY in out
    assert FAKE_KEY not in out


def test_show_config_reports_setup_without_the_key(monkeypatch, capsys):
    from tools import start_twelve_data_feed as td_bridge

    monkeypatch.setenv(td.ENV_API_KEY, FAKE_KEY)
    monkeypatch.setattr(sys, "argv", ["start_twelve_data_feed.py", "--show-config"])
    assert td_bridge.main() == td_bridge.EXIT_OK
    out = capsys.readouterr().out
    assert FAKE_KEY not in out
    assert "wss://ws.twelvedata.com/v1/quotes/price?***" in out
    assert '"api_key_set": true' in out


# ----------------------------------------------------------- §protocol: subscribe frame
def test_subscription_payload_matches_the_twelve_data_contract(monkeypatch):
    monkeypatch.delenv(td.ENV_SYMBOLS, raising=False)
    assert td.subscription_payload() == {
        "action": "subscribe",
        "params": {"symbols": "EUR/USD,GBP/USD,USD/JPY,USD/CHF,AUD/USD"},
    }
    assert td.unsubscribe_payload() == {
        "action": "unsubscribe",
        "params": {"symbols": "EUR/USD,GBP/USD,USD/JPY,USD/CHF,AUD/USD"},
    }


def test_subscription_payload_accepts_project_assets(monkeypatch):
    monkeypatch.setenv(td.ENV_SYMBOLS, "EURUSD,USDJPY")
    assert td.subscription_payload() == {"action": "subscribe",
                                         "params": {"symbols": "EUR/USD,USD/JPY"}}
    assert td.subscription_payload(["AUDUSD"]) == {"action": "subscribe",
                                                   "params": {"symbols": "AUD/USD"}}


def test_bridge_subscribes_once_for_the_whole_instrument_set(monkeypatch):
    from tools import start_twelve_data_feed as td_bridge

    monkeypatch.setenv(td.ENV_API_KEY, FAKE_KEY)
    monkeypatch.setenv(td.ENV_SYMBOLS, "EUR/USD,USD/JPY")
    args = td_bridge.build_parser().parse_args([])
    instance = td_bridge.TwelveDataFeedBridge(args)
    sent: list[dict] = []
    monkeypatch.setattr(instance.source, "send_json",
                        lambda payload: (sent.append(payload), True)[1])
    instance._subscribe()
    assert sent == [{"action": "subscribe", "params": {"symbols": "EUR/USD,USD/JPY"}}]
    assert instance._subscription_sent is True
    # Twelve Data authenticates in the URL: no header session is ever sent
    assert instance._headers() == {} and instance._session() == ""


def test_bridge_never_sends_a_header_session(monkeypatch):
    from tools import start_twelve_data_feed as td_bridge

    monkeypatch.setenv(td.ENV_API_KEY, FAKE_KEY)
    monkeypatch.setenv("QUOTEX_SSID", "operator-session-value")
    args = td_bridge.build_parser().parse_args([])
    instance = td_bridge.TwelveDataFeedBridge(args)
    assert instance._headers() == {}
    health = json.dumps(instance.source.health())
    assert "operator-session-value" not in health


# ------------------------------------------------------------- §protocol: parsing/mapping
def test_symbol_mapping_is_explicit_and_never_guessed():
    for instrument, asset in (("EUR/USD", "EURUSD"), ("GBP/USD", "GBPUSD"), ("USD/JPY", "USDJPY"),
                              ("USD/CHF", "USDCHF"), ("AUD/USD", "AUDUSD")):
        assert td.canonical_asset(instrument) == asset
        assert td.canonical_asset(instrument.lower()) == asset
    assert td.canonical_asset("EURUSD") == "EURUSD"          # the slash-less spelling works
    assert td.canonical_asset("NZD/USD") is None             # no project asset behind it
    assert td.canonical_asset("BTC/USD") is None
    assert td.canonical_asset("WHATEVER") is None
    assert td.canonical_asset("") is None and td.canonical_asset(None) is None


def test_bridge_maps_instruments_onto_assets_and_refuses_the_rest(monkeypatch):
    from tools import start_twelve_data_feed as td_bridge

    monkeypatch.setenv(td.ENV_API_KEY, FAKE_KEY)
    monkeypatch.setenv(td.ENV_SYMBOLS, "EUR/USD,NZD/USD,USDBDT-OTCq,EURUSD")
    args = td_bridge.build_parser().parse_args([])
    instance = td_bridge.TwelveDataFeedBridge(args)
    assert instance.assets == ["EURUSD"]                     # order kept, duplicates dropped
    assert td.instruments() == ["EUR/USD"]
    assert set(td.unmapped_symbols()) == {"NZD/USD", "USDBDT-OTCQ"}


def test_price_event_parses_with_the_shared_parser():
    frame = price_frame("EUR/USD", 1.23456, NOW.replace(second=5))
    messages = parse_feed_message(frame)
    assert len(messages) == 1
    message = messages[0]
    assert message.kind == KIND_TICK
    assert message.asset == "EUR/USD"
    assert message.price == pytest.approx(1.23456)
    assert message.time_utc == datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)


def test_control_frames_are_classified_and_never_look_like_data():
    assert td.control_frame('{"event":"heartbeat","status":"ok"}')[0] == td.EVENT_HEARTBEAT
    assert td.control_frame(json.dumps({"event": "subscribe-status", "status": "ok",
                                        "success": [{"symbol": "EUR/USD"}],
                                        "fails": []}))[0] == td.EVENT_ACK
    refused = td.control_frame(json.dumps({"event": "subscribe-status", "status": "error",
                                           "success": [],
                                           "fails": [{"symbol": "ZZZ/USD"}]}))
    assert refused[0] == td.EVENT_REJECTED and "ZZZ/USD" in refused[1]
    auth = td.control_frame('{"event":"error","code":401,"message":"Invalid API key"}')
    assert auth[0] == td.EVENT_AUTH_FAILED
    # a rate limit is a rejection, not a dead session
    assert td.control_frame('{"event":"error","code":429,"message":"API limit reached"}')[0] \
        == td.EVENT_REJECTED
    # a price frame and anything unrecognised fall through to the shared parser
    assert td.control_frame(price_frame("EUR/USD", 1.2, NOW)) is None
    assert td.control_frame("not json") is None
    assert td.control_frame(None) is None


def test_subscribe_status_is_ignored_by_the_shared_parser_not_counted_as_invalid():
    for frame in ('{"event":"subscribe-status","status":"ok","success":[],"fails":[]}',
                  '{"event":"unsubscribe-status","status":"ok"}'):
        assert parse_feed_message(frame)[0].kind == KIND_IGNORED
    assert parse_feed_message('{"event":"heartbeat","status":"ok"}')[0].kind == KIND_IGNORED


# -------------------------------------------------------- §pipeline: tick → closed M1
def test_price_events_close_one_validated_m1_candle(bridge):
    instance = bridge("EUR/USD")
    base = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)
    for price, offset in ((1.23456, 0), (1.23510, 20), (1.23401, 40), (1.23488, 50)):
        instance.handle_message(price_frame("EUR/USD", price, base + timedelta(seconds=offset)))
    # the first tick of 12:01 closes the 12:00 candle
    instance.handle_message(price_frame("EUR/USD", 1.23499, base + timedelta(seconds=65)))

    assert instance.counts["ticks"] == 5
    assert instance.counts["candles"] == 1 and instance.counts["delivered"] == 1
    payload = bridge.delivered[0]
    assert payload["asset"] == "EURUSD"
    candle = payload["candles"][0]
    assert candle["ts_utc"].startswith("2026-10-02T12:00")
    assert candle["open"] == pytest.approx(1.23456)     # first tick
    assert candle["high"] == pytest.approx(1.23510)     # running maximum
    assert candle["low"] == pytest.approx(1.23401)      # running minimum
    assert candle["close"] == pytest.approx(1.23488)    # last tick of that minute
    assert "volume" not in candle, "volume must never be invented"
    # the delivered candle satisfies the API's own schema (extra="forbid", no `asset`)
    from backend.main import CandleIn

    CandleIn(**candle)
    assert "asset" not in candle


def test_bridge_to_store_finalises_the_candle_with_provenance(bridge, temp_db):
    instance = bridge("EUR/USD", target="store")
    base = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)
    instance.handle_message(price_frame("EUR/USD", 1.23456, base))
    assert store.candles("EURUSD", provider=td.PROVIDER_NAME) == []   # nothing until it closes
    instance.handle_message(price_frame("EUR/USD", 1.23499, base + timedelta(seconds=70)))

    rows = store.candles("EURUSD", provider=td.PROVIDER_NAME)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "twelve_data"
    assert row["source_class"] == "MARKET"
    assert row["realtime"] == 1
    assert row["is_simulated"] == 0 and row["is_replay"] == 0
    assert row["resolution_minutes"] == 1.0
    assert row["volume"] is None
    assert "Twelve Data" in row["source_label"]
    assert row["ingested_at_utc"]


def test_unfinished_candle_is_never_persisted(bridge, temp_db):
    instance = bridge("EUR/USD", target="store")
    instance.handle_message(price_frame("EUR/USD", 1.23456, NOW))
    assert store.candles("EURUSD", provider=td.PROVIDER_NAME) == []
    assert instance.counts["candles"] == 0
    assert instance.builder.stats("EURUSD")["candles_closed"] == 0

    from backend.signals import feed_state

    state = feed_state.live_feed_state(assets=["EURUSD"])
    assert state["state"] != "FRESH"
    assert state["assets_streaming"] == []


def test_invalid_future_and_duplicate_ticks_are_rejected(bridge, temp_db):
    instance = bridge("EUR/USD", target="store")
    base = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)
    # malformed price → the shared parser cannot verify it, so nothing is stored
    instance.handle_message('{"event":"price","symbol":"EUR/USD","type":"Forex",'
                            '"timestamp":1759406405,"price":"not-a-number"}')
    assert instance.counts["rejected_messages"] == 1
    # a non-positive price is refused by the shared tick validator
    instance.handle_message(price_frame("EUR/USD", 0.0, base))
    assert instance.counts["rejected_messages"] == 2
    # a future timestamp is refused before it can open a candle
    future = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    instance.handle_message(price_frame("EUR/USD", 1.23456, future))
    assert instance.counts["future_messages"] == 1
    # a duplicate (same timestamp, same price) is dropped
    instance.handle_message(price_frame("EUR/USD", 1.23510, base + timedelta(seconds=20)))
    instance.handle_message(price_frame("EUR/USD", 1.23510, base + timedelta(seconds=20)))
    assert instance.counts["ticks"] == 3            # the duplicate reached the validator …
    assert instance.counts["rejected_messages"] == 3  # … and was refused there
    assert instance.builder.stats("EURUSD")["duplicate_ticks"] == 1
    assert store.candles("EURUSD", provider=td.PROVIDER_NAME) == []


def test_incomplete_or_geometrically_impossible_bars_are_rejected(bridge, temp_db):
    instance = bridge("EUR/USD", target="store")
    incomplete = json.dumps({"event": "price", "symbol": "EUR/USD", "open": 1.2, "close": 1.3,
                             "timestamp": int(NOW.timestamp())})
    instance.handle_message(incomplete)
    broken = json.dumps({"type": "candle", "symbol": "EUR/USD", "open": 1.20, "high": 1.10,
                         "low": 1.30, "close": 1.25, "ts": iso(NOW)})
    instance.handle_message(broken)
    assert instance.counts["rejected_messages"] == 2
    assert store.candles("EURUSD", provider=td.PROVIDER_NAME) == []


def test_unknown_instruments_are_counted_never_substituted(bridge, temp_db):
    instance = bridge("EUR/USD", target="store")
    instance.handle_message(price_frame("NZD/USD", 0.6012, NOW))
    assert instance.counts["unknown_symbols"] == 1
    assert instance.counts["ticks"] == 0
    assert store.assets() == []


# ----------------------------------------------------------------- §provenance + freshness
def test_stored_twelve_data_rows_keep_their_provenance_in_the_engine_frame(twelve_env, temp_db):
    from backend.data.providers.live_store import live_store

    # one explicit clock for both the write and the read, so the frame's contents never
    # depend on a second (implicit) clock read inside `fetch`
    moment = datetime.now(tz=timezone.utc)
    _store_series("EURUSD", candles=3, now=moment)
    assert store.count("EURUSD", provider=td.PROVIDER_NAME) == 3
    frame = live_store.fetch(ASSET_MAP["EURUSD"], limit=10, now=moment)
    assert frame.meta.provider == "twelve_data"
    assert frame.meta.mode == "REALTIME"
    assert frame.meta.realtime is True
    assert frame.meta.is_simulated is False and frame.meta.is_replay is False
    assert frame.meta.source_class == "MARKET"
    assert len(frame.df) == 3


def test_freshness_requires_a_closed_candle_inside_the_realtime_budget(twelve_env, temp_db):
    from backend.signals import feed_state

    # configured but silent: nothing has closed yet
    assert twelve_env.health()["store_evidence"]["fresh"] is False
    assert twelve_env.health()["connected"] is False
    state = feed_state.live_feed_state(assets=["EURUSD"])
    assert state["state"] == "CONNECTED" and state["data"] == "NO_DATA"
    assert state["configured"] is True
    assert state["provider"] == "twelve_data"

    # a closed candle inside the 120 s budget → FRESH
    _store_series("EURUSD", candles=2, minutes_ago=2.0)
    health = twelve_env.health()
    assert health["store_evidence"]["fresh"] is True
    assert health["connected"] is True and health["state"] == STATE_CONNECTED
    assert health["store_evidence"]["budget_seconds"] == 120.0
    assert health["store_evidence"]["age_seconds"] < 120
    state = feed_state.live_feed_state(assets=["EURUSD"])
    assert state["state"] == "FRESH" and state["data"] == "FRESH"
    assert state["provider"] == "twelve_data"


def test_a_candle_that_stopped_minutes_ago_is_stale_not_fresh(twelve_env, temp_db):
    from backend.signals import feed_state

    _store_series("EURUSD", candles=2, minutes_ago=5.0)     # closed ~4 minutes ago
    stale = feed_state.live_feed_state(assets=["EURUSD"])
    assert stale["state"] == "CONNECTED" and stale["data"] == "STALE"
    assert stale["age_seconds"] > 120                       # outside the realtime budget
    assert twelve_env.health()["store_evidence"]["fresh"] is False
    assert twelve_env.health()["connected"] is False


def test_a_socket_or_heartbeat_alone_is_never_fresh(twelve_env, temp_db, bridge, monkeypatch):
    from backend.signals import feed_state

    instance = bridge("EUR/USD")
    instance.handle_message('{"event":"subscribe-status","status":"ok","success":[],"fails":[]}')
    instance.handle_message('{"event":"heartbeat","status":"ok"}')
    instance.handle_message('{"event":"heartbeat","status":"ok"}')
    assert instance.counts["ignored_messages"] == 3
    assert instance.counts["ticks"] == 0
    state = feed_state.live_feed_state(assets=["EURUSD"])
    assert state["state"] != "FRESH"
    assert state["assets_streaming"] == []
    assert twelve_env.health()["store_evidence"]["rows"] == 0


def test_live_status_reports_twelve_data_provenance(twelve_env, temp_db):
    from backend.signals import paper as paper_state

    _store_series("EURUSD", candles=5, minutes_ago=1.0)
    payload = paper_state.live_status(["EURUSD"])
    row = payload["assets"][0]
    assert row["provider"] == "twelve_data"
    assert row["freshness_status"] == "FRESH"
    assert row["realtime"] is True
    assert row["is_simulated"] is False and row["is_replay"] is False
    assert row["budget_seconds"] == pytest.approx(120.0)
    assert row["rows"] == 5


# ----------------------------------------------------- §transport: auth / drop / silence
def test_rejected_session_is_auth_failed_never_no_data(bridge, temp_db):
    instance = bridge("EUR/USD")
    outcome = instance.handle_message('{"event":"error","code":401,"message":"Invalid API key"}')
    assert outcome == "auth_failed"
    assert instance.source.state == STATE_AUTH_FAILED
    assert instance.counts["rejected_messages"] == 0
    assert instance.counts["ticks"] == 0
    assert FAKE_KEY not in json.dumps(instance.source.health())

    # the provider reports the same verdict, and never retries a dead session
    provider = td.TwelveDataFeedProvider()
    provider.note_auth_failure("Invalid API key")
    assert provider.health()["state"] == STATE_AUTH_FAILED
    assert provider.health()["connected"] is False


def test_heartbeats_are_counted_as_ignored_not_as_ticks(bridge, temp_db):
    instance = bridge("EUR/USD")
    instance.handle_message('{"event":"heartbeat","status":"ok"}')
    instance.handle_message('{"event":"subscribe-status","status":"ok","success":[],"fails":[]}')
    assert instance.counts["ignored_messages"] == 2
    assert instance.counts["ticks"] == 0 and instance.counts["candles"] == 0


def test_silent_socket_is_connected_until_the_link_drops(monkeypatch, twelve_env, temp_db):
    from tests.test_live_feed import _FakeSocket, _install_fake_websocket

    _install_fake_websocket(monkeypatch, lambda *a, **k: _FakeSocket())
    source = WebSocketFeedSource(td.connect_url(), ssid="", reconnect=True)
    assert source.connect()["state"] == STATE_CONNECTED
    assert source.recv(timeout=0.01) is None
    assert source.state == STATE_CONNECTED            # an idle socket is still connected
    source._ws.fail_on_recv = True
    assert source.recv(timeout=0.01) is None
    assert source.state == STATE_DISCONNECTED         # a reset is a drop
    assert "ConnectionResetError" in (source.last_error or "")
    assert source.ensure_connected() is True          # reconnects with backoff
    assert source.reconnects == 1
    source.close()
    assert source.state == STATE_DISCONNECTED


def test_provider_state_machine_matches_the_shared_adapter(monkeypatch, twelve_env, temp_db):
    provider = twelve_env
    assert provider.configured is True
    assert provider.status()["adapter_state"] == "AWAITING_TICK_STREAM"
    assert provider.health()["state"] == STATE_DISCONNECTED      # configured, still silent
    provider.note_connected()
    provider.push_tick("EURUSD", {"price": 1.23456, "time_utc": iso(NOW)})
    health = provider.health()
    assert health["state"] == STATE_CONNECTED
    assert health["ticks_received"] == 1
    assert provider.subscribe(ASSET_MAP["EURUSD"])["supported"] is True
    provider.note_disconnected("socket closed by peer")
    assert provider.health()["state"] == STATE_DISCONNECTED
    assert FAKE_KEY not in json.dumps(provider.health())


def test_no_data_from_a_stored_series_raises_market_data_unavailable(twelve_env, temp_db):
    from backend.data.base import MarketDataUnavailable

    with pytest.raises(MarketDataUnavailable) as excinfo:
        twelve_env.fetch(ASSET_MAP["EURUSD"])
    assert "Twelve Data" in excinfo.value.reason
    assert FAKE_KEY not in str(excinfo.value)
    assert store.candles("EURUSD", provider=td.PROVIDER_NAME) == []


# ---------------------------------------------------------------------- §OTC protection
def test_otc_pairs_are_never_available_from_twelve_data(twelve_env, temp_db):
    from backend.data.base import MarketDataUnavailable

    for symbol in OTC_SYMBOLS:
        spec = ASSET_MAP[symbol]
        assert spec.kind == "otc"
        assert td.canonical_asset(symbol) is None            # never mapped
        assert td.canonical_asset(symbol.replace("-", "")) is None
        assert td.canonical_asset(symbol.lower()) is None
        assert twelve_env.subscribe(spec)["supported"] is False
        with pytest.raises(MarketDataUnavailable) as excinfo:
            twelve_env.fetch(spec)
        assert "OTC" in excinfo.value.reason
        assert twelve_env.health()["supports_otc"] is False


def test_otc_rows_are_refused_even_if_they_exist_in_the_store(twelve_env, temp_db):
    """Defence in depth: a mislabelled row can never make an OTC pair tradable here."""
    from backend.data.base import MarketDataUnavailable

    for symbol in OTC_SYMBOLS:
        store.upsert_candles([{
            "asset": symbol, "provider": td.PROVIDER_NAME, "ts_utc": iso(NOW),
            "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
            "realtime": True, "is_simulated": False, "is_replay": False,
            "source_class": "MARKET", "source_label": "Twelve Data realtime standard FX",
        }], retention_minutes=0)
        with pytest.raises(MarketDataUnavailable):
            twelve_env.fetch(ASSET_MAP[symbol])


def test_otc_symbols_are_not_in_the_configured_instrument_set(monkeypatch):
    monkeypatch.setenv(td.ENV_SYMBOLS, "USDBDT-OTCQ,EURCAD-OTCQ")
    assert td.symbols() == []
    assert td.instruments() == []
    assert td.subscription_payload() is None                 # nothing to subscribe to
    assert set(td.unmapped_symbols()) == {"USDBDT-OTCQ", "EURCAD-OTCQ"}


# --------------------------------------------------------------- §ingestion API + AUTO SELECT
def test_api_ingest_accepts_twelve_data_rows_when_configured(client):
    payload = {"asset": "EURUSD", "provider": td.PROVIDER_NAME, "realtime": True, "replay": False,
               "simulated": False, "source_label": "Twelve Data realtime standard FX",
               "candles": [{"ts_utc": iso(NOW), "open": 1.23401, "high": 1.23510, "low": 1.23390,
                            "close": 1.23488, "resolution_minutes": 1.0}]}
    response = client.post("/api/data/ingest", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1 and body["rejected"] == 0
    assert FAKE_KEY not in response.text
    row = store.latest_row("EURUSD", provider=td.PROVIDER_NAME)
    assert row["realtime"] == 1 and row["is_simulated"] == 0 and row["is_replay"] == 0


def test_api_ingest_refuses_a_twelve_data_claim_without_a_key(client, monkeypatch):
    monkeypatch.delenv(td.ENV_API_KEY, raising=False)
    response = client.post("/api/data/ingest", json={
        "asset": "EURUSD", "provider": td.PROVIDER_NAME, "realtime": True, "replay": False,
        "candles": [{"ts_utc": iso(NOW), "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1}]})
    assert response.status_code == 409
    assert td.ENV_API_KEY in response.json()["detail"]
    assert store.candles("EURUSD", provider=td.PROVIDER_NAME) == []


def test_auto_select_reads_twelve_data_rows_through_the_existing_probe(twelve_env, temp_db):
    from backend.data.registry import registry
    from backend.signals.engine import get_engine

    _store_series("EURUSD", candles=140, minutes_ago=1.0)
    probe = registry.probe("EURUSD", data_source_mode="live_only")
    assert probe["available"] is True
    assert probe["provider"] == td.PROVIDER_NAME            # the row's own label wins
    assert probe["simulated"] is False and probe["mode"] == "REALTIME"

    # AUTO SELECT logic is untouched: AUTO still means "every enabled asset"
    engine = get_engine()
    settings = engine._settings
    original = (settings.auto_select, settings.selected_asset)
    try:
        settings.auto_select = True
        settings.selected_asset = "AUTO"
        candidates = engine.target_assets("AUTO")
        assert candidates == [a for a in settings.enabled_assets if a]
        assert "EURUSD" in candidates
    finally:
        settings.auto_select, settings.selected_asset = original
    assert engine.target_assets("EURUSD") == ["EURUSD"]


def test_otc_protection_holds_through_the_registry_chain(twelve_env, temp_db):
    from backend.data.registry import registry

    _store_series("EURUSD", candles=140, minutes_ago=1.0)
    for symbol in OTC_SYMBOLS:
        probe = registry.probe(symbol, data_source_mode="live_only")
        assert probe["available"] is False
        assert probe.get("provider") != td.PROVIDER_NAME
        assert td.PROVIDER_NAME not in json.dumps(probe)


def test_registry_reports_twelve_data_health_and_provider_status(twelve_env, temp_db):
    from backend.data.registry import registry

    health = registry.adapter_health()
    assert health["twelve_data"]["provider"] == "twelve_data"
    assert health["twelve_data"]["realtime_capable"] is True
    assert health["twelve_data"]["is_simulated"] is False
    assert health["twelve_data"]["api_key_set"] is True
    assert health["twelve_data"]["supports_otc"] is False
    assert health["_summary"]["providers"] >= 5
    assert "twelve_data" in [entry["provider"] for entry in registry.provider_report()]


# ----------------------------------------------------- regression: the operator feed is intact
def test_the_existing_operator_bridge_is_unaffected(monkeypatch, temp_db):
    """Twelve Data support must not change the Quotex-style bridge in any way."""
    from tools import start_live_feed as bridge_module

    monkeypatch.delenv(td.ENV_API_KEY, raising=False)
    monkeypatch.setenv("QUOTEX_WS_URL", "wss://operator.invalid/ws")
    monkeypatch.setenv("QUOTEX_SSID", "operator-session")
    monkeypatch.setattr(bridge_module, "api_post", lambda *a, **k: (True, "stub"))

    args = bridge_module.build_parser().parse_args(["--assets", "USDJPY"])
    args.url = "wss://operator.invalid/ws"
    instance = bridge_module.LiveFeedBridge(args)
    assert instance._endpoint() == "wss://operator.invalid/ws"
    assert instance._session() == "operator-session"
    assert instance._headers() == {"Authorization": "operator-session"}
    assert instance.builder.provider == "operator_bridge"
    instance.handle_message(json.dumps({"type": "tick", "symbol": "USDJPY", "price": 149.1,
                                        "ts": iso(NOW)}))
    assert instance.counts["ticks"] == 1


def test_both_bridges_share_one_m1_builder_and_one_engine_class(bridge):
    from backend.data.feed import ContinuousFeed as SharedFeed
    from backend.main import get_engine
    from backend.signals.engine import SignalEngine

    instance = bridge("EUR/USD")
    assert instance.builder.__class__ is SharedFeed
    assert isinstance(get_engine(), SignalEngine)


def test_twelve_data_module_defines_no_second_m1_builder():
    """Structural guard: the vendor module must not reimplement OHLC aggregation."""
    source = (PROJECT_ROOT / "backend" / "data" / "providers" / "twelve_data.py").read_text(
        encoding="utf-8")
    for forbidden in ("def _finalise", "FormingCandle", "def tick_minute", "class SignalEngine",
                      "class ContinuousFeed", "validate_candle"):
        assert forbidden not in source, f"twelve_data.py must not define {forbidden!r}"
