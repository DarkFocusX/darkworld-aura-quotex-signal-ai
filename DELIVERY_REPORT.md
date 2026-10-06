# Quotex Signal AI — FINAL MASTER DELIVERY REPORT (Phase 40)

> **SUPERSEDED** by [`FINAL_DELIVERY_REPORT.md`](FINAL_DELIVERY_REPORT.md) (Phase 41, final
> acceptance + hardening pass). This file is kept for history: it records the state after the
> Phase-40 master delivery. Its numbers are still true for that moment, but the current values
> (test count, API probe, artifact integrity, card wording, scenario letters) are in the newer
> report.

**Date:** 2026-10-02 · **Environment:** `production` / mode `PAPER` · **Model in service:** `v1-market`
(READY_MARKET) · **Classification:** **PAPER_TRADING_READY**
**Rule:** every number below is produced by code or an artefact in this repository. Nothing is
invented; where evidence is missing, the answer is NO TRADE.

---

## PROJECT

Single-page M1 signal system on the real pipeline
`fetch → freshness → features → ensemble → confidence → checks/gates → ONE signal or NO_TRADE`,
with IST timing, paper trading, Telegram delivery gating, daily best-signal lock, AUTO SELECT,
continuous-data ingestion, verified outcome resolution, research lab and read-only audit tooling.
Confidence is a calculated score — never an accuracy claim, never a promise. The build is
paper/study only: no order ever reaches a broker.

## BUILD

| item | value |
| --- | --- |
| Python | 3.13.14 · FastAPI 0.142.2 · uvicorn 0.54.0 · pandas 2.2.3 · numpy 2.3.5 |
| ML | scikit-learn 1.6.1 · LightGBM 4.7.0 · joblib 1.5.3 (XGBoost / torch: not installed → reported UNAVAILABLE) |
| Frontend | Node 20 · Next 16.3.8 · TypeScript 5.7.3 · Tailwind 3.4.17 · lightweight-charts 4.2.3 |
| Backend entry | `backend/main.py` — 41 HTTP routes (`GET /` + `/preview/` + 39 API routes) |
| Repo layout | `backend/` (engine, data, models, research, static dashboard) · `frontend/` (Next dashboard) · `tools/` (research + audit) · `tests/` · docs at the root |

## BACKEND

* **Data layer** — provider chain (CSV → broker WS → public delayed → simulated), rolling M1 store with
  per-row provenance, gap *reporting* (never gap filling), freshness budgets (120 s realtime / 180 s
  replay), FRESH / STALE / INVALID / UNAVAILABLE verdicts, `/api/data/live-status`.
* **Engine** — one `SignalEngine` used by the manual button, AUTO SELECT, the daily job and the paper
  scanner (`POST /api/signal/generate` body `{"asset":"AUTO","mode":"NOW"|"DAILY"}`). Nine named
  eligibility requirements with evidence; every failure ends in NO_TRADE / DATA_UNAVAILABLE /
  MODEL_NOT_READY / LOCKED with the exact reason.
* **Clock discipline** — UTC stored, IST displayed, backend-authoritative time, closed-candle
  prediction only. The store query is now clock-aware (`store.candles(..., until=now)`): a scan
  replayed at a historical minute sees exactly the candles that had closed by then. Regression test
  added (`tests/test_paper_phase.py::test_store_window_respects_the_scan_clock`).
* **Paper trading** — every scan recorded (qualified *and* rejected), PENDING → WIN/LOSS only from a
  verified closing candle, UNRESOLVED when the candle cannot be verified (expiry + 15 min grace),
  per-candle lock so a candle is never evaluated twice, provenance on every row.
* **Retraining** — controlled, never continuous: interval + minimum-new-rows gates, promotion only on
  out-of-sample improvement, reproducibility record written.

## FRONTEND

* **Single page** served at `/preview/` (88 494 B): header (DARKWORLD AURA / QUOTEX SIGNAL AI · data
  badge · IST clock · model state) → asset [AUTO] + M1 + GENERATE SIGNAL → SIGNAL CARD (MODEL
  CONFIDENCE, MODEL AGREEMENT) → MARKET CHECKS (DATA · FEATURES · MODEL · ANOMALY · REGIME ·
  CONSISTENCY · CONFIDENCE · RISK · LOCK) → TIMING → SIGNAL HISTORY → LIVE PAPER TRADING →
  MODEL QUALITY → DATA PROVENANCE → OPERATOR DATA IMPORT → SETTINGS. NO TRADE is presented
  intentionally: confidence, real reason, best candidate, next valid scan, scan counters.
* **Next dashboard** (`frontend/`, port 3000) — same panels, same engine through `/api/*`.
* No redesign was performed in this pass; wording rules hold (confidence ≠ accuracy, no
  profit language, SIMULATED/REPLAY never displayed as LIVE).

## API

41 routes; 25 sampled live over HTTP → **25/25 HTTP 200**. Highlights:

```
GET  /api/health · /api/time/ist · /api/assets · /api/market/{asset}
POST /api/signal/generate          → {"status":"DATA_UNAVAILABLE", ...} (0.62 s, AUTO/NOW)
GET  /api/signals/{history,performance,scans,daily} · POST /api/signals/evaluate
GET  /api/model/{status,metrics} · POST /api/model/reload
GET  /api/data/{status,provenance,live-status} · POST /api/data/{import,ingest,ingest/start,ingest/stop}
GET  /api/paper/{summary,card,ledger,scanner} · POST /api/paper/{scan,settle}
GET  /api/research/{experiments,latest,paper-evidence} · POST /api/research/paper-report
GET/POST /api/config · GET /api/system · GET /api/events · GET /preview/ · GET /
```

## TESTS

| check | result |
| --- | --- |
| `python3 -m pytest tests -q` | **206 passed** (5.4 s) |
| `python3 tools/verify_scenarios.py` (§18/§33 A–L) | **14/15 passed + 1 honest absence** (E) → `docs/VERIFICATION_PAPER_PHASE.md` |
| `node tools/smoke_dashboard.mjs` | all assertions passed (incl. MODEL QUALITY, LIVE PAPER TRADING, backend-down path) |
| `tests/test_research_isolation.py` | 5 passed — no backend module can import/mention the oracle, no oracle route exists, the engine's model service is always the real artifact |

## TYPESCRIPT

`cd frontend && npx tsc --noEmit` → **clean** (exit 0).

## BUILD (frontend)

`npm run build` → **PASS** (Next 16.3.8, static `/` + `/_not-found`, types collected, 3/3 pages
generated). `node_modules` was restored with `npm ci` in this environment; the lockfile is committed.

## SECURITY

* `pip-audit -r backend/requirements.txt` → **no known vulnerabilities**.
* Secrets: environment only (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `QUOTEX_WS_URL/SID`); the API
  rejects secret keys in `POST /api/config` (422); Telegram error strings are redacted (the URL
  contains the token); no `.env` value is echoed anywhere; no secret is committed.
* Preview/API docs gated by env (`ALLOW_PREVIEW_ORIGINS`, `ENABLE_API_DOCS`); the simulated feed is
  refused outside development; OTC pairs without a feed report DATA UNAVAILABLE.

## DATA

```
USDJPY  REPLAY  28 292 rows 2026-09-04T06:49Z → 2026-10-02T06:48Z   (32 gaps, 4 session breaks)
USDCHF  REPLAY  28 285 rows 2026-09-04T06:50Z → 2026-10-02T06:49Z   (37 gaps, 4 session breaks)
EURUSD / GBPUSD  DATA_UNAVAILABLE — dataset fails validation (flat feed, 99.8 % open=high=low=close)
AUDUSD           DATA_UNAVAILABLE — 2-minute resolution, not M1
USDBDT-OTCq / EURCAD-OTCq  DATA_UNAVAILABLE — no operator dataset and no public source (OTC)
```

Gaps and weekend closures are reported as gaps; no candle is ever fabricated. The two usable datasets
are **static snapshots**, so the freshness gate (120 s live / 180 s replay) cannot pass → the live
answer is DATA UNAVAILABLE until a continuously updating feed is connected.

## MODEL

| item | value |
| --- | --- |
| artifact | `v1-market` — READY_MARKET (trained on operator-supplied real market data) |
| rows / assets | 56 396 · USDJPY + USDCHF · feature version `m1-features-1.0` |
| walk-forward (out-of-fold) | accuracy 0.3677 · macro F1 0.3664 · ROC-AUC 0.5638 · directional win rate 34.5 % |
| untouched holdout | accuracy 0.3673 · macro F1 0.3658 · ROC-AUC 0.5622 · Brier 0.6563 (11 280 rows) |
| leakage probe | 44 features checked, 0 suspicious |

Read honestly: ~1/3 accuracy on three classes and ROC-AUC ≈ 0.56 is **not** a demonstrated edge. The
engine therefore answers NO TRADE. `READY_MARKET` means “trained on real data”, never “profitable”.

## GATE AUDIT

Full-history audit of the **unchanged production gate stack** over the real operator export
(`persist=False`, nothing written). Window **2026-09-04 12:09 → 2026-10-02 06:47 UTC**, USDJPY,
provider `operator_csv (REAL operator export)`.

* **Total historical candidates: 27 971 minutes**
* **DATA pass %: 99.99 %** (27 942 / 27 946 evaluated; 4 failures = the Sunday 23:00 UTC week-opens where the newest candle really is ~50 h old — the FX market was closed)
* **FEATURE pass %: 99.91 %** (27 942 / 27 967; 25 failures = first minutes after a gap in the export, feature block incomplete)
* **MODEL pass %: 0.06 %** (17 / 27 942; directional probability too low 17 705× · conflicting models 5 404× · NEUTRAL top class 4 500×)
* **ANOMALY pass %: 98.87 %** (27 626 / 27 942)
* **REGIME pass %: 75.83 %** (21 188 / 27 942; choppy / mean-reverting 6 438×)
* **CONSISTENCY pass %: 8.54 %** (2 386 / 27 942; “fewer than 2 of 4 rule groups support the direction” dominates)
* **CONFIDENCE pass %: 0.00 %** (0 / 27 942 — every candidate scored below 85)
* **RISK pass %: not reached** (0 evaluated — no candidate survived to the risk gate)
* **LOCK pass %: 100.0 %** (27 942 / 27 942)
* **Final qualified count: 0** (FINAL 0 / 27 971)

Cumulative funnel (each candidate counted at the first gate that stopped it):
`27 971 → DATA −4 → 27 967 → FEATURE −25 → 27 942 → MODEL −27 925 → 17 → REGIME −7 → 10 →
CONSISTENCY −10 → 0 → CONFIDENCE/RISK/LOCK 0 → FINAL 0`.
Scan outcomes over the window: `NO_TRADE 27 967` + `DATA_UNAVAILABLE 4` → **0 signals**.

**Oracle feasibility (research only, isolated in `tools/`, never reachable from the API, engine,
scanner or Telegram — proven by `tests/test_research_isolation.py`): FEASIBLE.**
27 037 of 27 942 candidates had a decidable next move (905 flat/no settlement); with a *perfect*
direction 461 minutes (1.7 %) cleared every gate. What still blocks a perfect model is CONFIDENCE
(26 120×) and market context — REGIME 6 222×, CONSISTENCY 3 507× structure + conflicting-rule
reasons. Conclusion: the architecture is satisfiable; the learned model is the binding constraint in
the production path; the honest answer stays NO TRADE, and no threshold was changed.

Near-miss mining (`docs/gate_diagnostics.json`, `--from-study`): 2 023 minutes were blocked by the
model layer alone; 19 165 minutes were exactly one gate away (CONSISTENCY); 297 one REGIME away.

## RESEARCH

* **Gate variants A–D** on the identical chronological minutes (`docs/RESEARCH_VARIANTS.md`):
  A (production) 0 signals · B (technical context only through confidence) 0 · C (regime as a −6 pt
  penalty) 0 · D (confidence sweep 60–95): **17 signals at ≥ 60** → 10 decided, 7 wins, hit rate 0.70,
  **Wilson 95 % LB 0.3968**, max losing streak 4 · ≥ 70: 7 signals · ≥ 75: 1 · ≥ 85/90/95: 0.
  **No variant adopted** — 10 decided trades cannot clear the published bar, and variants are never
  adopted for producing more signals.
* **Phases 4–9** (`docs/RESEARCH_REPORT.md/json`): 33 experiment cells, identical chronological folds
  with embargo; model families LogisticRegression / RandomForest / LightGBM plus optional challengers
  (XGBoost, neural) reported **UNAVAILABLE**, never faked; feature families causality-tested; regimes
  out-of-sample only; confidence buckets 50–60 … 95–100 reported with sample counts.
* **Threshold research**: bar = ≥ 100 signals **AND** Wilson 95 % LB > 0.50. Best observed cell reached
  47 signals and LB 0.7102 → **no threshold selected; NO QUALIFIED EDGE** is the recorded result.

## QUALIFIED RESEARCH CELLS

**0 of 33.** Every cell failed the evidence bar (never enough simultaneous signals *and* a
lower-bound above 0.50). The published consequence is exactly what the running system does: **NO TRADE**.

## PAPER TRADING

| item | value |
| --- | --- |
| mode | PAPER (only mode) · `PAPER_COLLECTION=true` records every signal *and* rejection |
| signals generated | **0** · coverage 0.00 % · scans recorded 810 |
| decided trades | 0 · wins 0 · losses 0 · unresolved 0 · pending 0 |
| evidence bar | **NOT PASSED** (needs ≥ 100 decided trades AND Wilson 95 % LB > 0.50) |
| mechanics proven | PENDING → WIN / LOSS / UNRESOLVED from verified candles only; duplicate scan → LOCKED with no second ledger row; provenance (provider, model version, feature version) on every row |

`PAPER_TRADING_REPORT.md` is regenerated from the live database; offline validation metrics are shown
in a separate panel and are never mixed with paper results.

## TELEGRAM

Gate = real + fresh + READY_MARKET + qualified + paper + unlocked. Verified matrix:
production+real+READY_MARKET+qualified → attempts delivery; READY_DEMO → `BLOCKED_NOT_MARKET`;
simulated/replay-only → `BLOCKED_NOT_MARKET`; unqualified → `BLOCKED_NOT_QUALIFIED`; development +
simulated → `BLOCKED_SIMULATED`. NO TRADE / DATA UNAVAILABLE are never sent.
Message format enforced by tests and scenario L: `🚨 QUOTEX M1 PAPER SIGNAL`, asset, `💎 M1`,
`🕐 ENTRY … IST`, `🔺 CALL` / `🔻 PUT`, `🎯 MODEL CONFIDENCE`, `🤖 MODEL AGREEMENT`, `⏳ EXPIRY 1 MIN`,
`📡 DATA: REAL|REPLAY|SIMULATED` (the badge is now authoritative — a replay is never labelled REAL).
No bot token is configured in this environment: **NOT_CONFIGURED** is reported and delivery is proven
against a stub transport, never faked.

## FINAL CLASSIFICATION

**PAPER_TRADING_READY**

| classification | meaning | this build |
| --- | --- | --- |
| RESEARCH_ONLY | pipeline works on historical data only; no real-data model and/or no validated real dataset | no — real data validated, READY_MARKET artifact |
| **PAPER_TRADING_READY** | real data validated + READY_MARKET artifact + gates enforced, but a continuously updating feed (or accumulated paper evidence) is still missing | **yes** |
| LIVE_DATA_READY_FOR_PAPER_SIGNALING | fresh real feed + READY_MARKET + a gate that cleared the evidence bar + enough decided paper trades | no — no live feed, no qualifying gate, no accumulated evidence |

The system is not labelled profitable, guaranteed, sure-shot, risk-free or 100 %-accurate: nothing
measured supports such a claim, and the correct behaviour today is NO TRADE.

## KNOWN EXTERNAL BLOCKERS

1. **No continuously updating real feed.** Every dataset is a static snapshot (newest candle
   06:48 UTC; replay budget 180 s), so a *live* signal is structurally impossible today. The broker
   adapter is complete but `NOT_CONFIGURED` (no `QUOTEX_WS_URL` / `QUOTEX_SSID`, no tick handler
   upstream); the external-push adapter (`POST /api/data/ingest`, `INGESTION_MODE=external`) is tested
   and waiting for a producer.
2. **OTC instruments have no public data source.** `USDBDT-OTCq`, `EURCAD-OTCq` stay DATA UNAVAILABLE;
   normal FX is never presented as OTC.
3. **Public delayed feed** (Yahoo) is rate-limited and, in this environment, returns flat/stale bars
   (EURUSD/GBPUSD 99.8 % flat) → it is labelled REPLAY and never treated as live or as a Quotex feed.
4. **Optional research dependencies unavailable**: XGBoost not installed, neural challenger untrained
   → reported UNAVAILABLE in every artefact (never fabricated).
5. **Telegram credentials absent** → `NOT_CONFIGURED`; the gate and message format are proven with a
   stub transport.
6. **Evidence cannot accumulate** without a live feed: 0 decided paper trades, evidence bar NOT PASSED.

## OPERATOR REQUIREMENTS

1. **A continuously updating real feed** — either
   (a) broker WebSocket: set `QUOTEX_WS_URL` + `QUOTEX_SSID` and attach the tick handler in
   `backend/data/providers/broker_ws.py`, or
   (b) external collector: `INGESTION_MODE=external` and POST verified candles to `/api/data/ingest`
   (`realtime` for live, `replay` for backfill; simulated data is refused outside development).
2. **OTC coverage** (optional): a CSV export or feed for the OTC pairs — nothing is invented for them.
3. **Telegram**: `ENABLE_TELEGRAM=true`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (environment only).
4. **Evidence**: let the scanner run; the system keeps answering NO TRADE until the published bar
   (≥ 100 decided paper trades AND Wilson LB > 0.50) is cleared. That is by design, not a defect.
5. **Model improvement** (optional): retrain on your own validated exports; promotion requires
   out-of-sample improvement over `v1-market`, never a training-accuracy gain.

## EXACT WINDOWS RUN COMMANDS

```powershell
# ---------- PowerShell ----------
cd C:\path\to\quotex-signal-ai
py -3.13 -m pip install -r backend\requirements.txt

# (optional) make the trained market model active if artifacts\current is missing
py -3.13 tools\activate_model.py            # list versions + state
py -3.13 tools\activate_model.py v1-market  # copy it into artifacts\current

# start the API + built-in dashboard
py -3.13 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
#   dashboard  http://localhost:8000/preview
#   API docs   http://localhost:8000/docs

# tests / scenario harness / dashboard smoke / audit (read-only, resumable)
py -3.13 -m pytest tests -q
py -3.13 tools\verify_scenarios.py
node tools\smoke_dashboard.mjs
py -3.13 tools\gate_study.py scan --minutes 0 --workers 2   # then:
py -3.13 tools\gate_study.py analyse
py -3.13 tools\blocking_reasons.py --from-study
py -3.13 -m backend.research.decision_report

# optional: Next.js dashboard
cd frontend
npm ci
npx tsc --noEmit
npm run build
npm run dev            # http://localhost:3000
```

```bat
:: ---------- CMD ----------
cd C:\path\to\quotex-signal-ai
py -3.13 -m pip install -r backend\requirements.txt
py -3.13 tools\activate_model.py v1-market
py -3.13 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
py -3.13 -m pytest tests -q
py -3.13 tools\gate_study.py scan --minutes 0 --workers 2
py -3.13 tools\gate_study.py analyse
```

---

### Changes made in this final pass (all verified)

1. **Clock-aware store query** (`backend/data/store.py::candles(..., until=)`,
   `backend/data/providers/live_store.py`): a replay scan now sees exactly the candles closed by its
   clock. Fixed a real defect that had made every historical minute look like “no data”.
2. **Full-history gate audit re-run** with the fix (27 971 minutes, export-complete) → `GATE_AUDIT.md/json`,
   `docs/RESEARCH_VARIANTS.md`, `docs/gate_diagnostics.json`, `RESEARCH_DECISION.md`.
3. **Audit tooling hardened**: durable, resumable shards inside the workspace
   (`tmp_gate_study_shards/`, `--no-resume` to force a fresh pass), tolerant of interrupted gzip members.
4. **Telegram data badge made authoritative** (a REPLAY of real candles is no longer printed as “REAL”),
   with new tests for the REPLAY / REAL / SIMULATED cases.
5. **`tools/activate_model.py`** — validates provenance and re-creates `artifacts/current` when a
   checkout/restore lost the symlink (this sandbox had lost it; the engine now loads `v1-market` again).
6. **Scenario E/F corrected and separated**: E stays an honest absence (production model: best real
   minute 83.38 < 85); F-aux now demonstrates the emit path on **real candles** with only the model
   layer supplied by the labelled harness double (card `PAPER SIGNAL PUT 86.17 %`, ledger PENDING,
   Telegram not sent), and Telegram L is bound to that same engine-produced signal.
7. `RESEARCH_DECISION.md`, `PAPER_TRADING_REPORT.md`, `README.md`, `docs/ARCHITECTURE.md`, `STATUS.md`
   updated to the corrected numbers (no stale 27 792 / OVERCONSTRAINED text remains).
