"""Paper-trading ledger + evidence tracker (spec §5, §11, §12).

The ledger *is* the `signals` table: every paper signal the engine actually
produced, with the provenance of the feed that produced it and a ledger status
(PENDING → WIN / LOSS / UNRESOLVED, or LOCKED / REJECTED for rows that never
became live signals). Nothing in here can invent an outcome — settlement goes
through `backend.signals.outcomes`, which requires a verified closing candle.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ..database import db
from ..timeutil import to_utc, utc_iso, utc_now_naive

PENDING = "PENDING"
WIN = "WIN"
LOSS = "LOSS"
UNRESOLVED = "UNRESOLVED"
LOCKED = "LOCKED"
REJECTED = "REJECTED"

LEDGER_STATUSES = (PENDING, WIN, LOSS, UNRESOLVED, LOCKED, REJECTED)

# §12 published evidence rule (same numbers as the research lab).
EVIDENCE_MIN_SIGNALS = 100
EVIDENCE_MIN_WILSON_LB = 0.50

# Columns added by this phase (existing databases are upgraded in place).
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ledger_status", "TEXT"),
    ("feed_provider", "TEXT"),
    ("feed_status", "TEXT"),
    ("model_version", "TEXT"),
    ("dataset_version", "TEXT"),
    ("feature_version", "TEXT"),
    ("expiry_time_utc", "TEXT"),
    ("outcome_source", "TEXT"),
)

_MIGRATED_PATHS: set[str] = set()


def ensure_schema() -> None:
    """Add the ledger columns to whatever database is currently configured.

    Keyed on the database path (tests point the app at a temporary file), so a
    fresh database is always upgraded before its first ledger write.
    """
    key = str(db.DB_PATH)
    if key in _MIGRATED_PATHS:
        return
    with db.connection() as conn:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
        for column, ddl in _ADDED_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {ddl}")
        # legacy rows: a PENDING signal is PENDING, a settled one is WIN/LOSS, a
        # VOID went unresolved (no decisive move) — never re-labelled as a win.
        conn.execute(
            "UPDATE signals SET ledger_status = CASE "
            "  WHEN result='WIN' THEN 'WIN' WHEN result='LOSS' THEN 'LOSS' "
            "  WHEN result='VOID' THEN 'UNRESOLVED' ELSE 'PENDING' END "
            "WHERE ledger_status IS NULL"
        )
        conn.execute("UPDATE signals SET outcome_source='VERIFIED_CANDLE' "
                     "WHERE outcome_source IS NULL AND result IN ('WIN','LOSS','VOID')")
    _MIGRATED_PATHS.add(key)


# ------------------------------------------------------------------- writing
def record_signal(signal: dict[str, Any], *, feed_provider: str | None = None,
                  feed_status: str | None = None, model_version: str | None = None,
                  dataset_version: str | None = None, feature_version: str | None = None,
                  expiry_time_utc: str | None = None, simulated: bool | None = None) -> int:
    """Persist a paper signal. If the engine already stored the row (the normal path —
    `SignalEngine` writes it inside the scan), the ledger columns are attached to that
    row instead of inserting a second one."""
    ensure_schema()
    payload = dict(signal)
    columns = {
        "ledger_status": PENDING,
        "feed_provider": feed_provider or payload.get("feed_provider")
        or (payload.get("feed") or {}).get("provider") or payload.get("feed_source"),
        "feed_status": feed_status or payload.get("feed_badge"),
        "model_version": model_version,
        "dataset_version": dataset_version,
        "feature_version": feature_version,
        "expiry_time_utc": expiry_time_utc or payload.get("expiry_time_utc"),
    }
    try:
        row_id = db.save_signal(payload)
        assignments = ", ".join(f"{key}=?" for key in columns)
        with db.connection() as conn:
            conn.execute(f"UPDATE signals SET {assignments} WHERE signal_uid=?",
                         (*columns.values(), payload["signal_uid"]))
        return row_id
    except sqlite3.IntegrityError:
        # IntegrityError is most often the per-candle lock (the unique signal_uid). If the
        # row is genuinely absent, this was a different constraint — re-raise it instead of
        # masquerading a schema/validation problem as a "signal already exists" outcome.
        existing = db.get_signal(payload["signal_uid"]) or {}
        if not existing:
            raise
        attach_metadata(payload["signal_uid"], **columns)
        return int(existing.get("id") or 0)


def attach_metadata(signal_uid: str, **columns: Any) -> bool:
    """Attach provenance/ledger columns to an existing signal row (no overwrite of a
    settled outcome: `ledger_status` is only set while the row is still PENDING)."""
    ensure_schema()
    allowed = {"feed_provider", "feed_status", "model_version", "dataset_version",
               "feature_version", "expiry_time_utc", "ledger_status", "outcome_source"}
    updates = {k: v for k, v in columns.items() if k in allowed and v is not None}
    if not updates:
        return False
    if "ledger_status" in updates:
        updates["ledger_status"] = PENDING  # never demote a settled row
    assignments = ", ".join(f"{key}=?" for key in updates)
    with db.connection() as conn:
        if updates.get("ledger_status") == PENDING:
            # A row the engine inserted itself still has ledger_status NULL (the engine
            # writes outcomes, not ledger state), so the "never demote a settled row"
            # guard has to accept NULL as PENDING — otherwise the provenance columns
            # (model/dataset/feature version) would silently never be attached.
            cur = conn.execute(
                f"UPDATE signals SET {assignments} WHERE signal_uid=? AND "
                "(ledger_status IS NULL OR ledger_status IN (?,?))",
                (*updates.values(), signal_uid, PENDING, PENDING))
        else:
            cur = conn.execute(f"UPDATE signals SET {assignments} WHERE signal_uid=?",
                               (*updates.values(), signal_uid))
        return cur.rowcount > 0


def settle(signal_uid: str, result: str, exit_price: float | None, reason: str, *,
           source: str = "VERIFIED_CANDLE") -> bool:
    """Close a paper signal with a verified outcome."""
    ensure_schema()
    ledger_status = {WIN: WIN, LOSS: LOSS, "VOID": UNRESOLVED}.get(result, UNRESOLVED)
    ok = db.mark_outcome(signal_uid, result, exit_price, reason)
    if ok:
        with db.connection() as conn:
            conn.execute(
                "UPDATE signals SET ledger_status=?, outcome_source=? WHERE signal_uid=?",
                (ledger_status, source, signal_uid),
            )
    return ok


def locked_attempts(*, limit: int = 20) -> list[dict[str, Any]]:
    """Duplicate scans of a candle that already carries a signal (spec §18 F).

    A lock is recorded on the *scan* record, not on the signal row: the ledger entry
    of a real signal keeps its true PENDING/WIN/LOSS/UNRESOLVED status, and the lock
    is visible as its own event.
    """
    ensure_schema()
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT scanned_at_ist, asset, status, reason FROM scans WHERE status='LOCKED' "
            "ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [{"scanned_at_ist": r["scanned_at_ist"], "asset": r["asset"], "status": r["status"],
             "reason": r["reason"]} for r in rows]


def mark_rejected(signal_uid: str, reason: str) -> bool:
    ensure_schema()
    with db.connection() as conn:
        cur = conn.execute(
            "UPDATE signals SET ledger_status=?, result_reason=? WHERE signal_uid=?",
            (REJECTED, reason, signal_uid),
        )
        return cur.rowcount > 0


# ------------------------------------------------------------------- reading
def _row_to_ledger(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("components_json", "model_votes_json", "checks_json"):
        raw = data.pop(key, None)
        if raw:
            try:
                data[key[:-5]] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data[key[:-5]] = None
    entry = data.get("entry_time_utc")
    expiry = data.get("expiry_time_utc")
    if not expiry and entry:
        minutes = int(data.get("expiry_minutes") or 1)
        expiry = utc_iso(to_utc(datetime.fromisoformat(str(entry).replace("Z", "+00:00"))
                                + timedelta(minutes=minutes)))
    return {
        "signal_id": data.get("signal_uid"),
        "asset": data.get("asset"),
        "entry_time_utc": entry,
        "entry_time_ist": data.get("entry_time_ist_full") or data.get("entry_time_ist"),
        "entry_date_ist": data.get("entry_date_ist"),
        "direction": data.get("direction"),
        "entry_price": data.get("price"),
        "expiry_minutes": data.get("expiry_minutes"),
        "expiry_time_utc": expiry,
        "expiry_time_ist": data.get("expiry_time_ist"),
        "model_version": data.get("model_version"),
        "dataset_version": data.get("dataset_version"),
        "feature_version": data.get("feature_version"),
        "confidence": data.get("confidence"),
        "model_agreement": data.get("model_agreement"),
        "feed_provider": data.get("feed_provider") or data.get("feed_source"),
        "feed_status": data.get("feed_status"),
        "is_simulated": bool(data.get("is_simulated")),
        "status": data.get("ledger_status") or (WIN if data.get("result") == WIN else
                                                LOSS if data.get("result") == LOSS else PENDING),
        "result": data.get("result"),
        "result_reason": data.get("result_reason"),
        "exit_price": data.get("exit_price"),
        "outcome_source": data.get("outcome_source"),
        "scan_mode": data.get("scan_mode"),
        "resolved_at_utc": data.get("resolved_at_utc"),
        "created_at_utc": data.get("created_at_utc"),
    }


def rows(*, limit: int = 100, day_ist: str | None = None, asset: str | None = None,
         statuses: list[str] | None = None) -> list[dict[str, Any]]:
    ensure_schema()
    sql = "SELECT * FROM signals"
    clauses: list[str] = []
    params: list[Any] = []
    if day_ist:
        clauses.append("entry_date_ist=?")
        params.append(day_ist)
    if asset:
        clauses.append("asset=?")
        params.append(str(asset).upper())
    if statuses:
        clauses.append("(" + " OR ".join("ledger_status=?" for _ in statuses) + ")")
        params.extend(statuses)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY entry_time_utc DESC LIMIT ?"
    params.append(int(limit))
    with db.connection() as conn:
        return [_row_to_ledger(row) for row in conn.execute(sql, params).fetchall()]


def summary(*, day_ist: str | None = None) -> dict[str, Any]:
    """Counters for the dashboard LIVE PAPER TRADING panel."""
    ensure_schema()
    day = day_ist
    with db.connection() as conn:
        all_rows = conn.execute(
            "SELECT ledger_status, result, entry_date_ist, asset, confidence, entry_time_utc "
            "FROM signals").fetchall()
    total = len(all_rows)
    todays = [r for r in all_rows if not day or r["entry_date_ist"] == day]
    counts = {status: 0 for status in LEDGER_STATUSES}
    for row in todays:
        key = row["ledger_status"] or PENDING
        counts[key] = counts.get(key, 0) + 1
    decided = counts[WIN] + counts[LOSS]
    return {
        "day_ist": day,
        "signals_today": len(todays),
        "pending": counts[PENDING],
        "wins": counts[WIN],
        "losses": counts[LOSS],
        "unresolved": counts[UNRESOLVED],
        "locked": counts[LOCKED],
        "rejected": counts[REJECTED],
        "decided_today": decided,
        "total_ledger_rows": total,
        "total_decided": sum(1 for r in all_rows if r["ledger_status"] in (WIN, LOSS)),
        "win_rate_today": (counts[WIN] / decided) if decided else None,
        "note": ("Paper results only. Model validation metrics are a separate panel and are never combined "
                 "with these numbers."),
    }


# ------------------------------------------------------------------ evidence (§12)
def wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float | None:
    """95 % Wilson lower bound for a binomial proportion (same rule the lab uses)."""
    if total <= 0:
        return None
    phat = wins / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denominator)


def max_losing_streak(statuses: list[str]) -> int:
    best = current = 0
    for status in statuses:
        if status == LOSS:
            current += 1
            best = max(best, current)
        elif status in (WIN,):
            current = 0
    return best


def evidence(*, scans_evaluated: int | None = None) -> dict[str, Any]:
    """Spec §12 — the published evidence bar, computed from the ledger only."""
    ensure_schema()
    with db.connection() as conn:
        rows_ = conn.execute(
            "SELECT ledger_status, result, entry_date_ist, asset, confidence, is_simulated, "
            "entry_time_utc FROM signals ORDER BY entry_time_utc ASC"
        ).fetchall()
        scan_total = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        locked_total = conn.execute("SELECT COUNT(*) FROM scans WHERE status='LOCKED'").fetchone()[0]
        no_trade_total = conn.execute("SELECT COUNT(*) FROM scans WHERE status='NO_TRADE'").fetchone()[0]
        scan_status_counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM scans GROUP BY status ORDER BY n DESC").fetchall()}
    no_trade_scans = conn.execute("SELECT COUNT(*) FROM scans WHERE status='NO_TRADE'").fetchone()[0]         if False else None  # filled below (kept in one query with the rest)
    decided_rows = [r for r in rows_ if r["ledger_status"] in (WIN, LOSS)]
    wins = sum(1 for r in decided_rows if r["ledger_status"] == WIN)
    losses = sum(1 for r in decided_rows if r["ledger_status"] == LOSS)
    unresolved = sum(1 for r in rows_ if r["ledger_status"] == UNRESOLVED)
    total_decided = len(decided_rows)
    lb = wilson_lower_bound(wins, total_decided)
    streak = max_losing_streak([r["ledger_status"] for r in rows_])
    days = sorted({r["entry_date_ist"] for r in rows_ if r["entry_date_ist"]})
    assets = sorted({r["asset"] for r in rows_ if r["asset"]})
    qualified = len(rows_)
    scans = scans_evaluated if scans_evaluated is not None else int(scan_total or 0)
    coverage = (qualified / scans) if scans else None
    no_trade_rate = ((int(no_trade_total or 0) / scans) if scans else None)
    bar_passed = bool(total_decided >= EVIDENCE_MIN_SIGNALS and lb is not None
                      and lb > EVIDENCE_MIN_WILSON_LB)
    return {
        "total_decided_trades": total_decided,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_decided) if total_decided else None,
        "wilson_95_lower_bound": lb,
        "coverage": coverage,
        "no_trade_scans": int(no_trade_total or 0),
        "no_trade_rate": no_trade_rate,
        "scan_status_counts": scan_status_counts,
        "qualified_signals": qualified,
        "scans_recorded": scans,
        "max_losing_streak": streak,
        "unresolved_trades": unresolved,
        "locked_attempts": int(locked_total or 0),
        "pending_trades": sum(1 for r in rows_ if r["ledger_status"] == PENDING),
        "days": len(days),
        "day_list": days[-30:],
        "assets": assets,
        "simulated_rows": sum(1 for r in rows_ if r["is_simulated"]),
        "evidence_rule": {
            "minimum_qualifying_signals": EVIDENCE_MIN_SIGNALS,
            "wilson_95_lower_bound_above": EVIDENCE_MIN_WILSON_LB,
            "note": ("A threshold that does not satisfy this rule remains unqualified; no profitability "
                     "label is attached to any result here."),
        },
        "evidence_bar": "PASSED" if bar_passed else "NOT PASSED",
        "evidence_bar_passed": bar_passed,
        "reasons": [
            reason for reason in (
                None if total_decided >= EVIDENCE_MIN_SIGNALS
                else f"only {total_decided} decided paper trades (need {EVIDENCE_MIN_SIGNALS})",
                None if (lb is not None and lb > EVIDENCE_MIN_WILSON_LB)
                else "Wilson 95 % lower bound not above 0.50",
            ) if reason
        ],
        "notes": [
            "`no_trade_rate` is NO_TRADE scans divided by ALL recorded scans; DATA_UNAVAILABLE scans never "
            "reached a trading decision, so read the scan_status_counts breakdown alongside it.",
            "Every scan is recorded (qualified signals and rejections alike) — that is what makes the "
            "coverage and no-trade numbers auditable.",
        ],
        "disclaimer": ("Decided paper trades are the only evidence counted here. Offline validation metrics "
                       "are reported separately and are never mixed into this number."),
    }


def today_ist() -> str:
    from ..timeutil import ist_clock

    return ist_clock(utc_now_naive())[:10]
