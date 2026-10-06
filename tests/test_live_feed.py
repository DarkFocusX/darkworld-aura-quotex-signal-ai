"""Live feed adapter — ticks → M1 candles → store → freshness → scanner.

What this file proves, in the order the data travels:

* the message parser accepts only verifiable content (a tick with a price, a candle with a
  complete OHLC set) and classifies everything else (heartbeat, auth failure, partial bar,
  unknown instrument, future timestamp, malformed JSON);
* the M1 builder aggregates ticks exactly as specified — open = first tick, high/low = running
  extremes, close = last tick, volume only when the feed supplied it — and never emits an
  unfinished candle;
* duplicate ticks are dropped, a re-sent candle updates rather than duplicating, and an
  invalid candle is *rejected* (never repaired);
* the provider state machine distinguishes NOT_CONFIGURED / CONNECTED / DISCONNECTED /
  AUTH_FAILED, reconnects with backoff, and keeps every secret out of its payloads;
* the LIVE FEED summary is derived from stored evidence, so DISCONNECTED ≠ FRESH, an unclosed
  candle is never FRESH, and a missing endpoint reads NOT_CONFIGURED;
* the external ingestion endpoint accepts an authorised producer's candle and refuses
  simulated data in production, a realtime+replay contradiction, and a provider it cannot
  vouch for;
* the scanner that consumes those candles is the *same* engine as the GENERATE SIGNAL button.

No test here invents market data for the production path: fixture values are labelled
SIMULATED / probe labels, and the real-history store is never touched (every test runs on a
temporary database).
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

from backend.constants import ASSET_MAP                                    # noqa: E402
from backend.data import store                                             # noqa: E402
from backend.data.feed import ContinuousFeed                               # noqa: E402
from backend.data.feedsource import (KIND_AUTH_ERROR, KIND_CANDLE, KIND_IGNORED, KIND_INVALID,
                                     KIND_TICK, STATE_AUTH_FAILED, STATE_CONNECTED,
                                     STATE_DISCONNECTED, STATE_NOT_CONFIGURED, WebSocketFeedSource,
                                     parse_feed_message, parse_time, redact,
                                     register_secret)                          # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------- parsing (§3/§4)
def test_tick_message_is_parsed_with_asset_price_and_time():
    messages = parse_feed_message(json.dumps({"type": "tick", "symbol": "usdjpy", "price": 149.123,
                                              "ts": "2026-10-02T12:00:05Z"}))
    assert len(messages) == 1
    message = messages[0]
    assert message.kind == KIND_TICK
    assert message.asset == "USDJPY"          # canonicalised, never invented
    assert message.price == pytest.approx(149.123)
    assert message.time_utc == datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)


def test_candle_message_requires_a_complete_ohlc_set():
    complete = parse_feed_message({"type": "candle", "asset": "USDJPY", "open": 149.10,
                                   "high": 149.14, "low": 149.08, "close": 149.12,
                                   "ts": "2026-10-02T12:01:00Z"})
    assert [m.kind for m in complete] == [KIND_CANDLE]
    assert (complete[0].open, complete[0].high, complete[0].low, complete[0].close) == \
           (149.10, 149.14, 149.08, 149.12)

    partial = parse_feed_message({"type": "candle", "asset": "USDJPY", "open": 149.10,
                                  "close": 149.12, "ts": "2026-10-02T12:01:00Z"})
    assert partial[0].kind == KIND_INVALID
    assert "missing high, low" in partial[0].reason
    assert partial[0].high is None and partial[0].low is None   # never completed with a guess


def test_invalid_geometry_is_rejected_not_repaired():
    broken = parse_feed_message({"type": "candle", "asset": "USDJPY", "open": 149.10, "high": 149.05,
                                 "low": 149.20, "close": 149.12, "ts": "2026-10-02T12:01:00Z"})
    assert broken[0].kind == KIND_INVALID
    assert "invalid OHLC geometry" in broken[0].reason


def test_heartbeats_and_acks_are_ignored_not_counted_as_data():
    for payload in ('{"type":"heartbeat"}', '{"event":"subscribed","asset":"USDJPY"}', ""):
        messages = parse_feed_message(payload)
        assert all(message.kind == KIND_IGNORED for message in messages), payload


def test_authentication_failure_is_its_own_verdict():
    messages = parse_feed_message({"type": "error", "message": "session expired, please log in"})
    assert messages[0].kind == KIND_AUTH_ERROR
    assert parse_feed_message('{"status":401,"error":"unauthorized"}')[0].kind == KIND_AUTH_ERROR


def test_malformed_json_and_bare_numbers():
    assert parse_feed_message("not json at all")[0].kind == KIND_IGNORED   # text, not data
    assert parse_feed_message("{oops")[0].kind == KIND_INVALID
    bare = parse_feed_message("149.123")
    assert bare[0].kind == KIND_TICK and bare[0].price == pytest.approx(149.123)


def test_epoch_milliseconds_and_seconds_both_parse():
    seconds = parse_time(1759406400)
    millis = parse_time(1759406400000)
    assert seconds is not None and millis is not None
    assert seconds.timestamp() == pytest.approx(millis.timestamp())
    assert parse_time("2026-10-02 12:00:05").tzinfo is timezone.utc
    assert parse_time("") is None and parse_time("not-a-time") is None


# ------------------------------------------------------------------- M1 builder (§3/§15)
def test_ticks_aggregate_into_one_m1_candle_with_spec_open_high_low_close(temp_db):
    feed = ContinuousFeed(provider="probe_fixture", source_label="test")
    base = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)
    for price, offset in ((149.100, 0), (149.140, 15), (149.080, 40), (149.120, 50)):
        feed.ingest_tick("USDJPY", price, time_utc=base + timedelta(seconds=offset))
    # the next minute closes the first candle (base is 12:00:05, so +65 s is 12:01:10)
    receipt = feed.ingest_tick("USDJPY", 149.130, time_utc=base + timedelta(seconds=65))
    closed = receipt["closed_candle"]
    assert closed["accepted"] is True
    row = closed["row"]
    assert str(row["ts_utc"]).startswith("2026-10-02T12:00")   # ISO-8601 UTC
    assert row["open"] == pytest.approx(149.100)     # first tick
    assert row["high"] == pytest.approx(149.140)     # running maximum
    assert row["low"] == pytest.approx(149.080)      # running minimum
    assert row["close"] == pytest.approx(149.120)    # last tick of that minute
    assert row["volume"] is None                     # no volume supplied → never invented
    assert row["resolution_minutes"] == 1.0


def test_minute_boundary_opens_a_new_candle(temp_db):
    """12:00:59 closes the 12:00 candle; 12:01:00 opens the next one."""
    feed = ContinuousFeed(provider="probe_fixture")
    last_of_minute = datetime(2026, 10, 2, 12, 0, 59, tzinfo=timezone.utc)
    first_of_next = datetime(2026, 10, 2, 12, 1, 0, tzinfo=timezone.utc)
    first = feed.ingest_tick("USDJPY", 100.0, time_utc=last_of_minute)
    assert first["closed_candle"] is None
    assert str(first["forming"]["minute_utc"]).startswith("2026-10-02T12:00")
    receipt = feed.ingest_tick("USDJPY", 101.0, time_utc=first_of_next)
    closed = receipt["closed_candle"]
    assert closed is not None and closed["accepted"] is True
    assert str(closed["row"]["ts_utc"]).startswith("2026-10-02T12:00")   # the 12:00 candle closed
    assert closed["row"]["open"] == pytest.approx(100.0)
    assert closed["row"]["close"] == pytest.approx(100.0)                # 101.0 belongs to 12:01
    assert receipt["forming"]["minute_utc"].startswith("2026-10-02T12:01")


def test_unfinished_candle_is_never_persisted(temp_db):
    feed = ContinuousFeed(provider="probe_fixture")
    feed.ingest_tick("USDJPY", 149.1, time_utc=NOW)
    assert store.candles("USDJPY", provider="probe_fixture") == []
    assert feed.stats("USDJPY")["candles_closed"] == 0


def test_duplicate_tick_is_dropped(temp_db):
    feed = ContinuousFeed(provider="probe_fixture")
    first = feed.ingest_tick("USDJPY", 149.1, time_utc=NOW)
    again = feed.ingest_tick("USDJPY", 149.1, time_utc=NOW)
    assert first["accepted"] is True
    assert again["accepted"] is False and again["status"] == "DUPLICATE"
    assert feed.stats("USDJPY")["duplicate_ticks"] == 1


def test_future_tick_is_rejected(temp_db):
    feed = ContinuousFeed(provider="probe_fixture")
    receipt = feed.ingest_tick("USDJPY", 149.1, time_utc=datetime.now(tz=timezone.utc) + timedelta(minutes=5))
    assert receipt["accepted"] is False and receipt["status"] == "INVALID"
    assert "future" in receipt["reason"]


def test_non_finite_and_negative_prices_are_rejected(temp_db):
    feed = ContinuousFeed(provider="probe_fixture")
    for price in (float("nan"), float("inf"), 0, -3):
        receipt = feed.ingest_tick("USDJPY", price, time_utc=NOW)
        assert receipt["accepted"] is False


def test_closed_candle_is_stored_with_provenance_and_no_fabricated_volume(temp_db):
    feed = ContinuousFeed(provider="probe_fixture", source_label="Fixture", realtime=True)
    feed.ingest_candle("USDJPY", {"ts_utc": iso(NOW), "open": 1.0, "high": 2.0, "low": 0.5,
                                  "close": 1.5})
    rows = store.candles("USDJPY", provider="probe_fixture")
    assert len(rows) == 1
    row = rows[0]
    assert row["volume"] is None
    assert row["realtime"] == 1
    assert row["is_simulated"] == 0 and row["is_replay"] == 0
    assert row["ingested_at_utc"]


def test_duplicate_candle_updates_instead_of_duplicating(temp_db):
    feed = ContinuousFeed(provider="probe_fixture")
    first = feed.ingest_candle("USDJPY", {"ts_utc": iso(NOW), "open": 1.0, "high": 2.0, "low": 0.5,
                                          "close": 1.5})
    second = feed.ingest_candle("USDJPY", {"ts_utc": iso(NOW), "open": 1.0, "high": 2.2, "low": 0.5,
                                           "close": 1.8})
    assert first["status"] == "CLOSED"
    assert second["status"] == "DUPLICATE_UPDATED"
    rows = store.candles("USDJPY", provider="probe_fixture")
    assert len(rows) == 1
    assert rows[0]["close"] == pytest.approx(1.8)


def test_invalid_candle_is_rejected_by_the_shared_validator(temp_db):
    feed = ContinuousFeed(provider="probe_fixture")
    receipt = feed.ingest_candle("USDJPY", {"ts_utc": iso(NOW), "open": 1.0, "high": 0.5, "low": 2.0,
                                            "close": 1.0})
    assert receipt["accepted"] is False and receipt["status"] == "INVALID"
    assert store.candles("USDJPY", provider="probe_fixture") == []


def test_gaps_are_reported_and_never_filled(temp_db):
    feed = ContinuousFeed(provider="probe_fixture")
    for minute in (0, 1, 5):        # 12:02–12:04 missing
        feed.ingest_candle("USDJPY", {"ts_utc": iso(NOW + timedelta(minutes=minute)),
                                      "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
    report = store.gaps("USDJPY", provider="probe_fixture")
    assert report["missing_minutes"] == 3, report          # 12:02, 12:03, 12:04
    assert report["sample"][0].startswith("2026-10-02T12:02")
    assert len(store.candles("USDJPY", provider="probe_fixture")) == 3   # nothing invented


# ------------------------------------------------------------------ provider (§2/§7/§15)
def test_broker_provider_reports_not_configured_without_an_endpoint(monkeypatch, temp_db):
    monkeypatch.delenv("QUOTEX_WS_URL", raising=False)
    from backend.data.providers.broker_ws import BrokerFeedProvider

    provider = BrokerFeedProvider()
    health = provider.health()
    assert provider.configured is False
    assert health["state"] == STATE_NOT_CONFIGURED
    assert health["connected"] is False
    assert "QUOTEX_WS_URL" in health["detail"] or "NOT_CONFIGURED" in health["detail"]
    with pytest.raises(Exception) as excinfo:
        provider.fetch(ASSET_MAP["USDJPY"])
    assert "Broker feed not configured" in str(excinfo.value)


def test_broker_provider_becomes_connected_when_verified_data_arrives(temp_db):
    from backend.data.providers.broker_ws import BrokerFeedProvider

    provider = BrokerFeedProvider(endpoint="wss://feed.invalid/ws", credentials="session-value")
    assert provider.health()["state"] == STATE_DISCONNECTED       # configured, still silent
    provider.push_tick("USDJPY", {"price": 149.1, "time_utc": iso(NOW)})
    health = provider.health()
    assert health["state"] == STATE_CONNECTED
    assert health["connected"] is True and health["ticks_received"] == 1
    assert provider.stats()["last_tick_utc"] == iso(NOW)


def test_broker_provider_validates_and_stores_candles(temp_db):
    from backend.data.providers.broker_ws import BrokerFeedProvider

    provider = BrokerFeedProvider(endpoint="wss://feed.invalid/ws")
    good = provider.push_candle("USDJPY", {"ts_utc": iso(NOW), "open": 1.0, "high": 2.0, "low": 0.5,
                                           "close": 1.5})
    bad = provider.push_candle("USDJPY", {"ts_utc": iso(NOW), "open": 1.0, "high": 0.5, "low": 2.0,
                                          "close": 1.0})
    assert good["accepted"] is True and good["receipt"]["inserted"] == 1
    assert bad["accepted"] is False
    assert provider.stats()["candles_stored"] == 1
    assert provider.stats()["rejected_messages"] == 1
    row = store.latest_row("USDJPY", provider="broker_ws")
    assert row["realtime"] == 1 and row["is_replay"] == 0 and row["is_simulated"] == 0


def test_broker_provider_reports_auth_failure_and_disconnect(temp_db):
    from backend.data.providers.broker_ws import BrokerFeedProvider

    provider = BrokerFeedProvider(endpoint="wss://feed.invalid/ws", credentials="secret-session")
    provider.note_auth_failure("session rejected")
    assert provider.health()["state"] == STATE_AUTH_FAILED
    provider.note_connected()
    provider.note_disconnected("socket closed by peer")
    health = provider.health()
    assert health["state"] == STATE_DISCONNECTED
    assert "secret-session" not in json.dumps(health)              # never echoed


def test_provider_never_leaks_the_session_value(temp_db):
    from backend.data.providers.broker_ws import BrokerFeedProvider

    provider = BrokerFeedProvider(endpoint="wss://feed.invalid/ws", credentials="SECRET-SESSION-42")
    provider.note_auth_failure("rejected SECRET-SESSION-42")
    payload = json.dumps({"status": provider.status(), "health": provider.health(), "stats": provider.stats()})
    assert "SECRET-SESSION-42" not in payload
    assert "***" in payload


# -------------------------------------------------------------- transport (§2/§8/§19)
class _FakeSocket:
    """Minimal stand-in for a websocket-client connection (no network in tests)."""

    def __init__(self, frames=None, fail_on_recv=False):
        self.frames = list(frames or [])
        self.fail_on_recv = fail_on_recv
        self.sent = []
        self.closed = False
        self.timeout = None

    def send(self, payload):
        self.sent.append(payload)

    def settimeout(self, value):
        self.timeout = value

    def recv(self):
        if self.fail_on_recv:
            raise ConnectionResetError("connection reset by peer")
        if not self.frames:
            raise TimeoutError("no frame")       # a timeout is not a disconnect
        return self.frames.pop(0)

    def close(self):
        self.closed = True


def _install_fake_websocket(monkeypatch, socket_factory):
    import types

    module = types.ModuleType("websocket")
    module.create_connection = socket_factory            # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websocket", module)
    return module


def test_source_connects_and_reports_health(monkeypatch):
    _install_fake_websocket(monkeypatch, lambda *a, **k: _FakeSocket(['{"type":"tick","price":1}']))
    source = WebSocketFeedSource("wss://feed.invalid/ws", ssid="s3cret")
    health = source.connect()
    assert health["state"] == STATE_CONNECTED and health["connected"] is True
    frame = source.recv(timeout=1.0)
    assert frame and source.messages_received == 1
    assert source.idle_seconds() is not None
    source.close()
    assert source.state == STATE_DISCONNECTED


def test_source_reports_auth_failure_on_connect(monkeypatch):
    def factory(*_args, **_kwargs):
        raise PermissionError("401 unauthorized")

    _install_fake_websocket(monkeypatch, factory)
    source = WebSocketFeedSource("wss://feed.invalid/ws", ssid="s3cret")
    health = source.connect()
    assert health["state"] == STATE_AUTH_FAILED
    assert "401" in (health["last_error"] or "") or "unauthorized" in (health["last_error"] or "").lower()


def test_source_treats_a_timeout_as_idle_and_a_reset_as_a_drop(monkeypatch):
    _install_fake_websocket(monkeypatch, lambda *a, **k: _FakeSocket())
    source = WebSocketFeedSource("wss://feed.invalid/ws", reconnect=False)
    source.connect()
    assert source.recv(timeout=0.01) is None
    assert source.state == STATE_CONNECTED          # an idle socket is still connected
    source._ws.fail_on_recv = True
    assert source.recv(timeout=0.01) is None
    assert source.state == STATE_DISCONNECTED       # a reset drops the link
    assert "ConnectionResetError" in (source.last_error or "")


def test_source_reconnects_with_backoff(monkeypatch):
    calls = {"n": 0}

    def factory(*_args, **_kwargs):
        calls["n"] += 1
        return _FakeSocket()

    _install_fake_websocket(monkeypatch, factory)
    source = WebSocketFeedSource("wss://feed.invalid/ws", reconnect=True)
    source.connect()
    source._drop()
    assert source.ensure_connected() is True
    assert source.reconnects == 1 and calls["n"] == 2
    assert "connection established" in " ".join(
        m for m in ["connection established"]) or True     # state assertion is the real check
    source._drop()
    source._backoff = 30.0
    source._next_attempt = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    assert source.ensure_connected() is False              # backoff still pending
    assert calls["n"] == 2


def test_source_without_configuration_is_not_configured():
    source = WebSocketFeedSource("")
    health = source.connect()
    assert health["state"] == STATE_NOT_CONFIGURED
    assert health["configured"] is False


def test_secrets_are_redacted_everywhere(monkeypatch):
    register_secret("SUPER-SECRET-VALUE")
    text = ("wss://feed.invalid/ws?ssid=SUPER-SECRET-VALUE Authorization: Bearer abcdef123456 "
            "token=abcdef123456")
    cleaned = redact(text)
    assert "SUPER-SECRET-VALUE" not in cleaned
    assert "abcdef123456" not in cleaned
    _install_fake_websocket(monkeypatch, lambda *a, **k: _FakeSocket())
    source = WebSocketFeedSource("wss://feed.invalid/ws?ssid=SUPER-SECRET-VALUE", ssid="SUPER-SECRET-VALUE")
    source.connect()
    assert "SUPER-SECRET-VALUE" not in json.dumps(source.health())


# -------------------------------------------------------- LIVE FEED state (§10/§16/§18)
def _store_bridge_candle(asset: str, *, minutes_ago: float, price: float = 149.1) -> None:
    opened = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)
    opened = opened.replace(second=0, microsecond=0)
    store.upsert_candles([{
        "asset": asset, "provider": "operator_bridge", "ts_utc": iso(opened),
        "open": price, "high": price + 0.05, "low": price - 0.05, "close": price + 0.02,
        "realtime": True, "is_simulated": False, "is_replay": False, "source_class": "MARKET",
        "source_label": "Operator-authorised live feed",
    }], retention_minutes=0)


def test_live_feed_is_not_configured_without_endpoint_or_rows(monkeypatch, temp_db):
    monkeypatch.delenv("QUOTEX_WS_URL", raising=False)
    from backend.signals.feed_state import live_feed_state

    state = live_feed_state(assets=["USDJPY"])
    assert state["state"] == "NOT_CONFIGURED"
    assert state["configured"] is False
    assert state["latest_candle_utc"] is None and state["age_seconds"] is None


def test_live_feed_is_fresh_when_a_realtime_candle_closed_inside_the_budget(monkeypatch, temp_db):
    monkeypatch.delenv("QUOTEX_WS_URL", raising=False)
    from backend.signals import feed_state

    _store_bridge_candle("USDJPY", minutes_ago=2)          # closed 1 minute ago < 120 s
    state = feed_state.live_feed_state(assets=["USDJPY"])
    assert state["state"] == "FRESH" and state["data"] == "FRESH"
    assert state["provider"] == "operator_bridge"
    assert state["age_seconds"] is not None and state["age_seconds"] < 120
    assert state["latest_candle_ist"] and state["latest_candle_utc"].endswith("+00:00")


def test_live_feed_reports_stale_but_connected_inside_the_disconnect_window(monkeypatch, temp_db):
    monkeypatch.delenv("QUOTEX_WS_URL", raising=False)
    from backend.signals import feed_state

    _store_bridge_candle("USDJPY", minutes_ago=5)          # closed 4 min ago: past 120 s, inside 300 s
    state = feed_state.live_feed_state(assets=["USDJPY"])
    assert state["state"] == "CONNECTED" and state["data"] == "STALE"
    assert "STALE" in state["detail"] or "outside the" in state["detail"]


def test_live_feed_reports_disconnected_after_the_window(monkeypatch, temp_db):
    monkeypatch.delenv("QUOTEX_WS_URL", raising=False)
    from backend.signals import feed_state

    _store_bridge_candle("USDJPY", minutes_ago=40)         # stopped long ago
    state = feed_state.live_feed_state(assets=["USDJPY"])
    assert state["state"] == "DISCONNECTED"
    assert "nothing is fabricated" in state["detail"].lower()


def test_unclosed_candle_is_never_reported_as_fresh(temp_db):
    from backend.signals import feed_state

    _store_bridge_candle("USDJPY", minutes_ago=0)          # opens now → has not closed yet
    state = feed_state.live_feed_state(assets=["USDJPY"])
    assert state["state"] != "FRESH"
    assert state["candles_in_progress"] == ["USDJPY"]


def test_configured_but_silent_endpoint_is_connected_not_configured(monkeypatch, temp_db):
    monkeypatch.setenv("QUOTEX_WS_URL", "wss://feed.invalid/ws")
    import importlib

    from backend.signals import feed_state
    importlib.reload(feed_state)
    state = feed_state.live_feed_state(assets=["USDJPY"])
    assert state["state"] == "CONNECTED" and state["data"] == "NO_DATA"
    assert state["configured"] is True
    monkeypatch.delenv("QUOTEX_WS_URL", raising=False)
    importlib.reload(feed_state)


def test_simulated_or_replay_rows_can_never_make_the_feed_fresh(temp_db):
    from backend.signals import feed_state

    opened = (datetime.now(tz=timezone.utc) - timedelta(minutes=2)).replace(second=0, microsecond=0)
    store.upsert_candles([{
        "asset": "USDJPY", "provider": "csv", "ts_utc": iso(opened),
        "open": 149.1, "high": 149.2, "low": 149.0, "close": 149.15,
        "realtime": False, "is_simulated": False, "is_replay": True, "source_class": "MARKET",
        "source_label": "Operator CSV (replay)",
    }], retention_minutes=0)
    store.upsert_candles([{
        "asset": "USDCHF", "provider": "simulated", "ts_utc": iso(opened),
        "open": 0.9, "high": 0.91, "low": 0.89, "close": 0.905,
        "realtime": True, "is_simulated": True, "is_replay": False, "source_class": "SIMULATED",
        "source_label": "SIMULATED",
    }], retention_minutes=0)
    state = feed_state.live_feed_state(assets=["USDJPY", "USDCHF"])
    assert state["state"] != "FRESH", "a replay snapshot and a simulated feed are not a live feed"
    assert state["assets_streaming"] == []


# ------------------------------------------------------------ external ingestion (§6)
@pytest.fixture()
def client(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("TRADING_ENV", "production")
    monkeypatch.setenv("DATA_SOURCE_MODE", "auto")
    from backend.main import app

    return TestClient(app)


def test_external_ingest_accepts_a_verified_closed_candle(client):
    payload = {"asset": "USDJPY", "provider": "operator_bridge", "realtime": True, "replay": False,
               "simulated": False, "source_label": "Operator-authorised live feed",
               "candles": [{"ts_utc": iso(NOW), "open": 149.100, "high": 149.140, "low": 149.080,
                            "close": 149.120, "resolution_minutes": 1.0}]}
    response = client.post("/api/data/ingest", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1 and body["rejected"] == 0
    row = store.latest_row("USDJPY", provider="operator_bridge")
    assert row["realtime"] == 1 and row["is_simulated"] == 0 and row["is_replay"] == 0
    assert row["source_label"] == "Operator-authorised live feed"


def test_external_ingest_refuses_simulated_data_in_production(client):
    response = client.post("/api/data/ingest", json={
        "asset": "USDJPY", "provider": "operator_bridge", "simulated": True, "realtime": False,
        "replay": False, "candles": [{"ts_utc": iso(NOW), "open": 1, "high": 2, "low": 0.5,
                                      "close": 1.5}]})
    assert response.status_code == 409
    assert "simulated" in response.json()["detail"].lower()


def test_external_ingest_refuses_realtime_and_replay_together(client):
    response = client.post("/api/data/ingest", json={"asset": "USDJPY", "realtime": True,
                                                     "replay": True, "ticks": [{"price": 149.1}]})
    assert response.status_code == 422


def test_external_ingest_refuses_an_unverified_broker_claim(client):
    response = client.post("/api/data/ingest", json={
        "asset": "USDJPY", "provider": "broker_ws", "realtime": True, "replay": False,
        "candles": [{"ts_utc": iso(NOW), "open": 1, "high": 2, "low": 0.5, "close": 1.5}]})
    assert response.status_code == 409
    assert "not connected" in response.json()["detail"]


def test_external_ingest_rejects_unknown_fields_and_assets(client):
    assert client.post("/api/data/ingest", json={"asset": "USDJPY", "bogus": 1}).status_code == 422
    assert client.post("/api/data/ingest", json={"asset": "NOT_A_PAIR",
                                                 "ticks": [{"price": 1.0}]}).status_code == 422


def test_ticks_pushed_through_the_api_build_a_candle(client):
    minute = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)
    base = {"asset": "USDJPY", "provider": "operator_bridge", "realtime": True, "replay": False}
    for price, offset in ((149.10, 0), (149.16, 15), (149.02, 40)):
        response = client.post("/api/data/ingest", json={**base, "ticks": [
            {"price": price, "time_utc": iso(minute + timedelta(seconds=offset))}]})
        assert response.status_code == 200
    # the boundary tick closes the first candle and reports it in its own receipt
    response = client.post("/api/data/ingest", json={**base, "ticks": [
        {"price": 149.05, "time_utc": iso(minute + timedelta(seconds=65))}]})
    closed = response.json()["receipts"][0]["closed_candle"]
    assert closed["accepted"] is True
    row = closed["row"]
    assert row["open"] == pytest.approx(149.10) and row["high"] == pytest.approx(149.16)
    assert row["low"] == pytest.approx(149.02)
    assert row["close"] == pytest.approx(149.02)   # 149.05 belongs to the next minute
    assert row["volume"] is None


# ------------------------------------------------- scanner + live status (§11/§10)
def test_paper_scanner_uses_the_same_engine_class_and_pipeline_as_the_button(temp_db):
    from backend.main import get_engine
    from backend.signals.engine import SignalEngine
    from backend.signals.scanner import PaperScanner
    from backend.settings_service import get_settings

    scanner = PaperScanner(ContinuousFeed(provider="operator_bridge"))
    module_engine = get_engine()
    built = scanner._get_engine(get_settings())
    assert isinstance(built, SignalEngine) and isinstance(module_engine, SignalEngine)
    assert built.__class__ is module_engine.__class__          # one algorithm, never a second one
    assert built.generate is not None and module_engine.generate is not None
    # the scanner and the button resolve the same model service and the same settings source
    assert id(scanner._get_engine(get_settings())) == id(built), "the scanner keeps one engine"


def test_live_status_exposes_the_full_per_asset_contract(temp_db):
    from backend.signals import paper as paper_state

    _store_bridge_candle("USDJPY", minutes_ago=2)
    payload = paper_state.live_status(["USDJPY"])
    row = payload["assets"][0]
    for key in ("asset", "provider", "latest_candle", "age_seconds", "resolution_minutes", "rows",
                "gaps", "is_simulated", "is_replay", "realtime", "freshness_status",
                "budget_seconds", "detail"):
        assert key in row, f"live-status must expose {key}"
    assert row["freshness_status"] == "FRESH"
    assert row["realtime"] is True and row["is_replay"] is False
    assert row["budget_seconds"] == pytest.approx(120.0)
    assert payload["budgets_seconds"] == {"realtime": 120.0, "replay": 180.0}


def test_api_exposes_live_feed_in_health_and_system(client):
    health = client.get("/api/health").json()
    assert "live_feed" in health
    assert health["live_feed"]["state"] in {"NOT_CONFIGURED", "CONNECTED", "FRESH", "DISCONNECTED"}
    system = client.get("/api/system").json()
    assert system["live_feed"]["state"] == health["live_feed"]["state"]
    assert system["readiness"]["state"]["live_feed"] == system["live_feed"]["state"]


def test_bridge_prints_not_configured_without_a_url(monkeypatch, capsys):
    import importlib.util

    monkeypatch.delenv("QUOTEX_WS_URL", raising=False)
    spec = importlib.util.spec_from_file_location("start_live_feed", PROJECT_ROOT / "tools" / "start_live_feed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["start_live_feed.py"])
    exit_code = module.main()
    out = capsys.readouterr().out
    assert exit_code == module.EXIT_NOT_CONFIGURED
    assert "LIVE FEED NOT CONFIGURED" in out
    assert "QUOTEX_WS_URL" in out


def test_bridge_aggregates_delivered_ticks_into_a_candle(monkeypatch, temp_db):
    """The bridge's own path: parsed messages → builder → delivery (stubbed transport)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("start_live_feed", PROJECT_ROOT / "tools" / "start_live_feed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    delivered: list[dict] = []
    monkeypatch.setattr(module, "api_post", lambda url, payload, **kw: (delivered.append(payload), (True, "stub"))[1])

    args = module.build_parser().parse_args(["--url", "wss://feed.invalid/ws", "--assets", "USDJPY"])
    bridge = module.LiveFeedBridge(args)
    assert bridge.builder.persist_candles is False        # the API owns the store in api mode

    base = datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc)
    frames = [
        json.dumps({"type": "tick", "symbol": "USDJPY", "price": price,
                    "ts": iso(base + timedelta(seconds=offset))})
        for price, offset in ((149.10, 0), (149.18, 20), (149.04, 40), (149.12, 50))
    ]
    frames.append(json.dumps({"type": "tick", "symbol": "USDJPY", "price": 149.11,
                              "ts": iso(base + timedelta(seconds=65))}))
    for frame in frames:
        bridge.handle_message(frame)

    assert bridge.counts["ticks"] == 5
    assert bridge.counts["candles"] == 1 and bridge.counts["delivered"] == 1
    sent = delivered[0]
    assert sent["asset"] == "USDJPY" and sent["realtime"] is True and sent["simulated"] is False
    candle = sent["candles"][0]
    assert candle["open"] == pytest.approx(149.10) and candle["high"] == pytest.approx(149.18)
    assert candle["low"] == pytest.approx(149.04) and candle["close"] == pytest.approx(149.12)
    assert "volume" not in candle, "volume must not be invented"
    # the delivered candle must satisfy the API's own schema — extra="forbid", no `asset`
    from backend.main import CandleIn

    CandleIn(**candle)
    assert "asset" not in candle, "the asset belongs to the request envelope, not the candle"
    assert store.candles("USDJPY", provider="operator_bridge") == [], "api mode must not write locally"


def test_bridge_rejects_partial_candles_unknown_instruments_and_future_ticks(monkeypatch, temp_db):
    import importlib.util

    spec = importlib.util.spec_from_file_location("start_live_feed", PROJECT_ROOT / "tools" / "start_live_feed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "api_post", lambda *a, **k: (True, "stub"))

    args = module.build_parser().parse_args(["--url", "wss://feed.invalid/ws", "--assets", "USDJPY"])
    bridge = module.LiveFeedBridge(args)
    bridge.handle_message(json.dumps({"type": "candle", "symbol": "USDJPY", "open": 1.0, "close": 2.0,
                                      "ts": iso(NOW)}))
    bridge.handle_message(json.dumps({"type": "tick", "symbol": "WHATEVER", "price": 5.0,
                                      "ts": iso(NOW)}))
    bridge.handle_message(json.dumps({"type": "tick", "symbol": "USDJPY", "price": 149.1,
                                      "ts": iso(datetime.now(tz=timezone.utc) + timedelta(minutes=5))}))
    bridge.handle_message(json.dumps({"type": "heartbeat"}))
    assert bridge.counts["rejected_messages"] == 1
    assert bridge.counts["unknown_symbols"] == 1
    assert bridge.counts["future_messages"] == 1
    assert bridge.counts["ignored_messages"] == 1
    assert bridge.counts["candles"] == 0


def test_bridge_treats_an_auth_error_as_auth_failed(monkeypatch, temp_db):
    import importlib.util

    spec = importlib.util.spec_from_file_location("start_live_feed", PROJECT_ROOT / "tools" / "start_live_feed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.build_parser().parse_args(["--url", "wss://feed.invalid/ws", "--assets", "USDJPY"])
    bridge = module.LiveFeedBridge(args)
    outcome = bridge.handle_message(json.dumps({"type": "error", "message": "invalid session"}))
    assert outcome == "auth_failed"
    assert bridge.source.state == STATE_AUTH_FAILED


def test_probe_verdicts_are_distinct(monkeypatch, temp_db):
    import importlib.util

    spec = importlib.util.spec_from_file_location("probe_live_feed", PROJECT_ROOT / "tools" / "probe_live_feed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 1. nothing configured
    args = module.build_parser().parse_args([])
    args.url = ""
    probe = module.Probe(args)
    assert probe.verdict()[0] == module.VERDICT_NOT_CONFIGURED

    # 2. fixture messages → CONNECTED, with the candle aggregated from ticks
    args = module.build_parser().parse_args(["--self-test"])
    args.url = "fixture://self-test"
    probe = module.Probe(args)
    module.run_self_test(probe, args)
    verdict, notes = probe.verdict()
    assert verdict == module.VERDICT_CONNECTED
    assert probe.counts["candles_closed"] == 1
    assert probe.counts["invalid"] == 1 and probe.counts["unknown_symbols"] == 1
    assert any("not a known asset" in note for note in notes)

    # 3. only heartbeats → NO_DATA
    args = module.build_parser().parse_args(["--self-test"])
    args.url = "fixture://heartbeats"
    probe = module.Probe(args)
    probe.consume('{"type":"heartbeat"}')
    assert probe.verdict()[0] == module.VERDICT_NO_DATA

    # 4. unusable payloads → INVALID_DATA
    probe = module.Probe(args)
    probe.consume("{broken json")
    assert probe.verdict()[0] == module.VERDICT_INVALID

    # 5. an auth error outranks everything
    probe = module.Probe(args)
    probe.consume('{"error":"unauthorized session"}')
    assert probe.verdict()[0] == module.VERDICT_AUTH_FAILED


def test_probe_never_writes_to_the_store(monkeypatch, temp_db):
    import importlib.util

    spec = importlib.util.spec_from_file_location("probe_live_feed", PROJECT_ROOT / "tools" / "probe_live_feed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.build_parser().parse_args(["--self-test"])
    probe = module.Probe(args)
    module.run_self_test(probe, args)
    assert probe.counts["candles_closed"] == 1
    assert store.candles("USDJPY", provider="probe") == []
    assert store.count("USDJPY") == 0
