"""HTTP API contract tests (FastAPI TestClient, isolated SQLite database)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db, monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DATA_SOURCE_MODE", "simulated")
    monkeypatch.setenv("ENABLE_TELEGRAM", "false")
    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_subsystems(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert payload["timezone"] == "Asia/Kolkata"
    assert "database" in payload and "model" in payload and "scheduler" in payload
    assert "confidence is a model-derived score" in payload["disclaimer"].lower() or "not a guarantee" in payload["disclaimer"].lower()
    assert any(mode["mode"] == "LIVE" and mode["available"] is False for mode in payload["execution"])


def test_ist_time_endpoint(client):
    response = client.get("/api/time/ist")
    assert response.status_code == 200
    payload = response.json()
    assert payload["timezone"] == "Asia/Kolkata"
    assert len(payload["ist_clock"]) == 8 and payload["ist_clock"].count(":") == 2
    assert payload["ist_offset"] == "+0530"
    assert 0 <= payload["seconds_to_next_candle"] <= 60
    assert payload["next_candle_ist"]


def test_assets_listing_covers_the_requested_universe(client):
    response = client.get("/api/assets")
    assert response.status_code == 200
    symbols = {asset["symbol"] for asset in response.json()["assets"]}
    for symbol in ("USDBDT-OTCq", "EURCAD-OTCq", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"):
        assert symbol in symbols


def test_market_endpoint_returns_candles_and_provenance(client):
    response = client.get("/api/market/EURUSD?limit=60")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "OK"
    assert payload["timezone"] == "Asia/Kolkata"
    assert len(payload["candles"]) == 60
    assert payload["feed"]["simulated"] is True          # simulated mode is pinned in tests
    assert payload["feed"]["source_label"]
    first = payload["candles"][0]
    assert {"time_utc", "time_ist", "open", "high", "low", "close"} <= set(first)
    assert first["high"] >= first["low"]


def test_market_endpoint_rejects_unknown_asset(client):
    assert client.get("/api/market/BTCUSDT").status_code == 400


def test_generate_rejects_invalid_asset(client):
    response = client.post("/api/signal/generate", json={"asset": "NOPE", "mode": "NOW"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["status"] == "INVALID_ASSET"
    assert "known_assets" in detail


def test_generate_returns_structured_payload(client):
    response = client.post("/api/signal/generate", json={"asset": "EURUSD", "mode": "NOW"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"SIGNAL", "NO_TRADE", "DATA_UNAVAILABLE", "MODEL_NOT_READY", "LOCKED"}
    assert payload["timezone"] == "Asia/Kolkata"
    assert payload["session"]["ist_time"]
    assert "model_status" in payload and "checks" in payload

    if payload["status"] == "SIGNAL":
        assert payload["direction"] in {"CALL", "PUT"}
        assert payload["confidence"] >= 85
        assert payload["entry_time"]
        assert payload["signal"]["expiry_minutes"] >= 1
    else:
        assert payload["signal"] is None
        assert payload["reason"]


def test_latest_history_and_performance_endpoints(client):
    latest = client.get("/api/signal/latest")
    assert latest.status_code == 200
    assert "signal" in latest.json()

    history = client.get("/api/signals/history?limit=10")
    assert history.status_code == 200
    assert "signals" in history.json()

    performance = client.get("/api/signals/performance")
    assert performance.status_code == 200
    payload = performance.json()
    assert "signal_performance" in payload and "model_validation" in payload
    assert "must never be combined" in payload["separation_note"]
    assert payload["signal_performance"]["generated"] >= 0
    # win rate is only reported when there are decided signals
    if payload["signal_performance"]["decided"] == 0:
        assert payload["signal_performance"]["win_rate"] is None


def test_model_status_and_metrics_endpoints(client):
    status = client.get("/api/model/status")
    assert status.status_code == 200
    payload = status.json()
    # Spec §4: READY_MARKET (operator real data) / READY_DEMO (simulated) / NOT_READY
    assert payload["state"] in {"READY_MARKET", "READY_DEMO", "NOT_READY"}
    assert isinstance(payload["models"], list)
    if not payload["ready"]:
        assert "TRAIN" in payload["message"].upper() or "ARTIFACT" in payload["message"].upper()

    metrics = client.get("/api/model/metrics")
    assert metrics.status_code == 200
    assert "model_metrics" in metrics.json()


def test_config_roundtrip_and_validation(client):
    config = client.get("/api/config")
    assert config.status_code == 200
    settings = config.json()["settings"]
    assert settings["timezone"] == "Asia/Kolkata"
    assert settings["timeframe"] == "M1"

    update = client.post("/api/config", json={"min_confidence": 88, "expiry_minutes": 2})
    assert update.status_code == 200
    updated = update.json()
    assert updated["errors"] == []
    assert updated["settings"]["settings"]["min_confidence"] == 88
    assert updated["settings"]["settings"]["expiry_minutes"] == 2

    # restore defaults for other tests
    client.post("/api/config", json={"min_confidence": 85, "expiry_minutes": 1})


def test_config_rejects_out_of_range_and_unknown_fields(client):
    assert client.post("/api/config", json={"min_confidence": 10}).status_code == 422
    assert client.post("/api/config", json={"expiry_minutes": 99}).status_code == 422
    assert client.post("/api/config", json={"mode": "LIVE"}).status_code == 422
    # secrets may never be pushed through the API
    assert client.post("/api/config", json={"telegram_bot_token": "abc"}).status_code == 422
    assert client.post("/api/config", json={"chat_id": "123"}).status_code == 422


def test_scans_system_and_events_endpoints(client):
    assert client.get("/api/signals/scans?limit=5").status_code == 200
    system = client.get("/api/system")
    assert system.status_code == 200
    payload = system.json()
    assert payload["paper_trading"] is True
    assert payload["data"]["status"] in {"OK", "PARTIAL", "SIMULATED_ONLY", "UNAVAILABLE"}
    assert client.get("/api/events?limit=5").status_code == 200


def test_daily_endpoint_reports_no_trade_honestly(client):
    response = client.get("/api/signals/daily?asset=EURUSD")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"SIGNAL", "STUDY_ONLY", "NO_TRADE", "MODEL_NOT_READY", "DATA_UNAVAILABLE"}
    if payload["status"] == "NO_TRADE":
        assert payload["reason"]


def test_evaluate_endpoint(client):
    response = client.post("/api/signals/evaluate")
    assert response.status_code == 200
    payload = response.json()
    assert "evaluated" in payload and "pending" in payload
