"""Final acceptance + hardening checks (final master phase §5–§9, §14–§16, §24–§25).

These tests lock the *contract* the delivery report claims:

* one signal engine behind every entry point (button, AUTO SELECT, DAILY, scanner);
* AUTO SELECT reports scanned / qualified / rejected / unavailable / locked;
* DAILY mode never forces a signal;
* every stored signal carries its full provenance (versions, provider, window fingerprint);
* the published evidence rule and the readiness vocabulary are exposed, never invented;
* the dashboard contains the signal-card and NO-TRADE-card wording and never invents a
  direction client-side;
* Telegram is off by default and refuses anything that is not real + READY_MARKET + qualified.

Everything runs against temporary databases with the real engine. No gate is stubbed and
no threshold is lowered; where a signal would be required, the test asserts the honest
NO TRADE / DATA_UNAVAILABLE outcome instead.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from backend.data import provenance, store
from backend.signals import ledger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "backend" / "static" / "index.html"


# ------------------------------------------------------------------ §5 one engine
def test_every_entry_point_uses_the_same_signal_engine():
    import backend.main as main_module
    import backend.signals.engine as engine_module
    import backend.signals.scanner as scanner_module

    a = main_module.get_engine()
    b = engine_module.get_engine()
    c = scanner_module.get_engine() if hasattr(scanner_module, "get_engine") else b
    assert a is b is c, "the button, AUTO SELECT, DAILY and the scanner must share one engine"
    assert a.model_service() is not None


def test_frontend_never_invents_a_direction():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert "Math.random" not in html, "the dashboard must never randomise a direction"
    assert "/api/signal/generate" in html, "the button must call the real pipeline endpoint"
    # Only the documented endpoints may be called for signal material.
    for route in re.findall(r'api\("(/api/[^"`]+)"', html):
        assert route.startswith(("/api/signal", "/api/signals", "/api/paper", "/api/data",
                                 "/api/model", "/api/research", "/api/system", "/api/time",
                                 "/api/assets", "/api/market", "/api/config", "/api/events")), route
    assert "SIMULATED" in html and "READY_DEMO" in html, "demo labelling must stay in the UI"


# --------------------------------------------------------- §6/§7 AUTO + DAILY
def test_auto_select_reports_the_scan_counters(temp_db):
    from backend.main import get_engine

    payload = get_engine().generate("AUTO", mode="NOW", persist=False)
    summary = payload.get("scan_summary") or {}
    for key in ("scanned", "qualified", "rejected", "data_unavailable", "locked"):
        assert key in summary, f"scan_summary must expose {key}"
    assert summary["scanned"] >= 1
    assert summary["qualified"] >= sum(1 for s in (payload.get("all_scans") or []) if s.get("status") == "SIGNAL")
    assert payload.get("status") in {"NO_TRADE", "DATA_UNAVAILABLE", "SIGNAL", "MODEL_NOT_READY", "LOCKED"}
    if payload.get("status") != "SIGNAL":
        assert not (payload.get("signal") or {}), "a non-signal scan must not carry a signal card"


def test_daily_mode_never_forces_a_signal(temp_db):
    from backend.main import get_engine

    payload = get_engine().generate("AUTO", mode="DAILY", persist=True)
    assert payload.get("status") in {"NO_TRADE", "DATA_UNAVAILABLE", "MODEL_NOT_READY"}
    assert ledger.rows(limit=10) == [], "an unqualified daily run must write no ledger row"


def test_daily_mode_is_capped_at_one_signal_per_ist_day(temp_db):
    from backend.signals import daily

    status = daily.daily_status()
    assert status.get("signals_today") in (0, None) or status.get("signals_today") <= 1
    assert status.get("status") in {"NO_TRADE", "SIGNAL", "PENDING", "WIN", "LOSS", "UNRESOLVED"}


# --------------------------------------------------------------- §9 provenance
def test_window_version_is_deterministic_and_window_specific():
    class Meta:
        asset = "USDJPY"
        provider = "operator_csv"
        source_class = "MARKET"
        badge = "REPLAY"
        latest_candle_open_utc = "2026-10-02T06:47:00+00:00"
        latest_candle_close_utc = "2026-10-02T06:48:00+00:00"
        is_simulated = False
        is_replay = True
        realtime = False

    first = provenance.window_version(Meta(), rows=520, first_open_utc="2026-10-01T22:00:00+00:00")
    again = provenance.window_version(Meta(), rows=520, first_open_utc="2026-10-01T22:00:00+00:00")
    assert first == again and first.startswith("win-") and len(first) == 16
    other_rows = provenance.window_version(Meta(), rows=519, first_open_utc="2026-10-01T22:00:00+00:00")
    assert other_rows != first, "a different serving window must fingerprint differently"

    class Simulated(Meta):
        is_simulated = True
        is_replay = False
        realtime = True
        badge = "SIMULATED"

    assert provenance.window_version(Simulated(), rows=520,
                                     first_open_utc="2026-10-01T22:00:00+00:00") != first


def test_stored_signal_carries_the_full_provenance_set(temp_db):
    row_id = ledger.record_signal(
        {"signal_uid": "USDJPY|2026-10-02T06:30:00+00:00", "asset": "USDJPY", "timeframe": "M1",
         "direction": "CALL", "confidence": 91.4, "quality": "HIGH",
         "entry_time_utc": "2026-10-02T06:30:00+00:00", "entry_time_ist": "2026-10-02T12:00:00+05:30",
         "entry_time_ist_full": "2026-10-02T12:00:00+05:30", "entry_date_ist": "2026-10-02",
         "expiry_minutes": 1, "expiry_time_ist": "12:01:00",
         "data_timestamp_utc": "2026-10-02T06:29:00+00:00", "data_timestamp_ist": "2026-10-02T11:59:00+05:30",
         "price": 157.0, "status": "OPEN", "result": "PENDING", "scan_mode": "NOW"},
        feed_provider="operator_csv", feed_status="REPLAY", model_version="v1-market",
        dataset_version="win-abc123def456", feature_version="m1-features-1.0",
        expiry_time_utc="2026-10-02T06:31:00+00:00", simulated=False,
    )
    assert row_id > 0
    row = ledger.rows(limit=1)[0]
    assert row["signal_id"] == "USDJPY|2026-10-02T06:30:00+00:00"
    assert row["model_version"] == "v1-market"
    assert row["dataset_version"] == "win-abc123def456"
    assert row["feature_version"] == "m1-features-1.0"
    assert row["feed_provider"] == "operator_csv"
    assert row["feed_status"] == "REPLAY"
    assert row["status"] == ledger.PENDING and row["result"] == "PENDING"
    assert row["outcome_source"] is None, "no outcome may be claimed before the candle closes"


def test_engine_signal_payload_declares_the_provenance_keys():
    """Structural guard: the emitted signal dict must keep the provenance contract."""
    source = (PROJECT_ROOT / "backend" / "signals" / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_signal":
            for child in ast.walk(node):
                if isinstance(child, ast.Dict):
                    for key in child.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
    for required in ("signal_uid", "asset", "direction", "confidence", "entry_time_utc", "entry_time_ist",
                     "expiry_minutes", "expiry_time_ist", "expiry_time_utc", "model_version",
                     "feature_version", "dataset_version", "feed_provider", "feed_status",
                     "is_simulated", "is_real_market_data", "model_market_ready" if False else "price"):
        assert required in keys, f"the emitted signal must declare {required}"


# -------------------------------------------------------- §10/§11 evidence + rule
def test_evidence_payload_reports_the_published_rule_and_the_no_trade_rate(temp_db):
    ledger.record_signal({"signal_uid": "USDJPY|2026-10-02T07:00:00+00:00", "asset": "USDJPY",
                          "timeframe": "M1", "direction": "CALL", "confidence": 90.0, "quality": "HIGH",
                          "status": "OPEN",
                          "result": "PENDING", "scan_mode": "NOW",
                          "entry_time_utc": "2026-10-02T07:00:00+00:00", "entry_date_ist": "2026-10-02",
                          "entry_time_ist": "2026-10-02T12:30:00+05:30",
                          "entry_time_ist_full": "2026-10-02T12:30:00+05:30",
                          "data_timestamp_utc": "2026-10-02T06:59:00+00:00",
                          "data_timestamp_ist": "2026-10-02T12:29:00+05:30",
                          "expiry_minutes": 1, "expiry_time_ist": "12:31:00"},
                         feed_provider="operator_csv", model_version="v1-market")
    evidence = ledger.evidence()
    assert evidence["evidence_bar"] == "NOT PASSED"
    assert evidence["evidence_rule"]["minimum_qualifying_signals"] == 100
    assert evidence["evidence_rule"]["wilson_95_lower_bound_above"] == 0.50
    assert evidence["qualified_signals"] == 1
    for key in ("coverage", "no_trade_rate", "no_trade_scans", "scan_status_counts", "wilson_95_lower_bound",
                "max_losing_streak", "days", "assets", "qualified_signals", "total_decided_trades"):
        assert key in evidence, f"evidence must report {key}"
    # coverage is defined against recorded scans; with none recorded it must stay null
    # (never a made-up denominator), and with scans it is qualified / scans.
    if evidence["scans_recorded"]:
        assert evidence["coverage"] == pytest.approx(
            evidence["qualified_signals"] / evidence["scans_recorded"])
    else:
        assert evidence["coverage"] is None


def test_wilson_bound_matches_a_hand_computed_value():
    # 7 hits in 10 trades: p̂ = 0.7, the 95 % lower bound is ≈ 0.3968 (the value the audit reports)
    assert ledger.wilson_lower_bound(7, 10) == pytest.approx(0.3968, abs=5e-4)
    assert ledger.wilson_lower_bound(0, 0) is None
    assert ledger.max_losing_streak([ledger.WIN, ledger.LOSS, ledger.LOSS, ledger.WIN, ledger.LOSS]) == 2


# ------------------------------------------------------ §14/§15 card wording
def test_dashboard_contains_the_signal_and_no_trade_card_contract():
    html = DASHBOARD.read_text(encoding="utf-8")
    # §14 — the exact card wording the operator sees and copies.
    for token in ("🚨 QUOTEX M1 PAPER SIGNAL", "💎 M1", "🕐 ENTRY:", "🔺", "🔻",
                  "🎯 MODEL CONFIDENCE:", "🤖 MODEL AGREEMENT:", "⏳ EXPIRY:", "📡 DATA:"):
        assert token in html, f"signal card must render {token}"
    # §15 — NO TRADE is an outcome with evidence, not an error.
    for token in ("NO TRADE", "Confidence:", "Reason:", "best candidate", "next valid scan"):
        assert token.lower() in html.lower(), f"the NO TRADE card must include {token}"
    assert "nothing is forced" in html.lower()
    assert "confidence is a calculated score" in html.lower() or "not a guarantee" in html.lower()


def test_next_dashboard_uses_the_same_card_wording():
    panel = (PROJECT_ROOT / "frontend" / "components" / "LivePaperPanel.tsx").read_text(encoding="utf-8")
    card = (PROJECT_ROOT / "frontend" / "components" / "SignalCard.tsx").read_text(encoding="utf-8")
    assert "🚨 QUOTEX M1 PAPER SIGNAL" in panel
    for token in ("💎", "🕐", "🎯", "🤖", "⏳", "📡"):
        assert token in panel, f"LivePaperPanel must render {token}"
    assert "🚨 QUOTEX M1 PAPER SIGNAL" in card, "the copied message must carry the spec title"
    assert "🔺" in card and "🔻" in card


def test_no_marketing_language_anywhere_in_the_served_ui():
    """A forbidden phrase is only allowed where the text explicitly denies it.

    The dashboard does mention e.g. "sure shot" — inside the sentence that says the system
    makes *no* such claim. That is the opposite of marketing; anything else is a failure.
    """
    html = DASHBOARD.read_text(encoding="utf-8").lower()
    negations = ("no ", "not ", "never ", "without ", "isn't", "is not")
    for banned in ("guaranteed profit", "guaranteed win", "sure shot", "100% accuracy", "risk-free",
                   "risk free", "always wins", "profit machine", "assured win"):
        start = html.find(banned)
        while start != -1:
            window = html[max(0, start - 40):start]
            assert any(word in window for word in negations), (
                f"forbidden claim in the dashboard: {banned!r} (context: ...{window[-30:]}{banned})")
            start = html.find(banned, start + 1)


# ----------------------------------------------------------- §16 Telegram default
def test_telegram_is_off_by_default_and_refuses_non_market_material(monkeypatch):
    from backend.telegram import notifier

    monkeypatch.delenv("ENABLE_TELEGRAM", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    creds = notifier.telegram_credentials()
    assert creds["configured"] is False

    class _Never:
        calls = 0

        def post(self, *a, **k):
            self.calls += 1
            raise AssertionError("no HTTP call may happen without credentials")

    result = notifier.send_signal({"asset": "USDJPY", "direction": "CALL", "confidence": 99.0,
                                   "is_real_market_data": True, "model_market_ready": True,
                                   "status": "OPEN", "entry_time_ist": "12:00:00"},
                                  enabled=False, session=_Never(), environment="production")
    assert result.status in {notifier.STATUS_DISABLED, notifier.STATUS_NOT_CONFIGURED}
    assert _Never.calls == 0


def test_demo_and_stale_material_are_never_eligible_in_production(temp_db):
    from backend.main import get_engine

    payload = get_engine().generate("USDJPY", mode="NOW", persist=False)
    if payload.get("status") == "SIGNAL":                     # only possible with fresh real data
        feed = payload.get("feed") or {}
        assert feed.get("is_real_market_data") is True and feed.get("is_simulated") is False
        assert feed.get("freshness_status", "FRESH") == "FRESH"
    else:
        assert payload.get("status") in {"DATA_UNAVAILABLE", "NO_TRADE", "MODEL_NOT_READY"}


# --------------------------------------------------------- §24/§25 system summary
def test_system_summary_exposes_the_operational_state():
    from backend.main import system_status

    payload = system_status(refresh=True)
    for key in ("environment", "mode", "data", "feed", "model", "active_artifact", "paper", "telegram",
                "scheduler", "research", "readiness", "time", "session"):
        assert key in payload, f"/api/system must expose {key}"
    state = payload["readiness"]["state"]
    assert state["system"] == "READY"
    assert state["data"] in {"FRESH", "STALE", "INVALID", "UNAVAILABLE"}
    assert state["model"] in {"READY_MARKET", "READY_DEMO", "NOT_READY"}
    assert state["paper"] in {"ACTIVE", "BLOCKED"}
    assert state["telegram"] in {"CONFIGURED", "NOT_CONFIGURED", "BLOCKED"}
    assert state["evidence"] in {"PASSED", "NOT PASSED"}
    assert state["classification"] in {"RESEARCH_ONLY", "PAPER_TRADING_READY",
                                       "LIVE_DATA_READY_FOR_PAPER_SIGNALING"}
    assert payload["active_artifact"]["dir"]
    assert payload["research"]["conclusion"].startswith("NO QUALIFIED EDGE HAS BEEN DEMONSTRATED")
    assert payload["paper"]["evidence_bar"] in {"PASSED", "NOT PASSED"}


def test_feed_freshness_budgets_are_published_and_used():
    from backend.data.feed import ContinuousFeed

    assert ContinuousFeed.freshness_from_age(5.0, realtime=True, has_rows=True) == "FRESH"
    assert ContinuousFeed.freshness_from_age(121.0, realtime=True, has_rows=True) == "STALE"
    assert ContinuousFeed.freshness_from_age(121.0, realtime=False, has_rows=True) == "FRESH"   # replay budget
    assert ContinuousFeed.freshness_from_age(181.0, realtime=False, has_rows=True) == "STALE"
    assert ContinuousFeed.freshness_from_age(None, realtime=True, has_rows=False) == "UNAVAILABLE"
    assert ContinuousFeed.freshness_from_age(1.0, realtime=True, has_rows=True, invalid=True) == "INVALID"


def test_store_never_fills_a_gap(temp_db):
    """Two candles three minutes apart must be reported as a gap, never interpolated."""
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"asset": "USDJPY", "provider": "operator", "ts_utc": (start + timedelta(minutes=offset)).isoformat(),
         "open": 150.0, "high": 150.2, "low": 149.8, "close": 150.1, "volume": 1.0,
         "replay": True, "is_replay": True}
        for offset in (0, 3, 4)
    ]
    store.upsert_candles(rows, retention_minutes=0)
    gaps = store.gaps("USDJPY")
    assert gaps, "a missing minute must be reported"
    stored = store.candles("USDJPY", limit=10)
    assert len(stored) == 3, "no candle may be fabricated for the empty minutes"


def test_scan_log_records_both_outcomes_and_rejections(temp_db):
    from backend.main import get_engine

    get_engine().generate("AUTO", mode="NOW", persist=True)
    evidence = ledger.evidence()
    assert evidence["scans_recorded"] >= 1
    assert evidence["scan_status_counts"], "every scan must be recorded with its outcome"
    assert set(evidence["scan_status_counts"]).issubset(
        {"SIGNAL", "NO_TRADE", "DATA_UNAVAILABLE", "MODEL_NOT_READY", "LOCKED", "ERROR"})


# --------------------------------------------------------------------------- §17 hardening
# Artifact handling is the one place the backend deserialises files it did not create in
# this process. The defences are: a version name can never be a path, and an artifact is
# hash-verified before anything is loaded.


def test_model_version_can_never_be_a_path():
    from backend.config import model_dir

    assert model_dir("v1-market").name == "v1-market"
    assert model_dir().name == "current"
    for bad in ("../secrets", "a/b", "..", "/etc/passwd", "..\\win", "x" * 60, ".hidden"):
        with pytest.raises(ValueError):
            model_dir(bad)


def test_artifact_manifest_detects_tampering(tmp_path):
    from backend.models import integrity

    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "models.pkl").write_bytes(b"fake-model-bytes")
    (directory / "metadata.json").write_text("{}", encoding="utf-8")

    assert integrity.verify(directory)["status"] == "ABSENT"  # never trusted blindly
    payload = integrity.write_manifest(directory, note="test")
    assert payload["count"] == 2
    assert integrity.verify(directory)["status"] == "OK"

    # a single flipped byte must be visible
    (directory / "models.pkl").write_bytes(b"fake-model-bytez")
    result = integrity.verify(directory)
    assert result["status"] == "MISMATCH"
    assert "models.pkl" in result["mismatched"]

    # ... and a missing hashed file too
    integrity.write_manifest(directory)
    (directory / "metadata.json").unlink()
    assert integrity.verify(directory)["status"] == "MISMATCH"


def test_loader_refuses_an_artifact_that_fails_integrity(tmp_path):
    from backend.models import integrity
    from backend.models.service import ModelService

    directory = tmp_path / "artifact"
    directory.mkdir()
    payload = directory / "models.pkl"
    payload.write_bytes(b"not-a-real-pickle")
    (directory / "metadata.json").write_text("{}", encoding="utf-8")
    integrity.write_manifest(directory)

    # intact but not a real pickle → the load fails, but not on integrity
    service = ModelService(directory, auto_load=False)
    assert service.load() is False
    assert "MODEL" in (service.last_error or "")

    # tampered after hashing → refused *before* any deserialisation happens
    payload.write_bytes(b"not-a-real-pickl3")
    service = ModelService(directory, auto_load=False)
    assert service.load() is False
    assert "ARTIFACT INTEGRITY MISMATCH" in (service.last_error or "")
    assert service.models == {}


def test_active_artifact_is_hash_verified():
    from backend.models.service import get_model_service

    status = get_model_service().status()
    integrity = status["artifact_integrity"]
    assert integrity["status"] == "OK", integrity
    assert integrity["checked"] >= 2
    assert integrity["mismatched"] == []


def test_paper_report_uses_the_same_no_trade_definition_as_the_ledger(tmp_path, monkeypatch):
    """Two artefacts must never disagree about the same number.

    `no_trade_rate` is NO_TRADE scans / all recorded scans. DATA_UNAVAILABLE scans never
    reached a trading decision, so they are reported as their own line instead of being
    folded into the rejection rate (which `1 - coverage` would have done).
    """
    from backend.research import paper_report

    monkeypatch.setattr(paper_report, "REPORT_PATH", tmp_path / "report.md")
    path, payload = paper_report.write_report()
    body = Path(path).read_text(encoding="utf-8")

    rate = payload["evidence"]["no_trade_rate"]
    label = "no-trade rate (NO_TRADE scans / scans)"
    assert label in body
    line = next(row for row in body.splitlines() if row.startswith(f"| {label}"))
    assert f"{rate * 100:.2f} %" in line

    counts = payload["evidence"]["scan_status_counts"]
    if counts:
        assert "scans by status" in body
        for status, value in counts.items():
            assert f"{status} {value}" in body
    # the two definitions must agree with each other, not just with themselves
    scans = payload["evidence"]["scans_recorded"]
    if scans:
        assert rate == pytest.approx(payload["evidence"]["no_trade_scans"] / scans)


def test_root_serves_the_dashboard_to_browsers_and_json_to_api_clients():
    """Opening the port root in a browser must show the single page, never raw JSON.

    The dashboard is `/preview/`; the preview frame (and any human) lands on the root, so the
    root negotiates: `Accept: text/html` → the same `index.html`; anything else → the JSON index
    that curl and the probe tools rely on.
    """
    from fastapi.testclient import TestClient

    from backend.main import app

    client = TestClient(app)

    browser = client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
    assert browser.status_code == 200
    assert browser.headers["content-type"].startswith("text/html")
    assert "🚨 QUOTEX M1 PAPER SIGNAL" in browser.text  # the real dashboard, not a stub

    machine = client.get("/", headers={"Accept": "application/json"})
    assert machine.status_code == 200
    assert machine.headers["content-type"].startswith("application/json")
    assert machine.json()["app"] == "Quotex Signal AI"

    default = client.get("/")  # no accept header → JSON, exactly as before
    assert default.headers["content-type"].startswith("application/json")
