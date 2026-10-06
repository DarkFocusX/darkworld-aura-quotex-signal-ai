"""Continuous M1 data + paper-trading collection tests (spec §17 of this phase).

Covers:

    * continuous feed lifecycle (connect / disconnect / health / latest_tick)
    * M1 candle construction from ticks and from closed candles
    * duplicate tick handling and duplicate candle handling
    * freshness verdicts (FRESH / STALE / INVALID / UNAVAILABLE) and budgets
    * paper scanner: one evaluation per candle, lock on re-scan, no gate weakening
    * verified outcome resolution — including "must stay UNRESOLVED, never guessed"
    * daily signal lock, Telegram gate matrix for paper rows
    * evidence calculations (Wilson bound, losing streak, the published bar)

The tests never touch the production database (temp_db fixture) and never lower a
gate: where a signal is required, the engine is fed through the store and the
environment is switched to development — the same path an operator would use.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.data import store
from backend.data.feed import ContinuousFeed, build_simulated_feed, simulated_tick, validate_candle
from backend.signals import ledger, outcomes
from backend.signals.scanner import PaperScanner, paper_collection_enabled

MINUTE = timedelta(minutes=1)


# ---------------------------------------------------------------------- helpers
def _feed(provider: str = "simulated", **kwargs) -> ContinuousFeed:
    feed = ContinuousFeed(provider=provider, source_label=f"TEST FEED ({provider})",
                          realtime=kwargs.pop("realtime", True),
                          is_simulated=kwargs.pop("is_simulated", provider == "simulated"),
                          is_replay=kwargs.pop("is_replay", False), **kwargs)
    feed.connect()
    return feed


def _pump(feed: ContinuousFeed, asset: str, *, minutes: int, end: datetime,
          ticks_per_minute: int = 4, base: float = 1.1000, step: float = 0.0002) -> None:
    """Deterministic ticks: each minute drifts by `step`, so the built candles are
    predictable and the test never depends on a random walk."""
    start = end - timedelta(minutes=minutes)
    for index in range(minutes):
        minute = start + timedelta(minutes=index)
        price = base + index * step
        for tick in range(ticks_per_minute):
            moment = minute.replace(second=5 + tick * 12)
            feed.ingest_tick(asset, round(price + tick * 0.00001, 6), time_utc=moment)


# ------------------------------------------------------------- §1 continuous feed
def test_feed_lifecycle_and_health(temp_db):
    feed = _feed("broker_ws", is_simulated=False)
    health = feed.health()
    assert health["connected"] is True
    assert health["status"] == "CONNECTED"
    assert health["realtime_capable"] is True
    assert health["supports_subscribe"] is True
    feed.subscribe("USDJPY")
    assert "USDJPY" in feed.health()["subscribed_assets"]

    feed.disconnect()
    assert feed.connected is False
    assert feed.health()["status"] == "DISCONNECTED"
    # a disconnected feed serves nothing — the store stays empty, no invented rows
    assert feed.live_status("USDJPY")["freshness_status"] == "UNAVAILABLE"


def test_ticks_build_one_m1_candle_per_minute(temp_db):
    feed = _feed()
    feed.subscribe("USDJPY")
    end = datetime.now(tz=timezone.utc).replace(second=30, microsecond=0)
    _pump(feed, "USDJPY", minutes=3, end=end, ticks_per_minute=4)

    rows = store.candles("USDJPY")
    assert len(rows) == 2                       # the current minute is still forming
    first = rows[0]
    # o=base, h/l span the four ticks, c=last tick of the minute
    assert first["open"] == pytest.approx(1.1000)
    assert first["close"] == pytest.approx(1.10003)
    assert first["high"] >= first["close"] >= first["low"]
    assert first["provider"] == "simulated"
    assert first["is_simulated"] is True
    assert first["resolution_minutes"] == 1.0
    stamp = datetime.fromisoformat(first["ts_utc"])
    assert stamp.second == 0 and stamp.microsecond == 0      # candle opens on the minute
    assert feed.latest_tick("USDJPY")["price"] > 0            # live price comes from ticks only


def test_duplicate_ticks_are_ignored(temp_db):
    feed = _feed()
    feed.subscribe("USDJPY")
    moment = datetime.now(tz=timezone.utc).replace(second=10, microsecond=0)
    first = feed.ingest_tick("USDJPY", 1.1000, time_utc=moment)
    again = feed.ingest_tick("USDJPY", 1.1000, time_utc=moment)
    assert first["accepted"] is True
    assert again["accepted"] is False and again["status"] == "DUPLICATE"
    fresh = feed.ingest_tick("USDJPY", 1.1001, time_utc=moment)
    assert fresh["accepted"] is True
    assert feed.stats("USDJPY")["duplicate_ticks"] == 1


def test_invalid_ticks_are_rejected_not_stored(temp_db):
    feed = _feed()
    feed.subscribe("USDJPY")
    now = datetime.now(tz=timezone.utc)
    for bad in (0, -1, float("nan"), float("inf"), "abc"):
        receipt = feed.ingest_tick("USDJPY", bad, time_utc=now)
        assert receipt["accepted"] is False
    future = feed.ingest_tick("USDJPY", 1.1, time_utc=now + timedelta(minutes=5))
    assert future["accepted"] is False and "future" in future["reason"]
    assert store.count("USDJPY") == 0
    assert feed.stats("USDJPY")["rejected_ticks"] >= 5


def test_duplicate_candle_updates_the_same_row(temp_db):
    feed = _feed()
    feed.subscribe("USDJPY")
    stamp = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - MINUTE
    candle = {"ts_utc": stamp.isoformat(), "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.105}
    first = feed.ingest_candle("USDJPY", candle)
    second = feed.ingest_candle("USDJPY", {**candle, "close": 1.104})
    assert first["status"] == "CLOSED"
    assert second["status"] == "DUPLICATE_UPDATED"
    rows = store.candles("USDJPY")
    assert len(rows) == 1                      # no second row for the same minute
    assert rows[0]["close"] == pytest.approx(1.104)
    assert feed.stats("USDJPY")["duplicate_candles"] == 1


def test_invalid_candles_are_rejected(temp_db):
    feed = _feed()
    ok, reason = validate_candle({"open": 1.0, "high": 0.9, "low": 0.8, "close": 0.95})
    assert ok is False and "high" in reason
    ok, reason = validate_candle({"open": 1.0, "high": 1.1, "low": 1.05, "close": 1.08})
    assert ok is False and "low" in reason
    receipt = feed.ingest_candle("USDJPY", {"ts_utc": datetime.now(tz=timezone.utc).isoformat(),
                                            "open": 1.0, "high": 0.5, "low": 0.4, "close": 0.9})
    assert receipt["accepted"] is False
    assert store.count("USDJPY") == 0


def test_gaps_are_reported_and_never_filled(temp_db):
    feed = _feed()
    feed.subscribe("USDJPY")
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    for offset in (5, 4, 2):                    # minute 3 is missing from the feed
        stamp = now - timedelta(minutes=offset)
        feed.ingest_candle("USDJPY", {"ts_utc": stamp.isoformat(), "open": 1.1, "high": 1.11,
                                      "low": 1.09, "close": 1.105})
    gap = store.gaps("USDJPY")
    assert gap["missing_minutes"] == 1
    assert len(store.candles("USDJPY")) == 3    # the missing minute stays missing


# ------------------------------------------------------------------- §3 freshness
def test_freshness_verdicts_and_budgets(temp_db):
    feed = _feed()
    feed.subscribe("USDJPY")
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    # newest closed candle = the minute that ended at `now`
    feed.ingest_candle("USDJPY", {"ts_utc": (now - MINUTE).isoformat(), "open": 1.1, "high": 1.11,
                                  "low": 1.09, "close": 1.105})
    fresh = feed.live_status("USDJPY", now=now + timedelta(seconds=30))
    assert fresh["freshness_status"] == "FRESH"
    assert fresh["budget_seconds"] == 120.0     # realtime feed
    assert fresh["age_seconds"] == pytest.approx(30, abs=1)

    stale = feed.live_status("USDJPY", now=now + timedelta(seconds=400))
    assert stale["freshness_status"] == "STALE"
    assert "budget" in stale["detail"]

    other = _feed("csv_replay", is_simulated=False, is_replay=True, realtime=False)
    receipt = other.live_status("USDCHF", now=now)           # nothing stored for this asset
    assert receipt["freshness_status"] == "UNAVAILABLE"
    assert receipt["detail"] and receipt["latest_candle"] is None
    assert receipt["is_replay"] is True


def test_replay_feed_uses_the_replay_budget(temp_db):
    feed = _feed("csv", is_simulated=False, is_replay=True, realtime=False)
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    feed.ingest_candle("USDJPY", {"ts_utc": (now - MINUTE).isoformat(), "open": 1.1, "high": 1.11,
                                  "low": 1.09, "close": 1.105})
    status = feed.live_status("USDJPY", now=now + timedelta(seconds=150))
    assert status["budget_seconds"] == 180.0
    assert status["freshness_status"] == "FRESH"
    assert status["is_replay"] is True


def test_future_dated_row_is_invalid(temp_db):
    feed = _feed()
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    store.upsert_candles([{
        "asset": "USDJPY", "provider": "simulated", "ts_utc": (now + timedelta(minutes=10)).isoformat(),
        "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.105,
        "is_simulated": True, "realtime": True,
    }])
    status = feed.live_status("USDJPY", now=now)
    assert status["freshness_status"] == "INVALID"


# ---------------------------------------------------------------- §4 paper scanner
def test_scanner_never_evaluates_the_same_candle_twice(temp_db, monkeypatch):
    monkeypatch.setenv("DATA_SOURCE_MODE", "simulated")
    feed = _feed()
    feed.subscribe("USDJPY")
    now = datetime.now(tz=timezone.utc)
    _pump(feed, "USDJPY", minutes=8, end=now, base=1.10)
    scanner = PaperScanner(feed, tick_seconds=0.1)
    first = scanner.tick(now)
    evaluated = [d for d in first["scanned"] if d["asset"] == "USDJPY"]
    assert evaluated, first
    assert evaluated[0]["status"] in {"NO_TRADE", "DATA_UNAVAILABLE", "MODEL_NOT_READY", "SIGNAL"}
    second = scanner.tick(now + timedelta(seconds=5))
    assert second["scanned"] == []
    assert any("already evaluated" in s["reason"] for s in second["skipped"])
    assert scanner.skipped_duplicate_candles == 1


def test_scanner_records_the_blocking_reason(temp_db, monkeypatch):
    monkeypatch.setenv("DATA_SOURCE_MODE", "simulated")
    feed = _feed()
    feed.subscribe("USDJPY")
    now = datetime.now(tz=timezone.utc)
    _pump(feed, "USDJPY", minutes=8, end=now)
    scanner = PaperScanner(feed, tick_seconds=0.1)
    decision = scanner.tick(now)["scanned"][0]
    assert decision["status"] in {"NO_TRADE", "DATA_UNAVAILABLE", "MODEL_NOT_READY"}
    assert decision["reason"]                       # a rejection always carries a reason
    assert decision["candle_utc"]
    from backend.database import db

    logged = db.list_scans(limit=1)[0]
    assert logged["scan_mode"] == "PAPER"
    assert logged["asset"] == "USDJPY"


def test_paper_collection_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("PAPER_COLLECTION", raising=False)
    assert paper_collection_enabled() is False
    monkeypatch.setenv("PAPER_COLLECTION", "true")
    assert paper_collection_enabled() is True


# ------------------------------------------------------- §5 ledger / §18 F lock
def _seed_signal(temp_db, *, asset: str = "USDJPY", entry: datetime, direction: str = "CALL",
                 confidence: float = 88.0, simulated: bool = True) -> str:
    uid = f"{asset}|{entry.isoformat()}"
    ledger.record_signal({
        "signal_uid": uid, "asset": asset, "timeframe": "M1", "direction": direction,
        "confidence": confidence, "quality": "HIGH", "entry_time_utc": entry.isoformat(),
        "entry_time_ist": "12:00:00", "entry_time_ist_full": "2026-10-02T12:00:00+05:30",
        "entry_date_ist": "2026-10-02", "expiry_minutes": 1, "expiry_time_ist": "12:01:00",
        "data_timestamp_utc": entry.isoformat(), "data_timestamp_ist": "12:00:00",
        "price": 1.1000, "model_probability": 0.61, "model_agreement": 0.72,
        "is_simulated": simulated, "status": "OPEN", "result": "PENDING", "scan_mode": "NOW",
    }, feed_provider="simulated", feed_status="SIMULATED", model_version="v1-market",
        dataset_version="ds-test", feature_version="m1-features-1.0",
        expiry_time_utc=(entry + MINUTE).isoformat())
    return uid


def test_ledger_records_provenance_and_status(temp_db):
    entry = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    uid = _seed_signal(temp_db, entry=entry)
    row = ledger.rows(limit=1)[0]
    assert row["signal_id"] == uid
    assert row["status"] == ledger.PENDING
    assert row["feed_provider"] == "simulated"
    assert row["model_version"] == "v1-market"
    assert row["feature_version"] == "m1-features-1.0"
    assert row["expiry_time_utc"] == (entry + MINUTE).isoformat()
    assert row["entry_time_utc"] == entry.isoformat()


def test_ledger_provenance_attaches_to_an_engine_inserted_row(temp_db):
    """Regression: the engine inserts the signal row itself, so `ledger_status` is still
    NULL when the scanner attaches provenance. The "never demote a settled row" guard
    must treat NULL as PENDING — otherwise model/dataset/feature versions were silently
    dropped from every real paper signal (only a hand-seeded row hid the bug).
    """
    from backend.database import db

    entry = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    uid = f"USDJPY|{entry.isoformat()}"
    db.save_signal({
        "signal_uid": uid, "asset": "USDJPY", "timeframe": "M1", "direction": "CALL",
        "confidence": 88.0, "quality": "HIGH", "entry_time_utc": entry.isoformat(),
        "entry_time_ist": "12:00:00", "entry_date_ist": "2026-10-02", "expiry_minutes": 1,
        "expiry_time_ist": "12:01:00", "data_timestamp_utc": entry.isoformat(),
        "data_timestamp_ist": "12:00:00", "scan_mode": "NOW", "price": 1.1000,
        "model_agreement": 0.9, "status": "OPEN", "result": "PENDING",
    })
    assert ledger.rows(limit=1)[0]["model_version"] is None       # nothing attached yet

    assert ledger.attach_metadata(uid, ledger_status=ledger.PENDING, feed_provider="operator_csv",
                                  model_version="v1-market", feature_version="m1-features-1.0") is True
    row = ledger.rows(limit=1)[0]
    assert row["status"] == ledger.PENDING
    assert row["feed_provider"] == "operator_csv"
    assert row["model_version"] == "v1-market"
    assert row["feature_version"] == "m1-features-1.0"

    # …and a settled row is still never demoted back to PENDING.
    ledger.settle(uid, ledger.WIN, 1.1005, "verified candle")
    assert ledger.attach_metadata(uid, ledger_status=ledger.PENDING, feed_provider="other") is False
    assert ledger.rows(limit=1)[0]["status"] == ledger.WIN


def test_second_scan_of_a_candle_is_locked(temp_db, settings_simulated, model_ready, monkeypatch):
    """Spec §18 F — the same (asset, candle) can never produce a second paper signal.

    The production gates have their own tests; here the eligibility policy is stubbed
    to allow so the lock rule is the only thing under test — no gate is weakened.
    """
    from backend.risk.policy import RiskPolicy
    from backend.signals import engine as engine_module
    from backend.signals.engine import SignalEngine
    from backend.signals.policy import EligibilityDecision

    if not model_ready:
        pytest.skip("model artifacts required for the full pipeline")
    settings_simulated.environment = "development"   # labelled simulated feed may reach the lock
    feed = _feed()
    feed.subscribe("EURUSD")
    now = datetime.now(tz=timezone.utc)
    _pump(feed, "EURUSD", minutes=8, end=now)
    scanner = PaperScanner(feed, tick_seconds=0.1)
    scanner._engine = SignalEngine(settings_simulated)

    entry_utc = RiskPolicy(settings_simulated).resolve_entry_time(now)
    _seed_signal(temp_db, asset="EURUSD", entry=entry_utc)
    monkeypatch.setattr(engine_module, "evaluate_eligibility",
                        lambda **_: EligibilityDecision(allowed=True, requirements=[]))
    decision = scanner.evaluate_asset("EURUSD", entry_utc.isoformat(), now=now)
    assert decision["status"] == "LOCKED"
    assert "locked" in (decision["reason"] or "").lower()
    assert decision.get("signal_id") is None


# --------------------------------------------------------- §6 verified outcomes
def _verified_pair(temp_db, *, asset: str = "USDJPY", entry: float, exit_price: float,
                   entry_time: datetime, direction: str = "CALL") -> str:
    """Store the one candle a 1-minute paper trade needs: fill at its OPEN, settle on
    its CLOSE (exactly the convention the engine's entry time implies)."""
    feed = _feed()
    feed.ingest_candle(asset, {"ts_utc": entry_time.isoformat(), "open": entry,
                               "high": max(entry, exit_price), "low": min(entry, exit_price),
                               "close": exit_price})
    return _seed_signal(temp_db, asset=asset, entry=entry_time, direction=direction)


def test_outcome_resolution_uses_verified_candles(temp_db):
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    entry_time = now - timedelta(minutes=5)
    uid = _verified_pair(temp_db, entry=1.1000, exit_price=1.1040, entry_time=entry_time)
    receipt = outcomes.resolve_due(now=now)
    settled = [s for s in receipt["settled"] if s["signal_id"] == uid]
    assert settled and settled[0]["result"] == "WIN"
    assert settled[0]["exit_price"] == pytest.approx(1.1040)
    row = ledger.rows(limit=1)[0]
    assert row["status"] == ledger.WIN and row["result"] == "WIN"
    assert row["outcome_source"] == "VERIFIED_CANDLE"
    assert "1.104000" in (row["result_reason"] or "")


def test_outcome_is_a_loss_when_price_moves_against_the_call(temp_db):
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    entry_time = now - timedelta(minutes=5)
    uid = _verified_pair(temp_db, entry=1.1000, exit_price=1.0960, entry_time=entry_time, direction="CALL")
    outcomes.resolve_due(now=now)
    row = [r for r in ledger.rows(limit=5) if r["signal_id"] == uid][0]
    assert row["status"] == ledger.LOSS


def test_put_wins_when_price_falls(temp_db):
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    entry_time = now - timedelta(minutes=5)
    uid = _verified_pair(temp_db, entry=1.1000, exit_price=1.0960, entry_time=entry_time, direction="PUT")
    outcomes.resolve_due(now=now)
    row = [r for r in ledger.rows(limit=5) if r["signal_id"] == uid][0]
    assert row["status"] == ledger.WIN


def test_missing_expiry_candle_is_never_guessed(temp_db):
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    entry_time = now - timedelta(minutes=40)                    # long past the grace window
    uid = _seed_signal(temp_db, entry=entry_time)
    receipt = outcomes.resolve_due(now=now)
    settled = [s for s in receipt["settled"] if s["signal_id"] == uid]
    assert settled and settled[0]["result"] == "UNRESOLVED"
    row = [r for r in ledger.rows(limit=5) if r["signal_id"] == uid][0]
    assert row["status"] == ledger.UNRESOLVED
    assert row["outcome_source"] == "UNVERIFIED"
    assert row["exit_price"] is None                            # no invented price


def test_flat_move_settles_as_void_not_a_win(temp_db):
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    entry_time = now - timedelta(minutes=5)
    uid = _verified_pair(temp_db, entry=1.1000, exit_price=1.1000, entry_time=entry_time)
    outcomes.resolve_due(now=now)
    row = [r for r in ledger.rows(limit=5) if r["signal_id"] == uid][0]
    assert row["status"] == ledger.UNRESOLVED
    assert row["result"] == "VOID"


def test_expiry_still_forming_stays_pending(temp_db):
    now = datetime.now(tz=timezone.utc).replace(second=10, microsecond=0)
    uid = _seed_signal(temp_db, entry=now)                      # expiry candle is the next minute
    receipt = outcomes.resolve_due(now=now)
    assert any(p["signal_id"] == uid for p in receipt["still_pending"])
    row = [r for r in ledger.rows(limit=5) if r["signal_id"] == uid][0]
    assert row["status"] == ledger.PENDING


# ------------------------------------------------------------ §9 daily / §12 evidence
def test_daily_status_is_no_trade_when_nothing_qualified(temp_db):
    scanner = PaperScanner(_feed(), tick_seconds=0.1)
    status = scanner.daily_status()
    assert status["status"] == "NO_TRADE"
    assert status["locked"] is False
    assert "nothing is forced" in status["note"]


def test_daily_status_reports_a_locked_selection(temp_db):
    from backend.database import db

    db.upsert_daily_best("2026-10-02", {"signal_uid": "USDJPY|2026-10-02T06:30:00+00:00",
                                        "asset": "USDJPY", "direction": "CALL", "confidence": 88.0,
                                        "entry_time_ist": "12:00:00", "status": "SIGNAL"})
    import backend.timeutil as tu

    day = tu.ist_clock(datetime.now(tz=timezone.utc))[:10]
    if day != "2026-10-02":                                     # keep the test date-agnostic
        db.upsert_daily_best(day, {"signal_uid": "USDJPY|day", "asset": "USDJPY", "direction": "CALL",
                                   "confidence": 88.0, "entry_time_ist": "12:00:00", "status": "SIGNAL"})
    status = PaperScanner(_feed(), tick_seconds=0.1).daily_status()
    assert status["status"] == "SIGNAL" and status["locked"] is True
    assert "locked" in status["note"]


def test_wilson_lower_bound_matches_known_values():
    assert ledger.wilson_lower_bound(0, 0) is None
    lb = ledger.wilson_lower_bound(60, 100)
    assert 0.49 < lb < 0.52                                     # 60/100 → LB ≈ 0.502
    low = ledger.wilson_lower_bound(4, 10)
    assert low < 0.30                                            # small samples stay unconvincing
    assert ledger.wilson_lower_bound(0, 10) == 0.0


def test_max_losing_streak_counts_only_consecutive_losses():
    assert ledger.max_losing_streak(["LOSS", "LOSS", "WIN", "LOSS"]) == 2
    assert ledger.max_losing_streak(["WIN", "WIN"]) == 0
    assert ledger.max_losing_streak([]) == 0


def test_evidence_bar_needs_sample_size_and_wilson(temp_db):
    entry = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    for index in range(10):
        uid = _seed_signal(temp_db, entry=entry + timedelta(minutes=index))
        ledger.settle(uid, "WIN", 1.2, "verified win", source="VERIFIED_CANDLE")
    evidence = ledger.evidence()
    assert evidence["total_decided_trades"] == 10
    assert evidence["wins"] == 10
    assert evidence["evidence_bar"] == "NOT PASSED"              # 10 wins is not 100 decided
    assert any("need 100" in reason for reason in evidence["reasons"])
    assert evidence["win_rate"] == pytest.approx(1.0)


def test_evidence_bar_passes_only_with_enough_decided_trades(temp_db):
    entry = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    for index in range(120):
        uid = _seed_signal(temp_db, entry=entry + timedelta(minutes=index))
        ledger.settle(uid, "WIN" if index % 3 else "LOSS", 1.2, "verified", source="VERIFIED_CANDLE")
    evidence = ledger.evidence()
    assert evidence["total_decided_trades"] == 120
    assert evidence["max_losing_streak"] >= 1
    if evidence["wilson_95_lower_bound"] > 0.50:
        assert evidence["evidence_bar"] == "PASSED"
    else:
        assert evidence["evidence_bar"] == "NOT PASSED"


def test_ledger_summary_counts_statuses(temp_db):
    entry = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    win = _seed_signal(temp_db, entry=entry)
    loss = _seed_signal(temp_db, entry=entry + MINUTE)
    pending = _seed_signal(temp_db, entry=entry + 2 * MINUTE)
    ledger.settle(win, "WIN", 1.2, "verified", source="VERIFIED_CANDLE")
    ledger.settle(loss, "LOSS", 1.0, "verified", source="VERIFIED_CANDLE")
    day = ledger.rows(limit=3)[0]["entry_date_ist"]
    summary = ledger.summary(day_ist=day)
    assert summary["wins"] == 1 and summary["losses"] == 1 and summary["pending"] >= 1
    assert summary["decided_today"] == 2
    assert summary["win_rate_today"] == pytest.approx(0.5)
    assert pending


# ------------------------------------------------------------------ §13 retrain
def test_retraining_policy_requires_out_of_sample_improvement():
    from backend.research.retrain import RetrainPolicy, should_promote

    policy = RetrainPolicy(interval_days=7, min_new_rows=500, min_improvement=0.01)
    # only a strict holdout improvement beyond the margin promotes
    assert should_promote({"holdout_accuracy": 0.40}, {"holdout_accuracy": 0.415}, policy) is True
    assert should_promote({"holdout_accuracy": 0.40}, {"holdout_accuracy": 0.405}, policy) is False
    assert should_promote({"holdout_accuracy": 0.40}, {"holdout_accuracy": 0.385}, policy) is False
    # a candidate that only wins on training data is never promoted
    assert should_promote({"holdout_accuracy": 0.40, "train_accuracy": 0.30},
                          {"holdout_accuracy": 0.38, "train_accuracy": 0.95}, policy) is False
    # a missing baseline can never promote anything
    assert should_promote({}, {"holdout_accuracy": 0.99}, policy) is False
    assert policy.as_dict()["interval_days"] == 7


# --------------------------------------------------- clock-aware store query (regression)
def test_store_window_respects_the_scan_clock(temp_db):
    """A replay must not see candles that had not closed yet.

    Regression guard for the audit defect: the store provider used to read the *newest*
    N rows and then apply the caller's cutoff, so a scan clocked in the past received an
    empty window and every historical minute looked like `NO DATA`. The query is now
    bounded by the caller's clock instead.
    """
    from backend.data.providers.live_store import LiveStoreProvider

    start = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"asset": "USDJPY", "provider": "operator_csv",
         "ts_utc": (start + i * MINUTE).isoformat(),
         "open": 150.0 + i, "high": 150.5 + i, "low": 149.5 + i, "close": 150.2 + i, "volume": 1.0,
         "replay": True, "is_replay": True, "realtime": False, "source_label": "Operator CSV (REAL)"}
        for i in range(10)
    ]
    store.upsert_candles(rows, retention_minutes=0)

    clock = start + 5 * MINUTE                                  # candles 0–5 have opened, 0–4 have closed
    windowed = store.candles("USDJPY", limit=100, until=clock)
    assert [row["ts_utc"] for row in windowed] == [row["ts_utc"] for row in rows[:6]]

    frame = LiveStoreProvider().fetch("USDJPY", limit=100, now=clock)
    assert len(frame.df) == 5                                   # the 6th candle has not closed yet
    assert frame.meta.latest_candle_close_utc == clock.isoformat()
