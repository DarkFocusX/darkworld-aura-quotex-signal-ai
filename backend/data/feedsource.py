"""Transport + message parsing for an operator-authorised live feed.

This module is the *only* new networking code in the project. It does not know any
broker's protocol by heart and it never guesses one: the operator supplies the
endpoint, the optional authentication value, and (if their feed needs one) the
subscribe message template. Everything here is generic plumbing:

    WebSocketFeedSource   connect / send / receive / reconnect / redact / health
    parse_feed_message()  a tolerant parser for the two message shapes a feed can send

The parser accepts only what it can verify:

* a **tick** message carries one price and (optionally) a timestamp;
* a **candle** message carries a full OHLC set — `open`, `high`, `low` **and**
  `close`. A message with a partial bar is *rejected*, never completed with an
  invented value;
* anything else (heartbeats, subscription acks, …) is counted as `ignored`.

AUTHENTICATION_FAILED is reported as its own verdict: a rejected session must
never look like "no data".

Secrets: `redact()` strips every configured secret (and any `token=`/`ssid=`/
`authorization:` fragment) from anything this module logs or raises.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

# ----------------------------------------------------------------- states (§9/§18)
STATE_NOT_CONFIGURED = "NOT_CONFIGURED"
STATE_CONNECTING = "CONNECTING"
STATE_CONNECTED = "CONNECTED"
STATE_AUTH_FAILED = "AUTH_FAILED"
STATE_DISCONNECTED = "DISCONNECTED"
STATE_ERROR = "ERROR"

# ------------------------------------------------------------------ message kinds
KIND_TICK = "tick"
KIND_CANDLE = "candle"
KIND_IGNORED = "ignored"
KIND_AUTH_ERROR = "auth_error"
KIND_INVALID = "invalid"

ASSET_KEYS = ("asset", "symbol", "pair", "instrument", "ticker", "market", "name")
PRICE_KEYS = ("price", "tick", "p", "quote", "last", "value", "rate")
TIME_KEYS = ("ts_utc", "time_utc", "timestamp", "time", "ts", "at", "datetime", "date", "t")
TYPE_KEYS = ("type", "event", "kind", "action", "method", "channel", "msg_type", "msgtype")
OPEN_KEYS = ("open", "o", "op")
HIGH_KEYS = ("high", "h", "hi")
LOW_KEYS = ("low", "l", "lo")
CLOSE_KEYS = ("close", "c", "cl")
VOLUME_KEYS = ("volume", "vol", "v", "size", "qty")
MESSAGE_KEYS = ("message", "error", "reason", "detail", "description", "status_text", "text")

_IGNORED_TYPES = {"heartbeat", "ping", "pong", "subscribe", "subscribed", "subscription",
                  "ack", "connected", "welcome", "info", "session", "authorized", "authenticated"}
_AUTH_HINTS = ("auth", "unauthor", "invalid session", "session expired", "login failed",
               "access denied", "forbidden", "not authorized", "401", "403")
_HEARTBEAT_SECONDS = 20.0


# ------------------------------------------------------------------------ redaction
def redact(text: Any, secrets: Iterable[str] | None = None) -> str:
    """Return `text` with every known secret and credential-looking fragment removed."""
    out = str(text)
    for secret in list(secrets or []) + _SECRETS:
        if secret and len(str(secret)) >= 4:
            out = out.replace(str(secret), "***")
    out = re.sub(r"(?i)\b(ssid|token|session|authorization|api[_-]?key|password)=([^&\s\"']+)",
                 r"\1=***", out)
    out = re.sub(
        r"(?i)\b(bearer|authorization|token|api[_-]?key|session[_-]?id|ssid)\s*:?\s*(?:bearer\s+)?"
        r"[A-Za-z0-9._~+/=-]{6,}",
        lambda match: f"{match.group(1)} ***", out)
    out = re.sub(r"(?i)(wss?://[^\s?]+)\?[^\s]*", r"\1?***", out)
    return out


_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """Teach the redactor a value that must never appear in a log line or an error."""
    if value and str(value).strip() and str(value) not in _SECRETS:
        _SECRETS.append(str(value).strip())


def _safe_url(url: str) -> str:
    """A connectable URL with any query string (which often carries the session) masked."""
    return re.sub(r"\?.*$", "?***", str(url))


# ------------------------------------------------------------------------ messages
@dataclass
class FeedMessage:
    """One parsed feed message. `kind` says what the parser could verify."""

    kind: str
    asset: str | None = None
    time_utc: datetime | None = None
    price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    raw_kind: str = ""
    reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.kind in {KIND_TICK, KIND_CANDLE}

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "asset": self.asset,
            "time_utc": self.time_utc.isoformat() if self.time_utc else None,
            "price": self.price,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "volume": self.volume,
            "raw_kind": self.raw_kind,
            "reason": self.reason,
        }


def parse_time(value: Any) -> datetime | None:
    """Parse an ISO-8601 string or an epoch number (seconds or milliseconds) to UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)) or float(value) <= 0:
            return None
        seconds = float(value)
        if seconds > 1e11:            # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,13}", text):        # epoch as a string
        return parse_time(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                    "%Y-%m-%d %H:%M", "%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _first(raw: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for key in keys:
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text_blob(raw: dict[str, Any] | str) -> str:
    if isinstance(raw, str):
        return raw
    parts = [str(raw.get(key)) for key in MESSAGE_KEYS if raw.get(key) is not None]
    parts.append(str(raw.get("type") or raw.get("event") or raw.get("kind") or ""))
    return " ".join(parts).lower()


def parse_feed_message(payload: Any) -> list[FeedMessage]:
    """Parse one raw feed payload into zero or more messages.

    A JSON array is parsed element by element. Strings are tried as JSON first
    (many feeds send JSON inside a text frame) and otherwise read as a bare
    number (a price-only feed).
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return [FeedMessage(kind=KIND_IGNORED, raw_kind="empty")]
        if text[0] in "[{":
            try:
                return parse_feed_message(json.loads(text))
            except json.JSONDecodeError:
                return [FeedMessage(kind=KIND_INVALID, reason="malformed JSON", raw_kind="text")]
        number = _as_float(text)
        if number is not None:
            return [FeedMessage(kind=KIND_TICK, price=number, raw_kind="bare-number")]
        blob = text.lower()
        if any(hint in blob for hint in _AUTH_HINTS):
            return [FeedMessage(kind=KIND_AUTH_ERROR, raw_kind="text", reason=text[:120])]
        return [FeedMessage(kind=KIND_IGNORED, raw_kind="text")]
    if isinstance(payload, list):
        out: list[FeedMessage] = []
        for item in payload:
            out.extend(parse_feed_message(item))
        return out
    if not isinstance(payload, dict):
        return [FeedMessage(kind=KIND_INVALID, reason=f"unsupported message type {type(payload).__name__}")]

    raw: dict[str, Any] = payload
    declared = str(_first(raw, TYPE_KEYS) or "").lower()
    asset = _first(raw, ASSET_KEYS)
    asset = str(asset).strip().upper() if asset not in (None, "") else None
    when = parse_time(_first(raw, TIME_KEYS))
    volume = _as_float(_first(raw, VOLUME_KEYS))

    blob = _text_blob(raw)
    if any(hint in blob for hint in _AUTH_HINTS):
        return [FeedMessage(kind=KIND_AUTH_ERROR, raw_kind=declared or "object",
                            asset=asset, reason=_text_blob(raw)[:160])]

    # a nested payload ({"data": {...}} / {"tick": {...}}) is unwrapped once
    for nested_key in ("data", "payload", "tick", "candle", "bar", "quote", "result"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            merged = {**nested}
            merged.setdefault("asset", asset)
            if when is not None:
                merged.setdefault("time", when.isoformat())
            return parse_feed_message(merged)

    close = _as_float(_first(raw, CLOSE_KEYS))
    open_ = _as_float(_first(raw, OPEN_KEYS))
    high = _as_float(_first(raw, HIGH_KEYS))
    low = _as_float(_first(raw, LOW_KEYS))

    if any(value is not None for value in (open_, high, low, close)):
        missing = [name for name, value in (("open", open_), ("high", high), ("low", low),
                                            ("close", close)) if value is None]
        if missing:
            return [FeedMessage(kind=KIND_INVALID, asset=asset, time_utc=when, raw_kind="candle",
                                reason=f"candle is missing {', '.join(missing)} — never completed "
                                       f"with an invented value")]
        if high < max(open_, close) or low > min(open_, close) or high < low:
            return [FeedMessage(kind=KIND_INVALID, asset=asset, time_utc=when, raw_kind="candle",
                                reason=(f"invalid OHLC geometry (o={open_} h={high} l={low} "
                                        f"c={close})"))]
        return [FeedMessage(kind=KIND_CANDLE, asset=asset, time_utc=when, open=open_, high=high,
                            low=low, close=close, volume=volume,
                            raw_kind=declared or "candle", raw=raw)]

    price = _as_float(_first(raw, PRICE_KEYS))
    if price is not None:
        return [FeedMessage(kind=KIND_TICK, asset=asset, time_utc=when, price=price, volume=volume,
                            raw_kind=declared or "tick", raw=raw)]

    if declared in _IGNORED_TYPES or not declared:
        return [FeedMessage(kind=KIND_IGNORED, asset=asset, raw_kind=declared or "unknown")]
    return [FeedMessage(kind=KIND_INVALID, asset=asset, raw_kind=declared,
                        reason=f"message type {declared!r} carries neither a price nor an OHLC set")]


class FeedAuthError(RuntimeError):
    """The feed refused the session — reported as AUTH_FAILED, never as 'no data'."""


class FeedTransportError(RuntimeError):
    """The socket could not be established or was lost."""


class WebSocketFeedSource:
    """Minimal, dependency-light WebSocket client for an authorised operator feed.

    The connection is created with `websocket-client` (synchronous). Authentication is
    whatever the operator's feed requires and is expressed through configuration:

        LIVE_FEED_AUTH_HEADER   header name (default: Authorization)
        LIVE_FEED_AUTH_SCHEME   value prefix, e.g. "Bearer"
        LIVE_FEED_AUTH_IN_MESSAGE  include the SSID as {ssid} in the subscribe template

    No URL, no session id and no header value is ever logged verbatim.
    """

    def __init__(self, url: str, *, ssid: str | None = None, headers: dict[str, str] | None = None,
                 timeout: float = 10.0, reconnect: bool = True, max_backoff: float = 30.0,
                 on_log: Callable[[str, str], None] | None = None) -> None:
        self.url = (url or "").strip()
        self.ssid = (ssid or "").strip()
        self.timeout = float(timeout)
        self.reconnect = bool(reconnect)
        self.max_backoff = float(max_backoff)
        self.state = STATE_NOT_CONFIGURED if not self.url else STATE_DISCONNECTED
        self.last_error: str | None = None
        self.last_message_utc: datetime | None = None
        self.messages_received = 0
        self.ignored_messages = 0
        self.reconnects = 0
        self.connected_at_utc: datetime | None = None
        self._ws: Any = None
        self._backoff = 1.0
        self._next_attempt: datetime | None = None
        self._headers = dict(headers or {})
        self._on_log = on_log
        register_secret(self.ssid)
        for value in self._headers.values():
            register_secret(value)

    # ------------------------------------------------------------------ lifecycle
    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _log(self, level: str, message: str) -> None:
        if self._on_log:
            self._on_log(level, redact(message))

    def connect(self) -> dict[str, Any]:
        """Open the socket (idempotent). Returns this source's health payload."""
        if not self.configured:
            self.state = STATE_NOT_CONFIGURED
            self.last_error = "QUOTEX_WS_URL is not set"
            return self.health()
        if self._ws is not None and self.state == STATE_CONNECTED:
            return self.health()
        try:
            import websocket  # type: ignore
        except ImportError as exc:      # pragma: no cover - dependency guard
            self.state = STATE_ERROR
            self.last_error = ("the 'websocket-client' package is required for the live feed "
                               "(pip install websocket-client)")
            raise FeedTransportError(self.last_error) from exc

        self.state = STATE_CONNECTING
        header = [f"{key}: {value}" for key, value in self._headers.items()]
        try:
            self._ws = websocket.create_connection(self.url, timeout=self.timeout, header=header)
        except Exception as exc:  # noqa: BLE001 - the operator must see the real reason
            self.state = STATE_ERROR
            text = redact(f"{type(exc).__name__}: {exc}")
            self.last_error = text
            if any(hint in text.lower() for hint in _AUTH_HINTS):
                self.state = STATE_AUTH_FAILED
            self._log("ERROR", f"connection failed: {text}")
            return self.health()
        self.state = STATE_CONNECTED
        self.last_error = None
        self.connected_at_utc = datetime.now(tz=timezone.utc)
        self._backoff = 1.0
        self._log("INFO", f"connection established ({_safe_url(self.url)})")
        return self.health()

    def send_json(self, payload: dict[str, Any]) -> bool:
        if self._ws is None:
            return False
        try:
            self._ws.send(json.dumps(payload))
            return True
        except Exception as exc:  # noqa: BLE001
            self._log("WARNING", f"send failed: {redact(f'{type(exc).__name__}: {exc}')}")
            self._drop()
            return False

    def recv(self, timeout: float | None = None) -> Any | None:
        """Receive one frame. `None` means 'nothing arrived before the timeout'."""
        if self._ws is None:
            return None
        try:
            if timeout is not None:
                self._ws.settimeout(float(timeout))
            frame = self._ws.recv()
        except Exception as exc:  # noqa: BLE001 - timeouts are normal, everything else drops the link
            name = type(exc).__name__
            if "Timeout" in name:
                return None
            text = redact(f"{name}: {exc}")
            self.last_error = text
            self._log("WARNING", f"connection lost: {text}")
            self._drop()
            return None
        if frame is None or frame == "":
            return None
        self.note_message()
        return frame

    def note_message(self, when: datetime | None = None) -> None:
        """Record that a frame arrived (heartbeats included) — used for liveness."""
        self.messages_received += 1
        self.last_message_utc = when or datetime.now(tz=timezone.utc)

    def idle_seconds(self, now: datetime | None = None) -> float | None:
        if self.last_message_utc is None:
            return None
        moment = now or datetime.now(tz=timezone.utc)
        return round((moment - self.last_message_utc).total_seconds(), 3)

    def _drop(self) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        self._ws = None
        if self.state == STATE_CONNECTED:
            self.state = STATE_DISCONNECTED

    def close(self) -> None:
        self._drop()
        if self.state != STATE_AUTH_FAILED:
            self.state = STATE_DISCONNECTED if self.configured else STATE_NOT_CONFIGURED
        self._log("INFO", "connection closed")

    def ensure_connected(self, now: datetime | None = None) -> bool:
        """Reconnect with exponential backoff when the link dropped (spec §8)."""
        if not self.configured or not self.reconnect:
            return False
        if self.state == STATE_AUTH_FAILED:
            return False
        if self._ws is not None and self.state == STATE_CONNECTED:
            return True
        moment = now or datetime.now(tz=timezone.utc)
        if self._next_attempt is not None and moment < self._next_attempt:
            return False
        self.reconnects += 1
        self._log("INFO", f"reconnecting (attempt {self.reconnects}, backoff {self._backoff:.0f}s)")
        self.connect()
        if self.state == STATE_CONNECTED:
            self._backoff = 1.0
            self._next_attempt = None
            return True
        self._backoff = min(self.max_backoff, self._backoff * 2)
        self._next_attempt = datetime.fromtimestamp(moment.timestamp() + self._backoff,
                                                   tz=timezone.utc)
        return False

    def note_auth_failure(self, reason: str = "") -> None:
        self.state = STATE_AUTH_FAILED
        self.last_error = redact(reason or "the feed rejected the session")[:200]
        self._log("ERROR", f"authentication failed: {self.last_error}")

    def health(self) -> dict[str, Any]:
        return {
            "provider": "broker_ws",
            "label": "Operator-authorised live feed (WebSocket bridge)",
            "configured": self.configured,
            "state": self.state,
            "connected": self.state == STATE_CONNECTED,
            "realtime_capable": True,
            "supports_subscribe": True,
            "is_simulated": False,
            "endpoint": _safe_url(self.url) if self.url else None,
            "credentials_set": bool(self.ssid),
            "messages_received": self.messages_received,
            "ignored_messages": self.ignored_messages,
            "reconnects": self.reconnects,
            "last_message_utc": self.last_message_utc.isoformat() if self.last_message_utc else None,
            "idle_seconds": self.idle_seconds(),
            "connected_at_utc": self.connected_at_utc.isoformat() if self.connected_at_utc else None,
            "last_error": self.last_error,
        }
