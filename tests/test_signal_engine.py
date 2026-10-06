"""Signal-engine behaviour: schema, locking, honest rejection paths."""
from __future__ import annotations

import os

import pytest

from backend.database import db
from backend.models.service import ModelService
from backend.signals.engine import (
    STATUS_DATA_UNAVAILABLE,
    STATUS_LOCKED,
    STATUS_MODEL_NOT_READY,
    STATUS_NO_TRADE,
    STATUS_SIGNAL,
    SignalEngine,
)
from backend.timeutil import ist_clock, to_ist, to_utc, utc_iso

from .helpers import synthetic_market_frame

SIGNAL_KEYS = {
    "signal_uid", "asset", "timeframe", "direction", "confidence", "quality", "entry_time_utc",
    "entry_time_ist", "expiry_minutes", "expiry_time_ist", "data_timestamp_utc", "model_probability",
    "model_agreement", "is_simulated", "components", "model_votes", "scan_mode", "result", "status",
}


def test_unknown_asset_yields_data_unavailable(settings_simulated):
    engine = SignalEngine(settings_simulated)
    scan = engine.scan_asset("NOTAREALPAIR", persist=False)
    assert scan.status == STATUS_DATA_UNAVAILABLE


def test_otc_asset_without_broker_feed_never_invents_prices(settings_simulated):
    """live_only mode + OTC instrument (no public source) must report unavailability."""
    settings_simulated.data_source_mode = "live_only"
    engine = SignalEngine(settings_simulated)
    scan = engine.scan_asset("USDBDT-OTCq", persist=False)
    assert scan.status == STATUS_DATA_UNAVAILABLE
    assert "broker" in f"{scan.reason} {scan.detail}".lower()
    assert scan.signal is None


def test_stale_feed_is_rejected(settings_simulated, model_ready, monkeypatch):
    if not model_ready:
        pytest.skip("model artifacts required for the full pipeline")
    from backend.data.registry import MarketDataRegistry

    stale = synthetic_market_frame(minutes=220, last_candle_close_offset_seconds=1200)
    monkeypatch.setattr(MarketDataRegistry, "get_frame", lambda *a, **k: stale)

    engine = SignalEngine(settings_simulated)
    scan = engine.scan_asset("EURUSD", persist=False)
    assert scan.status == STATUS_DATA_UNAVAILABLE
    assert scan.signal is None
    assert "Stale data" in (scan.reason or "")


def test_model_not_ready_blocks_the_pipeline(settings_simulated, temp_db, tmp_path, monkeypatch):
    empty_service = ModelService(directory=tmp_path / "no-artifacts", auto_load=True)
    assert empty_service.ready is False
    monkeypatch.setattr("backend.signals.engine.get_model_service", lambda reload=False: empty_service)

    # DEVELOPMENT mode: a simulated feed is permitted there, so the model check is the
    # gate under test (in production the feed itself would already be refused).
    settings_simulated.environment = "development"
    engine = SignalEngine(settings_simulated)
    scan = engine.scan_asset("EURUSD", persist=False)
    assert scan.status == STATUS_MODEL_NOT_READY
    assert scan.signal is None
    assert "MODELS NOT TRAINED" in (scan.reason or "") or "MODEL" in (scan.reason or "").upper()


def test_engine_produces_only_schema_valid_signals(settings_simulated, model_ready):
    if not model_ready:
        pytest.skip("model artifacts required for the full pipeline")
    engine = SignalEngine(settings_simulated)
    payload = engine.generate("EURUSD", persist=True)

    assert payload["status"] in {STATUS_SIGNAL, STATUS_NO_TRADE, STATUS_LOCKED, STATUS_DATA_UNAVAILABLE}
    assert payload["timezone"] == "Asia/Kolkata"

    if payload["status"] == STATUS_SIGNAL:
        signal = payload["signal"]
        assert SIGNAL_KEYS.issubset(signal.keys())
        assert signal["direction"] in {"CALL", "PUT"}
        assert signal["confidence"] >= settings_simulated.min_confidence
        assert signal["quality"] in {"VERY_HIGH", "HIGH"}
        assert signal["expiry_minutes"] == settings_simulated.expiry_minutes
        assert signal["entry_time_utc"].endswith("+00:00")
        assert signal["signal_uid"].startswith(f"{signal['asset']}|")
        # stored with a per-candle lock key
        stored = db.signal_exists_for_candle(signal["asset"], signal["entry_time_utc"])
        assert stored is not None
        assert stored["result"] == "PENDING"
    else:
        assert payload["signal"] is None
        assert payload["reason"], "a rejection must always explain itself"


def test_duplicate_signal_for_the_same_candle_is_locked(settings_simulated, model_ready, temp_db, monkeypatch):
    """Spec §17 E: the second click on the same candle must return SIGNAL LOCKED.

    The production gates (real data, READY_MARKET, confidence, consistency) are covered
    by their own tests; here the policy is stubbed to *allow* so the lock rule itself is
    the only thing under test.
    """
    from backend.signals import engine as engine_module
    from backend.signals.engine import SignalEngine, STATUS_LOCKED
    from backend.signals.policy import EligibilityDecision

    if not model_ready:
        pytest.skip("model artifacts required for the full pipeline")
    settings_simulated.environment = "development"   # simulated feed must be permitted to reach the lock
    engine = SignalEngine(settings_simulated)
    entry = engine.scan_asset("EURUSD", persist=False).entry_time_ist
    assert entry is not None

    # simulate a signal that already exists for that candle
    from backend.timeutil import now_utc
    from backend.risk.policy import RiskPolicy

    policy = RiskPolicy(settings_simulated)
    entry_utc = policy.resolve_entry_time(now_utc())
    db.save_signal({
        "signal_uid": f"EURUSD|{utc_iso(entry_utc)}",
        "asset": "EURUSD",
        "timeframe": "M1",
        "direction": "CALL",
        "confidence": 90.0,
        "quality": "VERY_HIGH",
        "entry_time_utc": utc_iso(entry_utc),
        "entry_time_ist": ist_clock(entry_utc),
        "expiry_minutes": 1,
        "expiry_time_ist": ist_clock(entry_utc),
        "data_timestamp_utc": utc_iso(entry_utc),
        "data_timestamp_ist": ist_clock(entry_utc),
        "model_probability": 0.5,
        "model_agreement": 0.8,
        "created_at_utc": utc_iso(now_utc()),
    })

    monkeypatch.setattr(engine_module, "evaluate_eligibility",
                        lambda **_: EligibilityDecision(allowed=True, requirements=[]))
    scan = engine.scan_asset("EURUSD", persist=True)
    assert scan.status == STATUS_LOCKED
    assert scan.signal is None
    assert scan.next_valid_scan_ist is not None
    assert db.get_signal(f"EURUSD|{utc_iso(entry_utc)}") is not None


def test_lock_only_blocker_is_reported_as_locked_not_as_no_trade(settings_simulated, model_ready, temp_db, monkeypatch):
    """The policy blocks the lock as requirement 9 — the status must still say LOCKED."""
    from backend.signals import engine as engine_module
    from backend.signals.engine import SignalEngine, STATUS_LOCKED
    from backend.signals.policy import EligibilityDecision, Requirement

    if not model_ready:
        pytest.skip("model artifacts required for the full pipeline")
    settings_simulated.environment = "development"
    engine = SignalEngine(settings_simulated)

    def _locked_only(**_: object) -> EligibilityDecision:
        return EligibilityDecision(
            allowed=False,
            requirements=[Requirement(name="SIGNAL LOCK PASSES", passed=False, detail="locked")],
            blocked_by=["SIGNAL LOCK PASSES"],
            reason="SIGNAL LOCK PASSES: locked",
        )

    # a real signal row for the upcoming candle: the lock is evaluated against the DB
    from backend.risk.policy import RiskPolicy
    from backend.timeutil import now_utc

    entry_utc = RiskPolicy(settings_simulated).resolve_entry_time(now_utc())
    db.save_signal({
        "signal_uid": f"USDJPY|{utc_iso(entry_utc)}",
        "asset": "EURUSD",
        "timeframe": "M1",
        "direction": "PUT",
        "confidence": 88.0,
        "quality": "HIGH",
        "entry_time_utc": utc_iso(entry_utc),
        "entry_time_ist": ist_clock(entry_utc),
        "expiry_minutes": 1,
        "expiry_time_ist": ist_clock(entry_utc),
        "data_timestamp_utc": utc_iso(entry_utc),
        "data_timestamp_ist": ist_clock(entry_utc),
        "model_probability": 0.5,
        "model_agreement": 0.8,
        "created_at_utc": utc_iso(now_utc()),
    })

    monkeypatch.setattr(engine_module, "evaluate_eligibility", _locked_only)
    scan = engine.scan_asset("EURUSD", persist=False)
    assert scan.status == STATUS_LOCKED
    assert "locked" in (scan.reason or "").lower()


def test_database_lock_is_enforced_by_a_unique_index(settings_simulated, temp_db):
    payload = {
        "signal_uid": "EURUSD|2026-01-01T00:00:00+00:00",
        "asset": "EURUSD",
        "timeframe": "M1",
        "direction": "PUT",
        "confidence": 88.0,
        "quality": "HIGH",
        "entry_time_utc": "2026-01-01T00:00:00+00:00",
        "entry_time_ist": "2026-01-01T05:30:00",
        "expiry_minutes": 1,
        "expiry_time_ist": "2026-01-01T05:31:00",
        "data_timestamp_utc": "2025-12-31T23:59:00+00:00",
        "data_timestamp_ist": "2026-01-01T05:29:00",
        "model_probability": 0.51,
        "model_agreement": 0.9,
    }
    db.save_signal(payload)
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.save_signal(payload)


def test_auto_select_scans_every_enabled_asset(settings_simulated, model_ready):
    if not model_ready:
        pytest.skip("model artifacts required for the full pipeline")
    settings_simulated.auto_select = True
    settings_simulated.selected_asset = "AUTO"
    settings_simulated.enabled_assets = ["EURUSD", "GBPUSD", "USDBDT-OTCq"]
    engine = SignalEngine(settings_simulated)
    payload = engine.generate(None, persist=False)
    assert payload["assets_scanned"] == ["EURUSD", "GBPUSD", "USDBDT-OTCq"]
    assert len(payload["all_scans"]) == 3
    assert payload["status"] in {STATUS_SIGNAL, STATUS_NO_TRADE, STATUS_DATA_UNAVAILABLE, STATUS_MODEL_NOT_READY}


def test_rejections_carry_a_human_readable_reason(settings_simulated, model_ready):
    if not model_ready:
        pytest.skip("model artifacts required for the full pipeline")
    engine = SignalEngine(settings_simulated)
    payload = engine.generate("EURUSD", persist=False)
    if payload["status"] != STATUS_SIGNAL:
        for rejection in payload["rejections"]:
            assert rejection["reason"], f"{rejection['asset']} rejected without a reason"


def test_daily_risk_limit_blocks_further_signals(settings_simulated, model_ready, temp_db):
    if not model_ready:
        pytest.skip("model artifacts required for the full pipeline")
    from backend.risk.policy import RiskPolicy
    from backend.timeutil import now_utc

    settings_simulated.max_signals_per_day = 1
    policy = RiskPolicy(settings_simulated)
    today_ist = to_ist(now_utc()).strftime("%Y-%m-%d")
    decision = policy.evaluate(
        now=now_utc(),
        signals_today_dates=[today_ist],
        existing_signal_for_candle=False,
    )
    assert decision.allowed is False
    assert any("Daily signal limit" in reason for reason in decision.reasons)

    # a signal from yesterday must not consume today's budget
    allowed = policy.evaluate(
        now=now_utc(),
        signals_today_dates=["2020-01-01"],
        existing_signal_for_candle=False,
    )
    assert allowed.allowed is True
