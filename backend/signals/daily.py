"""Daily mode — "Best signal of the day".

Two clearly separated outputs:

1. TODAY'S EMITTED SIGNAL
   The strongest *actually emitted* (future-dated, stored, tracked) signal of the
   current IST trading day. This is what counts for performance statistics.

2. DAY STUDY SCAN (replay)
   The strongest setup the pipeline *would* have qualified at any closed candle
   of the day so far, found by replaying today's candles through the exact same
   feature/model/validation pipeline. These entries have already passed, are not
   tradable, are never sent to Telegram and are excluded from performance stats.
   If the loaded model was trained on data that covers the scan period the result
   is additionally flagged `in_sample=true` (a leakage warning, not a win rate).

If nothing qualifies, both stay empty and the dashboard says NO TRADE — which is
the intended, honest outcome.
"""
from __future__ import annotations

import json
import threading
import time

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from ..constants import TRADABLE_MIN_QUALITY
from ..data.registry import registry
from ..database import db
from ..features.pipeline import FEATURE_NAMES, build_feature_frame, latest_features
from ..features.rules import (
    candle_score,
    freshness_score,
    momentum_score,
    regime_score,
    structure_score,
    trend_score,
    volatility_score,
)
from ..models.service import ModelNotReady, get_model_service
from ..signals.confidence import compute_confidence
from ..timeutil import ist_clock, ist_iso, to_ist, utc_iso, utc_now_naive
from .engine import STATUS_MODEL_NOT_READY, STATUS_NO_TRADE, STATUS_SIGNAL, get_engine


def _snapshot_series(df: pd.DataFrame) -> dict[str, Any]:
    """Vectorised rule inputs for every closed candle (all causal)."""
    from ..features import indicators as ind

    close = df["close"]
    ema9, ema21, ema50 = ind.ema(close, 9), ind.ema(close, 21), ind.ema(close, 50)
    atr14 = ind.atr(df, 14)
    macd_df = ind.macd(close)
    adx_df = ind.adx(df, 14)
    anatomy = ind.candle_anatomy(df)
    roll_low = df["low"].rolling(90, min_periods=30).min().shift(2)
    roll_high = df["high"].rolling(90, min_periods=30).max().shift(2)
    span = (roll_high - roll_low).replace(0, np.nan)
    feat = build_feature_frame(df)
    return {
        "df": df,
        "features": feat,
        "direction": np.sign(close.diff()).fillna(0.0),
        "ema_stack": np.where(ema9 > ema21, np.where(ema21 > ema50, 1.0, 0.0), np.where(ema21 < ema50, -1.0, 0.0)),
        "rsi7": ind.rsi(close, 7),
        "rsi14": ind.rsi(close, 14),
        "macd_hist": macd_df["hist"],
        "adx": adx_df["adx"],
        "atr14": atr14,
        "anatomy": anatomy,
        "eff10": ind.efficiency_ratio(close, 10),
        "roll_low": roll_low,
        "roll_high": roll_high,
        "range_position": ((close - roll_low) / span).clip(0, 1),
        "median_atr_pct": feat["atr_pct"].rolling(200, min_periods=60).median(),
    }


def _snapshot_at(series: dict[str, Any], i: int) -> tuple[dict[str, Any], dict[str, float]]:
    df = series["df"]
    row = df.iloc[i]
    prev = df.iloc[i - 1] if i >= 1 else row
    anatomy = series["anatomy"]
    price = float(row["close"])
    atr = float(series["atr14"].iloc[i]) or 1e-12
    stack = float(series["ema_stack"][i])
    sup = float(series["roll_low"].iloc[i]) if series["roll_low"].iloc[i] is not None else price
    res = float(series["roll_high"].iloc[i]) if series["roll_high"].iloc[i] is not None else price
    snapshot = {
        "candle_time_ist": to_ist(df.index[i].to_pydatetime()).strftime("%H:%M:%S"),
        "price": price,
        "atr14": round(atr, 6),
        "atr_pct": round(float(series["features"]["atr_pct"].iloc[i]) * 100, 4),
        "ema_stack": "BULL" if stack > 0 else "BEAR" if stack < 0 else "MIXED",
        "rsi7": float(series["rsi7"].iloc[i]),
        "rsi14": float(series["rsi14"].iloc[i]),
        "adx14": float(series["adx"].iloc[i]),
        "efficiency_ratio_10": float(series["eff10"].iloc[i]),
        "median_atr_pct_200": float(series["median_atr_pct"].iloc[i] or 0.0) * 100
        if not np.isnan(series["median_atr_pct"].iloc[i]) else 0.0,
        "last_candle": {
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": price,
            "direction": "UP" if row["close"] > row["open"] else "DOWN" if row["close"] < row["open"] else "FLAT",
            "body_ratio": float(anatomy["body_ratio"].iloc[i]),
            "upper_wick_ratio": float(anatomy["upper_wick_ratio"].iloc[i]),
            "lower_wick_ratio": float(anatomy["lower_wick_ratio"].iloc[i]),
            "close_pos": float(anatomy["close_pos"].iloc[i]),
        },
        "prev_candle": {
            "direction": "UP" if prev["close"] > prev["open"] else "DOWN" if prev["close"] < prev["open"] else "FLAT",
        },
        "support_resistance": {
            "nearest_support": sup,
            "nearest_resistance": res,
            "range_position": float(series["range_position"].iloc[i]) if not np.isnan(
                series["range_position"].iloc[i]) else 0.5,
            "support_distance_atr": (price - sup) / atr,
            "resistance_distance_atr": (res - price) / atr,
        },
    }
    values = {name: float(series["features"][name].iloc[i]) for name in FEATURE_NAMES}
    return snapshot, values


def study_scan(asset: str, *, day_ist: str | None = None, max_rows: int = 240,
               settings: Settings | None = None) -> dict[str, Any]:
    """Replay today's closed candles through the pipeline and return the best setup."""
    engine = get_engine()
    settings = settings or engine.settings
    model_service = get_model_service()
    today_ist = day_ist or to_ist(utc_now_naive().replace(tzinfo=None)).strftime("%Y-%m-%d")

    try:
        frame = registry.get_frame(asset, limit=max(300, max_rows + 120),
                                   data_source_mode=settings.data_source_mode, use_cache=False)
    except Exception as exc:  # noqa: BLE001
        return {"asset": asset, "status": "DATA_UNAVAILABLE", "reason": str(exc)[:200], "candidates": []}

    if not model_service.ready:
        return {"asset": asset, "status": STATUS_MODEL_NOT_READY,
                "reason": model_service.status().get("message"), "candidates": []}

    df = frame.df
    ist_index = df.index.tz_convert("Asia/Kolkata")
    day_mask = (ist_index.strftime("%Y-%m-%d") == today_ist)
    rows = np.where(day_mask)[0]
    if len(rows) == 0:
        return {"asset": asset, "status": "NO_DATA_TODAY", "reason": f"No closed candles for {today_ist} IST yet",
                "candidates": [], "feed": frame.meta.public_dict()}
    rows = rows[-max_rows:]

    series = _snapshot_series(df)
    provenance = model_service.metadata.data_provenance or {}
    period_end = provenance.get("period_end_utc")
    in_sample = bool(period_end and period_end >= f"{today_ist}T00:00:00")

    candidates: list[dict[str, Any]] = []
    evaluations = 0

    # Batch inference: one estimator pass for every eligible candle (was 1 call per row).
    prepared: list[tuple[int, dict[str, Any], dict[str, float]]] = []
    for i in rows:
        if i < 60:
            continue
        try:
            snapshot, values = _snapshot_at(series, i)
        except Exception:  # noqa: BLE001
            continue
        if not all(np.isfinite(v) for v in values.values()):
            continue
        prepared.append((i, snapshot, values))

    if not prepared:
        return {"asset": asset, "status": STATUS_NO_TRADE,
                "reason": "No candles today with a complete, finite feature vector",
                "candidates": [], "feed": frame.meta.public_dict()}

    try:
        ensembles = model_service.predict_many([values for _, _, values in prepared])
    except ModelNotReady:
        return {"asset": asset, "status": STATUS_MODEL_NOT_READY,
                "reason": model_service.status().get("message"), "candidates": []}

    for (i, snapshot, values), ensemble in zip(prepared, ensembles):
        if ensemble is None:
            continue
        evaluations += 1
        direction = ensemble.direction or "CALL"
        rule_scores = {
            "trend": trend_score(snapshot, values, direction),
            "momentum": momentum_score(snapshot, values, direction),
            "candle": candle_score(snapshot, values, direction),
            "structure": structure_score(snapshot, values, direction),
            "volatility": volatility_score(snapshot, values),
            "regime": regime_score(snapshot, values, direction, ensemble.anomaly_flag),
            "freshness": freshness_score(0.0, frame.meta.realtime, 180.0),
        }
        breakdown = compute_confidence(ensemble=ensemble, rule_scores=rule_scores,
                                       freshness=rule_scores["freshness"])
        if breakdown.confidence < settings.min_confidence or breakdown.quality not in {"VERY_HIGH", "HIGH"}:
            continue
        from ..validation.checks import consistency_check

        consistency = consistency_check(direction, breakdown.components)
        if not consistency.passed:
            continue
        entry = df.index[i].to_pydatetime()
        candidates.append({
            "asset": asset,
            "entry_time_ist": ist_clock(entry),
            "direction": direction,
            "confidence": round(breakdown.confidence, 2),
            "quality": breakdown.quality,
            "quality_label": breakdown.quality_label,
            "model_agreement": round(ensemble.agreement, 4),
            "p_neutral": round(ensemble.p_neutral, 4),
            "components": {k: v["score"] for k, v in breakdown.components.items()},
            "outcome": _replay_outcome(df, i, direction, settings.expiry_minutes),
        })

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    best = candidates[0] if candidates else None
    model_status = model_service.status()
    market_ready = bool(model_status.get("market_ready"))
    feed_is_real = not frame.meta.is_simulated
    production_eligible = bool(market_ready and feed_is_real)
    if best:
        best["production_eligible"] = production_eligible
        best["eligibility_note"] = (
            "Market model + real feed: this candle would have met the production requirements."
            if production_eligible else
            "NOT production-eligible: "
            + ("" if market_ready else "the loaded model is not READY_MARKET. ")
            + ("" if feed_is_real else "the feed is not real market data.")
        )
    return {
        "asset": asset,
        "production_eligible": production_eligible,
        "market_ready": market_ready,
        "model_state": model_status.get("state"),
        "feed_is_real_market_data": feed_is_real,
        "status": STATUS_SIGNAL if best else STATUS_NO_TRADE,
        "day_ist": today_ist,
        "candles_evaluated": evaluations,
        "qualified": len(candidates),
        "best": best,
        "candidates": candidates[:8],
        "in_sample": in_sample,
        "in_sample_note": (
            "Model training data covers this period — results are in-sample and must not be read as performance."
            if in_sample else
            "Model training data does not cover this period (out-of-sample replay)."
        ),
        "feed": frame.meta.public_dict(),
        "note": (
            "Study replay of already-closed candles. These entries have passed, are not tradable, "
            "are not sent to Telegram and are excluded from performance statistics."
            + ("" if production_eligible else
               " This replay is NOT production-eligible: it ran on "
               + ("simulated" if frame.meta.is_simulated else "non-market")
               + " data and/or a non-market model, so it is a pipeline demonstration only.")
        ),
    }


def _replay_outcome(df: pd.DataFrame, i: int, direction: str, expiry_minutes: int) -> dict[str, Any]:
    """Outcome of the replayed candle using verified candles (WIN/LOSS/VOID/PENDING)."""
    exit_index = i + expiry_minutes - 1
    if exit_index >= len(df):
        return {"result": "PENDING", "reason": "expiry candle not closed yet"}
    entry_price = float(df["open"].iloc[i])
    exit_price = float(df["close"].iloc[exit_index])
    if exit_price == entry_price:
        return {"result": "VOID", "entry_price": entry_price, "exit_price": exit_price,
                "reason": "price unchanged at expiry"}
    won = (exit_price > entry_price) if direction == "CALL" else (exit_price < entry_price)
    return {
        "result": "WIN" if won else "LOSS",
        "entry_price": entry_price,
        "exit_price": exit_price,
        "reason": f"open {entry_price:.5f} → close {exit_price:.5f}",
    }


# --- study replay cache ------------------------------------------------------
# The daily panel auto-refreshes; replaying a day of candles on every poll burned
# CPU for no new information. Results are cached for a few seconds and the cache
# is cleared whenever something could change them (new signal, config, model).
_STUDY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STUDY_CACHE_TTL_SECONDS = 45.0
_STUDY_CACHE_LOCK = threading.Lock()


def clear_study_cache() -> None:
    """Invalidate the daily study cache (called on new signals, config changes, model reload)."""
    with _STUDY_CACHE_LOCK:
        _STUDY_CACHE.clear()


def _cache_get(key: str) -> dict[str, Any] | None:
    with _STUDY_CACHE_LOCK:
        entry = _STUDY_CACHE.get(key)
    if entry is None:
        return None
    stamped, payload = entry
    if time.monotonic() - stamped > _STUDY_CACHE_TTL_SECONDS:
        return None
    return json.loads(json.dumps(payload, default=str))


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    with _STUDY_CACHE_LOCK:
        if len(_STUDY_CACHE) > 16:
            _STUDY_CACHE.clear()
        _STUDY_CACHE[key] = (time.monotonic(), payload)


def daily_status(asset: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Combine today's emitted signals with the replay study scan."""
    engine = get_engine()
    settings = engine.settings
    now = now or utc_now_naive()
    today_ist = to_ist(now).strftime("%Y-%m-%d")

    cache_key = "|".join(str(part) for part in (
        (asset or "AUTO").upper(), today_ist, settings.min_confidence, settings.expiry_minutes,
        settings.data_source_mode, settings.selected_asset,
    ))
    cached = _cache_get(cache_key)
    if cached is not None:
        cached["cached"] = True
        return cached

    today_signals = db.list_history(limit=400, day_ist=today_ist)
    best_emitted = max(today_signals, key=lambda s: s.get("confidence") or 0.0) if today_signals else None

    if asset and asset.upper() != "AUTO":
        assets = [asset]
    elif settings.selected_asset not in (None, "AUTO"):
        assets = [settings.selected_asset]
    else:
        assets = list(settings.enabled_assets)
    studies = []
    for symbol in assets[:4]:
        studies.append(study_scan(symbol, day_ist=today_ist, settings=settings))
    ranked_studies = sorted(
        [s for s in studies if s.get("best")], key=lambda s: s["best"]["confidence"], reverse=True
    )
    best_study = ranked_studies[0] if ranked_studies else None

    existing_daily = db.get_daily_best(today_ist)
    locked_record = None
    if existing_daily and existing_daily.get("status") == "EMITTED":
        # SPEC §7: once a qualified signal has been selected for the day, the
        # selection is LOCKED. A later, weaker signal never replaces it.
        locked_record = existing_daily

    if locked_record:
        status = "SIGNAL"
        reason = (
            "Daily selection locked — the best qualified signal of the day was already "
            f"selected at {locked_record.get('entry_time_ist')} IST and is not replaced by later signals."
        )
    elif best_emitted:
        status = "SIGNAL"
        reason = None
    elif best_study:
        status = "STUDY_ONLY"
        reason = (
            "No tradable signal was emitted today. The study replay below found a candle that "
            "would have passed the gates — it is historical, not tradable, and is excluded from "
            "performance statistics."
        )
    else:
        status = "NO_TRADE"
        reason = "No qualified setup found today — the engine rejected every scanned candle."

    payload = {
        "status": status,
        "day_ist": today_ist,
        "reason": reason,
        "min_confidence": settings.min_confidence,
        "emitted_today": {
            "count": len(today_signals),
            "best": {
                "asset": best_emitted.get("asset"),
                "direction": best_emitted.get("direction"),
                "confidence": best_emitted.get("confidence"),
                "entry_time_ist": best_emitted.get("entry_time_ist"),
                "quality": best_emitted.get("quality"),
                "result": best_emitted.get("result"),
                "is_simulated": best_emitted.get("is_simulated"),
            } if best_emitted else None,
        },
        "study": {
            "best": best_study["best"] if best_study else None,
            "asset": best_study["asset"] if best_study else None,
            "candidates": best_study["candidates"][:5] if best_study else [],
            "in_sample": best_study["in_sample"] if best_study else None,
            "in_sample_note": best_study["in_sample_note"] if best_study else None,
            "scanned_assets": [s.get("asset") for s in studies],
            "note": (
                "Study replay of today's already-closed candles. Past candles cannot be traded; "
                "these results are excluded from performance statistics."
            ),
        },
        "daily_best_record": locked_record or existing_daily,
        "locked": bool(locked_record),
        "lock_note": (
            "One best qualified signal per day WHEN A SETUP EXISTS. If nothing qualifies, "
            "the honest outcome is NO TRADE for the day — no signal is forced."
        ),
        "environment": settings.environment,
    }
    if locked_record:
        payload["daily_best_record"] = locked_record
    elif best_emitted:
        db.upsert_daily_best(today_ist, {
            "signal_uid": best_emitted.get("signal_uid"),
            "asset": best_emitted.get("asset"),
            "direction": best_emitted.get("direction"),
            "confidence": best_emitted.get("confidence"),
            "entry_time_ist": best_emitted.get("entry_time_ist"),
            "status": "EMITTED",
            "reason": None,
        })
    elif best_study:
        db.upsert_daily_best(today_ist, {
            "signal_uid": None,
            "asset": best_study["asset"],
            "direction": best_study["best"]["direction"],
            "confidence": best_study["best"]["confidence"],
            "entry_time_ist": best_study["best"]["entry_time_ist"],
            "status": "STUDY_ONLY",
            "reason": "study replay (already closed)",
        })
    payload["cached"] = False
    _cache_put(cache_key, payload)
    return payload
