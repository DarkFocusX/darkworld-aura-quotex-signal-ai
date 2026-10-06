"""Anomaly detection (IsolationForest) over the M1 feature vector.

Used as a veto: when the current market state is unlike anything in the training
distribution (news spike, feed glitch, regime break) the engine refuses to trade
rather than extrapolating.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np


@dataclass
class AnomalyResult:
    available: bool
    is_anomaly: bool
    score: float | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {"available": self.available, "is_anomaly": self.is_anomaly,
                "score": None if self.score is None else round(self.score, 4), "detail": self.detail}


class AnomalyDetector:
    name = "isolation_forest"

    def __init__(self) -> None:
        self.model = None
        self.scaler = None
        self.reason = "Not loaded"

    def load(self, directory: Path) -> bool:
        path = Path(directory) / "isolation_forest.pkl"
        scaler_path = Path(directory) / "scaler.pkl"
        if not path.exists():
            self.reason = f"Artifact missing ({path.name}) — run the training pipeline"
            return False
        try:
            payload = joblib.load(path)
            self.model = payload["model"] if isinstance(payload, dict) else payload
            self.scaler = payload.get("scaler") if isinstance(payload, dict) else None
            if self.scaler is None and scaler_path.exists():
                scaler_payload = joblib.load(scaler_path)
                self.scaler = scaler_payload.get("anomaly_scaler") if isinstance(scaler_payload, dict) else None
            self.reason = "OK"
            return True
        except Exception as exc:  # noqa: BLE001
            self.reason = f"Failed to load: {type(exc).__name__}: {exc}"
            self.model = None
            return False

    @property
    def available(self) -> bool:
        return self.model is not None

    def _scaled(self, matrix: np.ndarray) -> np.ndarray:
        """Scale without triggering sklearn's feature-name mismatch warnings.

        The training pipeline fitted the anomaly scaler on a named DataFrame; feeding it a
        bare numpy array works but prints UserWarnings on every call. Re-attach the names.
        """
        if self.scaler is None:
            return matrix
        names = getattr(self.scaler, "feature_names_in_", None)
        if names is not None:
            import pandas as pd  # local import: pandas is only needed for this path

            return self.scaler.transform(pd.DataFrame(matrix, columns=list(names)))
        return self.scaler.transform(matrix)

    def score_many(self, feature_matrix: np.ndarray) -> list[AnomalyResult]:
        """Batch anomaly scoring — mirrors `score()` but in a single estimator call."""
        matrix = np.asarray(feature_matrix, dtype=float)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if not self.available:
            return [AnomalyResult(available=False, is_anomaly=False, detail=self.reason)
                    for _ in range(matrix.shape[0])]
        x = self._scaled(matrix)
        predictions = self.model.predict(x)
        raw_scores = self.model.decision_function(x)
        results: list[AnomalyResult] = []
        for i in range(x.shape[0]):
            is_anomaly = int(predictions[i]) == -1
            results.append(AnomalyResult(
                available=True,
                is_anomaly=is_anomaly,
                score=float(raw_scores[i]),
                detail="Feature vector outside the training distribution" if is_anomaly else "Within distribution",
            ))
        return results

    def score(self, feature_vector: np.ndarray) -> AnomalyResult:
        if not self.available:
            return AnomalyResult(available=False, is_anomaly=False, detail=self.reason)
        x = self._scaled(np.asarray(feature_vector, dtype=float).reshape(1, -1))
        prediction = int(self.model.predict(x)[0])
        raw = float(self.model.decision_function(x)[0])
        return AnomalyResult(
            available=True,
            is_anomaly=prediction == -1,
            score=raw,
            detail="Feature vector outside the training distribution" if prediction == -1 else "Within distribution",
        )
