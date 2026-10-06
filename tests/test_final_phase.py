"""Final-phase tests (spec §16).

Covers the operator-data path end to end at the unit level:

    * CSV ingestion + validation (duplicates, gaps, bad OHLC, resolution,
      non-monotonic timestamps, flat/stale feeds, insufficient history)
    * freshness budgets per feed type
    * production eligibility policy (real data + READY_MARKET + gates + lock)
    * demo-model rejection outside development
    * M1 closed-candle boundaries
    * dataset registry status / provenance reporting for unavailable assets
    * API hardening (payload caps, forbidden fields, secret rejection, CORS)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.data.base import MarketDataUnavailable
from backend.data.validation import csv_text_to_frame, validate_candles
from backend.timeutil import utc_iso

MINUTE = timedelta(minutes=1)


# --------------------------------------------------------------------- helpers
def _csv(rows: int = 400, *, start: datetime | None = None, step: timedelta = MINUTE,
         base: float = 1.1000, flat_after: int | None = None) -> str:
    """Deterministic OHLC CSV text with a header, ISO-8601 UTC timestamps."""
    start = start or (datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - rows * step)
    lines = ["time,open,high,low,close,volume"]
    price = base
    for i in range(rows):
        stamp = start + i * step
        if flat_after is not None and i >= flat_after:
            o = h = low = c = round(price, 6)
        else:
            price = round(price + (0.00012 if i % 3 else -0.00009), 6)
            o, c = round(price, 6), round(price + (0.00007 if i % 2 else -0.00005), 6)
            h, low = round(max(o, c) + 0.00003, 6), round(min(o, c) - 0.00003, 6)
        lines.append(f"{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')},{o},{h},{low},{c},120")
    return "\n".join(lines) + "\n"


def _frame_from_csv(text: str) -> pd.DataFrame:
    frame, _ = csv_text_to_frame(text)
    return frame


# ------------------------------------------------------------------ CSV parsing
def test_csv_text_parses_utf8_iso_utc_into_a_minute_grid():
    frame = _frame_from_csv(_csv(300))
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert len(frame) == 300
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.tz is not None
    assert (frame.index.tz.utcoffset(None) == timedelta(0))
    assert frame.index.is_monotonic_increasing


def test_csv_accepts_epoch_seconds_and_milliseconds():
    start = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - 300 * MINUTE
    for scale, fmt in ((1, lambda d: str(int(d.timestamp()))),
                       (1000, lambda d: str(int(d.timestamp() * 1000)))):
        rows = ["time,open,high,low,close"]
        for i in range(240):
            stamp = start + i * MINUTE
            rows.append(f"{fmt(stamp)},{1.10 + i * 1e-5},{1.11 + i * 1e-5},{1.09 + i * 1e-5},{1.105 + i * 1e-5}")
        frame = _frame_from_csv("\n".join(rows) + "\n")
        assert len(frame) == 240, f"epoch scale {scale}"


def test_csv_rejects_invalid_ohlc_geometry():
    text = _csv(240)
    broken = text.replace(",1.1", ",0.9", 1)  # low above high for at least one row
    report = validate_candles(_frame_from_csv(broken), check_freshness=False)
    assert report.status == "INVALID"
    assert report.invalid_ohlc >= 1
    assert any("high" in message.lower() or "ohlc" in message.lower() for message in report.errors)


def test_csv_rejects_duplicate_and_non_monotonic_timestamps():
    frame = _frame_from_csv(_csv(120))
    duplicated = pd.concat([frame, frame.iloc[[10]]]).sort_index()
    report = validate_candles(duplicated, check_freshness=False)
    assert report.duplicates >= 1
    assert report.status == "INVALID"

    shuffled = frame.copy()
    index = list(shuffled.index)
    index[5], index[6] = index[6], index[5]   # reorder the timestamps themselves
    shuffled.index = pd.DatetimeIndex(index)
    out_of_order = validate_candles(shuffled, check_freshness=False)
    assert out_of_order.non_monotonic >= 1
    assert out_of_order.status == "INVALID"


def test_csv_rejects_the_wrong_candle_resolution():
    frame = _frame_from_csv(_csv(240, step=timedelta(minutes=2)))
    report = validate_candles(frame, check_freshness=False)
    assert report.status == "INVALID"
    assert report.interval_minutes == pytest.approx(2.0)
    assert any("resolution" in message.lower() or "minute" in message.lower() for message in report.errors)


def test_csv_rejects_a_flat_stale_feed():
    """A frozen quote repeated every minute is not tradeable price action."""
    frame = _frame_from_csv(_csv(240, flat_after=2))
    report = validate_candles(frame, check_freshness=False)
    assert report.flat_ratio > 0.5
    assert report.status == "INVALID"
    assert any("flat" in message.lower() for message in report.errors)


def test_csv_rejects_insufficient_history():
    frame = _frame_from_csv(_csv(30))
    report = validate_candles(frame, check_freshness=False)
    assert report.status == "INVALID"
    assert any("history" in message.lower() or "candle" in message.lower() for message in report.errors)


def test_csv_missing_candles_are_reported_but_session_breaks_are_not_fatal():
    frame = _frame_from_csv(_csv(600))
    gapped = frame.drop(frame.index[100:105])
    report = validate_candles(gapped, check_freshness=False)
    assert report.missing_candles == 5
    assert report.status in {"OK", "WARNING"}


def test_csv_text_rejects_empty_payload_and_nul_bytes():
    with pytest.raises(ValueError):
        csv_text_to_frame("")
    with pytest.raises(ValueError):
        csv_text_to_frame("time,open,high,low,close\n\x00,1,1,1,1\n")


# ------------------------------------------------------------------- freshness
def test_freshness_budget_is_tighter_for_live_feeds_than_replay():
    from backend.constants import GATES

    live, replay = GATES["max_data_lag_seconds"], GATES["max_replay_lag_seconds"]
    assert live < replay
    assert live > 60 and replay > 60


def test_stale_replay_dataset_is_flagged_by_the_validator():
    frame = _frame_from_csv(_csv(300))
    old = frame.copy()
    old.index = old.index - timedelta(days=30)
    report = validate_candles(old, check_freshness=True)
    assert report.stale_seconds is not None and report.stale_seconds > 3600
    assert report.status in {"WARNING", "INVALID"}


# --------------------------------------------------------- M1 candle boundaries
def test_forming_candle_is_never_part_of_the_dataset():
    from backend.data.base import drop_forming_candle

    now = datetime.now(tz=timezone.utc).replace(second=25, microsecond=0)
    index = pd.date_range(end=now.replace(second=0), periods=5, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 1.0}, index=index
    )
    trimmed, dropped = drop_forming_candle(frame, now=now)
    assert dropped == 1
    assert trimmed.index[-1] == now.replace(second=0) - MINUTE  # newest fully closed candle


def test_time_payload_reports_closed_candles_and_a_forward_entry_time():
    from backend.main import _time_payload

    payload = _time_payload()
    entry = datetime.strptime(payload["entry_time_ist"], "%H:%M:%S")
    current = datetime.strptime(payload["current_candle_ist"], "%H:%M:%S")
    assert payload["authoritative"] == "backend"
    assert entry > current
    assert payload["expiry_minutes"] >= 1
    assert 0 <= payload["countdown_seconds"] <= 60
    last_closed = datetime.strptime(payload["last_closed_candle_ist"], "%H:%M:%S")
    assert payload["last_closed_candle_ist"] != payload["current_candle_ist"]
    assert (current - last_closed) % timedelta(minutes=1) == timedelta(minutes=59) or current > last_closed


# --------------------------------------------------------------------- policy
def _settings(**overrides):
    from backend.settings_service import get_settings

    settings = get_settings()
    settings.environment = "production"
    settings.min_confidence = 85.0
    settings.min_model_agreement = 0.60
    settings.data_source_mode = "real_only"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _market_status(state: str = "READY_MARKET") -> dict:
    return {"state": state, "ready": True, "market_ready": state == "READY_MARKET",
            "demo_ready": state == "READY_DEMO", "version": "v1-market"}


def _real_feed(replay: bool = True) -> dict:
    return {"provider": "csv", "source_label": "Operator CSV: USDJPY.csv", "source_class": "MARKET",
            "is_simulated": False, "is_replay": replay, "badge": "REPLAY" if replay else "LIVE"}


def _sim_feed() -> dict:
    return {"provider": "simulated", "source_label": "SIMULATED DEMO FEED", "source_class": "SIMULATED",
            "is_simulated": True, "is_replay": False, "badge": "SIMULATED"}


def _checks(all_pass: bool = True) -> dict:
    """The bundle the engine hands to the policy (see backend/validation/checks.py)."""
    from backend.constants import GATES

    entries = [{"check": name, "passed": all_pass, "detail": "test"} for name in GATES]
    return {
        "passed": all_pass,
        "blocking": [] if all_pass else [GATES and "DATA FRESHNESS"],
        "reasons": [] if all_pass else ["test: gate failed"],
        "checks": entries,
    }


def _decide(**kwargs):
    from backend.signals.policy import evaluate_eligibility

    payload = dict(
        settings=_settings(), feed=_real_feed(), checks=_checks(), confidence=90.0, quality="VERY HIGH",
        ensemble={"agreement": 0.9, "votes": {}}, lock={"locked": False}, candles=300,
        freshness_seconds=45.0, model_status=_market_status(),
    )
    payload.update(kwargs)
    return evaluate_eligibility(**payload)


def test_real_data_market_model_and_all_gates_yield_allow_signal():
    decision = _decide()
    assert decision.allowed is True
    assert decision.as_dict()["decision"] == "ALLOW_SIGNAL"
    assert all(requirement.passed for requirement in decision.requirements)


@pytest.mark.parametrize(
    "override, blocker",
    [
        ({"feed": _sim_feed()}, "DATA SOURCE IS REAL"),
        ({"model_status": _market_status("READY_DEMO")}, "MODEL READY_MARKET"),
        ({"confidence": 70.0, "quality": "MODERATE"}, "CONFIDENCE ≥ THRESHOLD"),
        ({"ensemble": {"agreement": 0.4}}, "AGREEMENT ≥ THRESHOLD"),
        ({"freshness_seconds": 900.0}, "DATA IS FRESH"),
        ({"candles": 40}, "ENOUGH CANDLES"),
        ({"checks": _checks(all_pass=False)}, "MANDATORY GATES PASS"),
        ({"lock": {"locked": True}}, "SIGNAL LOCK PASSES"),
    ],
)
def test_each_production_requirement_can_block_a_signal(override, blocker):
    decision = _decide(**override)
    assert decision.allowed is False
    assert decision.as_dict()["decision"] == "NO_TRADE"
    assert blocker in decision.blocked_by
    assert blocker in (decision.reason or "")


def test_demo_model_never_signals_in_production_but_is_labelled_in_development():
    blocked = _decide(model_status=_market_status("READY_DEMO"))
    assert blocked.allowed is False
    assert blocked.as_dict()["decision"] == "NO_TRADE"
    assert "MODEL READY_MARKET" in blocked.blocked_by

    dev = _decide(settings=_settings(environment="development"),
                  model_status=_market_status("READY_DEMO"), feed=_sim_feed())
    assert dev.allowed is True
    assert dev.label == "DEVELOPMENT"
    downgraded = [r.name for r in dev.requirements if r.downgraded]
    assert "MODEL READY_MARKET" in downgraded
    assert "DATA SOURCE IS REAL" in downgraded


def test_low_quality_confidence_bands_are_enforced():
    from backend.constants import TRADABLE_MIN_QUALITY

    assert TRADABLE_MIN_QUALITY >= 85
    decision = _decide(confidence=84.9, quality="MODERATE")
    assert decision.allowed is False
    assert "CONFIDENCE ≥ THRESHOLD" in decision.blocked_by


# ------------------------------------------------------- dataset registry state
def test_registry_reports_unavailable_assets_without_inventing_history():
    from backend.data.registry import unavailable_report

    record = unavailable_report("AUDUSD", "No operator dataset for this asset", "no file", "csv")
    assert record["status"] == "DATA_UNAVAILABLE"
    assert record["rows"] == 0
    assert record["badge"] == "DATA UNAVAILABLE"
    assert "reason" in record and "detail" in record


def test_invalid_dataset_raises_unavailable_instead_of_being_served(tmp_path, monkeypatch):
    """A dataset that fails validation must never reach the model."""
    from backend.constants import ASSET_MAP
    from backend.data.providers.csv_provider import CsvProvider

    provider = CsvProvider(tmp_path)
    spec = ASSET_MAP["USDJPY"]
    # Two-minute candles: a resolution the M1 model cannot use.
    text = _csv(300, step=timedelta(minutes=2))
    frame, _ = csv_text_to_frame(text)
    frame.index.name = "time"
    (tmp_path / f"{spec.symbol}.csv").write_text(frame.to_csv(date_format="%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8")

    with pytest.raises(MarketDataUnavailable) as excinfo:
        provider.fetch(spec, limit=200, now=datetime.now(tz=timezone.utc))
    assert "validation" in excinfo.value.reason.lower()
    assert excinfo.value.provider == "csv"


def test_dataset_status_shape_for_a_real_registered_file():
    from backend.data.registry import registry

    status = registry.dataset_status("USDJPY")
    assert status["asset"] == "USDJPY"
    assert status["status"] in {"OK", "DEGRADED", "DATA_UNAVAILABLE"}
    if status["status"] != "DATA_UNAVAILABLE":
        assert status["rows"] > 0
        assert status["validation"]["status"] in {"VALID", "WARNING"}
        assert status["is_real_market_data"] is True
        assert status["badge"] in {"LIVE", "REPLAY"}


# ------------------------------------------------------------------ API layer
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.database import db as db_module
    from backend.main import app

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "api_test.db", raising=True)
    db_module.init_db(tmp_path / "api_test.db")
    with TestClient(app) as test_client:
        yield test_client


def test_import_endpoint_rejects_empty_payload(client):
    response = client.post("/api/data/import", json={"asset": "EURUSD", "csv_text": "   "})
    assert response.status_code == 422


def test_import_endpoint_caps_the_payload_size(client, monkeypatch):
    import backend.main as main

    monkeypatch.setattr(main, "MAX_CSV_CHARS", 500, raising=True)
    response = client.post("/api/data/import", json={"asset": "EURUSD", "csv_text": _csv(120)})
    assert response.status_code == 413
    assert "too large" in response.json()["detail"].lower()


def test_import_endpoint_rejects_path_like_filenames(client):
    response = client.post(
        "/api/data/import",
        json={"asset": "EURUSD", "csv_text": _csv(120), "filename": "../../etc/passwd"},
    )
    assert response.status_code == 422


def test_import_endpoint_rejects_unknown_assets(client):
    response = client.post("/api/data/import", json={"asset": "NOTAPAIR", "csv_text": _csv(120)})
    body = response.json()
    assert body["status"] == "REJECTED"
    assert "Unknown asset" in body["reason"]


def test_import_dry_run_validates_without_writing(client):
    response = client.post(
        "/api/data/import",
        json={"asset": "EURUSD", "csv_text": _csv(400), "source_class": "MARKET", "dry_run": True},
    )
    body = response.json()
    assert body["status"] in {"VALIDATED", "REJECTED"}
    if body["status"] == "VALIDATED":
        assert body["validation"]["status"] in {"VALID", "WARNING"}
        assert "path" not in body


def test_data_status_endpoint_lists_every_asset_with_a_badge(client):
    payload = client.get("/api/data/status").json()
    assert payload["counts"]["total"] == len(payload["assets"])
    for row in payload["assets"]:
        assert row["status"] in {"OK", "DEGRADED", "DATA_UNAVAILABLE"}
        assert row["badge"] in {"LIVE", "REPLAY", "SIMULATED", "DATA UNAVAILABLE"}
        if row["status"] == "DATA_UNAVAILABLE":
            assert row["rows"] == 0
            assert row["reason"]


def test_config_rejects_secrets_and_unknown_fields(client):
    secret = client.patch("/api/config", json={"telegram_bot_token": "leak"})
    assert secret.status_code in {405, 422}
    unknown = client.post("/api/config", json={"not_a_setting": 1})
    assert unknown.status_code == 422
    environment = client.post("/api/config", json={"environment": "prod-ish"})
    assert environment.status_code == 422


def test_cors_is_not_a_wildcard_by_default(monkeypatch):
    import backend.main as main

    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("ALLOW_PREVIEW_ORIGINS", "false")
    origins, regex = main._cors_settings()
    assert "*" not in origins
    assert regex is None
    assert any("localhost" in origin for origin in origins)

    monkeypatch.setenv("ALLOWED_ORIGINS", "*")
    wildcard, _ = main._cors_settings()
    assert wildcard == ["*"]

    # a foreign origin is never matched by the default policy
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("ALLOW_PREVIEW_ORIGINS", raising=False)
    origins, regex = main._cors_settings()
    assert not any("evil" in origin for origin in origins)
    assert regex is not None and "e2b" in regex


def test_telegram_error_detail_redacts_the_bot_token():
    from backend.telegram.notifier import _redact

    token = "123456:AAHsecret"
    assert token not in _redact(f"failed url=/bot{token}/sendMessage", token)
    assert "redacted" in _redact(f"/bot{token}/sendMessage", token).lower()


# ------------------------------------------------- production feed discipline
def test_production_reports_synthetic_feeds_as_data_unavailable(settings_simulated, temp_db):
    """Spec §1/§5: in production an instrument with only a synthetic feed is
    DATA UNAVAILABLE — it is never analysed from simulated candles."""
    from backend.signals.engine import SignalEngine

    settings_simulated.environment = "production"
    scan = SignalEngine(settings_simulated).scan_asset("EURUSD", persist=False)
    assert scan.status == "DATA_UNAVAILABLE"
    assert "No verified market data" in (scan.reason or "")
    assert scan.signal is None


def test_development_allows_the_labelled_simulated_feed(settings_simulated, temp_db, model_ready):
    from backend.signals.engine import SignalEngine

    settings_simulated.environment = "development"
    scan = SignalEngine(settings_simulated).scan_asset("EURUSD", persist=False)
    assert scan.status != "DATA_UNAVAILABLE"
    assert (scan.feed or {}).get("badge") == "SIMULATED"
    assert (scan.eligibility or {}).get("label") == "DEVELOPMENT"
