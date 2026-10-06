"""Candle-dataset validation.

Every operator-supplied dataset (CSV import, broker download, exchange export) is
validated BEFORE it can be used by the model or the signal engine. Validation is
structural and statistical — it never "fixes" prices, it reports:

    duplicate timestamps     exact duplicate candle opens
    missing candles          gaps inside the requested window
    invalid OHLC             high < low, negative prices, non-finite values
    non-monotonic            timestamps out of order
    insufficient history     fewer candles than the feature pipeline needs
    stale                    newest candle older than the freshness budget
    volume                   whether volume is present (informational)

Nothing in this module invents candles. A dataset with holes stays a dataset with
holes — the caller decides whether the missing ratio is acceptable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from ..constants import GATES
from ..timeutil import utc_iso

# A dataset may carry a small number of gaps (flat minutes, export hiccups, feed
# outages). Beyond this ratio it is not a usable M1 series. Long gaps are treated
# as non-trading periods (weekend/session closes) and counted separately — an FX
# file that skips the weekend is complete, not corrupt.
MAX_MISSING_RATIO = 0.10
SESSION_BREAK_MINUTES = 20
OHLC = ("open", "high", "low", "close")


@dataclass
class DatasetValidation:
    """Structured validation outcome — serialised verbatim to the API/UI."""

    ok: bool = True
    status: str = "VALID"                     # VALID | WARNING | INVALID
    rows: int = 0
    first_candle_utc: str | None = None
    last_candle_utc: str | None = None
    span_hours: float | None = None
    duplicates: int = 0
    non_monotonic: int = 0
    missing_candles: int = 0
    missing_ratio: float = 0.0
    session_breaks: int = 0
    longest_gap_minutes: int = 0
    interval_minutes: float | None = None
    invalid_ohlc: int = 0
    non_finite: int = 0
    negative_prices: int = 0
    has_volume: bool = False
    stale_seconds: float | None = None
    flat_candles: int = 0
    flat_ratio: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "rows": self.rows,
            "first_candle_utc": self.first_candle_utc,
            "last_candle_utc": self.last_candle_utc,
            "span_hours": self.span_hours,
            "duplicates": self.duplicates,
            "non_monotonic": self.non_monotonic,
            "missing_candles": self.missing_candles,
            "missing_ratio": round(self.missing_ratio, 5),
            "session_breaks": self.session_breaks,
            "longest_gap_minutes": self.longest_gap_minutes,
            "interval_minutes": self.interval_minutes,
            "invalid_ohlc": self.invalid_ohlc,
            "non_finite": self.non_finite,
            "negative_prices": self.negative_prices,
            "has_volume": self.has_volume,
            "stale_seconds": None if self.stale_seconds is None else round(self.stale_seconds, 1),
            "flat_candles": self.flat_candles,
            "flat_ratio": round(self.flat_ratio, 5),
            "errors": self.errors,
            "warnings": self.warnings,
            "min_required_candles": GATES["min_candles"],
        }


def validate_candles(
    df: pd.DataFrame,
    *,
    now: datetime | None = None,
    min_candles: int | None = None,
    max_stale_seconds: float | None = None,
    max_missing_ratio: float = MAX_MISSING_RATIO,
    check_freshness: bool = False,
) -> DatasetValidation:
    """Validate a candle frame (index = UTC candle open, columns = OHLC[V])."""
    required = int(min_candles if min_candles is not None else GATES["min_candles"])
    report = DatasetValidation()
    if df is None or len(df) == 0:
        report.ok = False
        report.status = "INVALID"
        report.errors.append("Dataset is empty")
        return report

    report.rows = int(len(df))
    report.duplicates = int(df.index.duplicated(keep=False).sum())
    if report.duplicates:
        report.warnings.append(f"{report.duplicates} duplicate timestamp row(s)")

    # monotonicity
    diffs = np.diff(df.index.view("int64")) if len(df) > 1 else np.array([0])
    report.non_monotonic = int((diffs <= 0).sum())
    if report.non_monotonic:
        report.errors.append(f"{report.non_monotonic} row(s) out of chronological order")

    # OHLC integrity
    for column in OHLC:
        if column not in df.columns:
            report.errors.append(f"Missing column '{column}'")
            return report
    numeric = df[list(OHLC)].apply(pd.to_numeric, errors="coerce")
    non_finite_mask = ~np.isfinite(numeric.to_numpy(dtype=float))
    report.non_finite = int(non_finite_mask.sum())
    if report.non_finite:
        report.errors.append(f"{report.non_finite} non-numeric/NaN OHLC value(s)")
    report.negative_prices = int((numeric[list(OHLC)] <= 0).to_numpy().sum())
    if report.negative_prices:
        report.errors.append(f"{report.negative_prices} zero/negative price value(s)")
    bad_range = (numeric["high"] < numeric["low"]).sum()
    # a candle whose open/close sits outside its own high/low is malformed too
    outside = (
        (numeric["open"] > numeric["high"]) | (numeric["open"] < numeric["low"])
        | (numeric["close"] > numeric["high"]) | (numeric["close"] < numeric["low"])
    ).sum()
    report.invalid_ohlc = int(bad_range + outside)
    if report.invalid_ohlc:
        report.errors.append(
            f"{report.invalid_ohlc} candle(s) with high < low or open/close outside the range"
        )

    report.has_volume = "volume" in df.columns and bool(df["volume"].notna().any())
    report.first_candle_utc = utc_iso(df.index[0].to_pydatetime())
    report.last_candle_utc = utc_iso(df.index[-1].to_pydatetime())
    span = (df.index[-1] - df.index[0]).total_seconds() / 3600.0
    report.span_hours = round(span, 2)

    # gaps: split into short holes (data problems) vs long runs (session breaks)
    unique_index = df.index.unique().sort_values()
    expected = int(round((unique_index[-1] - unique_index[0]).total_seconds() / 60.0)) + 1
    short_gaps = 0
    breaks = 0
    longest = 0
    if len(unique_index) > 1:
        step_minutes = np.diff(unique_index.view("int64")) / 60_000_000_000.0
        for gap in step_minutes:
            hole = int(round(gap)) - 1
            if hole <= 0:
                continue
            longest = max(longest, hole)
            if hole > SESSION_BREAK_MINUTES:
                breaks += 1          # market closed / session break — expected for FX
            else:
                short_gaps += hole   # genuine hole inside a session
    # resolution: the modal spacing tells us whether this is really an M1 series
    if len(unique_index) > 10:
        steps = np.diff(unique_index.view("int64")) / 60_000_000_000.0
        in_session = steps[steps <= SESSION_BREAK_MINUTES]
        if len(in_session):
            values, counts = np.unique(np.round(in_session, 3), return_counts=True)
            report.interval_minutes = float(values[int(np.argmax(counts))])
    if report.interval_minutes is not None and report.interval_minutes > 1.5:
        report.errors.append(
            f"Dataset resolution is {report.interval_minutes:g}-minute, but this engine requires "
            "M1 (1-minute) candles. Export 1-minute candles from your platform."
        )
        report.missing_candles = int(short_gaps)
        report.session_breaks = int(breaks)
        report.longest_gap_minutes = int(longest)
        report.missing_ratio = (short_gaps / expected) if expected else 0.0
        report.ok = False
        report.status = "INVALID"
        return report

    report.missing_candles = int(short_gaps)
    report.session_breaks = int(breaks)
    report.longest_gap_minutes = int(longest)
    report.missing_ratio = (short_gaps / expected) if expected else 0.0

    if report.missing_ratio > max_missing_ratio:
        report.errors.append(
            f"{report.missing_candles} missing candle(s) inside trading sessions "
            f"({report.missing_ratio:.1%} > {max_missing_ratio:.0%} tolerated)"
        )
    elif report.missing_candles:
        report.warnings.append(
            f"{report.missing_candles} missing candle(s) inside trading sessions "
            f"({report.missing_ratio:.1%} — tolerated)"
        )
    if report.session_breaks:
        report.warnings.append(
            f"{report.session_breaks} session break(s) detected (longest gap "
            f"{report.longest_gap_minutes} min) — treated as market closures, not data loss"
        )

    # A feed that repeats one price for most candles is stale/broken, not quiet.
    flat_mask = (
        (numeric["open"] == numeric["high"]) & (numeric["high"] == numeric["low"])
        & (numeric["low"] == numeric["close"])
    )
    report.flat_candles = int(flat_mask.sum())
    report.flat_ratio = report.flat_candles / len(numeric)
    if report.flat_ratio > 0.5:
        report.errors.append(
            f"{report.flat_ratio:.0%} of candles have open = high = low = close — this dataset is a "
            "flat/stale feed, not tradeable price action"
        )
    elif report.flat_ratio > 0.2:
        report.warnings.append(
            f"{report.flat_ratio:.0%} of candles are perfectly flat (open = high = low = close)"
        )

    if check_freshness:
        reference = now or datetime.now(tz=UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        age = (reference.astimezone(UTC) - (df.index[-1] + pd.Timedelta(minutes=1)).to_pydatetime()).total_seconds()
        report.stale_seconds = age
        budget = max_stale_seconds if max_stale_seconds is not None else GATES["max_replay_lag_seconds"]
        if age > budget:
            report.warnings.append(
                f"Newest candle closed {age:.0f}s ago (freshness budget {budget:.0f}s) — "
                "usable for training/backtest, not for a live entry"
            )

    if len(df) < required:
        report.errors.append(
            f"Insufficient history: {len(df)} candles (the feature pipeline needs {required})"
        )

    report.ok = not report.errors
    report.status = "VALID" if report.ok and not report.warnings else ("WARNING" if report.ok else "INVALID")
    return report


def csv_text_to_frame(csv_text: str, *, max_rows: int = 500_000) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Parse operator CSV text into a normalised candle frame.

    Security: the text is parsed with the csv module and pandas only — never
    eval'd, never written to disk here, and hard-capped in size/row count so a
    hostile upload cannot exhaust memory. The caller decides where (and whether)
    to persist the validated result.
    """
    import csv as _csv
    import io

    if csv_text is None or not str(csv_text).strip():
        raise ValueError("CSV payload is empty")
    text = str(csv_text)
    if len(text) > 40_000_000:
        raise ValueError("CSV payload too large (limit 40 MB of text)")

    # Reject NUL bytes / control characters that break parsers downstream.
    if "\x00" in text:
        raise ValueError("CSV payload contains NUL bytes")

    # Sniff the delimiter from the header line (comma, semicolon or tab).
    probe = text[:4096]
    delimiter = ","
    try:
        delimiter = _csv.Sniffer().sniff(probe, delimiters=",;\t|").delimiter
    except Exception:  # noqa: BLE001 — fall back to comma
        delimiter = ","

    try:
        raw = pd.read_csv(io.StringIO(text), sep=delimiter, engine="python", nrows=max_rows)
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator verbatim
        raise ValueError(f"CSV could not be parsed: {type(exc).__name__}: {str(exc)[:180]}") from exc

    if raw.empty:
        raise ValueError("CSV contains a header but no data rows")
    if len(raw.columns) > 40:
        raise ValueError(f"CSV has {len(raw.columns)} columns (limit 40)")

    columns = {str(c).strip().lower(): c for c in raw.columns}
    time_col = next(
        (columns[key] for key in
         ("time", "datetime", "date", "timestamp", "epoch", "open_time", "open time", "time_utc")
         if key in columns),
        None,
    )
    if time_col is None:
        raise ValueError(
            f"CSV is missing a time column (found: {list(raw.columns)[:8]})"
        )
    missing = [name for name in ("open", "high", "low", "close") if name not in columns]
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(missing)}")

    frame = pd.DataFrame({
        "open": pd.to_numeric(raw[columns["open"]], errors="coerce"),
        "high": pd.to_numeric(raw[columns["high"]], errors="coerce"),
        "low": pd.to_numeric(raw[columns["low"]], errors="coerce"),
        "close": pd.to_numeric(raw[columns["close"]], errors="coerce"),
        "volume": pd.to_numeric(raw[columns["volume"]], errors="coerce") if "volume" in columns else np.nan,
    })

    from ..timeutil import parse_any_ts

    timestamps: list[pd.Timestamp] = []
    unparsed = 0
    for value in raw[time_col].tolist():
        try:
            timestamps.append(pd.Timestamp(parse_any_ts(value)))
        except Exception:  # noqa: BLE001
            timestamps.append(pd.NaT)
            unparsed += 1
    frame.index = pd.DatetimeIndex(timestamps)

    meta: dict[str, Any] = {
        "rows_read": int(len(raw)),
        "rows_unparsed_timestamp": unparsed,
        "delimiter": "\\t" if delimiter == "\t" else delimiter,
        "columns_detected": [str(c) for c in raw.columns][:12],
    }
    return frame, meta


def data_quality_block(report: "DatasetValidation | None", *, rows: int | None = None,
                       latest_candle_utc: str | None = None,
                       interval_minutes: float | None = None,
                       stale_seconds: float | None = None) -> dict[str, Any]:
    """Spec §10 — DATA QUALITY for one asset, plus the fields the panel displays.

    The grade is deliberately conservative: any validation error is an F, a
    warning is at best a B, and a clean file earns an A. It describes the
    *dataset*, never the profitability of anything.
    """
    if report is None:
        return {
            "grade": "UNAVAILABLE", "score": None, "latest_candle_utc": latest_candle_utc,
            "age_seconds": stale_seconds, "rows": rows, "resolution_minutes": interval_minutes,
            "gaps": None, "session_breaks": None, "duplicates": None, "invalid_ohlc": None,
            "flat_ratio": None, "errors": [], "warnings": [], "checked": False,
        }
    score = 100
    score -= min(60, report.duplicates * 5 + report.non_monotonic * 10 + report.invalid_ohlc * 5)
    score -= min(25, round(report.missing_ratio * 200))
    score -= min(15, round(report.flat_ratio * 100))
    score -= min(15, len(report.warnings) * 5)
    score = max(0, score)
    if report.errors:
        grade = "F"
    elif score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 50:
        grade = "C"
    else:
        grade = "D"
    return {
        "grade": grade,
        "score": int(score),
        "latest_candle_utc": report.last_candle_utc or latest_candle_utc,
        "first_candle_utc": report.first_candle_utc,
        "age_seconds": stale_seconds if stale_seconds is not None else report.stale_seconds,
        "rows": report.rows if rows is None else rows,
        "resolution_minutes": interval_minutes if interval_minutes is not None else report.interval_minutes,
        "gaps": report.missing_candles,
        "gap_ratio": round(report.missing_ratio, 5),
        "session_breaks": report.session_breaks,
        "duplicates": report.duplicates,
        "invalid_ohlc": report.invalid_ohlc,
        "flat_ratio": round(report.flat_ratio, 5),
        "status": report.status,
        "errors": report.errors[:6],
        "warnings": report.warnings[:6],
        "checked": True,
        "checked_at_utc": utc_iso(),
    }


def adapter_view(meta: Any, health: dict[str, Any] | None = None) -> dict[str, Any]:
    """Which adapter served these candles and what it can actually do (spec §11)."""
    provider = getattr(meta, "provider", None)
    report = (health or {}).get(provider) or (health or {}).get(
        {"csv": "csv", "broker_ws": "broker_ws", "yahoo": "public_feed", "simulated": "simulated"}.get(provider, provider),
        {},
    )
    return {
        "provider": provider,
        "label": report.get("label"),
        "status": report.get("status"),
        "connected": report.get("connected"),
        "realtime_capable": report.get("realtime_capable"),
        "supports_subscribe": report.get("supports_subscribe"),
        "detail": report.get("detail"),
        "badge": getattr(meta, "display_status", None),
        "is_replay": bool(getattr(meta, "is_replay", False)),
        "is_real_market_data": bool(getattr(meta, "is_market_data", False)),
    }


def unavailable_report(asset: str, reason: str, detail: str | None = None,
                       provider: str | None = None, tried: list[dict[str, Any]] | None = None,
                       *, quality: dict[str, Any] | None = None,
                       adapter: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canonical DATA UNAVAILABLE payload (spec §1/§13)."""
    return {
        "quality": quality or data_quality_block(None),
        "adapter": adapter or {},
        "asset": asset,
        "status": "DATA_UNAVAILABLE",
        "badge": "DATA UNAVAILABLE",
        "reason": reason,
        "detail": detail,
        "provider": provider,
        "tried": tried or [],
        "rows": 0,
        "last_update_utc": None,
        "validation": None,
        "as_of_utc": utc_iso(),
    }
