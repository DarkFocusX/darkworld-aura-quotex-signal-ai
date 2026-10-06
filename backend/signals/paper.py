"""Live paper-trading state (spec §3, §7, §8, §11).

Everything the dashboard needs in one place:

* `live_status()`  — per-asset continuous-feed status with the FRESH / STALE /
  INVALID / UNAVAILABLE verdict and the exact budget it was judged against;
* `panel()`        — top-level LIVE PAPER TRADING block (freshness, IST clock,
  latest M1, next M1, ledger counters, coverage, no-trade rate, last decision);
* `card()`         — the real-time signal card: either the qualified paper signal
  or `NO TRADE` with the *actual* blocking reason recorded by the engine.

Nothing here computes a signal. It only reports what the engine and the ledger
already recorded, so the UI can never show a setup the gates did not approve.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..constants import GATES
from ..data import store
from ..data.feed import ContinuousFeed, validate_candle
from ..database import db
from ..timeutil import ist_clock, ist_iso, seconds_to_next_minute, to_utc, utc_iso, utc_now_naive
from . import ledger

FRESH = "FRESH"
STALE = "STALE"
INVALID = "INVALID"
UNAVAILABLE = "UNAVAILABLE"


def _feed_row(asset: str, *, now: datetime, feed: ContinuousFeed | None) -> dict[str, Any]:
    """One asset's live status — the shared implementation in `data.feed`, so the
    dashboard and the feed API can never disagree."""
    from ..data.feed import asset_live_status

    return asset_live_status(
        asset, now=now,
        provider=feed.provider if feed else None,
        realtime_hint=feed.realtime if feed else False,
        simulated_hint=feed.is_simulated if feed else False,
        replay_hint=feed.is_replay if feed else False,
    )


def live_status(assets: list[str] | None = None, *, now: datetime | None = None,
                feed: ContinuousFeed | None = None) -> dict[str, Any]:
    """§3 payload. `assets` defaults to every asset the store knows about."""
    now = to_utc(now or utc_now_naive())
    if assets:
        symbols = [str(a).upper() for a in assets]
    else:
        from ..constants import ASSET_MAP

        symbols = list(ASSET_MAP.keys())
        for symbol in store.assets():
            if symbol not in symbols:
                symbols.append(symbol)
    rows = [_feed_row(symbol, now=now, feed=feed) for symbol in symbols]
    counts = {status: 0 for status in (FRESH, STALE, INVALID, UNAVAILABLE)}
    for row in rows:
        counts[row["freshness_status"]] = counts.get(row["freshness_status"], 0) + 1
    return {
        "generated_at_utc": utc_iso(now),
        "generated_at_ist": ist_clock(now),
        "budgets_seconds": {"realtime": float(GATES["max_data_lag_seconds"]),
                            "replay": float(GATES["max_replay_lag_seconds"])},
        "counts": counts,
        "assets": rows,
        "worst_status": (INVALID if counts[INVALID] else STALE if counts[STALE] else
                         FRESH if counts[FRESH] else UNAVAILABLE),
        "notes": [
            "FRESH requires the newest verified candle to have closed inside the published budget "
            f"({GATES['max_data_lag_seconds']:.0f}s realtime, {GATES['max_replay_lag_seconds']:.0f}s replay).",
            "Missing minutes are reported as gaps and are never filled with a fabricated candle.",
            "Simulated rows keep their SIMULATED label and can never back a production signal.",
        ],
    }


# ------------------------------------------------------------------ dashboard (§7)
def _scan_counters() -> dict[str, Any]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM scans GROUP BY status").fetchall()
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM scans WHERE scan_mode='PAPER' "
            "AND substr(scanned_at_utc, 1, 10) = ?", (utc_iso()[:10],)).fetchone()
    by_status = {row["status"]: int(row["n"]) for row in rows}
    total = sum(by_status.values())
    qualified = by_status.get("SIGNAL", 0)
    coverage = (qualified / total) if total else None
    return {
        "scans_recorded": total,
        "scans_by_status": by_status,
        "paper_scans_today": int(today["n"]) if today else 0,
        "coverage": coverage,
        "no_trade_rate": (1 - coverage) if coverage is not None else None,
    }


def last_decision() -> dict[str, Any] | None:
    """The newest recorded scan decision — the honest source for the NO TRADE card."""
    rows = db.list_scans(limit=1)
    if not rows:
        return None
    row = rows[0]
    details = row.get("details") or {}
    return {
        "asset": row.get("asset"),
        "status": row.get("status"),
        "reason": row.get("reason"),
        "detail": details.get("detail"),
        "blocked_by": details.get("blocked_by") or [],
        "confidence": row.get("confidence"),
        "feed_source": row.get("feed_source"),
        "is_simulated": bool(row.get("is_simulated")),
        "scan_mode": row.get("scan_mode"),
        "scanned_at_utc": row.get("scanned_at_utc"),
        "scanned_at_ist": row.get("scanned_at_ist"),
    }


def panel(*, now: datetime | None = None, feed: ContinuousFeed | None = None,
          assets: list[str] | None = None) -> dict[str, Any]:
    """Everything the LIVE PAPER TRADING panel renders."""
    now = to_utc(now or utc_now_naive())
    status = live_status(assets, now=now, feed=feed)
    today = ist_clock(now)[:10]
    summary = ledger.summary(day_ist=today)
    counters = _scan_counters()
    newest = None
    for row in status["assets"]:
        candle = row.get("latest_candle")
        if candle and (newest is None or candle["open_time_utc"] > newest["open_time_utc"]):
            newest = {**candle, "asset": row["asset"], "provider": row["provider"]}
    next_minute = (now + timedelta(seconds=seconds_to_next_minute(now))).replace(second=0, microsecond=0)
    daily = db.get_daily_best(today)
    return {
        "generated_at_utc": utc_iso(now),
        "current_ist": ist_clock(now),
        "mode": "PAPER",
        "data_freshness": status["worst_status"],
        "freshness_counts": status["counts"],
        "latest_m1": newest,
        "next_m1": {
            "open_time_utc": utc_iso(next_minute),
            "open_time_ist": ist_iso(next_minute),
            "seconds_away": round(seconds_to_next_minute(now), 1),
        },
        "signal_lock": {
            "daily_locked": bool(daily and daily.get("signal_uid")),
            "daily": daily,
            "locked_attempts_recorded": len(ledger.locked_attempts(limit=5)),
        },
        "current_feed_provider": next((row["provider"] for row in status["assets"] if row.get("provider")),
                                      None),
        "paper": summary,
        "scans": counters,
        "last_decision": last_decision(),
        "live": status,
        "disclaimer": ("Paper mode only. A signal here is a qualified setup according to the configured "
                       "gates — never a promise of profit."),
    }


# ------------------------------------------------------------------- card (§8)
def card(*, now: datetime | None = None) -> dict[str, Any]:
    """The real-time card: the newest signal, or the newest rejection with its reason."""
    now = to_utc(now or utc_now_naive())
    latest = ledger.rows(limit=1)
    if latest:
        row = latest[0]
        return {
            "kind": "PAPER SIGNAL",
            "asset": row["asset"],
            "time_ist": row["entry_time_ist"],
            "direction": row["direction"],
            "confidence": row["confidence"],
            "model_agreement": row["model_agreement"],
            "expiry_minutes": row["expiry_minutes"],
            "data": "SIMULATED" if row["is_simulated"] else ("REPLAY" if row.get("is_replay") else "REAL"),
            "provider": row["feed_provider"],
            "model": row["model_version"],
            "status": row["status"],
            "result": row["result"],
            "generated_at_ist": ist_clock(now),
        }
    decision = last_decision()
    if decision is None:
        return {"kind": "NO TRADE", "reason": "no scan has been recorded yet", "generated_at_ist": ist_clock(now)}
    return {
        "kind": "NO TRADE",
        "reason": decision.get("reason") or "no qualified setup",
        "detail": decision.get("detail"),
        "blocked_by": decision.get("blocked_by"),
        "asset": decision.get("asset"),
        "status": decision.get("status"),
        "confidence": decision.get("confidence"),
        "scanned_at_ist": decision.get("scanned_at_ist"),
        "generated_at_ist": ist_clock(now),
    }
