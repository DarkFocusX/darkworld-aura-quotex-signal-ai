"""Telegram formatting and delivery status handling (no real network calls)."""
from __future__ import annotations

import pytest

from backend.telegram import notifier

# A production-eligible signal: real market data, READY_MARKET model, qualified.
SIGNAL = {
    "asset": "USDBDT-OTCq",
    "flag": "🇧🇩",
    "timeframe": "M1",
    "direction": "CALL",
    "entry_time_ist": "06:30:00",
    "confidence": 94.2,
    "model_agreement": 0.91,
    "expiry_minutes": 1,
    "quality_label": "VERY HIGH",
    "is_simulated": False,
    "is_replay": True,
    "is_real_market_data": True,
    "model_market_ready": True,
    "feed_badge": "REPLAY",
    "feed_mode": "REPLAY",
    "feed_source": "Operator CSV: USDBDT-OTCq.csv",
    "status": "OPEN",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"ok": True}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - mirror requests API
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


@pytest.fixture()
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    monkeypatch.setenv("ENABLE_TELEGRAM", "true")
    return True


def test_message_matches_the_required_format():
    """Spec §11: the exact production message layout."""
    message = notifier.format_signal_message(SIGNAL)
    assert "🚨 QUOTEX M1 PAPER SIGNAL" in message      # paper build (spec §14)
    assert "🇧🇩 USDBDT-OTCq" in message
    assert "💎 M1" in message
    assert "🕐 ENTRY:" in message
    assert "06:30:00 IST" in message
    assert "🔺 CALL" in message
    assert "🎯 MODEL CONFIDENCE:" in message
    assert "🤖 MODEL AGREEMENT:" in message            # spec §23 of the final master phase
    assert "94.2%" in message
    assert "91.0%" in message
    assert "⏳ EXPIRY:" in message
    assert "1 MIN" in message
    assert "📡 DATA:" in message
    # The badge is authoritative: this signal's feed is a replay of real candles, so the
    # message says REPLAY — it never upgrades a replay to a live "REAL" claim.
    assert "REPLAY" in message
    # never an accuracy claim
    for banned in ("guaranteed", "sure shot", "100% accuracy", "risk-free"):
        assert banned.lower() not in message.lower()


def test_live_real_feed_is_labelled_real():
    """A live real feed (badge REAL) is the only thing the message calls REAL."""
    message = notifier.format_signal_message({**SIGNAL, "feed_badge": "REAL", "is_replay": False})
    data_block = message.split("📡 DATA:")[1]
    assert data_block.strip().startswith("REAL")


def test_simulated_feed_is_labelled_simulated_and_warned():
    message = notifier.format_signal_message({**SIGNAL, "feed_badge": "SIMULATED",
                                              "is_simulated": True, "is_real_market_data": False})
    assert "SIMULATED" in message
    assert "NOT REAL MARKET DATA" in message


def test_put_uses_the_down_arrow():
    message = notifier.format_signal_message({**SIGNAL, "direction": "PUT"})
    assert "🔻 PUT" in message
    assert "🔺" not in message


def test_multi_minute_expiry_is_pluralised():
    message = notifier.format_signal_message({**SIGNAL, "expiry_minutes": 3})
    assert "3 MIN" in message


def test_simulated_signal_is_flagged_in_the_message():
    message = notifier.format_signal_message({**SIGNAL, "is_simulated": True, "is_real_market_data": False,
                                              "feed_badge": "SIMULATED"})
    assert "SIMULATED" in message
    assert "NOT REAL MARKET DATA" in message


def test_delivery_is_disabled_when_setting_is_off(telegram_env):
    session = FakeSession()
    result = notifier.send_signal(SIGNAL, enabled=False, session=session)
    assert result.status == notifier.STATUS_DISABLED
    assert session.calls == []


def test_missing_credentials_reports_not_configured(monkeypatch):
    monkeypatch.setenv("ENABLE_TELEGRAM", "true")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
    session = FakeSession()
    result = notifier.send_signal(SIGNAL, enabled=True, session=session)
    assert result.status == notifier.STATUS_NOT_CONFIGURED
    assert session.calls == []


def test_simulated_signals_are_blocked_by_default(telegram_env):
    session = FakeSession()
    result = notifier.send_signal({**SIGNAL, "is_simulated": True}, enabled=True, allow_simulated=False, session=session)
    assert result.status == notifier.STATUS_BLOCKED_NOT_MARKET  # production gate blocks first
    assert session.calls == []


def test_development_mode_requires_the_simulated_opt_in(telegram_env):
    simulated = {**SIGNAL, "is_simulated": True, "is_real_market_data": False, "feed_badge": "SIMULATED"}
    session = FakeSession()
    blocked = notifier.send_signal(simulated, enabled=True, allow_simulated=False, session=session,
                                   environment="development")
    assert blocked.status == notifier.STATUS_BLOCKED_SIMULATED
    assert session.calls == []

    allowed = notifier.send_signal(simulated, enabled=True, allow_simulated=True, session=session,
                                   environment="development")
    assert allowed.status == notifier.STATUS_SENT
    assert "DEVELOPMENT" in allowed.message


def test_production_blocks_demo_model_and_simulated_feed(telegram_env):
    session = FakeSession()
    demo = notifier.send_signal({**SIGNAL, "model_market_ready": False}, enabled=True, session=session)
    assert demo.status == notifier.STATUS_BLOCKED_NOT_MARKET
    simulated = notifier.send_signal({**SIGNAL, "is_real_market_data": False, "is_simulated": True},
                                     enabled=True, session=session)
    assert simulated.status == notifier.STATUS_BLOCKED_NOT_MARKET
    unqualified = notifier.send_signal(SIGNAL, enabled=True, session=session, qualified=False)
    assert unqualified.status == notifier.STATUS_BLOCKED_NOT_QUALIFIED
    assert session.calls == []


def test_successful_delivery_posts_expected_payload(telegram_env):
    session = FakeSession()
    result = notifier.send_signal(SIGNAL, enabled=True, session=session)
    assert result.status == notifier.STATUS_SENT
    assert len(session.calls) == 1
    call = session.calls[0]
    assert "/bot123456:TESTTOKEN/sendMessage" in call["url"]
    assert call["json"]["chat_id"] == "-100999"
    assert "🚨 QUOTEX M1 PAPER SIGNAL" in call["json"]["text"]


def test_http_error_is_reported_not_swallowed(telegram_env):
    session = FakeSession(response=FakeResponse(403, {"description": "Forbidden: bot was blocked"}))
    result = notifier.send_signal(SIGNAL, enabled=True, session=session)
    assert result.status == notifier.STATUS_FAILED
    assert "403" in result.detail


def test_network_error_is_reported(telegram_env):
    import requests

    session = FakeSession(error=requests.ConnectionError("no route to host"))
    result = notifier.send_signal(SIGNAL, enabled=True, session=session)
    assert result.status == notifier.STATUS_FAILED
    assert "ConnectionError" in result.detail


def test_test_message_requires_credentials(monkeypatch):
    monkeypatch.setenv("ENABLE_TELEGRAM", "true")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
    assert notifier.test_message(FakeSession()).status == notifier.STATUS_NOT_CONFIGURED


def test_live_mode_title_drops_the_paper_word(telegram_env):
    """Only an explicitly LIVE mode may omit the PAPER banner (spec §14)."""
    session = FakeSession()
    notifier.send_signal(SIGNAL, enabled=True, session=session, mode="LIVE")
    text = session.calls[0]["json"]["text"]
    assert text.startswith("🚨 QUOTEX M1 SIGNAL")
    assert "PAPER SIGNAL" not in text
