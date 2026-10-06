"""Execution factory. PAPER and SIGNAL_ONLY are available; LIVE is not implemented."""
from __future__ import annotations

from typing import Any

from .base import ExecutionAdapter, ExecutionResult  # noqa: F401
from .paper import LiveBrokerAdapter, PaperExecutionAdapter, SignalOnlyAdapter  # noqa: F401

_ADAPTERS: dict[str, Any] = {
    "PAPER": PaperExecutionAdapter,
    "SIGNAL_ONLY": SignalOnlyAdapter,
}

_paper_singleton = PaperExecutionAdapter()


def get_execution_adapter(mode: str) -> ExecutionAdapter:
    mode = (mode or "PAPER").upper()
    if mode == "PAPER":
        return _paper_singleton
    if mode == "SIGNAL_ONLY":
        return SignalOnlyAdapter()
    if mode == "LIVE":
        return LiveBrokerAdapter()
    return _paper_singleton


def available_modes() -> list[dict[str, Any]]:
    return [
        {"mode": "PAPER", "available": True, "detail": "Paper trading — records signals only."},
        {"mode": "SIGNAL_ONLY", "available": True, "detail": "Dashboard signals only, no execution record."},
        {"mode": "LIVE", "available": False,
         "detail": "Not implemented — requires a licensed broker adapter. Never enabled by default."},
    ]
