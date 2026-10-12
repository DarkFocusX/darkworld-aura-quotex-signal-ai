"""LIVE FEED state — one honest answer to "is a continuous real feed arriving?".

Derived from **stored evidence only** (never from a claim, never from configuration alone):

    NOT_CONFIGURED  no feed endpoint is configured and no realtime real candle has ever
                    been stored — the deployment is exactly where it started
    CONNECTED       realtime real candles exist; either they are inside the freshness
                    budget (FRESH) or they stopped recently (NO_DATA / STALE)
    DISCONNECTED    realtime candles were flowing and then stopped for longer than the
                    disconnect window — the bridge or the upstream feed went away
    FRESH           newest realtime real candle closed inside the published budget
                    (120 s) — the state a live bridge is expected to produce

`GET /api/data/live-status` remains the per-asset detail; this module is the single-line
summary the dashboard header and `/api/health` show, so the operator can tell
"not configured" from "configured but silent" from "streaming".
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..data import store
from ..timeutil import ist_clock, to_utc
from . import paper as paper_state

#: A realtime feed that stopped for longer than this is reported as DISCONNECTED.
DISCONNECT_AFTER_SECONDS = 300.0

STATE_NOT_CONFIGURED = "NOT_CONFIGURED"
STATE_CONNECTED = "CONNECTED"
STATE_DISCONNECTED = "DISCONNECTED"
STATE_FRESH = "FRESH"

DATA_FRESH = "FRESH"
DATA_STALE = "STALE"
DATA_NO_DATA = "NO_DATA"


def _broker_health() -> dict[str, Any]:
    try:
        from ..data.registry import registry

        return registry.broker.health() or {}
    except Exception as exc:  # noqa: BLE001 — a health probe must never raise
        return {"state": "ERROR", "detail": f"{type(exc).__name__}: {exc}", "configured": False}


def _twelve_data_configured() -> bool:
    """Twelve Data is configured when its key is present — the endpoint has a default."""
    try:
        from ..data.providers.twelve_data import configured as twelve_data_configured

        return bool(twelve_data_configured())
    except Exception:  # noqa: BLE001 — a health probe must never raise
        return False


def _configured_providers() -> list[str]:
    """Every *configured* live source, in the order the chain tries them.

    Being configured is not the same as delivering: this list only says a source is
    wired up, so a deployment that has a Twelve Data key reads CONNECTED/NO_DATA
    instead of the (wrong) NOT_CONFIGURED.
    """
    import os

    names: list[str] = []
    if (os.environ.get("QUOTEX_WS_URL", "") or "").strip() or _broker_health().get("configured"):
        names.append("broker_ws")
    if _twelve_data_configured():
        names.append("twelve_data")
    return names


def _configured() -> bool:
    return bool(_configured_providers())


def live_feed_state(*, now: datetime | None = None,
                    assets: list[str] | None = None) -> dict[str, Any]:
    """Summarise the continuous real feed from the store + provider configuration."""
    moment = now or datetime.now(tz=timezone.utc)
    status = paper_state.live_status(assets, now=moment)
    rows = status.get("assets") or []
    # A candle that has not closed yet is *not* verified data: the engine never uses it
    # (`live_store` filters on close time), so the summary must not call it FRESH either.
    in_progress = [row for row in rows
                   if row.get("rows") and row.get("age_seconds") is not None
                   and row["age_seconds"] < 0]
    realtime_rows = [row for row in rows
                     if row.get("realtime") and not row.get("is_simulated") and row.get("rows")
                     and row.get("age_seconds") is not None and row["age_seconds"] >= 0]
    with_rows = [row for row in rows if row.get("rows")]
    newest = max((row["age_seconds"] for row in realtime_rows
                  if row.get("age_seconds") is not None), default=None)
    newest_row = None
    if realtime_rows:
        newest_row = min(realtime_rows, key=lambda row: row.get("age_seconds", 1e18))
    budget = float((status.get("budgets_seconds") or {}).get("realtime", 120.0))

    if not realtime_rows:
        if in_progress:
            state, detail = STATE_CONNECTED, (
                f"candles are being written for {', '.join(sorted(r['asset'] for r in in_progress))} "
                f"but none has closed yet — the first closed M1 candle makes the feed FRESH")
            data = DATA_NO_DATA
        elif _configured():
            sources = ", ".join(_configured_providers())
            state, detail = STATE_CONNECTED, (
                f"{sources} is configured but no realtime candle has been stored yet — verify with "
                "tools/probe_live_feed.py (add --provider twelve_data for standard FX), then start "
                "the bridge")
            data = DATA_NO_DATA
        else:
            state, detail = STATE_NOT_CONFIGURED, (
                "no authorised feed endpoint and no realtime candles: set QUOTEX_WS_URL / "
                "QUOTEX_SSID (tools/start_live_feed.py) or TWELVE_DATA_API_KEY "
                "(tools/start_twelve_data_feed.py) — see LIVE_FEED_SETUP.md")
            data = DATA_NO_DATA

    elif newest is not None and newest <= budget:
        state, detail = STATE_FRESH, (
            f"newest realtime candle closed {newest:.0f}s ago (budget {budget:.0f}s)")
        data = DATA_FRESH
    elif newest is not None and newest <= DISCONNECT_AFTER_SECONDS:
        state, detail = STATE_CONNECTED, (
            f"the feed delivered a candle {newest:.0f}s ago (outside the {budget:.0f}s freshness "
            f"budget, inside the {DISCONNECT_AFTER_SECONDS:.0f}s disconnect window) — the scan "
            f"reports STALE")
        data = DATA_STALE
    else:
        state, detail = STATE_DISCONNECTED, (
            f"no realtime candle for {newest:.0f}s (> {DISCONNECT_AFTER_SECONDS:.0f}s) — the "
            f"bridge or the upstream feed has stopped; nothing is fabricated to fill the gap")
        data = DATA_STALE

    return {
        "state": state,
        "data": data,
        "detail": detail,
        "configured": _configured(),
        "provider": ((newest_row or {}).get("provider")
                     or ", ".join(_configured_providers()) or None),
        "assets_streaming": sorted(row["asset"] for row in realtime_rows),
        "assets_with_rows": sorted(row["asset"] for row in with_rows),
        "latest_candle_utc": _candle_label(newest_row),
        "latest_candle_ist": _candle_label_ist(newest_row),
        "age_seconds": newest,
        "freshness_budget_seconds": budget,
        "disconnect_after_seconds": DISCONNECT_AFTER_SECONDS,
        "provider_health": _broker_health(),
        "store_rows": sum(int(row.get("rows") or 0) for row in with_rows),
        "candles_in_progress": sorted(row["asset"] for row in in_progress),
        "checked_at_utc": moment.astimezone(timezone.utc).isoformat(),
        "note": ("FRESH requires a candle that closed inside the realtime budget. A replay "
                 "snapshot, a simulated feed and a configured-but-silent endpoint are all "
                 "reported distinctly — none of them is displayed as live."),
    }


def _candle_label(row: dict[str, Any] | None) -> str | None:
    """The M1 candle's OPEN time in UTC — the label the dashboard shows as LATEST M1."""
    candle = (row or {}).get("latest_candle") or {}
    return candle.get("open_time_utc")


def _candle_label_ist(row: dict[str, Any] | None) -> str | None:
    label = _candle_label(row)
    if not label:
        return None
    try:
        return ist_clock(to_utc(datetime.fromisoformat(str(label).replace("Z", "+00:00"))))
    except (TypeError, ValueError):
        return None


def acceptance_state(state: str) -> str:
    """Map the operational state onto the acceptance vocabulary (spec §18)."""
    return {
        STATE_NOT_CONFIGURED: STATE_NOT_CONFIGURED,
        STATE_CONNECTED: STATE_CONNECTED,
        STATE_DISCONNECTED: STATE_DISCONNECTED,
        STATE_FRESH: STATE_FRESH,
    }.get(state, STATE_CONNECTED)


def store_has_realtime_rows() -> bool:
    for symbol in store.assets():
        row = store.latest_row(symbol)
        if row and row.get("realtime") and not row.get("is_simulated"):
            return True
    return False
