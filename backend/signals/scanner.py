"""Automatic paper scanner (spec §4, §9, §11 of the paper-trading phase).

On every newly closed M1 candle the scanner runs *the same pipeline the button
runs* — `SignalEngine.generate()` — and records the outcome:

    update data → verify freshness → features → model → gates → confidence →
    signal lock → eligibility → PAPER SIGNAL or NO_TRADE with the exact reason

Two rules make it safe to run unattended:

* **one evaluation per (asset, closed candle)** — a candle that was already
  evaluated is skipped, so a 1-second loop cannot spam the ledger or re-roll the
  same setup hoping for a different answer;
* **the scanner never overrides the engine** — it does not raise confidence, does
  not drop gates, and does not turn a rejection into a signal. In DEVELOPMENT
  environments a *simulated* scan can produce a paper row so the plumbing is
  testable end-to-end; that row is labelled SIMULATED everywhere and can never be
  sent to Telegram or presented as a market signal (the eligibility policy blocks
  it in production exactly as before).

The scanner is deliberately separate from the production scheduler so paper
collection can run without changing anything about the deployment defaults.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

from ..config import Settings
from ..constants import ENV_DEVELOPMENT
from ..data import store
from ..data.feed import ContinuousFeed
from ..database import db
from ..settings_service import get_settings
from ..timeutil import floor_minute, ist_clock, ist_iso, to_utc, utc_iso, utc_now_naive
from . import ledger, outcomes
from .engine import STATUS_DATA_UNAVAILABLE, STATUS_SIGNAL, SignalEngine
from .tracker import evaluate_open_signals

STATE_KIND = "PAPER_SCANNER"
DAILY_KIND = "DAILY_LOCK"

_MEMORY_KEY = "paper_scanner_state"


def paper_collection_enabled() -> bool:
    """`PAPER_COLLECTION=true` switches collection on (default: off)."""
    flag = os.getenv("PAPER_COLLECTION")
    if flag is None:
        return False
    return str(flag).strip().lower() in {"1", "true", "yes", "on"}


class PaperScanner:
    def __init__(self, feed: ContinuousFeed | None = None, *, tick_seconds: float = 1.0) -> None:
        self.feed = feed
        self.tick_seconds = float(tick_seconds)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._engine: SignalEngine | None = None
        self._evaluated: dict[str, str] = {}          # asset → last evaluated candle (UTC ISO)
        self.started_at_utc: str | None = None
        self.last_run_utc: str | None = None
        self.last_error: str | None = None
        self.runs = 0
        self.skipped_duplicate_candles = 0

    # ------------------------------------------------------------------ control
    def start(self, *, enabled: bool | None = None) -> dict[str, Any]:
        if enabled is not None and not enabled:
            self.stop()
            return self.status()
        if self.running:
            return self.status()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="paper-scanner", daemon=True)
        self._thread.start()
        if self.started_at_utc is None:
            self.started_at_utc = utc_iso()
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        return {
            "enabled": paper_collection_enabled(),
            "running": self.running,
            "runs": self.runs,
            "started_at_utc": self.started_at_utc,
            "last_run_utc": self.last_run_utc,
            "last_error": self.last_error,
            "feed": self.feed.health() if self.feed else None,
            "evaluated_candles": dict(self._evaluated),
            "skipped_duplicate_candles": self.skipped_duplicate_candles,
            "note": ("The scanner runs the same SignalEngine as the button; it never loosens a gate and "
                     "never re-evaluates a candle it has already evaluated."),
        }

    def _loop(self) -> None:  # pragma: no cover - exercised through tick()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — the loop must survive bad data
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.tick_seconds)

    # --------------------------------------------------------------------- tick
    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        now = to_utc(now or utc_now_naive())
        self.runs += 1
        self.last_run_utc = utc_iso(now)
        if self.feed and self.feed.running:
            try:
                self.feed.tick(now)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"feed: {type(exc).__name__}: {exc}"
        settings = get_settings()
        result: dict[str, Any] = {
            "at_utc": utc_iso(now), "at_ist": ist_clock(now),
            "scanned": [], "skipped": [], "outcomes": None, "daily": None,
        }
        result["outcomes"] = self._settle(now)
        for asset in self.assets():
            candle = self._closed_candle_for(asset, now)
            if candle is None:
                result["skipped"].append({"asset": asset, "reason": "no closed candle is due yet"})
                continue
            if self._evaluated.get(asset) == candle:
                self.skipped_duplicate_candles += 1
                result["skipped"].append({"asset": asset, "reason": f"candle {candle} already evaluated"})
                continue
            decision = self.evaluate_asset(asset, candle, settings=settings, now=now)
            self._evaluated[asset] = candle
            result["scanned"].append(decision)
        result["daily"] = self.daily_status(now=now)
        self._persist_state(now)
        return result

    # ------------------------------------------------------------------ helpers
    def assets(self) -> list[str]:
        symbols = list(store.assets())
        if self.feed:
            for symbol in self.feed.health().get("subscribed_assets", []):
                if symbol not in symbols:
                    symbols.append(symbol)
        return symbols

    def _closed_candle_for(self, asset: str, now: datetime) -> str | None:
        """The newest stored candle that has fully closed (never the forming one)."""
        row = store.latest_row(asset)
        if not row:
            return None
        opened = to_utc(datetime.fromisoformat(str(row["ts_utc"]).replace("Z", "+00:00")))
        resolution = float(row.get("resolution_minutes") or 1.0)
        if opened.timestamp() + resolution * 60 > now.timestamp():
            rows = store.candles(asset, limit=2)
            if len(rows) < 2:
                return None
            row = rows[-2]
            opened = to_utc(datetime.fromisoformat(str(row["ts_utc"]).replace("Z", "+00:00")))
        return utc_iso(opened)

    def evaluate_asset(self, asset: str, candle_utc: str, *, settings: Settings | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
        """Run the real engine for one (asset, candle) and record the result."""
        settings = settings or get_settings()
        now = to_utc(now or utc_now_naive())
        engine = self._get_engine(settings)
        decision: dict[str, Any] = {"asset": asset, "candle_utc": candle_utc}
        try:
            payload = engine.generate(asset, mode="NOW", now=now, persist=True)
        except Exception as exc:  # noqa: BLE001 — a scan error is a scan outcome, not a crash
            self.last_error = f"{asset}: {type(exc).__name__}: {exc}"
            decision.update({"status": "ERROR", "reason": self.last_error})
            self._log_scan(asset, "ERROR", self.last_error, None, None, now)
            return decision

        scan = (payload.get("all_scans") or [{}])[0] or {}
        status = payload.get("status") or scan.get("status") or "NO_TRADE"
        # Prefer the *asset-level* reason: it is the exact blocking reason for this
        # asset (e.g. "Insufficient candles: 4 (minimum 120)"), whereas the payload
        # reason is the aggregated headline.
        reason = scan.get("reason") or payload.get("reason")
        detail = scan.get("detail") or payload.get("reason_detail")
        confidence = payload.get("confidence")
        signal = payload.get("signal")
        decision.update({
            "status": status,
            "reason": reason,
            "detail": detail,
            "confidence": confidence,
            "direction": payload.get("direction"),
            "signal_id": (signal or {}).get("signal_uid"),
            "blocked_by": (scan.get("eligibility") or {}).get("blocked_by") or [],
            "feed": scan.get("feed") or payload.get("feed"),
            "components": scan.get("components"),
            "checks": scan.get("checks"),
            "production": settings.environment != ENV_DEVELOPMENT,
        })
        if status == STATUS_SIGNAL and signal:
            decision["ledger"] = self._record_signal(signal, scan=scan, settings=settings, now=now)
        else:
            self._log_scan(asset, status if status != STATUS_DATA_UNAVAILABLE else "DATA_UNAVAILABLE",
                           reason, confidence, scan.get("feed"), now, detail=detail,
                           blocked_by=decision["blocked_by"])
        return decision

    # ---------------------------------------------------------------- recording
    def _record_signal(self, signal: dict[str, Any], *, scan: dict[str, Any], settings: Settings,
                       now: datetime) -> dict[str, Any]:
        # NOTE: the engine has already persisted the signal row inside generate(); this
        # call attaches the ledger columns (provider, versions, status) to that same row.
        feed = scan.get("feed") or {}
        model_status = self._get_engine(settings).model_service().status()
        try:
            ledger.record_signal(
                signal,  # idempotent: attaches ledger metadata when the engine already stored it
                feed_provider=feed.get("provider"),
                feed_status=feed.get("badge") or feed.get("display_status"),
                model_version=model_status.get("version"),
                dataset_version=(model_status.get("training") or {}).get("dataset_version")
                or model_status.get("dataset_version"),
                feature_version=model_status.get("feature_version"),
                expiry_time_utc=signal.get("expiry_time_utc"),
                simulated=bool(feed.get("is_simulated")),
            )
            return {"recorded": True, "signal_id": signal.get("signal_uid")}
        except Exception as exc:  # noqa: BLE001 — a duplicate insert means the lock already fired
            return {"recorded": False, "signal_id": signal.get("signal_uid"),
                    "detail": f"{type(exc).__name__}: {exc}"}

    def _settle(self, now: datetime) -> dict[str, Any]:
        try:
            receipts = evaluate_open_signals(now=now)          # legacy tracker (registry feed)
        except Exception as exc:  # noqa: BLE001
            receipts = {"error": f"{type(exc).__name__}: {exc}", "settled": []}
        try:
            store_receipts = outcomes.resolve_due(now=now)      # store-only verification
        except Exception as exc:  # noqa: BLE001
            store_receipts = {"error": f"{type(exc).__name__}: {exc}", "settled": []}
        return {"registry": receipts, "store": store_receipts}

    def _log_scan(self, asset: str, status: str, reason: str | None, confidence: float | None,
                  feed: dict[str, Any] | None, now: datetime, *, detail: str | None = None,
                  blocked_by: list[str] | None = None) -> None:
        details = {"detail": detail, "blocked_by": blocked_by or [], "paper_scanner": True}
        db.log_scan(
            asset=asset, status=status, reason=reason, confidence=confidence,
            feed_source=(feed or {}).get("provider"), is_simulated=bool((feed or {}).get("is_simulated")),
            scan_mode="PAPER", details=details, scanned_at_utc=now,
            scanned_at_ist=ist_clock(now),
        )

    # -------------------------------------------------------------- daily (§9)
    def daily_status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """One best qualified signal per IST day — never forced."""
        now = to_utc(now or utc_now_naive())
        day = ist_clock(now)[:10]
        best = db.get_daily_best(day)
        if best and best.get("signal_uid"):
            return {"day_ist": day, "status": "SIGNAL", "locked": True,
                    "asset": best.get("asset"), "direction": best.get("direction"),
                    "confidence": best.get("confidence"), "entry_time_ist": best.get("entry_time_ist"),
                    "note": "today's selection is locked; a later weaker setup can never replace it"}
        return {"day_ist": day, "status": "NO_TRADE", "locked": False,
                "note": "no qualified setup has appeared today — nothing is forced"}

    # ------------------------------------------------------------------ state
    def _persist_state(self, now: datetime) -> None:
        try:
            db.set_config({
                _MEMORY_KEY: json.dumps({"evaluated": self._evaluated, "runs": self.runs,
                                         "updated_at_utc": utc_iso(now)})[:4000]
            })
        except Exception:  # noqa: BLE001 — state persistence is best-effort only
            pass

    def _get_engine(self, settings: Settings) -> SignalEngine:
        """The scanner and the button must use the same engine class; in development
        the settings are pinned so a simulated paper run cannot be re-read mid-scan."""
        if self._engine is None or settings.environment == ENV_DEVELOPMENT:
            self._engine = SignalEngine(settings) if settings.environment == ENV_DEVELOPMENT else SignalEngine()
        return self._engine


scanner = PaperScanner()
