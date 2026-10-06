"""Background scheduler.

Two jobs, both optional and controlled from settings:
  * every new M1 candle: outcome evaluation for open signals (verified candles only)
  * every new M1 candle: an automatic scan (the same pipeline the button runs),
    so the dashboard always shows the latest opportunity or the latest rejection.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

from .database import db
from .settings_service import get_settings
from .signals.engine import get_engine
from .signals.tracker import evaluate_open_signals
from .timeutil import floor_minute, ist_clock, utc_iso, utc_now_naive


class BackgroundScheduler:
    def __init__(self, tick_seconds: float = 1.0) -> None:
        self.tick_seconds = tick_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_minute: datetime | None = None
        self.last_run_utc: str | None = None
        self.last_scan_status: str | None = None
        self.last_error: str | None = None
        self.runs = 0

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="signal-scheduler", daemon=True)
        self._thread.start()

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
            "running": self.running,
            "runs": self.runs,
            "last_run_utc": self.last_run_utc,
            "last_scan_status": self.last_scan_status,
            "last_error": self.last_error,
            "tick_seconds": self.tick_seconds,
        }

    # --------------------------------------------------------------------- loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — the scheduler must never die
                self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                try:
                    db.log_event("scheduler", self.last_error, level="ERROR")
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(self.tick_seconds)

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        settings = get_settings()
        now = now or utc_now_naive()
        current_minute = floor_minute(now)
        if self._last_minute == current_minute:
            return {"action": "idle"}
        self._last_minute = current_minute
        self.runs += 1
        self.last_run_utc = utc_iso(now)
        actions: dict[str, Any] = {"minute_ist": ist_clock(current_minute), "evaluate": None, "scan": None}

        if settings.outcome_evaluation_enabled:
            try:
                actions["evaluate"] = evaluate_open_signals(now=now)
            except Exception as exc:  # noqa: BLE001
                actions["evaluate"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
                self.last_error = actions["evaluate"]["error"]

        if settings.scheduler_enabled:
            try:
                engine = get_engine()
                result = engine.generate(mode="NOW", now=now, persist=True)
                self.last_scan_status = result.get("status")
                actions["scan"] = {
                    "status": result.get("status"),
                    "asset": result.get("asset"),
                    "confidence": result.get("confidence"),
                    "reason": result.get("reason"),
                }
            except Exception as exc:  # noqa: BLE001
                self.last_scan_status = "ERROR"
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                actions["scan"] = {"error": self.last_error}
        return actions


scheduler = BackgroundScheduler()
