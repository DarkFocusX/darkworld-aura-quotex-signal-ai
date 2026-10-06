"""Evaluation metrics — computed from stored predictions, never hard-coded."""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

CLASS_LABELS = ("PUT", "NEUTRAL", "CALL")


def confusion_matrix_3(y_true: Sequence[int], y_pred: Sequence[int]) -> list[list[int]]:
    matrix = np.zeros((3, 3), dtype=int)
    for t, p in zip(y_true, y_pred):
        ti, pi = int(t), int(p)
        if 0 <= ti <= 2 and 0 <= pi <= 2:
            matrix[ti, pi] += 1
    return matrix.tolist()


def classification_metrics(y_true: Sequence[int], y_pred: Sequence[int]) -> dict[str, Any]:
    yt = np.asarray(y_true, dtype=int)
    yp = np.asarray(y_pred, dtype=int)
    matrix = confusion_matrix_3(yt, yp)
    per_class: dict[str, dict[str, float | int]] = {}
    f1_scores: list[float] = []
    precisions: list[float] = []
    recalls: list[float] = []
    for cls in range(3):
        tp = int(((yt == cls) & (yp == cls)).sum())
        fp = int(((yt != cls) & (yp == cls)).sum())
        fn = int(((yt == cls) & (yp != cls)).sum())
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        support = int((yt == cls).sum())
        per_class[CLASS_LABELS[cls]] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support,
        }
        if support:
            f1_scores.append(f1)
            precisions.append(precision)
            recalls.append(recall)
    accuracy = float((yt == yp).mean()) if len(yt) else 0.0
    return {
        "accuracy": round(accuracy, 4),
        "macro_f1": round(float(np.mean(f1_scores)) if f1_scores else 0.0, 4),
        "macro_precision": round(float(np.mean(precisions)) if precisions else 0.0, 4),
        "macro_recall": round(float(np.mean(recalls)) if recalls else 0.0, 4),
        "per_class": per_class,
        "confusion_matrix": matrix,
        "samples": int(len(yt)),
    }


def roc_auc_ovr(y_true: Sequence[int], probabilities: np.ndarray) -> dict[str, float | None]:
    from sklearn.metrics import roc_auc_score

    yt = np.asarray(y_true, dtype=int)
    proba = np.asarray(probabilities, dtype=float)
    out: dict[str, float | None] = {}
    scores: list[float] = []
    for cls in range(3):
        binary = (yt == cls).astype(int)
        if binary.min() == binary.max():
            out[CLASS_LABELS[cls]] = None
            continue
        try:
            auc = float(roc_auc_score(binary, proba[:, cls]))
            out[CLASS_LABELS[cls]] = round(auc, 4)
            scores.append(auc)
        except ValueError:
            out[CLASS_LABELS[cls]] = None
    out["macro"] = round(float(np.mean(scores)), 4) if scores else None
    return out


def brier_score(y_true: Sequence[int], probabilities: np.ndarray) -> float:
    yt = np.asarray(y_true, dtype=int)
    proba = np.asarray(probabilities, dtype=float)
    onehot = np.zeros_like(proba)
    valid = (yt >= 0) & (yt <= 2)
    onehot[np.arange(len(yt))[valid], yt[valid]] = 1.0
    return round(float(np.mean(np.sum((proba - onehot) ** 2, axis=1))), 4)


def calibration_table(y_true: Sequence[int], probabilities: np.ndarray, bins: int = 10) -> dict[str, Any]:
    """Reliability of the *predicted class* confidence vs realised accuracy."""
    yt = np.asarray(y_true, dtype=int)
    proba = np.asarray(probabilities, dtype=float)
    confidence = proba.max(axis=1)
    predicted = proba.argmax(axis=1)
    correct = (predicted == yt).astype(float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows: list[dict[str, Any]] = []
    ece = 0.0
    total = len(yt)
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (confidence >= lo) & (confidence < hi if i < bins - 1 else confidence <= hi)
        count = int(mask.sum())
        if count == 0:
            rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "count": 0, "mean_confidence": None,
                         "accuracy": None, "gap": None})
            continue
        mean_conf = float(confidence[mask].mean())
        acc = float(correct[mask].mean())
        ece += (count / total) * abs(mean_conf - acc)
        rows.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "count": count,
            "mean_confidence": round(mean_conf, 4),
            "accuracy": round(acc, 4),
            "gap": round(mean_conf - acc, 4),
        })
    return {"bins": rows, "expected_calibration_error": round(float(ece), 4), "samples": total}


def fit_temperature(y_true: Sequence[int], probabilities: np.ndarray,
                    bounds: tuple[float, float] = (0.5, 3.0)) -> float:
    """1-D search for the temperature that minimises multiclass log-loss."""
    from scipy.optimize import minimize_scalar

    yt = np.asarray(y_true, dtype=int)
    proba = np.asarray(probabilities, dtype=float)
    if len(yt) < 50:
        return 1.0

    def nll(temperature: float) -> float:
        p = np.clip(proba, 1e-9, 1.0) ** (1.0 / temperature)
        p = p / p.sum(axis=1, keepdims=True)
        picked = p[np.arange(len(yt)), np.clip(yt, 0, 2)]
        return float(-np.mean(np.log(np.clip(picked, 1e-12, 1.0))))

    try:
        result = minimize_scalar(nll, bounds=bounds, method="bounded")
        if result.success and np.isfinite(result.x):
            return round(float(result.x), 4)
    except Exception:  # noqa: BLE001 - calibration is best-effort, never fabricated
        pass
    return 1.0


def directional_signal_stats(y_true: Sequence[int], directional_pred: Sequence[int],
                             probabilities: np.ndarray | None = None) -> dict[str, Any]:
    """Win rate of *tradable* (non-neutral) directional calls, plus streaks.

    A prediction is tradable when the model commits to PUT/CALL (0/2). Neutral
    outcomes in the label are counted as losses for the signal (the market did
    not move enough to pay), which keeps the reported win rate conservative.
    """
    yt = np.asarray(y_true, dtype=int)
    yp = np.asarray(directional_pred, dtype=int)
    tradable = np.isin(yp, [0, 2])
    if not tradable.any():
        return {"signals": 0, "wins": 0, "losses": 0, "win_rate": None,
                "max_losing_streak": 0, "average_confidence": None}
    results = (yt[tradable] == yp[tradable])
    wins = int(results.sum())
    losses = int((~results).sum())
    streak = 0
    max_streak = 0
    for won in results:
        streak = 0 if won else streak + 1
        max_streak = max(max_streak, streak)
    avg_conf = None
    if probabilities is not None:
        proba = np.asarray(probabilities, dtype=float)[tradable]
        chosen = np.where(yp[tradable] == 2, proba[:, 2], proba[:, 0])
        avg_conf = round(float(chosen.mean()), 4)
    return {
        "signals": int(tradable.sum()),
        "wins": wins,
        "losses": losses,
        "win_rate": round(100.0 * wins / max(1, wins + losses), 2),
        "max_losing_streak": max_streak,
        "average_confidence": avg_conf,
    }


def summarize_folds(folds: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [f[key] for f in folds if isinstance(f.get(key), (int, float))]
    if not values:
        return {"mean": None, "min": None, "max": None, "folds": 0}
    return {
        "mean": round(float(np.mean(values)), 4),
        "min": round(float(np.min(values)), 4),
        "max": round(float(np.max(values)), 4),
        "folds": len(values),
    }
