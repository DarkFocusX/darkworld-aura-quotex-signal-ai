"""Optional neural challenger (LSTM / GRU / Transformer).

Design intent: a sequence model over the last N M1 feature vectors that competes
with the tree ensemble. It is intentionally OPTIONAL — the engine never claims a
neural result it cannot compute.

Availability is determined at runtime:
  * `torch` must be installed  (pip install torch --index-url ...)
  * a neural artifact must exist in backend/models/artifacts/current/neural.pt
    plus `neural_meta.json` describing architecture + sequence length.

Until both are present the service reports MODEL STATUS: "OPTIONAL — NOT
TRAINED" and the ensemble simply runs without it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class NeuralStatus:
    available: bool
    reason: str
    architecture: str | None = None
    sequence_length: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": "neural_challenger",
            "available": self.available,
            "reason": self.reason,
            "architecture": self.architecture,
            "sequence_length": self.sequence_length,
        }


class NeuralChallenger:
    """Interface kept deliberately thin so a trained model can be dropped in."""

    name = "neural"

    def __init__(self) -> None:
        self.model = None
        self.sequence_length: int | None = None
        self.architecture: str | None = None
        self.reason = "Not loaded"

    def load(self, directory: Path) -> bool:
        directory = Path(directory)
        weights = directory / "neural.pt"
        meta = directory / "neural_meta.json"
        if not weights.exists() or not meta.exists():
            self.reason = "OPTIONAL — no trained challenger artifact present"
            return False
        try:
            import json

            import torch  # noqa: F401  (import proves the dependency exists)
        except ImportError:
            self.reason = "OPTIONAL — 'torch' is not installed in this environment"
            return False
        try:
            import json

            import torch

            info = json.loads(meta.read_text(encoding="utf-8"))
            self.architecture = info.get("architecture", "LSTM")
            self.sequence_length = int(info.get("sequence_length", 30))
            self.model = torch.load(weights, map_location="cpu")
            self.model.eval()
            self.reason = "OK"
            return True
        except Exception as exc:  # noqa: BLE001
            self.reason = f"Failed to load neural artifact: {type(exc).__name__}: {exc}"
            self.model = None
            return False

    @property
    def available(self) -> bool:
        return self.model is not None

    def status(self) -> NeuralStatus:
        return NeuralStatus(self.available, self.reason, self.architecture, self.sequence_length)

    def predict(self, sequence: np.ndarray) -> np.ndarray | None:
        """Return [p_put, p_neutral, p_call] or None when unavailable."""
        if not self.available:
            return None
        raise NotImplementedError(
            "Neural inference hook: implement forward pass for the trained architecture in "
            "backend/models/neural.py::NeuralChallenger.predict"
        )
