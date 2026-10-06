"""Execution layer (deliberately separated from the signal engine).

The signal engine produces and stores signals; execution is an *optional*
downstream consumer. In this build only the paper adapter exists — no order can
reach a live broker.

To add a real broker later, implement `ExecutionAdapter.place_order` in a new
module and register it in `get_execution_adapter`. Nothing in the signal engine
needs to change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionResult:
    status: str                 # PAPER_RECORDED | REJECTED | NOT_IMPLEMENTED | ERROR
    adapter: str
    detail: str = ""
    ticket: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "adapter": self.adapter, "detail": self.detail,
                "ticket": self.ticket, "meta": self.meta}


class ExecutionAdapter(ABC):
    name = "abstract"

    @abstractmethod
    def place_order(self, signal: dict[str, Any]) -> ExecutionResult:
        ...

    def health(self) -> dict[str, Any]:
        return {"adapter": self.name, "mode": getattr(self, "mode", "unknown")}
