"""SQLite persistence layer (local development database).

Tables
------
signals      every signal the engine actually produced (with the full evidence
             bundle: component scores, model votes, feed provenance).
scans        every scan attempt, including rejected ones — this is what powers
             the "why did it say NO TRADE" transparency log.
config_kv    runtime config overrides written by POST /api/config.
daily_best   best qualified setup per IST trading day (DAILY mode).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..config import DB_PATH
from ..timeutil import utc_now_naive, utc_iso

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_uid            TEXT UNIQUE NOT NULL,   -- asset|entry_time_utc (signal lock key)
    asset                 TEXT NOT NULL,
    timeframe             TEXT NOT NULL,
    direction             TEXT NOT NULL,
    confidence            REAL NOT NULL,
    quality               TEXT NOT NULL,
    entry_time_utc        TEXT NOT NULL,
    entry_time_ist        TEXT NOT NULL,          -- "06:30:00" (display / Telegram)
    entry_time_ist_full   TEXT,                  -- "2026-10-02T06:30:00+05:30"
    entry_date_ist        TEXT,                  -- "2026-10-02" (daily filters + caps)
    expiry_minutes        INTEGER NOT NULL,
    expiry_time_ist       TEXT NOT NULL,
    data_timestamp_utc    TEXT NOT NULL,
    data_timestamp_ist    TEXT NOT NULL,
    price                 REAL,
    model_probability     REAL,
    model_agreement       REAL,
    anomaly_flag          INTEGER DEFAULT 0,
    feed_source           TEXT,
    feed_mode             TEXT,
    is_simulated          INTEGER DEFAULT 0,
    components_json       TEXT,
    model_votes_json      TEXT,
    checks_json           TEXT,
    status                TEXT DEFAULT 'OPEN',    -- OPEN | CLOSED
    result                TEXT DEFAULT 'PENDING', -- PENDING | WIN | LOSS | VOID
    result_reason         TEXT,
    resolved_at_utc       TEXT,
    exit_price            REAL,
    scan_mode             TEXT,                   -- NOW | DAILY
    telegram_status       TEXT DEFAULT 'NOT_SENT',
    created_at_utc        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_lock ON signals(asset, entry_time_utc);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(result, status);

CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at_utc TEXT NOT NULL,
    scanned_at_ist TEXT NOT NULL,
    asset         TEXT,
    status        TEXT NOT NULL,      -- SIGNAL | NO_TRADE | DATA_UNAVAILABLE | MODEL_NOT_READY | LOCKED
    reason        TEXT,
    confidence    REAL,
    details_json  TEXT,
    feed_source   TEXT,
    is_simulated  INTEGER DEFAULT 0,
    scan_mode     TEXT
);
CREATE INDEX IF NOT EXISTS idx_scans_time ON scans(scanned_at_utc DESC);

CREATE TABLE IF NOT EXISTS config_kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_best (
    day_ist        TEXT PRIMARY KEY,
    signal_uid     TEXT,
    asset          TEXT,
    direction      TEXT,
    confidence     REAL,
    entry_time_ist TEXT,
    status         TEXT,
    reason         TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT NOT NULL,
    level      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT,
    meta_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts_utc DESC);
"""


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def connection(db_path: Path | str | None = None) -> Iterable[sqlite3.Connection]:
    with _LOCK:
        conn = _connect(db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    with connection(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# Columns added after the first release. Existing dev databases are upgraded in
# place instead of being dropped (never lose stored signal history).
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entry_time_ist_full", "TEXT"),
    ("entry_date_ist", "TEXT"),
    ("exit_price", "REAL"),
    ("telegram_status", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
    for column, ddl in _ADDED_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {ddl}")

    # backfill IST calendar day for rows written before the column existed
    rows = conn.execute(
        "SELECT signal_uid, entry_time_utc FROM signals WHERE entry_date_ist IS NULL"
    ).fetchall()
    for row in rows:
        try:
            dt = datetime.fromisoformat(str(row["entry_time_utc"]).replace("Z", "+00:00"))
            ist_day = (dt + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
            conn.execute("UPDATE signals SET entry_date_ist=? WHERE signal_uid=?", (ist_day, row["signal_uid"]))
        except (TypeError, ValueError):
            continue


# --------------------------------------------------------------------- helpers
def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("components_json", "model_votes_json", "checks_json"):
        if key in d and d[key]:
            try:
                d[key[:-5]] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                d[key[:-5]] = None
    if "is_simulated" in d:
        d["is_simulated"] = bool(d["is_simulated"])
    if "anomaly_flag" in d:
        d["anomaly_flag"] = bool(d["anomaly_flag"])
    return d


def log_event(kind: str, message: str, level: str = "INFO", meta: dict | None = None) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO events (ts_utc, level, kind, message, meta_json) VALUES (?,?,?,?,?)",
            (utc_iso(), level, kind, message,
             json.dumps(meta or {}, default=str)),
        )


# ---------------------------------------------------------------------- config
def get_config(defaults: dict[str, Any]) -> dict[str, Any]:
    """Merge persisted overrides on top of the defaults."""
    out = dict(defaults)
    with connection() as conn:
        rows = conn.execute("SELECT key, value FROM config_kv").fetchall()
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            out[row["key"]] = row["value"]
    return out


def set_config(values: dict[str, Any]) -> dict[str, Any]:
    now = utc_iso()
    with connection() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO config_kv (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), now),
            )
    return values


# ------------------------------------------------------------------- scan log
def log_scan(
    *,
    asset: str | None,
    status: str,
    reason: str | None = None,
    confidence: float | None = None,
    details: dict | None = None,
    scanned_at_utc: datetime | None = None,
    scanned_at_ist: str | None = None,
    feed_source: str | None = None,
    is_simulated: bool = False,
    scan_mode: str | None = None,
) -> None:
    ts = scanned_at_utc or utc_now_naive()
    with connection() as conn:
        conn.execute(
            """INSERT INTO scans (scanned_at_utc, scanned_at_ist, asset, status, reason, confidence,
                                  details_json, feed_source, is_simulated, scan_mode)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                ts.isoformat(timespec="seconds"),
                scanned_at_ist or "",
                asset,
                status,
                reason,
                confidence,
                json.dumps(details or {}, default=str),
                feed_source,
                1 if is_simulated else 0,
                scan_mode,
            ),
        )


def list_scans(limit: int = 20) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [_row_to_dict(r) or {} for r in rows]


# --------------------------------------------------------------------- signals
SIGNAL_FIELDS = (
    "signal_uid", "asset", "timeframe", "direction", "confidence", "quality",
    "entry_time_utc", "entry_time_ist", "entry_time_ist_full", "entry_date_ist",
    "expiry_minutes", "expiry_time_ist",
    "data_timestamp_utc", "data_timestamp_ist", "price", "model_probability",
    "model_agreement", "anomaly_flag", "feed_source", "feed_mode", "is_simulated",
    "components_json", "model_votes_json", "checks_json", "status", "result",
    "result_reason", "scan_mode", "created_at_utc",
)


def save_signal(payload: dict[str, Any]) -> int:
    """Insert a signal. Returns row id. Raises sqlite3.IntegrityError on lock conflict."""
    data = {k: payload.get(k) for k in SIGNAL_FIELDS}
    # a stored signal is always OPEN/PENDING until a verified candle resolves it
    data["status"] = payload.get("status") or "OPEN"
    data["result"] = payload.get("result") or "PENDING"
    data["components_json"] = json.dumps(payload.get("components") or {}, default=str)
    data["model_votes_json"] = json.dumps(payload.get("model_votes") or {}, default=str)
    data["checks_json"] = json.dumps(payload.get("checks") or {}, default=str)
    data["anomaly_flag"] = 1 if payload.get("anomaly_flag") else 0
    data["is_simulated"] = 1 if payload.get("is_simulated") else 0
    data["created_at_utc"] = payload.get("created_at_utc") or utc_iso()
    cols = ", ".join(SIGNAL_FIELDS)
    placeholders = ", ".join(["?"] * len(SIGNAL_FIELDS))
    with connection() as conn:
        cur = conn.execute(
            f"INSERT INTO signals ({cols}) VALUES ({placeholders})",
            tuple(data[k] for k in SIGNAL_FIELDS),
        )
        return int(cur.lastrowid)


def signal_exists_for_candle(asset: str, entry_time_utc: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM signals WHERE asset=? AND entry_time_utc=?",
            (asset, entry_time_utc),
        ).fetchone()
    return _row_to_dict(row)


def get_signal(signal_uid: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM signals WHERE signal_uid=?", (signal_uid,)).fetchone()
    return _row_to_dict(row)


def get_latest_signal(asset: str | None = None, include_simulated: bool = True) -> dict[str, Any] | None:
    sql = "SELECT * FROM signals"
    clauses, params = [], []
    if asset:
        clauses.append("asset=?")
        params.append(asset)
    if not include_simulated:
        clauses.append("is_simulated=0")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT 1"
    with connection() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
    return _row_to_dict(row)


def list_history(limit: int = 30, asset: str | None = None, day_ist: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM signals"
    clauses, params = [], []
    if asset:
        clauses.append("asset=?")
        params.append(asset)
    if day_ist:
        clauses.append("entry_date_ist=?")
        params.append(day_ist)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) or {} for r in rows]


def recent_signals_for_asset(asset: str, limit: int = 200) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE asset=? ORDER BY id DESC LIMIT ?", (asset, int(limit))
        ).fetchall()
    return [_row_to_dict(r) or {} for r in rows]


def get_open_signals() -> list[dict[str, Any]]:
    """Signals still awaiting an outcome (PENDING or UNRESOLVED).

    UNRESOLVED means the expiry candle passed but the feed could not verify it —
    such a signal stays eligible for a later verification attempt, it is never
    silently counted as a win or a loss.
    """
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE result IN ('PENDING','UNRESOLVED') ORDER BY id ASC"
        ).fetchall()
    return [_row_to_dict(r) or {} for r in rows]


def mark_outcome(
    signal_uid: str,
    result: str,
    exit_price: float | None,
    reason: str,
    status: str = "CLOSED",
    resolved_at_utc: str | None = None,
) -> bool:
    """Update a signal outcome. Only used when the closing candle is verified."""
    with connection() as conn:
        cur = conn.execute(
            """UPDATE signals SET result=?, exit_price=?, result_reason=?, status=?,
                                   resolved_at_utc=?
               WHERE signal_uid=? AND result IN ('PENDING','UNRESOLVED')""",
            (
                result,
                exit_price,
                reason,
                status,
                resolved_at_utc or utc_iso(),
                signal_uid,
            ),
        )
        return cur.rowcount > 0


def update_telegram_status(signal_uid: str, status: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE signals SET telegram_status=? WHERE signal_uid=?", (status, signal_uid))


def get_signal_performance(include_simulated: bool = True, since_ist_day: str | None = None) -> dict[str, Any]:
    sql = "SELECT result, COUNT(*) c FROM signals"
    clauses, params = [], []
    if not include_simulated:
        clauses.append("is_simulated=0")
    if since_ist_day:
        clauses.append("entry_date_ist>=?")
        params.append(since_ist_day)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " GROUP BY result"
    with connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        streak_rows = conn.execute(
            "SELECT result, entry_time_utc FROM signals WHERE result IN ('WIN','LOSS') "
            "ORDER BY entry_time_utc ASC"
        ).fetchall()
    counts = {r["result"]: int(r["c"]) for r in rows}
    decided = counts.get("WIN", 0) + counts.get("LOSS", 0)
    win_rate = (counts.get("WIN", 0) / decided * 100.0) if decided else None
    max_loss_streak = 0
    current = 0
    for r in streak_rows:
        if r["result"] == "LOSS":
            current += 1
            max_loss_streak = max(max_loss_streak, current)
        else:
            current = 0
    return {
        "generated": sum(counts.values()),
        "wins": counts.get("WIN", 0),
        "losses": counts.get("LOSS", 0),
        "voids": counts.get("VOID", 0),
        "pending": counts.get("PENDING", 0),
        "unresolved": counts.get("UNRESOLVED", 0),
        "decided": decided,
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
        "max_losing_streak": max_loss_streak,
    }


# ------------------------------------------------------------------ daily best
def upsert_daily_best(day_ist: str, payload: dict[str, Any]) -> None:
    now = utc_iso()
    with connection() as conn:
        conn.execute(
            """INSERT INTO daily_best (day_ist, signal_uid, asset, direction, confidence,
                                       entry_time_ist, status, reason, updated_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day_ist) DO UPDATE SET
                 signal_uid=excluded.signal_uid, asset=excluded.asset, direction=excluded.direction,
                 confidence=excluded.confidence, entry_time_ist=excluded.entry_time_ist,
                 status=excluded.status, reason=excluded.reason, updated_at_utc=excluded.updated_at_utc""",
            (
                day_ist,
                payload.get("signal_uid"),
                payload.get("asset"),
                payload.get("direction"),
                payload.get("confidence"),
                payload.get("entry_time_ist"),
                payload.get("status"),
                payload.get("reason"),
                now,
            ),
        )


def get_daily_best(day_ist: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM daily_best WHERE day_ist=?", (day_ist,)).fetchone()
    return dict(row) if row else None


def ist_day_bounds_utc(day_ist_str: str) -> tuple[datetime, datetime]:
    """UTC bounds for an IST calendar day (used for daily aggregates)."""
    d = datetime.strptime(day_ist_str, "%Y-%m-%d")
    start_ist = d.replace(hour=0, minute=0, second=0)
    start_utc = start_ist - timedelta(hours=5, minutes=30)
    return start_utc, start_utc + timedelta(days=1)
