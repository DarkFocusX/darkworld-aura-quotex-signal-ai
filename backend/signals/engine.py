"""Signal engine.

ONE scan → ONE outcome. The engine never forces a direction:

    1. fetch the newest verified candles for the asset(s)
    2. verify freshness / completeness            (DATA CHECK)
    3. compute the M1 feature block               (FEATURE CHECK)
    4. run the model ensemble                     (MODEL CHECK)
    5. score the rule context (trend/momentum/candle/structure/vol/regime/freshness)
    6. weighted confidence → threshold            (CONFIDENCE CHECK)
    7. consistency + regime gates                 (REGIME / CONSISTENCY CHECK)
    8. risk policy + per-candle lock
    9. emit at most ONE signal for the next candle, or report NO TRADE with the
       exact reason and the next valid scan time

Everything rejected is logged with its reasons, so the dashboard can show why.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import Settings
from ..constants import (
    DISCLAIMER,
    ENV_DEVELOPMENT,
    GATES,
    TIMEZONE,
    TRADABLE_MIN_QUALITY,
    canonical_symbol,
)
from ..data import provenance
from ..data.registry import registry
from ..data.base import MarketDataUnavailable, data_lag_seconds
from ..database import db
from ..execution import get_execution_adapter
from ..features.pipeline import FeatureError, FeatureSnapshot, latest_features
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
from ..risk.policy import RiskPolicy
from .policy import evaluate_eligibility
from ..settings_service import get_settings
from ..telegram import notifier
from ..timeutil import (
    floor_minute,
    ist_clock,
    ist_iso,
    now_ist,
    now_utc,
    seconds_to_next_minute,
    to_ist,
    to_utc,
    utc_iso,
)
from ..validation.checks import (
    confidence_check,
    consistency_check,
    data_check,
    feature_check,
    max_lag_for,
    model_check,
    regime_check,
    run_all_checks,
)

STATUS_SIGNAL = "SIGNAL"
STATUS_NO_TRADE = "NO_TRADE"
STATUS_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
STATUS_MODEL_NOT_READY = "MODEL_NOT_READY"
STATUS_LOCKED = "LOCKED"
STATUS_ERROR = "ERROR"

_CANDLE_LIMIT = 300


@dataclass
class AssetScan:
    asset: str
    status: str
    reason: str | None = None
    detail: str | None = None
    confidence: float | None = None
    quality: str | None = None
    direction: str | None = None
    signal: dict[str, Any] | None = None
    checks: dict[str, Any] | None = None
    components: dict[str, Any] | None = None
    ensemble: dict[str, Any] | None = None
    feed: dict[str, Any] | None = None
    features: dict[str, Any] | None = None
    entry_time_ist: str | None = None
    expiry_time_ist: str | None = None
    next_valid_scan_ist: str | None = None
    lock: dict[str, Any] | None = None
    eligibility: dict[str, Any] | None = None
    data_validation: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "confidence": None if self.confidence is None else round(self.confidence, 2),
            "quality": self.quality,
            "direction": self.direction,
            "signal": self.signal,
            "checks": self.checks,
            "components": self.components,
            "ensemble": self.ensemble,
            "feed": self.feed,
            "features": self.features,
            "entry_time_ist": self.entry_time_ist,
            "expiry_time_ist": self.expiry_time_ist,
            "next_valid_scan_ist": self.next_valid_scan_ist,
            "lock": self.lock,
            "eligibility": self.eligibility,
            "data_validation": self.data_validation,
        }


def _feed_payload(meta) -> dict[str, Any]:
    payload = meta.public_dict()
    payload["latest_candle_ist"] = ist_iso(meta.latest_candle_open_utc) if meta.latest_candle_open_utc else None
    return payload


class SignalEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        # An explicitly injected settings object is "pinned" (used by tests and by
        # embedders); otherwise the engine re-reads the live configuration so that
        # POST /api/config takes effect without a restart.
        self._pinned = settings is not None
        self._settings = settings or get_settings()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ plumbing
    @property
    def settings(self) -> Settings:
        return self._settings

    def refresh_settings(self) -> Settings:
        if self._pinned:
            return self._settings
        with self._lock:
            self._settings = get_settings()
        return self._settings

    def model_service(self):
        return get_model_service()

    # -------------------------------------------------------------------- session
    def session(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or now_utc()
        nxt = floor_minute(now) + timedelta(minutes=1)
        return {
            "ist_time": ist_clock(now),
            "ist_date": to_ist(now).strftime("%d %b %Y"),
            "utc_time": utc_iso(now),
            "timezone": TIMEZONE,
            "current_candle_ist": ist_clock(floor_minute(now)),
            "next_candle_ist": ist_clock(nxt),
            "seconds_to_next_candle": round(seconds_to_next_minute(now), 1),
        }

    def lock_info(self, asset: str, entry_utc: datetime) -> dict[str, Any]:
        entry_key = utc_iso(entry_utc)
        existing = db.signal_exists_for_candle(asset, entry_key)
        nxt = entry_utc + timedelta(minutes=1)
        return {
            "locked": bool(existing),
            "candle_ist": ist_clock(entry_utc),
            "next_valid_signal_ist": ist_clock(nxt),
            "existing_signal_uid": existing.get("signal_uid") if existing else None,
            "existing_confidence": existing.get("confidence") if existing else None,
        }

    # ---------------------------------------------------------------- scan a single asset
    def scan_asset(self, symbol: str, *, now: datetime | None = None,
                   mode: str = "NOW", persist: bool = True) -> AssetScan:
        now = now or now_utc()
        settings = self._settings
        policy = RiskPolicy(settings)

        # --- 1. data ---------------------------------------------------------
        try:
            frame = registry.get_frame(symbol, limit=_CANDLE_LIMIT,
                                       data_source_mode=settings.data_source_mode,
                                       use_cache=False, now=now)
        except MarketDataUnavailable as exc:
            scan = AssetScan(asset=symbol, status=STATUS_DATA_UNAVAILABLE, reason=exc.reason, detail=exc.detail)
            if persist:
                db.log_scan(asset=symbol, status=scan.status, reason=scan.reason,
                            details={"provider": exc.provider, "detail": exc.detail},
                            scanned_at_utc=now, scanned_at_ist=ist_clock(now), scan_mode=mode)
            return scan
        except Exception as exc:  # noqa: BLE001
            scan = AssetScan(asset=symbol, status=STATUS_ERROR,
                             reason=f"{type(exc).__name__}: {str(exc)[:180]}",
                             detail="Unexpected data-layer failure")
            if persist:
                db.log_scan(asset=symbol, status=scan.status, reason=scan.reason,
                            scanned_at_utc=now, scanned_at_ist=ist_clock(now), scan_mode=mode)
            return scan

        feed = _feed_payload(frame.meta)
        # The freshness budget is judged against the scan's own clock. For a live scan
        # that clock is UTC now (identical to the old behaviour); for a chronological
        # replay it is the minute being replayed, which is what makes a historical
        # replay honest — the budget itself is never changed.
        lag = data_lag_seconds(frame.meta, now)

        # --- 1b. production refuses synthetic feeds outright (spec §1, §5) ----
        # In production an instrument with no real feed must read DATA UNAVAILABLE;
        # it must never be analysed from a simulated/synthetic series. Development
        # keeps the labelled simulated feed so the pipeline stays demonstrable.
        if settings.environment != ENV_DEVELOPMENT and not frame.meta.is_market_data:
            reason = (
                f"No verified market data for {symbol}: the configured chain fell back to "
                f"'{frame.meta.provider}' ({frame.meta.source_label}). Production requires real "
                "operator data — import candles or connect a provider."
            )
            scan = AssetScan(
                asset=symbol,
                status=STATUS_DATA_UNAVAILABLE,
                reason=reason,
                detail=f"provider={frame.meta.provider} | source_class={frame.meta.source_class}",
                feed=feed,
                next_valid_scan_ist=ist_clock(floor_minute(now) + timedelta(minutes=1)),
            )
            if persist:
                self._log(symbol, scan, frame, now, mode)
            return scan

        # --- 2. data check (short-circuit: never analyse candles we cannot trust)
        data_result = data_check(frame, lag, max_lag_for(frame.meta))
        if not data_result.passed:
            scan = AssetScan(
                asset=symbol,
                status=STATUS_DATA_UNAVAILABLE,
                reason=data_result.detail,
                feed=feed,
                checks={"checks": [data_result.as_dict()], "passed": False,
                        "blocking": ["DATA CHECK"], "reasons": [data_result.detail]},
                next_valid_scan_ist=ist_clock(floor_minute(now) + timedelta(minutes=1)),
            )
            if persist:
                self._log(symbol, scan, frame, now, mode)
            return scan

        # --- 3. features -----------------------------------------------------
        try:
            snapshot: FeatureSnapshot = latest_features(frame.df, registry.asset(symbol))
        except FeatureError as exc:
            scan = AssetScan(asset=symbol, status=STATUS_NO_TRADE,
                             reason=f"Feature check failed: {exc.reason}", detail=exc.detail,
                             feed=feed, checks={"checks": [feature_check({}, []).as_dict()], "passed": False,
                                                "blocking": ["FEATURE CHECK"], "reasons": [exc.reason]})
            if persist:
                db.log_scan(asset=symbol, status=scan.status, reason=scan.reason, details={"feed": feed},
                            scanned_at_utc=now, scanned_at_ist=ist_clock(now),
                            feed_source=frame.meta.provider, is_simulated=frame.meta.is_simulated, scan_mode=mode)
            return scan

        features_payload = snapshot.as_dict()

        # --- 4. model --------------------------------------------------------
        model_service = self.model_service()
        model_status = model_service.status()
        try:
            ensemble = model_service.predict(snapshot.values)
        except ModelNotReady as exc:
            scan = AssetScan(asset=symbol, status=STATUS_MODEL_NOT_READY, reason=exc.reason, detail=exc.detail,
                             feed=feed, features=features_payload,
                             checks={"checks": [{"check": "MODEL CHECK", "passed": False,
                                                 "severity": "BLOCK", "detail": exc.reason,
                                                 "evidence": model_status}],
                                     "passed": False, "blocking": ["MODEL CHECK"], "reasons": [exc.reason]})
            if persist:
                db.log_scan(asset=symbol, status=scan.status, reason=scan.reason, details={"feed": feed},
                            scanned_at_utc=now, scanned_at_ist=ist_clock(now),
                            feed_source=frame.meta.provider, is_simulated=frame.meta.is_simulated, scan_mode=mode)
            return scan
        except Exception as exc:  # noqa: BLE001
            scan = AssetScan(asset=symbol, status=STATUS_MODEL_NOT_READY,
                             reason=f"Model inference error: {type(exc).__name__}", detail=str(exc)[:180],
                             feed=feed, features=features_payload)
            if persist:
                db.log_scan(asset=symbol, status=scan.status, reason=scan.reason,
                            scanned_at_utc=now, scanned_at_ist=ist_clock(now), scan_mode=mode)
            return scan

        direction = ensemble.direction or "CALL"

        # --- 5. rule context + confidence -----------------------------------
        extras = snapshot.extras
        rule_scores = {
            "trend": trend_score(extras, snapshot.values, direction),
            "momentum": momentum_score(extras, snapshot.values, direction),
            "candle": candle_score(extras, snapshot.values, direction),
            "structure": structure_score(extras, snapshot.values, direction),
            "volatility": volatility_score(extras, snapshot.values),
            "regime": regime_score(extras, snapshot.values, direction, ensemble.anomaly_flag),
            "freshness": freshness_score(lag, frame.meta.realtime, max_lag_for(frame.meta)),
        }
        from .confidence import compute_confidence, quality_for

        breakdown = compute_confidence(ensemble=ensemble, rule_scores=rule_scores,
                                       freshness=rule_scores["freshness"])
        components_payload = breakdown.as_dict()

        # --- 6. validation checks -------------------------------------------
        checks = run_all_checks(
            data=data_result,
            model=model_check(ensemble, model_status),
            features=feature_check(snapshot.values, model_service.metadata.feature_names),
            regime=regime_check(breakdown.components, extras),
            confidence=confidence_check(breakdown.confidence, settings.min_confidence),
            consistency=consistency_check(direction, breakdown.components),
        )

        entry_utc = policy.resolve_entry_time(now)
        lock = self.lock_info(symbol, entry_utc)
        next_scan_ist = ist_clock(floor_minute(now) + timedelta(minutes=1))

        # --- 6b. hard production eligibility policy (spec §5) ----------------
        eligibility = evaluate_eligibility(
            settings=settings,
            feed=feed,
            checks=checks,
            confidence=breakdown.confidence,
            quality=breakdown.quality,
            ensemble=ensemble.to_dict(),
            lock=lock,
            candles=int(len(frame.df)),
            freshness_seconds=lag,
            model_status=model_status,
            data_validation=data_result.evidence,
        )

        base = AssetScan(
            asset=symbol,
            status=STATUS_NO_TRADE,
            direction=direction,
            confidence=breakdown.confidence,
            quality=breakdown.quality,
            feed=feed,
            features=features_payload,
            checks=checks,
            components=components_payload,
            ensemble=ensemble.to_dict(),
            entry_time_ist=ist_clock(entry_utc),
            expiry_time_ist=ist_clock(entry_utc + timedelta(minutes=settings.expiry_minutes)),
            next_valid_scan_ist=next_scan_ist,
            lock=lock,
            eligibility=eligibility.as_dict(),
            data_validation=data_result.evidence,
        )

        if not eligibility.allowed:
            # A candle that is still locked is reported as LOCKED (spec §17 E), not as a
            # generic NO TRADE — the setup was qualified, a signal simply already exists.
            other_blockers = [r.name for r in eligibility.requirements
                              if not r.passed and r.name != "SIGNAL LOCK PASSES"]
            if lock["locked"] and not other_blockers:
                base.status = STATUS_LOCKED
                base.reason = (
                    f"Signal locked — a signal already exists for the {lock['candle_ist']} IST candle"
                )
                base.next_valid_scan_ist = lock.get("next_valid_signal_ist") or base.next_valid_scan_ist
                if persist:
                    self._log(symbol, base, frame, now, mode)
                return base
            base.status = STATUS_NO_TRADE
            base.reason = eligibility.reason
            base.detail = "; ".join(
                f"{r.name}: {r.detail}" for r in eligibility.requirements if not r.passed
            )
            if persist:
                self._log(symbol, base, frame, now, mode)
            return base

        blocking_reasons = list(checks["reasons"])
        if lock["locked"]:
            base.status = STATUS_LOCKED
            base.reason = f"Signal locked — a signal already exists for the {lock['candle_ist']} IST candle"
            base.next_valid_scan_ist = lock["next_valid_signal_ist"]
            if persist:
                self._log(symbol, base, frame, now, mode)
            return base

        risk = policy.evaluate(
            now=now,
            signals_today_dates=[s.get("entry_date_ist") for s in self._todays_signals_ist()],
            existing_signal_for_candle=False,
        )
        blocking_reasons.extend(risk.reasons)
        base.checks = dict(checks)
        base.checks["risk"] = risk.as_dict()

        if blocking_reasons:
            base.status = STATUS_NO_TRADE
            base.reason = blocking_reasons[0]
            base.detail = "; ".join(blocking_reasons[1:]) if len(blocking_reasons) > 1 else None
            if persist:
                self._log(symbol, base, frame, now, mode)
            return base

        # --- 7. emit ---------------------------------------------------------
        signal = self._build_signal(symbol, ensemble, breakdown, snapshot, frame, entry_utc, settings, mode)
        try:
            if persist:
                db.save_signal(signal)
                # Provenance (§9): attach the ledger columns to the row we just wrote, so a
                # manual signal carries exactly what an automatic scan would (provider, feed
                # status, model/feature versions, window fingerprint, expiry). A failure here
                # is recorded as an event — it is never swallowed silently.
                try:
                    from . import ledger as ledger_module
                    ledger_module.attach_metadata(
                        signal["signal_uid"],
                        feed_provider=signal.get("feed_provider"),
                        feed_status=signal.get("feed_status") or signal.get("feed_badge"),
                        model_version=signal.get("model_version"),
                        dataset_version=signal.get("dataset_version"),
                        feature_version=signal.get("feature_version"),
                        expiry_time_utc=signal.get("expiry_time_utc"),
                    )
                except Exception as exc:  # noqa: BLE001
                    db.log_event("PROVENANCE",
                                 f"ledger provenance could not be attached to {signal.get('signal_uid')}: "
                                 f"{type(exc).__name__}: {str(exc)[:140]}", level="ERROR",
                                 meta={"signal_uid": signal.get("signal_uid")})
                # a new signal changes today's best -> drop the cached daily replay
                from .daily import clear_study_cache
                clear_study_cache()
        except sqlite3.IntegrityError:
            base.status = STATUS_LOCKED
            base.reason = f"Signal locked — {symbol} already has a signal for the {base.entry_time_ist} IST candle"
            base.next_valid_scan_ist = ist_clock(entry_utc + timedelta(minutes=1))
            if persist:
                self._log(symbol, base, frame, now, mode)
            return base
        except Exception as exc:  # noqa: BLE001 — database failure must not create a signal
            base.status = STATUS_ERROR
            base.reason = f"Database failure while storing the signal: {type(exc).__name__}"
            base.detail = str(exc)[:180]
            db.log_scan(asset=symbol, status=base.status, reason=base.reason,
                        scanned_at_utc=now, scanned_at_ist=ist_clock(now), scan_mode=mode)
            return base

        execution = get_execution_adapter(settings.mode)
        try:
            execution_result = execution.place_order(signal).as_dict()
        except NotImplementedError as exc:
            execution_result = {"status": "NOT_IMPLEMENTED", "adapter": execution.name, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            execution_result = {"status": "ERROR", "adapter": execution.name,
                                "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}

        # Telegram is gated on READY_MARKET + REAL DATA + QUALIFIED (spec §11).
        signal["model_market_ready"] = bool(model_status.get("market_ready"))
        signal["environment"] = settings.environment
        signal["model_version"] = model_status.get("version")
        signal["mode"] = settings.mode
        telegram_result = notifier.send_signal(
            signal,
            enabled=settings.telegram_enabled,
            allow_simulated=settings.telegram_allow_simulated,
            market_ready=bool(model_status.get("market_ready")),
            environment=settings.environment,
            mode=settings.mode,
            qualified=eligibility.allowed,
        )
        if persist:
            db.update_telegram_status(signal["signal_uid"], telegram_result.status)

        base.status = STATUS_SIGNAL
        base.signal = signal
        base.reason = None
        base.confidence = breakdown.confidence
        base.quality = breakdown.quality
        base.checks = {**checks, "risk": risk.as_dict(),
                       "execution": execution_result, "telegram": telegram_result.as_dict()}
        signal["execution"] = execution_result
        signal["telegram"] = telegram_result.as_dict()
        if persist:
            self._log(symbol, base, frame, now, mode)
        return base

    # ------------------------------------------------------------------ helpers
    def _todays_signals_ist(self) -> list[dict[str, Any]]:
        day = to_ist(now_utc()).strftime("%Y-%m-%d")
        return db.list_history(limit=400, day_ist=day)

    def _build_signal(self, symbol, ensemble, breakdown, snapshot, frame, entry_utc, settings, mode) -> dict[str, Any]:
        spec = registry.asset(symbol)
        entry_ist = ist_clock(entry_utc)                       # "06:30:00" — what the UI/Telegram display
        entry_date_ist = to_ist(entry_utc).strftime("%Y-%m-%d")  # IST trading day, used for daily filters/caps
        expiry_utc = entry_utc + timedelta(minutes=settings.expiry_minutes)
        quality_code, quality_label = breakdown.quality, breakdown.quality_label
        price = frame.live_price if frame.live_price is not None else snapshot.price
        # Provenance (§9): the artifact versions that produced this signal plus a
        # deterministic fingerprint of the exact window it was served from. A missing
        # version stays None — nothing is guessed.
        try:
            status = self.model_service().status() or {}
        except Exception:  # noqa: BLE001 — provenance must never block an emission
            status = {}
        try:
            first_open = utc_iso(frame.df.index[0].to_pydatetime()) if len(frame.df) else None
        except Exception:  # noqa: BLE001
            first_open = None
        window_id = provenance.window_version(frame.meta, rows=len(frame.df), first_open_utc=first_open)
        return {
            "signal_uid": f"{symbol}|{utc_iso(entry_utc)}",
            "asset": symbol,
            "flag": spec.flag,
            "label": spec.label,
            "timeframe": settings.timeframe,
            "direction": ensemble.direction,
            "confidence": round(breakdown.confidence, 2),
            "quality": quality_code,
            "quality_label": quality_label,
            "entry_time_utc": utc_iso(entry_utc),
            "entry_time_ist": entry_ist,
            "entry_time_ist_full": ist_iso(entry_utc),
            "entry_date_ist": entry_date_ist,
            "expiry_minutes": settings.expiry_minutes,
            "expiry_time_ist": ist_clock(expiry_utc),
            "expiry_time_utc": utc_iso(expiry_utc),
            "data_timestamp_utc": frame.meta.latest_candle_close_utc,
            "data_timestamp_ist": ist_iso(frame.meta.latest_candle_close_utc),
            "price": price,
            "model_probability": round(ensemble.direction_probability, 4),
            "model_agreement": round(ensemble.agreement, 4),
            "model_version": status.get("version"),
            "feature_version": status.get("feature_version"),
            "dataset_version": window_id,
            "anomaly_flag": bool(ensemble.anomaly_flag),
            "feed_source": frame.meta.source_label,
            "feed_mode": frame.meta.mode,
            "feed_provider": frame.meta.provider,
            "feed_status": frame.meta.source_status,
            "feed_badge": frame.meta.badge,
            "source_class": frame.meta.source_class,
            "source_status": frame.meta.source_status,
            "is_simulated": bool(frame.meta.is_simulated),
            "is_replay": bool(frame.meta.is_replay),
            "is_real_market_data": frame.meta.is_market_data,
            "data_timestamp_last_update": frame.meta.last_update_utc,
            "realtime": frame.meta.realtime,
            "components": breakdown.components,
            "model_votes": ensemble.to_dict(),
            "checks": None,
            "status": "OPEN",
            "result": "PENDING",
            "result_reason": None,
            "scan_mode": mode,
            "created_at_utc": utc_iso(now_utc()),
            "timezone": TIMEZONE,
            "environment": settings.environment,
            "disclaimer": DISCLAIMER,
        }

    def _log(self, symbol: str, scan: AssetScan, frame, now: datetime, mode: str) -> None:
        try:
            db.log_scan(
                asset=symbol,
                status=scan.status,
                reason=scan.reason,
                confidence=scan.confidence,
                details={
                    "quality": scan.quality,
                    "direction": scan.direction,
                    "feed": scan.feed,
                    "components": (scan.components or {}).get("components"),
                    "checks": (scan.checks or {}).get("checks"),
                },
                scanned_at_utc=now,
                scanned_at_ist=ist_clock(now),
                feed_source=frame.meta.provider if frame else None,
                is_simulated=bool(frame.meta.is_simulated) if frame else False,
                scan_mode=mode,
            )
        except Exception:  # noqa: BLE001 — logging must never break scanning
            pass

    # ------------------------------------------------------------------ generate
    def target_assets(self, requested: str | None = None) -> list[str]:
        settings = self._settings
        if requested and str(requested).upper() != "AUTO":
            canonical = canonical_symbol(str(requested))
            return [canonical or str(requested)]
        if settings.selected_asset and settings.selected_asset.upper() != "AUTO":
            canonical = canonical_symbol(settings.selected_asset)
            return [canonical or settings.selected_asset]
        if settings.auto_select:
            enabled = [a for a in settings.enabled_assets if a]
            return enabled or list(settings.enabled_assets)
        # AUTO with auto-select disabled: fall back to the first enabled asset
        enabled = [a for a in settings.enabled_assets if a]
        return enabled[:1] or list(settings.enabled_assets)[:1]

    def generate(self, asset: str | None = None, *, mode: str = "NOW",
                 now: datetime | None = None, persist: bool = True) -> dict[str, Any]:
        now = now or now_utc()
        settings = self.refresh_settings()
        assets = self.target_assets(asset)
        scans: list[AssetScan] = []

        for symbol in assets:
            try:
                scans.append(self.scan_asset(symbol, now=now, mode=mode, persist=persist))
            except Exception as exc:  # noqa: BLE001 — one bad asset must not kill the scan
                scans.append(AssetScan(asset=symbol, status=STATUS_ERROR,
                                       reason=f"{type(exc).__name__}: {str(exc)[:180]}"))

        signals = [s for s in scans if s.status == STATUS_SIGNAL and s.signal]
        best = max(signals, key=lambda s: s.confidence or 0.0) if signals else None
        ranked = sorted(
            [s for s in scans if s.confidence is not None],
            key=lambda s: s.confidence or 0.0,
            reverse=True,
        )
        primary = best or (ranked[0] if ranked else (scans[0] if scans else None))

        session = self.session(now)
        scanned_count = len(scans)
        qualified = [s for s in scans if s.status == STATUS_SIGNAL]
        unavailable = [s for s in scans if s.status == STATUS_DATA_UNAVAILABLE]
        locked = [s for s in scans if s.status == STATUS_LOCKED]
        not_ready = [s for s in scans if s.status == STATUS_MODEL_NOT_READY]
        rejected = [s for s in scans if s.status not in
                    (STATUS_SIGNAL, STATUS_DATA_UNAVAILABLE, STATUS_LOCKED, STATUS_MODEL_NOT_READY)]
        scan_summary = {
            "scanned": scanned_count,
            "qualified": len(qualified),
            "rejected": len(rejected),
            "data_unavailable": len(unavailable),
            "locked": len(locked),
            "model_not_ready": len(not_ready),
            "assets": {
                "scanned": [s.asset for s in scans],
                "qualified": [s.asset for s in qualified],
                "rejected": [s.asset for s in rejected],
                "unavailable": [s.asset for s in unavailable],
            },
            "best_candidate": (
                {"asset": primary.asset, "confidence": None if primary.confidence is None
                 else round(primary.confidence, 2), "quality": primary.quality,
                 "direction": primary.direction} if primary else None
            ),
            "note": (
                "Only qualified candidates are compared for AUTO SELECT — an unavailable or "
                "stale asset can never win a scan."
            ),
        }
        payload: dict[str, Any] = {
            "status": STATUS_SIGNAL if best else (primary.status if primary else STATUS_NO_TRADE),
            "scan_mode": mode,
            "requested_asset": (asset or "AUTO"),
            "assets_scanned": assets,
            "environment": settings.environment,
            "production_policy": {
                "require_real_data": bool(getattr(settings, "require_real_data", True)),
                "require_market_model": bool(getattr(settings, "require_market_model", True)),
                "min_model_agreement": float(getattr(settings, "min_model_agreement",
                                                     GATES["min_model_agreement"])),
            },
            "scan_summary": scan_summary,
            "asset": primary.asset if primary else None,
            "timeframe": settings.timeframe,
            "timezone": TIMEZONE,
            "generated_at_ist": ist_iso(now),
            "generated_at_utc": utc_iso(now),
            "session": session,
            "min_confidence": settings.min_confidence,
            "tradable_min_quality": TRADABLE_MIN_QUALITY,
            "feed": primary.feed if primary else None,
            "features": primary.features if primary else None,
            "components": primary.components if primary else None,
            "checks": primary.checks if primary else None,
            "ensemble": primary.ensemble if primary else None,
            "model_status": self.model_service().status(),
            "eligibility": primary.eligibility if primary else None,
            "data_validation": primary.data_validation if primary else None,
            "rejections": [
                {"asset": s.asset, "status": s.status, "reason": s.reason,
                 "detail": s.detail, "confidence": None if s.confidence is None else round(s.confidence, 2),
                 "blocked_by": ((s.eligibility or {}).get("blocked_by") or [])}
                for s in scans if s.status != STATUS_SIGNAL
            ],
            "all_scans": [s.as_dict() for s in scans],
            "disclaimer": DISCLAIMER,
        }

        if best:
            signal = best.signal or {}
            payload.update({
                "asset": signal.get("asset"),
                "direction": signal.get("direction"),
                "entry_time": signal.get("entry_time_ist"),
                "entry_time_ist": signal.get("entry_time_ist"),
                "expiry_time_ist": signal.get("expiry_time_ist"),
                "expiry_minutes": signal.get("expiry_minutes"),
                "confidence": signal.get("confidence"),
                "quality": signal.get("quality"),
                "quality_label": signal.get("quality_label"),
                "data_timestamp": signal.get("data_timestamp_utc"),
                "data_timestamp_ist": signal.get("data_timestamp_ist"),
                "model_agreement": signal.get("model_agreement"),
                "signal": signal,
                "reason": None,
            })
        else:
            reason, detail = self._aggregate_rejection(scans)
            payload.update({
                "direction": None, "entry_time": None, "expiry_minutes": settings.expiry_minutes,
                "confidence": primary.confidence if primary else None,
                "quality": primary.quality if primary else None,
                "data_timestamp": (primary.feed or {}).get("latest_candle_close_utc") if primary else None,
                "model_agreement": (primary.ensemble or {}).get("agreement") if primary else None,
                "signal": None,
                "reason": reason,
                "reason_detail": detail,
                "next_valid_scan_ist": primary.next_valid_scan_ist if primary else session["next_candle_ist"],
            })
        return payload

    @staticmethod
    def _aggregate_rejection(scans: list[AssetScan]) -> tuple[str, str | None]:
        if not scans:
            return "No assets were scanned", None
        model_ready = [s for s in scans if s.status not in (STATUS_MODEL_NOT_READY,)]
        data_ok = [s for s in scans if s.status not in (STATUS_MODEL_NOT_READY, STATUS_DATA_UNAVAILABLE, STATUS_ERROR)]
        if not model_ready:
            return "MODEL NOT READY", scans[0].detail or scans[0].reason
        if not data_ok:
            return "DATA UNAVAILABLE", "; ".join(
                f"{s.asset}: {s.reason}" for s in scans[:4]
            )
        best = max(data_ok, key=lambda s: s.confidence or 0.0)
        reasons = [r for r in [best.reason] if r]
        if best.quality == "MODERATE":
            reasons.append(f"quality MODERATE ({best.confidence:.1f}%) is not tradable (needs ≥ {TRADABLE_MIN_QUALITY:.0f}%)")
        detail = "; ".join(reasons) if reasons else "No setup met the validation gates"
        if len(data_ok) > 1:
            detail += f" — best of {len(data_ok)} scanned assets ({best.asset} at {best.confidence:.1f}%)"
        return (reasons[0] if reasons else "NO TRADE", detail)


_engine: SignalEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> SignalEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = SignalEngine()
        return _engine


def reset_engine() -> None:
    global _engine
    with _engine_lock:
        _engine = None
