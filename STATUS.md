# Quotex Signal AI — FINAL DELIVERY STATUS

**Date:** 2026-10-02 (IST) · **Model in service:** `v1-market` (READY_MARKET) · **Environment:** `production`
**Full-history gate audit:** COMPLETE — 27 971 minutes, 0 signals, oracle verdict FEASIBLE (461/27 037)
**Classification:** **PAPER_TRADING_READY** (see §13) — *not* live-ready.
**Judge of record:** the code, the data and the artefacts in this repository — nothing below is asserted
without a command or a file that reproduces it.

> **Honest headline.** The pipeline is complete and every safety rule is implemented *and tested* on the
> real operator export (USDJPY/USDCHF, 28 292 + 28 285 M1 candles, 2026-09-04 → 2026-10-02).
> On that history the engine emits **no signal**: the gate audit re-ran the unchanged gate stack over
> every one of the **27 971** usable minutes and the final count is **0** (see §12.2). The learned model
> is the first wall — MODEL passes 17 of 27 942 evaluated minutes (0.06 %) — and even when a
> research-only oracle supplies the *actual* next direction, confidence and market-context gates still
> block almost everything: 461 of 27 037 applicable minutes clear every gate (verdict **FEASIBLE**).
> That is the system working as specified — NO TRADE is the correct answer here, and no gate, threshold
> or artefact was adjusted to manufacture anything else.
> Freshness (120 s live / 180 s replay) additionally means the static snapshots in the repository can
> never produce a *live* signal: a continuously updating feed must be connected first (§10).

---

## 1. BUILD

| Item | Status |
| --- | --- |
| Backend (FastAPI, Python 3.13.14) | ✅ runs; `/api/health` over HTTP 200; imports clean, no circular deps |
| Single-page dashboard (`backend/static/index.html`, served at `/preview/` and at `/` for browser clients) | ✅ HTTP 200 (91 947 B), real pipeline behind the button · §14/§15 card wording verbatim · header shows `LIVE FEED <state> · LATEST M1 HH:MM:SS IST · AGE Ns` |
| Next.js dashboard (`frontend/`, Next 16.3.8) | ✅ `npx tsc --noEmit` clean · `npm run build` PASS (static `/` + `/_not-found`) |
| API surface | ✅ 45 registered routes (39 under `/api` + `/`, `/preview`, docs); live HTTP probe `tools/probe_endpoints.py` → **39 ok · 0 failed** (`--include-mutating`), 33 ok · 6 skipped read-only |
| Tests | ✅ **285 passed** (`python -m pytest tests -q`, 11.2 s) — incl. the final-acceptance contract suite, the 50-test live-feed suite (`tests/test_live_feed.py`) and the artifact-integrity tests |
| Dashboard DOM smoke | ✅ `node tools/smoke_dashboard.mjs` — all assertions pass (incl. MODEL QUALITY + LIVE PAPER TRADING, and graceful degraded mode) |
| Scenario harness | ✅ `python3 tools/verify_scenarios.py` — **15/16 checks passed + 1 honest absence** (`F-real`, see §12.1); matrix A–M includes the Phase-42 disconnect case (M) |
| Security | ✅ `pip-audit -r backend/requirements.txt` → no known vulnerabilities (§8) · model artifacts hash-verified before loading (`backend/models/integrity.py`), model versions can never be paths |

## 2. DATA

`GET /api/data/status` (live, repository database):

```
total 7 | available 2 | unavailable 5 | real_market 2 | simulated 0 | replay 2 | stale 2
USDJPY  OK               REPLAY  28 292 rows  (2026-09-04 → 2026-10-02 06:48Z)
USDCHF  OK               REPLAY  28 285 rows
EURUSD  DATA_UNAVAILABLE — dataset fails validation (flat feed: 100 % of candles open=high=low=close)
GBPUSD  DATA_UNAVAILABLE — same
AUDUSD  DATA_UNAVAILABLE — 2-minute resolution (not M1)
USDBDT-OTCq / EURCAD-OTCq  DATA_UNAVAILABLE — no operator dataset, no public source (OTC pair)
```

* Timezone discipline: stored **UTC**, displayed **IST**; every timestamp tz-aware; the browser clock is
  never used for decisions.
* Validation is enforced at **import**, at **fetch**, at **ingest** and at **training** — a dataset that
  fails is never served to the model.
* Four display states implemented: `LIVE | REPLAY | SIMULATED | DATA UNAVAILABLE`; `FRESH / STALE /
  INVALID / UNAVAILABLE` at the feed level (`GET /api/data/live-status`, with age + budget + provider).
* Continuous ingestion: `POST /api/data/ingest` (operator push, replay/realtime, **no** simulated data
  outside development, `422`/`409`/`422` refusals verified), automatic paper scanner
  (`POST /api/data/ingest/start`, never re-evaluates a candle), rolling store with per-row provenance.
* ⚠️ **The two usable datasets are static snapshots** (newest candle 06:48 UTC; replay budget 180 s).
  They support training/study/replay; they cannot produce a live signal.
* Research note (`docs/ARCHITECTURE.md`): the rolling store prunes by wall-clock retention, so
  long-history tools ingest with `retention_minutes=0`; otherwise old minutes would be scored as DATA
  failures (a defect that was found and fixed while building the gate audit).

## 3. MODEL

`GET /api/model/status` (live):

```
state            READY_MARKET          production_usable  true
version          v1-market             data_source_class  MARKET
row_count        56 396                asset_list         [USDJPY, USDCHF]
data_period      2026-09-04T07:52Z → 2026-10-02T06:48Z
validation (out-of-fold, 3 folds, embargo, no shuffle)
                 accuracy 0.3677 · macro F1 0.3664
holdout (untouched last 20 %, 11 280 rows)
                 accuracy 0.3673 · macro F1 0.3658 · ROC-AUC 0.5622 · Brier 0.6563
                 directional win rate 34.5 % · calibration temperature 0.9173
confidence-gate replay (research diagnostic, not a promise)
                 ≥60 % → 294 signals · ≥70 % → 143 · ≥75 % → 96 · ≥80 % → 58
                 ≥85 % → 36 signals, hit rate 44.4 % · ≥90 % → 21
```

**Read this honestly:** on the available real data the 3-class model is barely better than the class
frequency and the ≥85 % band did **not** produce a better-than-chance subset. The consequence is
already implemented: the engine answers `NO TRADE`. `READY_MARKET` means "trained on real data",
never "profitable". Simulated-trained `v2-demo` loads as `READY_DEMO` and can never signal in production.

Research outputs that constrain the model: `GATE_AUDIT.md` (phase 1 + oracle feasibility),
`docs/RESEARCH_VARIANTS.md` (variants A–D), `docs/RESEARCH_REPORT.md` (phases 4–9: targets, families,
features, regimes, calibration, thresholds; XGBoost/neural challengers reported UNAVAILABLE rather than
faked), `RESEARCH_DECISION.md` (classification + reasons).

## 4. API

Verified over HTTP on the running server (all 200 unless stated):

```
GET  /api/health              ok · environment production · model READY_MARKET · PAPER
GET  /api/time/ist            backend-authoritative clock (current / last-closed / next candle, entry, expiry)
GET  /api/assets              universe + availability
GET  /api/market/{asset}      candles + feed provenance + feature snapshot
POST /api/signal/generate     the pipeline. Body {"asset":"AUTO","mode":"NOW"|"DAILY"}
GET  /api/signal/latest       most recent stored signal + last scan
GET  /api/signals/{history,performance,scans,daily,study/{asset}}
POST /api/signals/evaluate    resolve open signals from verified candles
GET  /api/model/{status,metrics} · POST /api/model/reload
GET  /api/data/status · /api/data/provenance · POST /api/data/import
GET  /api/data/live-status    FRESH/STALE/INVALID/UNAVAILABLE + age + budget + gaps + provenance
POST /api/data/ingest · /api/data/ingest/start · /stop
GET/POST /api/paper/scanner · POST /api/paper/scan · POST /api/paper/settle
GET  /api/paper/{summary,card,ledger}
GET  /api/research/{experiments,latest,paper-evidence,experiments/{id}[/signals]}
POST /api/research/paper-report
GET/POST /api/config          runtime settings (secrets rejected: 422)
GET  /api/system · GET /api/events · GET /preview/ · GET /
```

`POST /api/signal/generate` returns, besides the signal: `eligibility` (nine named requirements with
pass/fail + evidence), `scan_summary` (`scanned / qualified / rejected / data_unavailable / locked /
model_not_ready`) and per-asset `rejections` with the blocking reason.

## 5. UI

* **Single page** (`/preview/`): header (DARKWORLD AURA / QUOTEX SIGNAL AI · data badge · IST clock ·
  model state) · ASSET[AUTO] + M1 + GENERATE SIGNAL · SIGNAL CARD (asset, entry IST, direction,
  **MODEL CONFIDENCE**, model agreement, expiry, DATA row) · MARKET CHECKS (DATA / FEATURES / MODEL /
  ANOMALY / REGIME / CONSISTENCY / CONFIDENCE / RISK / LOCK) · TIMING strip · NO TRADE block
  (confidence, real reason, best candidate, next valid scan, scan counters) · SIGNAL HISTORY ·
  **LIVE PAPER TRADING** panel (card, KPIs, evidence bar) · **MODEL QUALITY** panel (artifact, dataset,
  feature version, rows, period, walk-forward + holdout, paper results — offline and live kept apart) ·
  DATA PROVENANCE · OPERATOR DATA IMPORT · SETTINGS.
* **Next dashboard** (`:3000`): same engine through `/api/*` (server-side rewrite, no CORS leaks),
  same panels, MODEL CONFIDENCE wording, mobile-first layout.
* Wording is enforced: "confidence" is never "accuracy"; no guaranteed-profit language anywhere
  (checked by the DOM smoke and the tests).

## 6. TELEGRAM

Gate matrix verified by test (`tests/test_telegram.py`) and by scenarios K/L:

| Environment | Feed | Model | Qualified | Result |
| --- | --- | --- | --- | --- |
| production | real | READY_MARKET | yes | **attempts delivery** |
| production | real | READY_DEMO | yes | `BLOCKED_NOT_MARKET` |
| production | simulated / replay-only | — | — | `BLOCKED_NOT_MARKET` |
| production | real | READY_MARKET | no | `BLOCKED_NOT_QUALIFIED` |
| development | simulated | — | — | `BLOCKED_SIMULATED` (only `TELEGRAM_ALLOW_SIMULATED=true` prefixes a banner) |

`NO_TRADE` / `DATA_UNAVAILABLE` are never sent. Message format matches the spec: title
`🚨 QUOTEX M1 PAPER SIGNAL`, asset, `💎 M1`, `🕐 ENTRY … IST`, `🔺 CALL` / `🔻 PUT`,
`🎯 MODEL CONFIDENCE`, `🤖 MODEL AGREEMENT`, `⏳ EXPIRY 1 MIN`, `📡 DATA: REAL`. Tokens live in the
environment only and are redacted from every error string. Scenario L proves delivery against a stub
transport when credentials are configured; scenario K proves a SIMULATED signal is refused even then.

## 7. PAPER TRADING

* **Default and only mode** (`TRADING_MODE=PAPER`; `PAPER_COLLECTION=true` records every scan —
  qualified *and* rejected).
* `PENDING` → `WIN` / `LOSS` **only** from a verified closing candle; `UNRESOLVED` when the candle
  cannot be verified after the grace window (expiry + 15 min). Verified end-to-end in the harness:
  a CALL recorded on a real candle resolved to **WIN** (`open 157.67200 → close 157.68800`), and a
  missing expiry candle stayed `UNRESOLVED` — nothing was inferred from the prediction.
* Signal lock: one signal per asset per candle; a second scan returns **`LOCKED`** and writes no second
  row (verified in the harness and in `tests/test_paper_phase.py`).
* Evidence bar (published, not adjustable): **≥ 100 decided trades AND Wilson 95 % lower bound > 0.50**.
  Current state `PAPER_TRADING_REPORT.md`: mode PAPER, **0 signals**, 0 decided, coverage 0.00 %,
  no-trade 100 %, evidence bar **NOT PASSED**. Offline validation numbers are never mixed into it.
* Retraining is controlled (`RETRAIN_INTERVAL_DAYS`, `RETRAIN_MIN_NEW_ROWS`,
  `RETRAIN_MIN_IMPROVEMENT`): a candidate is promoted only on out-of-sample improvement, and the
  promotion writes a reproducibility record (dataset fingerprint, feature version, folds, timings).

## 8. SECURITY (spec §14/§29)

| Finding | Severity | Action |
| --- | --- | --- |
| CORS allowed `https?://.*` | HIGH | fixed: same-origin + localhost by default, explicit `ALLOWED_ORIGINS=*` opt-in, preview regex limited to the sandbox host |
| Telegram bot token embedded in exception strings | HIGH | fixed: token redacted from every returned detail |
| Unbounded CSV upload (memory exhaustion) | MEDIUM | fixed: 25 MB cap → HTTP 413, parser caps 500 k rows / 40 columns, NUL + control-char rejection |
| Client `filename` could look like a path | MEDIUM | fixed: validator rejects separators; stored path derives from the validated asset symbol |
| `/docs`, `/redoc`, `/openapi.json` exposed | LOW | switchable via `ENABLE_API_DOCS=false` |
| Secrets through the API | — | rejected (422) — environment only |
| SQL | — | all queries parameterised (sqlite3 placeholders) |
| Dependencies | — | `pip-audit -r backend/requirements.txt` → **no known vulnerabilities**; the advisory set that a bare `pip-audit` reports belongs to preinstalled sandbox packages (aiohttp, jupyter-server, nltk, pip, tornado, urllib3) and is not in this project's dependency tree |
| Debug mode / reloader | — | never enabled in the run commands |

## 9. TEST

* `python -m pytest tests -q` → **285 passed** (live feed: parsing/validation/M1 aggregation/minute
  boundary/duplicates/future stamps/provider states/reconnect/redaction/probe verdicts/external
  ingestion/LIVE FEED state; feed, candles, freshness, scanner, lock, outcomes,
  evidence, confidence/gates, timezone maths incl. IST minute boundaries, API schemas, Telegram gate
  matrix, research isolation, artifact integrity, plus the final-acceptance contract suite:
  one-engine identity, no client-side direction invention, AUTO/DAILY behaviour, provenance
  round-trip, evidence rule + Wilson, §14/§15 dashboard contract, banned claims, feed budgets,
  gaps-reported-not-filled).
* `node tools/smoke_dashboard.mjs` → all assertions pass (page JS executed against canned payloads,
  including the backend-down path).
* `python3 tools/verify_scenarios.py` → **15/16 + 1 honest absence** (`F-real`) → `docs/VERIFICATION_PAPER_PHASE.md`.
* `cd frontend && npx tsc --noEmit && npm run build` → clean / PASS.
* Live-surface check: `python3 tools/probe_endpoints.py` (reads the route table from the app, so it
  cannot drift; read-only by default, restores the scanner state) and
  `python3 tools/model_manifest.py` (artifact hash manifest report).
* Read-only research tools: `gate_study.py` (audit §12.2), `research_report.py` (phases 4–9),
  `blocking_reasons.py --from-study` (near-miss mining), `find_qualified_minute.py` (sweeps —
  superseded as a measurement by `GATE_AUDIT.json`).
* The audit is resumable and durable: shards are written to `tmp_gate_study_shards/` inside the
  project (not `/tmp`), every run merges all generations and `scan` only evaluates the minutes that
  are still missing. A restart therefore costs the minutes in flight, never the completed ones.
* `tools/activate_model.py` re-creates `backend/models/artifacts/current` from a trained version
  (validating provenance, refusing incomplete artifacts) when a checkout/copy lost the symlink.
* Isolation proof: `tests/test_research_isolation.py` — no backend source may reference the oracle or
  any research tool, and no oracle-shaped route may exist. The oracle can therefore never reach live
  inference, the button, the scanner, Telegram or any production path.

## 10. WHAT IS REQUIRED FROM THE OPERATOR

1. **A continuously updating real feed** (the only missing piece for live paper signalling):
   * broker WebSocket — set `QUOTEX_WS_URL` + `QUOTEX_SSID` in `.env` and run
     `python3 tools/start_live_feed.py` — the bridge connects, subscribes, parses, builds M1 candles,
     delivers them to the API and logs health (§10a). Verify first with `python3 tools/probe_live_feed.py`.
     Full step-by-step instructions: **`LIVE_FEED_SETUP.md`** (8 steps, Windows PowerShell commands).
   * external push — POST verified candles *or ticks* to `/api/data/ingest` from your own collector
     (`INGESTION_MODE=external`; ticks are aggregated into M1 candles by one long-lived builder and
     labelled with your provider/`source_label`).
   * no endpoint configured → the system reports `LIVE FEED NOT_CONFIGURED`, never a fabricated candle.
2. **OTC instruments**: no public source exists; they stay `DATA UNAVAILABLE` until your broker feed
   supplies them. Normal FX is never presented as OTC.
3. **Telegram**: `ENABLE_TELEGRAM=true`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (env only).
4. **Evidence**: even with a feed, the system will keep answering NO TRADE until the gate stack clears
   the published bar (≥ 100 decided paper trades AND Wilson LB > 0.50). That is by design.

## 10a. LIVE FEED (Phase 42)

| piece | what it does |
| --- | --- |
| `backend/data/feedsource.py` | WebSocket transport + message parser: JSON ticks/candles, epoch or ISO time, heartbeats ignored, partial candles rejected, auth errors classified; secrets registered for redaction; reconnect with backoff |
| `backend/data/providers/broker_ws.py` | adapter: state machine (NOT_CONFIGURED / CONNECTED / DISCONNECTED / AUTH_FAILED), tick buffer, `latest_tick` / `latest_candles` / `fetch`, validation, provenance, redacted `health()` |
| `backend/signals/feed_state.py` | store-evidence-only summary: state, data, provider, latest candle (UTC + IST), age, budgets, `assets_streaming`, candles still forming; a replay or simulated row can never read LIVE |
| `tools/probe_live_feed.py` | read-only probe → `CONNECTED` / `NOT_CONFIGURED` / `AUTH_FAILED` / `NO_DATA` / `INVALID_DATA` (exit 0/3/4/6/7); `--self-test` proves the plumbing on labelled fixtures |
| `tools/start_live_feed.py` | the bridge: connect → subscribe → parse → build M1 → deliver (`api` or `store`), heartbeat + health logs, safe reconnect, Ctrl+C, `LIVE FEED NOT CONFIGURED` when unset |
| `tools/fixture_feed_server.py` | **verification only** — a local fixture socket (`--i-know-this-is-a-fixture`) used to prove the chain end-to-end; never a provider |
| API | `GET /api/health.live_feed`, `GET /api/system.live_feed`, `GET /api/data/live-status` (per-asset provider/candle/age/resolution/rows/gaps/realtime/simulated/replay/freshness) |

Verified live this phase: fixture socket → bridge (`--target api`) → `POST /api/data/ingest` →
M1 builder → store → `live-status` **FRESH** (age 7.7 s) → dashboard header, on a disposable
database; the repository database stays honestly `NOT_CONFIGURED`. The live run also caught a real
defect (the bridge sent `asset` inside a candle where the schema forbids extras → HTTP 422) which is
fixed and now guarded by a contract test.

## 11. RUN COMMANDS (Windows — PowerShell / CMD)

```powershell
# PowerShell
cd C:\path\to\quotex-signal-ai
py -3.13 -m pip install -r backend\requirements.txt
py -3.13 -m backend.research.train --source csv --assets USDJPY,USDCHF --folds 3 --version v1-market
py -3.13 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000   # /preview/ · /docs
py -3.13 -m pytest tests -q
```

```bat
:: CMD
cd /d C:\path\to\quotex-signal-ai
py -3.13 -m pip install -r backend\requirements.txt
py -3.13 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

The substance of these commands (backend boot, training CLI, tests, dashboard build, HTTP smoke of `/`
and `/preview/`) was executed in this build environment; the Windows shells themselves were not
available here, so nothing above claims a Windows-specific execution result.

## 12. SCENARIO VERIFICATION

### 12.1 §18/§33 scenarios A–M (`docs/VERIFICATION_PAPER_PHASE.md`)

| # | Scenario | Expected | Observed |
| --- | --- | --- | --- |
| A | no real feed | `DATA_UNAVAILABLE` | ✅ engine + store both refuse; store row `None`; no signal row written |
| B | simulated data only | `READY_DEMO`, production blocked | ✅ feed `FRESH` but the scan is `DATA_UNAVAILABLE` ("the configured chain fell back to 'simulated'") — synthetic prices can never back a production signal |
| C | stale real feed | `STALE` → `DATA_UNAVAILABLE` | ✅ STALE (23 063 s old) → `DATA_UNAVAILABLE` (replay budget 180 s) |
| D | fresh real feed + weak model | `NO_TRADE` + the real reason | ✅ FRESH at 06:48:05Z · `NO_TRADE` · conf 40.80 · first blocker "Directional probability too low: 0.34 < 0.58" (520 real rows; no threshold lowered) |
| D2 | fresh feed, model not READY_MARKET | `MODEL_NOT_READY` | ✅ blocked ("READY_DEMO artifact may not signal in production") — a stricter model blocks, never the reverse |
| E | fresh real feed + model disagreement | `NO_TRADE` from the agreement gate | ✅ conf 51.01 · "weighted agreement 0.33 < 0.6" |
| F-real | fresh real feed + production artifact | `SIGNAL` | ⚠️ **NOT DEMONSTRABLE (honest absence)** — 6 real minutes, confidence 74.23–83.38 < 85; full-history audit 27 971 minutes → 0 signals; oracle (research only) clears every gate 461/27 037 times → **FEASIBLE** (see §12.2) |
| F | fresh real feed + qualified pipeline | signal built → ledger `PENDING` → card → Telegram not sent | ✅ `SIGNAL` · real minute 2026-10-02 03:30 UTC (PUT) · card `PAPER SIGNAL PUT 86.17 %` · 1 ledger row PENDING · Telegram NOT SENT — only the learned model *layer* is the labelled harness double, so this is a wiring proof, never a market event |
| G | the same candle scanned twice | `LOCKED`, no second row | ✅ LOCKED · 1 locked attempt · still exactly 1 ledger row |
| H | expiry | verified `WIN` / `LOSS` / `UNRESOLVED` | ✅ all three produced from verified candles only (3 settled, no invented price) |
| I | AUTO SELECT | only qualified candidates compared | ✅ scanned 1 · qualified 0 · unavailable 1 — an asset without a verified feed can never win |
| J | DAILY mode | one best *qualified* signal or NO TRADE | ✅ `NO_TRADE` today (nothing qualified) — never forced |
| K | Telegram with simulated/READY_DEMO data | blocked | ✅ `DISABLED` (unconfigured) and `BLOCKED_NOT_MARKET` (configured) · 0 HTTP calls |
| L | Telegram with a real qualified paper signal | delivered in the spec format | ✅ SENT · HTTP 200 · 1 call · format checked (stub transport; no real token in this build) |
| M | a real feed that **stops** (phase 42) | `DISCONNECTED`, nothing fabricated | ✅ `DISCONNECTED` (age 114 013 s > 300 s window) · freshness `STALE` (never FRESH) · scan `DATA_UNAVAILABLE` · ledger rows **0** — no candle is invented to fill the silence; the recovery half (a closed realtime candle inside the 120 s budget → `FRESH`) is unit-tested in `tests/test_live_feed.py` |
| — | API surface | all endpoints 200 | ✅ live-status, paper summary/card/ledger/scanner, paper-evidence, paper-report |

### 12.2 Gate audit over the real history (phase 1) and oracle feasibility (phase 2)

`tools/gate_study.py` re-ran the **unchanged** production gate stack, one minute at a time, over the
whole usable operator history (`persist=False`: the audit wrote no signal): window
2026-09-04 12:09 → 2026-10-02 06:47 UTC, **27 971 minutes**, USDJPY, operator_csv (REAL export).
Full tables: `GATE_AUDIT.md` + `GATE_AUDIT.json`.

| gate | evaluated | passed | failed | pass % (of evaluated) | pass % (of all candidates) | top failure reason |
| --- | --- | --- | --- | --- | --- | --- |
| DATA | 27 946 | 27 942 | 4 | **99.99 %** | 99.9 % | stale: newest candle 180 005 s old (the four Sunday 23:00 UTC week-opens — the market was closed) |
| FEATURE | 27 967 | 27 942 | 25 | **99.91 %** | 99.9 % | feature block incomplete (first minutes after a gap in the export) |
| MODEL | 27 942 | 17 | 27 925 | **0.06 %** | 0.06 % | directional probability too low (17 705×) / conflicting models (5 404×) / NEUTRAL top class (4 500×) |
| ANOMALY | 27 942 | 27 626 | 316 | **98.87 %** | 98.77 % | feature vector outside the training distribution |
| REGIME | 27 942 | 21 188 | 6 754 | **75.83 %** | 75.75 % | unfavourable regime: choppy / mean-reverting (ADX, efficiency) |
| CONSISTENCY | 27 942 | 2 386 | 25 556 | **8.54 %** | 8.53 % | conflicting indicators — fewer than 2 of 4 rule groups support the direction |
| CONFIDENCE | 27 942 | 0 | 27 942 | **0.00 %** | 0.00 % | confidence below the 85 threshold |
| RISK | 0 | 0 | 0 | — (never reached) | 0.0 % | — |
| LOCK | 27 942 | 27 942 | 0 | **100.0 %** | 99.9 % | — |
| FINAL | 27 971 | **0** | 27 971 | **0.00 %** | 0.00 % | — |

**Total historical candidates:** 27 971 · **final qualified count: 0** · scan outcomes
`NO_TRADE 27 967` + `DATA_UNAVAILABLE 4`.

Cumulative funnel (each candidate counted at the first gate that stopped it):
`27 971 → DATA −4 → 27 967 → FEATURE −25 → 27 942 → MODEL −27 925 → 17 → REGIME −7 → 10 →
CONSISTENCY −10 → 0 → CONFIDENCE/RISK/LOCK 0 → FINAL 0`.

**Oracle feasibility (research only — the future direction is known in advance, inside
`tools/gate_study.py` only): FEASIBLE.** 27 037 of 27 942 candidates had a decidable move (905 flat /
no settlement); with a perfect direction **461** cleared every gate (1.7 %). The gates that still block
a perfect model are CONFIDENCE (26 120×) and market context — REGIME (6 222×), CONSISTENCY (3 507×
structure + conflicting-rule reasons). Conclusion: the architecture is satisfiable, the learned model
is the binding constraint in the production path, and the honest answer stays NO TRADE.

Near-miss mining (`docs/gate_diagnostics.json`): 2 023 minutes were blocked by the model layer alone,
and 19 165 minutes were exactly one gate away (CONSISTENCY). Variants A–D were recomputed on the same
minutes (`docs/RESEARCH_VARIANTS.md`): A/B/C = 0 signals; the D sweep produced 17 signals at confidence
≥ 60 (10 decided, 7 wins, 0.70 hit rate, **Wilson 95 % LB 0.3968**, losing streak 4), 7 at ≥ 70, 1 at
≥ 75, none at ≥ 85. **No variant is adopted** — 10 decided trades cannot clear the published bar
(≥ 100 decided **and** Wilson LB > 0.50), and a variant is never adopted for producing more signals.

### 12.3 Paper-evidence tracker

`PAPER_TRADING_REPORT.md`: 0 signals collected, 903 scans recorded at the last regeneration (748
DATA_UNAVAILABLE · 155 NO_TRADE — the live scanner keeps recording one outcome per closed candle per
enabled asset, so the count grows while the API runs), coverage 0.00 %, no-trade rate **17.17 %**
(NO_TRADE ÷ all recorded scans — one definition shared by the ledger evidence, the dashboard and the
report), evidence bar **NOT PASSED** (shortfall: 0 of 100 decided trades; Wilson LB not above 0.50). Calibration buckets are reported per spec (50–60 … 95–100) with
sample counts so a thin bucket can never look like evidence.

## 13. FINAL CLASSIFICATION

**PAPER_TRADING_READY**

| classification | meaning | this build |
| --- | --- | --- |
| RESEARCH_ONLY | pipeline works on historical data only; no real-data model and/or no validated real dataset | no — real data validated, READY_MARKET artifact |
| **PAPER_TRADING_READY** | real data validated + READY_MARKET artifact + gates enforced, but a continuously updating feed (or accumulated paper evidence) is still missing | **yes** — this is the state |
| LIVE_DATA_READY_FOR_PAPER_SIGNALING | fresh real feed + READY_MARKET + a gate that cleared the evidence bar + enough decided paper trades | no — no live feed, no qualifying gate, no accumulated evidence |

The system is **not** labelled profitable, guaranteed or risk-free: nothing measured here supports such
a claim, and the correct behaviour today is NO TRADE.
