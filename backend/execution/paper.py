"""Paper-trading adapter — the only execution path enabled in this build."""
from __future__ import annotations

from typing import Any

from .base import ExecutionAdapter, ExecutionResult


class PaperExecutionAdapter(ExecutionAdapter):
    """Records the signal as a paper trade. No money, no broker, no order."""

    name = "paper"
    mode = "PAPER"

    def __init__(self) -> None:
        self.log: list[dict[str, Any]] = []

    def place_order(self, signal: dict[str, Any]) -> ExecutionResult:
        record = {
            "signal_uid": signal.get("signal_uid"),
            "asset": signal.get("asset"),
            "direction": signal.get("direction"),
            "entry_time_ist": signal.get("entry_time_ist"),
            "expiry_minutes": signal.get("expiry_minutes"),
            "stake": None,
            "money_moved": False,
        }
        self.log.append(record)
        return ExecutionResult(
            status="PAPER_RECORDED",
            adapter=self.name,
            detail="Paper trade recorded — no order was sent to any broker.",
            meta=record,
        )


class SignalOnlyAdapter(ExecutionAdapter):
    """SIGNAL_ONLY mode: emit nothing to any execution venue."""

    name = "signal_only"
    mode = "SIGNAL_ONLY"

    def place_order(self, signal: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(
            status="NOT_EXECUTED",
            adapter=self.name,
            detail="SIGNAL_ONLY mode — signal emitted to the dashboard only.",
        )


class LiveBrokerAdapter(ExecutionAdapter):
    """Placeholder for a real broker connection.

    Intentionally unimplemented: live money execution requires an explicit,
    licensed integration and is out of scope for this build.
    """

    name = "live_broker"
    mode = "LIVE"

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def place_order(self, signal: dict[str, Any]) -> ExecutionResult:
        raise NotImplementedError(
            "Live broker execution is not implemented in Quotex Signal AI. "
            "Signals are study/paper only. Implement a licensed broker adapter in "
            "backend/execution/ to enable it."
        )
