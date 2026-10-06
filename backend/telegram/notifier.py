"""Telegram delivery (Bot API, direct HTTP — no extra dependency).

Secrets come from environment variables only:
    ENABLE_TELEGRAM   true/false
    TELEGRAM_BOT_TOKEN (or BOT_TOKEN)
    TELEGRAM_CHAT_ID   (or CHAT_ID)

Delivery policy (spec §11)
-------------------------
Telegram is allowed ONLY for a signal that is simultaneously:

    * READY_MARKET  — the model was trained on operator-supplied real market data
    * REAL DATA     — the feed is real market data (not simulated, not a fixture)
    * QUALIFIED     — the production eligibility policy returned ALLOW_SIGNAL

Never sent: NO TRADE, DATA UNAVAILABLE, READY_DEMO, SIMULATED — unless the operator
explicitly enables DEVELOPMENT mode for the deployment (TRADING_ENV=development /
ENABLE_TELEGRAM_DEVELOPMENT=true), in which case the message is prefixed with a
DEVELOPMENT warning so it cannot be mistaken for a production alert.

Delivery failures are reported, never swallowed, and never retried into a fake
"SENT" state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from ..config import telegram_credentials

STATUS_SENT = "SENT"
STATUS_DISABLED = "DISABLED"
STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"
STATUS_BLOCKED_SIMULATED = "BLOCKED_SIMULATED"
STATUS_BLOCKED_NOT_MARKET = "BLOCKED_NOT_MARKET"
STATUS_BLOCKED_NOT_QUALIFIED = "BLOCKED_NOT_QUALIFIED"
STATUS_FAILED = "FAILED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass
class TelegramResult:
    status: str
    detail: str = ""
    message: str | None = None
    http_status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "http_status": self.http_status,
                "message_preview": (self.message or "")[:400]}


def _arrow(direction: str) -> str:
    return "🔺" if str(direction).upper() == "CALL" else "🔻"


def data_badge(signal: dict[str, Any]) -> str:
    """REAL | SIMULATED | REPLAY | DATA UNAVAILABLE — as shown on the dashboard."""
    if signal.get("feed_badge"):
        return str(signal["feed_badge"])
    if signal.get("is_simulated"):
        return "SIMULATED"
    if signal.get("is_replay"):
        return "REPLAY"
    return "REAL"


def format_signal_message(signal: dict[str, Any], *, mode: str = "PAPER") -> str:
    """Telegram layout (spec §11 + §14 of the research phase).

    Sections: title · asset · timeframe · entry time (IST) · direction · model
    confidence · expiry · data source (badge + provider) · model version.
    """
    flag = signal.get("flag", "🔔")
    direction = str(signal.get("direction", "")).upper()
    is_real = bool(signal.get("is_real_market_data")) and not signal.get("is_simulated")
    paper = str(mode).upper() != "LIVE"
    lines = [
        "🚨 QUOTEX M1 PAPER SIGNAL" if paper else "🚨 QUOTEX M1 SIGNAL",
        "",
        f"{flag} {signal.get('asset')}",
        "💎 M1",
        "",
        "🕐 ENTRY:",
        f"{signal.get('entry_time_ist')} IST",
        "",
        f"{_arrow(direction)} {direction}",
        "",
        "🎯 MODEL CONFIDENCE:",
        f"{float(signal.get('confidence', 0)):.1f}%",
    ]
    agreement = signal.get("model_agreement")
    if agreement is not None:
        lines += ["", "🤖 MODEL AGREEMENT:", f"{float(agreement) * 100:.1f}%"]
    lines += [
        "",
        "⏳ EXPIRY:",
        f"{signal.get('expiry_minutes', 1)} MIN",
        "",
        "📡 DATA:",
        # The badge is authoritative: a live real feed reads REAL, a replay of real
        # candles reads REPLAY (still real market data, but not live), and simulated /
        # demo material is never allowed to look like either.
        data_badge(signal)
        + (f" · {signal.get('feed_source')}" if signal.get("feed_source") else ""),
    ]
    if signal.get("model_version") or signal.get("model_market_ready") is not None:
        lines += ["", "🧠 MODEL:", str(signal.get("model_version") or "unknown")]
    if not is_real:
        lines += ["", "⚠️ NOT REAL MARKET DATA — demonstration/development only."]
    lines += [
        "",
        "Model confidence is a calculated score, not a guarantee of profit.",
    ]
    return "\n".join(lines)


def send_signal(signal: dict[str, Any], *, enabled: bool, allow_simulated: bool = False,
                session: requests.Session | None = None, market_ready: bool | None = None,
                environment: str = "production", mode: str = "PAPER",
                qualified: bool | None = None) -> TelegramResult:
    creds = telegram_credentials()
    message = format_signal_message(signal, mode=mode)

    if not enabled or not creds["enabled"]:
        return TelegramResult(STATUS_DISABLED, "Telegram notifications are disabled", message)
    if not creds["configured"]:
        return TelegramResult(
            STATUS_NOT_CONFIGURED,
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set in the environment",
            message,
        )

    development = str(environment).lower() == "development"
    is_simulated = bool(signal.get("is_simulated"))
    is_real = bool(signal.get("is_real_market_data")) and not is_simulated
    market_ready = bool(signal.get("model_market_ready")) if market_ready is None else market_ready
    qualified = (signal.get("status") == "OPEN") if qualified is None else qualified

    # In development the alert may be delivered for simulated/demo material, but it
    # must be impossible to mistake for a production alert: banner it explicitly.
    if development and not (is_real and market_ready and qualified):
        message = (
            "⚠️ DEVELOPMENT MODE — this alert would be blocked in production "
            "(no real market data, READY_MARKET model or qualified signal).\n\n" + message
        )

    # --- production rules (spec §11) ----------------------------------------
    if not development:
        if not is_real:
            return TelegramResult(
                STATUS_BLOCKED_NOT_MARKET,
                "Blocked: the signal did not come from real market data. "
                "Set TRADING_ENV=development to send demonstration alerts.",
                message,
            )
        if not market_ready:
            return TelegramResult(
                STATUS_BLOCKED_NOT_MARKET,
                "Blocked: the model is not READY_MARKET. "
                "Set TRADING_ENV=development to send demonstration alerts.",
                message,
            )
        if not qualified:
            return TelegramResult(
                STATUS_BLOCKED_NOT_QUALIFIED,
                "Blocked: the signal did not pass the production eligibility policy.",
                message,
            )
    elif is_simulated and not allow_simulated:
        return TelegramResult(
            STATUS_BLOCKED_SIMULATED,
            "Development mode, but simulated alerts are disabled "
            "(set TELEGRAM_ALLOW_SIMULATED=true to override)",
            message,
        )

    url = f"{creds['api_base']}/bot{creds['token']}/sendMessage"
    payload = {
        "chat_id": creds["chat_id"],
        "text": message,
        "disable_web_page_preview": True,
    }
    http = session or requests
    try:
        response = http.post(url, json=payload, timeout=creds["timeout"])
    except requests.RequestException as exc:
        # Security review (§14): requests exception strings embed the request URL,
        # which contains the bot token. Redact before it reaches a log or the API.
        return TelegramResult(
            STATUS_FAILED,
            f"Delivery failed: {type(exc).__name__}: {_redact(str(exc)[:240], creds['token'])}",
            message,
        )

    if response.status_code != 200:
        detail = ""
        try:
            detail = _redact(str(response.json().get("description", ""))[:160], creds["token"])
        except Exception:  # noqa: BLE001
            detail = _redact(response.text[:160], creds["token"])
        return TelegramResult(STATUS_FAILED, f"Telegram API returned HTTP {response.status_code}: {detail}",
                              message, response.status_code)
    return TelegramResult(STATUS_SENT, "Delivered to Telegram", message, response.status_code)


def _redact(text: str, token: str | None) -> str:
    """Remove the bot token from any string that might be logged or returned."""
    if token and token in text:
        return text.replace(token, "<bot-token-redacted>")
    return text


def test_message(session: requests.Session | None = None) -> TelegramResult:
    """Send a harmless connectivity test (used by the settings panel)."""
    creds = telegram_credentials()
    if not creds["configured"]:
        return TelegramResult(STATUS_NOT_CONFIGURED,
                              "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set in the environment")
    url = f"{creds['api_base']}/bot{creds['token']}/sendMessage"
    http = session or requests
    text = "✅ Quotex Signal AI connectivity test — no trade signal."
    try:
        response = http.post(url, json={"chat_id": creds["chat_id"], "text": text},
                             timeout=creds["timeout"])
    except requests.RequestException as exc:
        return TelegramResult(STATUS_FAILED, f"{type(exc).__name__}: {_redact(str(exc)[:240], creds['token'])}")
    if response.status_code != 200:
        return TelegramResult(STATUS_FAILED, f"HTTP {response.status_code}", http_status=response.status_code)
    return TelegramResult(STATUS_SENT, "Test message delivered", text, response.status_code)
