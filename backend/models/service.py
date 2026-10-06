"""Model service: artifact loading, status reporting, ensemble inference.

Nothing here invents a probability. If artifacts are missing or a model fails,
the service reports MODEL_NOT_READY / ERROR and the signal engine stops — it
never substitutes a hand-written confidence number.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

from . import integrity
import numpy as np
import pandas as pd

from ..config import model_dir
from ..constants import (
    FEATURE_VERSION,
    MODEL_STATE_NOT_READY,
    MODEL_STATE_READY_DEMO,
    MODEL_STATE_READY_MARKET,
)
from ..timeutil import utc_iso
from ..features.pipeline import FEATURE_NAMES
from .anomaly import AnomalyDetector
from .ensemble import BASE_WEIGHTS, EnsembleResult, ModelVote, aggregate, vote_from_probs
from .neural import NeuralChallenger

PRIMARY_MODELS = ("lightgbm", "random_forest", "logistic_regression", "xgboost")
OPTIONAL_MODELS = ("neural",)


class ModelNotReady(RuntimeError):
    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason + (f" ({detail})" if detail else ""))
        self.reason = reason
        self.detail = detail


@dataclass
class LoadedModel:
    name: str
    estimator: Any
    weight: float
    status: str = "OK"
    detail: str = ""
    needs_scaling: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "status": self.status,
            "weight": round(self.weight, 3),
            "detail": self.detail,
        }


@dataclass
class ModelMetadata:
    version: str = "unversioned"
    trained_at_utc: str | None = None
    feature_names: list[str] = field(default_factory=lambda: list(FEATURE_NAMES))
    horizon: int = 1
    neutral_atr_fraction: float = 0.10
    temperature: float = 1.0
    data_provenance: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    training_rows: int = 0
    feature_version: str = FEATURE_VERSION
    model_version: str | None = None
    data_source: str = "unknown"
    data_period: dict[str, Any] = field(default_factory=dict)
    asset_list: list[str] = field(default_factory=list)
    validation_metrics: dict[str, Any] = field(default_factory=dict)
    holdout_metrics: dict[str, Any] = field(default_factory=dict)
    training_timestamp: str | None = None
    source_class: str = "UNKNOWN"


class ModelService:
    """Loads artifacts once, then answers ensemble predictions for feature rows."""

    def __init__(self, directory: Path | None = None, auto_load: bool = True) -> None:
        self.integrity: dict[str, Any] = {"status": "UNKNOWN", "detail": "not loaded", "checked": 0, "mismatched": []}
        self.directory = Path(directory or model_dir())
        self._lock = threading.RLock()
        self.models: dict[str, LoadedModel] = {}
        self.scaler = None
        self.anomaly = AnomalyDetector()
        self.neural = NeuralChallenger()
        self.metadata = ModelMetadata()
        self.last_error: str | None = None
        self.loaded_at_utc: str | None = None
        if auto_load:
            self.load()

    # ------------------------------------------------------------------- loading
    def load(self) -> bool:
        with self._lock:
            self.models.clear()
            self.last_error = None
            models_path = self.directory / "models.pkl"
            meta_path = self.directory / "metadata.json"

            if not models_path.exists():
                self.last_error = (
                    f"MODELS NOT TRAINED — no artifact at {models_path}. "
                    "Run: python -m backend.research.train"
                )
                self.anomaly.load(self.directory)
                self.neural.load(self.directory)
                return False

            self.integrity = integrity.verify(self.directory)
            if self.integrity["status"] == "MISMATCH":
                # A corrupted or substituted artifact must never become the source of
                # predictions: refuse it instead of deserialising it.
                self.last_error = (
                    f"ARTIFACT INTEGRITY MISMATCH — {self.integrity['detail']}. "
                    "Re-run the training/activation step or restore the artifact from a "
                    "known-good copy; the manifest is written by tools/activate_model.py."
                )
                return False

            try:
                payload = joblib.load(models_path)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"MODEL LOADING ERROR — {type(exc).__name__}: {exc}"
                return False

            estimators = payload.get("models", payload) if isinstance(payload, dict) else payload
            scaler_payload = None
            scaler_path = self.directory / "scaler.pkl"
            if scaler_path.exists():
                try:
                    scaler_payload = joblib.load(scaler_path)
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"SCALER LOADING ERROR — {type(exc).__name__}: {exc}"

            if isinstance(estimators, dict):
                for name, estimator in estimators.items():
                    self.models[name] = LoadedModel(
                        name=name,
                        estimator=estimator,
                        weight=float(BASE_WEIGHTS.get(name, 0.10)),
                        needs_scaling=name in {"logistic_regression"},
                    )
            if not self.models:
                self.last_error = "MODEL ARTIFACT EMPTY — no estimators inside models.pkl"
                return False

            if isinstance(scaler_payload, dict):
                self.scaler = scaler_payload.get("features") or scaler_payload.get("scaler")

            if meta_path.exists():
                try:
                    info = json.loads(meta_path.read_text(encoding="utf-8"))
                    self.metadata = ModelMetadata(
                        version=info.get("version", "unversioned"),
                        trained_at_utc=info.get("trained_at_utc"),
                        feature_names=info.get("feature_names", list(FEATURE_NAMES)),
                        horizon=int(info.get("horizon", 1)),
                        neutral_atr_fraction=float(info.get("neutral_atr_fraction", 0.10)),
                        temperature=float(info.get("temperature", 1.0)),
                        data_provenance=info.get("data_provenance", {}) or {},
                        metrics=info.get("metrics", {}) or {},
                        training_rows=int(info.get("training_rows", 0)),
                        feature_version=info.get("feature_version", FEATURE_VERSION),
                        model_version=info.get("model_version") or info.get("version"),
                        data_source=info.get("data_source", "unknown"),
                        data_period=info.get("data_period", {}) or {},
                        asset_list=info.get("asset_list", []) or [],
                        validation_metrics=info.get("validation_metrics", {}) or {},
                        holdout_metrics=info.get("holdout_metrics", {}) or {},
                        training_timestamp=info.get("training_timestamp") or info.get("trained_at_utc"),
                    )
                    self.metadata.source_class = str(
                        (info.get("data_provenance") or {}).get("source_class")
                        or ("SIMULATED" if (info.get("data_provenance") or {}).get("is_simulated") else "MARKET")
                    )
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"METADATA PARSE ERROR — {type(exc).__name__}: {exc}"

            if self.metadata.feature_version and self.metadata.feature_version != FEATURE_VERSION:
                self.last_error = (
                    f"FEATURE VERSION MISMATCH — artifact trained with '{self.metadata.feature_version}', "
                    f"runtime expects '{FEATURE_VERSION}'. Retrain: python -m backend.research.train"
                )
                self.models.clear()
                return False

            if self.metadata.feature_names != list(FEATURE_NAMES):
                mismatched = set(self.metadata.feature_names) ^ set(FEATURE_NAMES)
                if mismatched:
                    self.last_error = (
                        "FEATURE SCHEMA MISMATCH — artifacts were trained on a different feature set "
                        f"({len(mismatched)} differing names). Retrain with: python -m backend.research.train"
                    )
                    self.models.clear()
                    return False

            self.anomaly.load(self.directory)
            self.neural.load(self.directory)
            self.loaded_at_utc = utc_iso()
            return True

    @property
    def ready(self) -> bool:
        return len(self.models) > 0 and self.last_error is None

    @property
    def is_simulated_trained(self) -> bool:
        return bool(self.metadata.data_provenance.get("is_simulated", False))

    @property
    def is_fixture_trained(self) -> bool:
        return str(self.metadata.source_class or "").upper() == "FIXTURE"

    @property
    def is_market_trained(self) -> bool:
        """True only when trained on operator-supplied REAL market data."""
        if not self.ready:
            return False
        if self.is_simulated_trained or self.is_fixture_trained:
            return False
        return True

    @property
    def state(self) -> str:
        if not self.ready:
            return MODEL_STATE_NOT_READY
        return MODEL_STATE_READY_MARKET if self.is_market_trained else MODEL_STATE_READY_DEMO

    # -------------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        available = [m.as_dict() for m in self.models.values()]
        missing_primary = [name for name in PRIMARY_MODELS if name not in self.models and name != "xgboost"]
        xgb_reason = None
        if "xgboost" not in self.models:
            try:
                import xgboost  # noqa: F401

                xgb_reason = "Not trained (available: xgboost installed)"
            except ImportError:
                xgb_reason = "Not available (optional dependency 'xgboost' not installed)"
        state = self.state
        if state == MODEL_STATE_NOT_READY:
            message = self.last_error or "MODELS NOT TRAINED"
        elif state == MODEL_STATE_READY_DEMO:
            reason = "SIMULATED" if self.is_simulated_trained else "FIXTURE"
            message = (
                f"Models loaded, but they were trained on {reason} data. "
                "Predictions are for pipeline demonstration only — not market-quality, "
                "and never usable for a production signal."
            )
        else:
            message = "Models loaded and trained on operator-supplied real market data."
        return {
            "state": state,
            "message": message,
            "artifact_integrity": self.integrity,
            "ready": self.ready,
            "market_ready": self.is_market_trained,
            "production_usable": self.is_market_trained,
            "demo_trained": self.is_simulated_trained or self.is_fixture_trained,
            "source_class": self.metadata.source_class,
            "feature_version": self.metadata.feature_version,
            "expected_feature_version": FEATURE_VERSION,
            "data_source": self.metadata.data_source,
            "data_source_class": self.metadata.source_class,
            "data_period": self.metadata.data_period,
            "row_count": self.metadata.training_rows,
            "training_timestamp": self.metadata.training_timestamp,
            # Spec §3 keys, exactly as persisted in the artifact metadata.
            "validation_metrics": self.metadata.validation_metrics,
            "holdout_metrics": self.metadata.holdout_metrics,
            "asset_list": self.metadata.asset_list or self.metadata.data_provenance.get("assets", []),
            "version": self.metadata.version,
            "trained_at_utc": self.metadata.trained_at_utc,
            "loaded_at_utc": self.loaded_at_utc,
            "artifact_dir": str(self.directory),
            "training_rows": self.metadata.training_rows,
            "horizon_minutes": self.metadata.horizon,
            "temperature": self.metadata.temperature,
            "data_provenance": self.metadata.data_provenance,
            "models": available,
            "unavailable": (
                [{"model": name, "status": "NOT_TRAINED", "detail": "Run the training pipeline"}
                 for name in missing_primary if name not in self.models]
                + ([{"model": "xgboost", "status": "NOT_TRAINED", "detail": xgb_reason}] if xgb_reason else [])
            ),
            "anomaly_detector": self.anomaly.as_dict() if hasattr(self.anomaly, "as_dict") else {
                "available": self.anomaly.available, "reason": self.anomaly.reason,
            },
            "neural_challenger": self.neural.status().as_dict(),
            "last_error": self.last_error,
        }

    def metrics(self) -> dict[str, Any]:
        return {
            "version": self.metadata.version,
            "trained_at_utc": self.metadata.trained_at_utc,
            "training_rows": self.metadata.training_rows,
            "horizon_minutes": self.metadata.horizon,
            "neutral_atr_fraction": self.metadata.neutral_atr_fraction,
            "calibration_temperature": self.metadata.temperature,
            "data_provenance": self.metadata.data_provenance,
            "model_metrics": self.metadata.metrics,
            "validation_metrics": self.metadata.validation_metrics or self.metadata.metrics.get("out_of_fold"),
            "holdout_metrics": self.metadata.holdout_metrics or self.metadata.metrics.get("holdout"),
            "feature_version": self.metadata.feature_version,
            "model_version": self.metadata.model_version or self.metadata.version,
            "data_source": self.metadata.data_source,
            "asset_list": self.metadata.asset_list,
            "feature_count": len(self.metadata.feature_names),
            "ready": self.ready,
            "last_error": self.last_error,
        }

    # ----------------------------------------------------------------- inference
    def _predict_one(self, name: str, model: LoadedModel, x) -> ModelVote:
        try:
            vector = x
            if model.needs_scaling and self.scaler is not None:
                vector = self.scaler.transform(x)
            proba = model.estimator.predict_proba(vector)[0]
            classes = list(getattr(model.estimator, "classes_", [0, 1, 2]))
            full = np.zeros(3, dtype=float)
            for idx, cls in enumerate(classes):
                ci = int(cls)
                if 0 <= ci <= 2:
                    full[ci] = float(proba[idx])
            if full.sum() <= 0:
                raise ValueError("empty probability vector")
            full = full / full.sum()
            return vote_from_probs(name, full, model.weight)
        except Exception as exc:  # noqa: BLE001 — a broken model must not fake a vote
            return ModelVote(model=name, p_put=0.0, p_neutral=1.0, p_call=0.0, weight=model.weight,
                             vote="NO_TRADE", status="ERROR",
                             detail=f"{type(exc).__name__}: {str(exc)[:160]}")

    def _proba_matrix(self, model: LoadedModel, frame: pd.DataFrame) -> np.ndarray | None:
        """Probability matrix (n_rows, 3) for one estimator — one call for the whole batch."""
        try:
            vector = frame
            if model.needs_scaling and self.scaler is not None:
                vector = self.scaler.transform(frame)
            raw = np.asarray(model.estimator.predict_proba(vector), dtype=float)
            classes = list(getattr(model.estimator, "classes_", [0, 1, 2]))
            full = np.zeros((raw.shape[0], 3), dtype=float)
            for idx, cls in enumerate(classes):
                ci = int(cls)
                if 0 <= ci <= 2:
                    full[:, ci] = raw[:, idx]
            totals = full.sum(axis=1, keepdims=True)
            totals[totals <= 0] = 1.0
            return full / totals
        except Exception:  # noqa: BLE001 — a broken model must not fake votes
            return None

    def predict_many(self, feature_rows: list[dict[str, float]]) -> list[EnsembleResult | None]:
        """Batch inference for study replays: one estimator call per model for ALL rows.

        Returns one EnsembleResult per input row (None where the row could not be scored).
        Never fabricates a probability: failed models are reported as ERROR votes and rows
        without a single usable vote come back as None.
        """
        if not feature_rows:
            return []
        with self._lock:
            if not self.ready:
                raise ModelNotReady(self.last_error or "MODELS NOT TRAINED")
            names = self.metadata.feature_names
            try:
                frame = pd.DataFrame([{name: float(row[name]) for name in names} for row in feature_rows],
                                     columns=names)
            except KeyError as exc:
                raise ModelNotReady("FEATURE CHECK FAILED", f"missing feature {exc}") from exc
            x_all = frame.to_numpy(dtype=float)
            finite = np.isfinite(x_all).all(axis=1)
            matrices = {name: self._proba_matrix(model, frame) for name, model in self.models.items()}
            anomalies = self.anomaly.score_many(x_all)

            results: list[EnsembleResult | None] = []
            for i in range(len(feature_rows)):
                if not finite[i]:
                    results.append(None)
                    continue
                votes: list[ModelVote] = []
                for name, matrix in matrices.items():
                    model = self.models[name]
                    if matrix is None:
                        votes.append(ModelVote(model=name, p_put=0.0, p_neutral=1.0, p_call=0.0,
                                               weight=model.weight, vote="NO_TRADE", status="ERROR",
                                               detail="batch prediction failed"))
                    else:
                        votes.append(vote_from_probs(name, matrix[i], model.weight))
                try:
                    result = aggregate(votes, temperature=self.metadata.temperature)
                except ValueError:
                    results.append(None)
                    continue
                anomaly = anomalies[i]
                result.anomaly_flag = bool(anomaly.available and anomaly.is_anomaly)
                result.anomaly_detail = anomaly.detail
                result.provenance = {
                    "model_version": self.metadata.version,
                    "trained_at_utc": self.metadata.trained_at_utc,
                    "data_provenance": self.metadata.data_provenance,
                    "anomaly_detector_available": anomaly.available,
                    "anomaly_score": None if anomaly.score is None else round(anomaly.score, 4),
                    "models_used": [v.model for v in votes if v.status == "OK"],
                    "models_failed": [v.model for v in votes if v.status == "ERROR"],
                }
                results.append(result)
            return results

    def predict(self, feature_values: dict[str, float]) -> EnsembleResult:
        with self._lock:
            if not self.ready:
                raise ModelNotReady(self.last_error or "MODELS NOT TRAINED")
            names = self.metadata.feature_names
            try:
                row = {name: float(feature_values[name]) for name in names}
            except KeyError as exc:
                raise ModelNotReady("FEATURE CHECK FAILED", f"missing feature {exc}") from exc
            if not all(np.isfinite(value) for value in row.values()):
                raise ModelNotReady("FEATURE CHECK FAILED", "non-finite values in the feature vector")
            # keep the pandas column order/schema the estimators were fitted with
            frame = pd.DataFrame([row], columns=names)
            x = frame.to_numpy(dtype=float)

            votes = [self._predict_one(name, model, frame) for name, model in self.models.items()]

            neural_vote = self._neural_vote(x)
            if neural_vote is not None:
                votes.append(neural_vote)

            result = aggregate(votes, temperature=self.metadata.temperature)
            anomaly = self.anomaly.score(x)
            result.anomaly_flag = bool(anomaly.available and anomaly.is_anomaly)
            result.anomaly_detail = anomaly.detail
            result.provenance = {
                "model_version": self.metadata.version,
                "trained_at_utc": self.metadata.trained_at_utc,
                "data_provenance": self.metadata.data_provenance,
                "anomaly_detector_available": anomaly.available,
                "anomaly_score": None if anomaly.score is None else round(anomaly.score, 4),
                "models_used": [v.model for v in votes if v.status == "OK"],
                "models_failed": [v.model for v in votes if v.status == "ERROR"],
            }
            return result

    def _neural_vote(self, x: np.ndarray) -> ModelVote | None:
        if not self.neural.available:
            return None
        try:
            probs = self.neural.predict(x)
            if probs is None:
                return None
            return vote_from_probs("neural", probs, float(BASE_WEIGHTS.get("neural", 0.10)))
        except NotImplementedError:
            return ModelVote(model="neural", p_put=0.0, p_neutral=1.0, p_call=0.0, weight=0.0,
                             vote="NO_TRADE", status="NOT_AVAILABLE",
                             detail="Inference hook not implemented for this artifact")
        except Exception as exc:  # noqa: BLE001
            return ModelVote(model="neural", p_put=0.0, p_neutral=1.0, p_call=0.0, weight=0.0,
                             vote="NO_TRADE", status="ERROR", detail=str(exc)[:160])


_service: ModelService | None = None
_service_lock = threading.Lock()


def get_model_service(reload: bool = False) -> ModelService:
    global _service
    with _service_lock:
        if _service is None:
            _service = ModelService()
        elif reload:
            _service.load()
        return _service


def reset_model_service() -> None:
    global _service
    with _service_lock:
        _service = None
