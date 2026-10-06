# Quotex Signal AI — FINAL MASTER DELIVERY REPORT (Phase 41)

**Date:** 2026-10-02 · **Environment:** `production` · **Mode:** `PAPER` · **Model in service:**
`v1-market` (READY_MARKET, artifact hash-verified) · **Classification:** **PAPER_TRADING_READY**

**Rule of this report:** every number below was produced by code or by an artefact in this
repository during the final acceptance pass. Nothing is invented, no gate was relaxed, no threshold
was lowered, and no market data, prediction, metric or win/loss was fabricated. Where the evidence
is missing the system answers **NO TRADE**, and that is reported as the correct result.

This report supersedes `DELIVERY_REPORT.md` (Phase 40, kept for history — see the header note in
that file).

> **Phase 42 addendum (2026-10-03):** the continuous live-feed path (adapter, transport, parser,
> M1 bridge, probe tool, LIVE FEED status, operator setup guide) is now complete and verified — see
> **PHASE 42 — LIVE DATA FEED** at the end of this report. Numbers below that changed in Phase 42
> (test count, dashboard size, route count) are updated in place; the classification does **not**
> change: a feed is still not connected.

---

## PROJECT

Single-page M1 signal system on the real pipeline
`fetch → freshness → features → ensemble → confidence → checks/gates → ONE signal or NO_TRADE`,
with IST timing, paper trading, Telegram delivery gating, daily best-signal lock, AUTO SELECT,
continuous-data ingestion, verified outcome resolution, a research lab and read-only audit tooling.

Commitments that hold in this build: confidence is a **calculated model score**, never an accuracy
claim and never mixed with live win rate; one engine serves the manual button, AUTO SELECT, the
daily job and the paper scanner; a production signal requires
`REAL + FRESH + READY_MARKET + VALID FEATURES + MODEL + ANOMALY + REGIME + CONSISTENCY + CONFIDENCE + RISK + LOCK`,
and any failure ends in `NO_TRADE` / `DATA_UNAVAILABLE` / `MODEL_NOT_READY` / `LOCKED` with the exact
reason. No order ever reaches a broker.

## BUILD

| item | value |
| --- | --- |
| Python | 3.13.14 · fastapi 0.142.2 · uvicorn 0.54.0 · numpy 2.3.5 · pandas 2.2.3 · scikit-learn 1.6.1 · lightgbm 4.7.0 · scipy 1.17.1 · pytest 9.0.3 |
| Node | 20 · Next.js 16.3.8 (lockfile committed, `npm ci` restored) |
| Sandbox | 2 CPU · 1984 MB — the audit runs in durable, resumable shards; training never blocks inference |
| Entry points | `python3 -m uvicorn backend.main:app --port 8000` (API + `/preview/` dashboard) · `frontend/` (Next dashboard, port 3000) |
| Optional deps | xgboost / torch **not installed** → every artefact reports the challengers `UNAVAILABLE` (never fabricated) |

## BACKEND

* **One engine, one path.** `backend/signals/engine.py::SignalEngine` is the only producer of
  signals; the button, AUTO SELECT, the daily job and the paper scanner all call it. Contract tests
  assert that no client-side logic (and no random/demo fallback) can create a direction.
* **Data layer.** Provider chain (CSV → broker WS → public delayed → simulated) with connect /
  disconnect / health / latest tick / latest candles / subscribe, a rolling M1 store with per-row
  provenance, gap **reporting** (never gap filling), freshness budgets **120 s realtime / 180 s
  replay**, verdicts `FRESH / STALE / INVALID / UNAVAILABLE`, and `/api/data/live-status` per asset
  (provider, latest candle, age, resolution, rows, gaps, simulated, replay, realtime, freshness).
* **Clock discipline.** UTC stored, `Asia/Kolkata` displayed, backend-authoritative time, prediction
  from **closed candles only**. The store query is clock-aware (`store.candles(..., until=now)`), so
  a scan replayed at a historical minute sees exactly the candles closed by then. Minute-boundary
  regressions are covered in `tests/test_timeutil.py` (entry = next M1 boundary, expiry = entry + 1
  minute, countdown in (0, 60], IST grid preserved).
* **Paper trading ledger.** Every scan is recorded — qualified *and* rejected
  (`PAPER_COLLECTION=true`). `PENDING → WIN / LOSS / UNRESOLVED` only from verified future candle
  data (expiry + 15 min grace; ATR·0.25 → VOID); a candle is never evaluated twice (per-candle lock,
  duplicate scan → `LOCKED`, no second row). Every row carries signal id, asset, direction, entry,
  expiry, confidence, model version, dataset version, feature version, provider, feed status and
  provenance.
* **Provenance (new in this pass).** `backend/data/provenance.py::window_version()` derives a
  deterministic data-window id `win-<sha256[:12]>` (separate id space from the research `ds-…` ids)
  from asset, provider, source class, badge, window bounds, row count and the simulated/replay/static
  flags. `SignalEngine._build_signal` emits `model_version`, `feature_version`, `dataset_version`
  (= window id) and `feed_status`; the emission path attaches them to the ledger row via
  `ledger.attach_metadata(...)` and logs a `PROVENANCE` ERROR event if that ever fails.
  End-to-end proof (temp DB): row `USDJPY|2026-10-02T03:31:00+00:00`, PUT 86.17, entry IST 09:01:00,
  expiry 09:02:00, `win-e91bffa4e312`, `m1-features-1.0`, `v1-market`, `operator_csv`, `OK`,
  `PENDING` / `outcome_source: None`.
* **Retraining** stays controlled: interval + minimum-new-rows gates, chronological validation and an
  untouched holdout; promotion only on out-of-sample improvement over the incumbent, otherwise
  `KEEP CURRENT MODEL`.
* **Artifact integrity (new in this pass).** A model version can no longer be a path
  (`config.model_dir()` rejects separators, `..`, absolute-style names and over-long slugs), and
  `backend/models/integrity.py` hashes every artifact file into `manifest.json` (written by
  `train`, `tools/activate_model.py` or `tools/model_manifest.py`). `ModelService.load()` verifies the
  manifest **before** deserialising: a mismatched file is refused with
  `ARTIFACT INTEGRITY MISMATCH` instead of being loaded; `/api/model/status` reports
  `artifact_integrity: {status: OK, checked: 4, mismatched: []}` for the active `v1-market` copy.

## FRONTEND

* **Single page** served at `/preview/` **and at `/`** for browser clients (91 947 B, HTTP 200
  `text/html`): header (DARKWORLD AURA /
  QUOTEX SIGNAL AI · data badge · IST clock · model state) → asset `[AUTO]` + `M1` + GENERATE SIGNAL
  → signal card → MARKET CHECKS (DATA · FEATURES · MODEL · ANOMALY · REGIME · CONSISTENCY ·
  CONFIDENCE · RISK · LOCK) → TIMING → SIGNAL HISTORY → LIVE PAPER TRADING → MODEL QUALITY → DATA
  PROVENANCE → OPERATOR DATA IMPORT → SETTINGS. No redesign was performed.
* **§14 card wording** now used verbatim by the served dashboard and by the Next components:
  `🚨 QUOTEX M1 PAPER SIGNAL`, asset, `💎 M1`, `🕐 ENTRY HH:MM:SS IST`, `🔺 CALL` / `🔻 PUT`,
  `🎯 MODEL CONFIDENCE`, `🤖 MODEL AGREEMENT`, `⏳ EXPIRY 1 MIN`, `📡 DATA: REAL|REPLAY|SIMULATED`
  (plus provider / model / ledger line). The “copy signal” text carries the same block.
* **§15 NO TRADE presentation** is intentional, never an error: `NO TRADE`, *Confidence*, *Reason*,
  *best candidate*, *next valid scan*, scan counters, and the sentence “Nothing is forced: a signal
  appears only when every gate passes.”
* Offline validation and paper results stay in separate panels; SIMULATED/REPLAY is never displayed
  as LIVE; the served page contains no profit/accuracy/“sure shot” claim other than the sentences
  that explicitly deny them.
* **Next dashboard** (`frontend/`): same engine through `/api/*`, same card wording, MODEL QUALITY
  and LIVE PAPER TRADING panels unchanged.

## API

**45 registered routes** (39 under `/api` + `/`, `/preview`, docs/redirects and the static mount);
the live probe
(`tools/probe_endpoints.py`, added in this pass) reads the route table from the app itself so it
cannot drift from the code.

| probe | result |
| --- | --- |
| `python3 tools/probe_endpoints.py` (read-only surface) | **33 ok · 0 failed · 6 skipped** (mutating routes are skipped by default — verification must not change the evidence it measures) |
| `python3 tools/probe_endpoints.py --include-mutating` | **39 ok · 0 failed · 0 skipped** |

Notable live answers: `POST /api/signal/generate {"asset":"AUTO","mode":"NOW"}` → `DATA_UNAVAILABLE`
(0.9 s); `GET /api/signals/daily` → `NO_TRADE`; `GET /api/paper/card` → `NO TRADE` (“newest candle
closed 23 289 s ago, limit 180 s for a replay feed”); `GET /api/data/live-status` →
`{FRESH:0, STALE:0, INVALID:0, UNAVAILABLE:7}`, budgets 120/180 s; `GET /api/research/paper-evidence`
→ `NOT PASSED`; `GET /api/model/status` → READY_MARKET `v1-market` (lightgbm / random_forest /
logistic_regression loaded, xgboost `Not available (optional dependency 'xgboost' not installed)`);
`POST /api/data/ingest` with an invalid payload → **422** (never a silent accept).
`GET /api/system` is extended (readiness + feed + paper + research + active artifact, 60 s cache,
`?refresh=true` to bypass). `GET /` negotiates: a browser (or the sandbox preview frame, which sends
`Accept: text/html`) receives the single-page dashboard itself, while a programmatic caller with no
HTML accept header still receives the documented JSON API index — so the port root is useful to both
a human and a script, and `/preview/` keeps serving the same file.

## TESTS

| check | result |
| --- | --- |
| `python3 -m pytest tests -q` | **285 passed** (11.2 s) — +50 live-feed contracts in Phase 42 |
| `tests/test_final_acceptance.py` (**new**, 25 contract tests) | passed — one-engine identity, no client-side direction invention, AUTO counters, DAILY never forces (and writes no row), daily cap, window-version determinism/sensitivity, ledger provenance round-trip, AST guard on the emitted keys, evidence rule + Wilson (7/10 = 0.3968), §14/§15 dashboard contract, no marketing claims, Telegram off + zero HTTP without credentials, demo/stale ineligibility, `/api/system` state keys, feed freshness budgets, gaps reported not filled, scan-log outcomes, artifact-integrity suite |
| `python3 tools/verify_scenarios.py` (A–M matrix) | **15/16 passed + 1 honest absence** (`F-real`) → `docs/VERIFICATION_PAPER_PHASE.md` |
| `node tools/smoke_dashboard.mjs` | all assertions passed (incl. MODEL QUALITY, LIVE PAPER TRADING, backend-down path, `/api/system` readiness + feed chip) |
| `tests/test_timeutil.py` | 13 passed (incl. 4 minute-boundary regressions) |
| `tests/test_research_isolation.py` | 5 passed — the oracle can never be imported by a backend module, no oracle route exists, the engine always uses the real artifact |

## TYPESCRIPT

`cd frontend && npx tsc --noEmit` → **clean** (exit 0). The `Signal` type now declares the
provenance fields the engine emits (`model_version`, `feature_version`, `dataset_version`,
`feed_status`), which is what the new card block reads.

## BUILD (frontend)

`npm run build` → **PASS** — Next.js 16.3.8 (Turbopack), “Compiled successfully”, TypeScript check
passed, 3/3 static pages generated, routes `/` and `/_not-found`.

## SECURITY

* `pip-audit -r backend/requirements.txt` → **no known vulnerabilities**.
* **Secrets:** environment only (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `QUOTEX_WS_URL/SID`).
  `POST /api/config` rejects secret keys (422); Telegram error strings are redacted (the URL contains
  the token); no `.env` value is echoed; nothing secret is committed; a repo scan for hard-coded
  tokens/secrets/passwords in `backend/` returns nothing.
* **Input validation:** `POST /api/data/ingest` and `POST /api/config` use `extra="forbid"` models;
  CSV import is text-only (no raw upload, no user-controlled path), capped at 40 MB / 40 columns /
  `max_rows`, rejects NUL bytes, wrong resolution, duplicates, non-monotonic or future timestamps,
  invalid OHLC, flat/missing/insufficient data and unsafe file names; the stored file name is derived
  from the validated symbol, and an advisory `filename` containing `/`, `\` or `..` is rejected.
* **SQL:** all queries are parameterised; the only interpolated identifiers are module-level table
  names. **No `pickle`/`eval`/`exec`** in backend code — artifacts are joblib/scikit-learn and are
  now hash-verified before loading (see BACKEND).
* **Surface control:** CORS restricted to localhost plus an explicit preview-host regex
  (`ALLOW_PREVIEW_ORIGINS`), credentials off, methods `GET/POST/OPTIONS`; `/docs`, `/redoc` and
  `/openapi.json` can be switched off with `ENABLE_API_DOCS=false`; the simulated feed is refused
  outside development; OTC pairs without a feed report DATA UNAVAILABLE.
* **Resource discipline:** the automatic scanner evaluates each closed candle once and drops
  unavailable/stale assets; large datasets are never held in API memory; research runs are resumable;
  training never blocks inference; the served dashboard stays lightweight (no external assets).

## DATA

```
USDJPY  REPLAY  28 292 rows 2026-09-04T06:49Z → 2026-10-02T06:48Z   (32 gaps, 4 session breaks)
USDCHF  REPLAY  28 285 rows 2026-09-04T06:50Z → 2026-10-02T06:49Z   (37 gaps, 4 session breaks)
EURUSD / GBPUSD / AUDUSD  DATA_UNAVAILABLE — dataset fails validation (flat feed ≈ 99.8 %; AUDUSD 2-min)
USDBDT-OTCq / EURCAD-OTCq DATA_UNAVAILABLE — OTC, no operator dataset and no public source
```

Live store right now: `{FRESH: 0, STALE: 0, INVALID: 0, UNAVAILABLE: 7}`, newest candle `null`,
budgets 120 s (realtime) / 180 s (replay). Providers: `csv REPLAY_ONLY` · `broker_ws NOT_CONFIGURED` ·
`public_feed REPLAY_ONLY` · `simulated READY` (never allowed to signal in production). Gaps and
weekend closures are reported as gaps; **no candle is ever fabricated**. The two usable datasets are
static snapshots, so the freshness gate cannot pass and the live answer stays DATA UNAVAILABLE until
a continuously updating feed is connected.

## MODEL

| item | value |
| --- | --- |
| artifact | `v1-market` — READY_MARKET (trained on operator-supplied real market data) · integrity OK (4 files hashed) |
| rows / assets | 56 396 · USDJPY + USDCHF · feature version `m1-features-1.0` |
| walk-forward (out-of-fold) | accuracy 0.3677 · macro F1 0.3664 · ROC-AUC 0.5638 · directional win rate 34.5 % |
| untouched holdout | accuracy 0.3673 · macro F1 0.3658 · ROC-AUC 0.5622 · Brier 0.6563 (11 280 rows) |
| leakage probe | 44 features checked, 0 suspicious |
| challengers | xgboost **UNAVAILABLE** (dependency not installed) · neural UNAVAILABLE (untrained) |

Read honestly: ~1/3 three-class accuracy and ROC-AUC ≈ 0.56 is **not** a demonstrated edge. The
engine therefore answers NO TRADE. `READY_MARKET` means “trained on real data” — it is not a
profitability statement.

## GATE AUDIT

Full-history audit of the **unchanged production gate stack** over the real operator export
(`persist=False`, nothing written). Window **2026-09-04 12:09 → 2026-10-02 06:47 UTC**, USDJPY,
provider `operator_csv (REAL operator export)`, **27 971 minutes**.

* **DATA** pass 99.99 % (27 942 / 27 946; 4 failures = Sunday week-opens where the newest candle is
  genuinely ~50 h old — the FX market was closed)
* **FEATURE** 99.91 % (27 942 / 27 967; 25 failures = first minutes after a gap)
* **MODEL** 0.06 % (17 / 27 942; directional probability too low 17 705× · conflicting models 5 404× ·
  NEUTRAL top class 4 500×)
* **ANOMALY** 98.87 % (27 626 / 27 942) · **REGIME** 75.83 % (21 188 / 27 942; choppy 6 438×)
* **CONSISTENCY** 8.54 % (2 386 / 27 942) · **CONFIDENCE 0.00 %** (0 / 27 942 — every candidate scored
  below 85) · **RISK not reached** · **LOCK 100 %**
* **Final qualified count: 0** — scan outcomes over the window: `NO_TRADE 27 967` + `DATA_UNAVAILABLE 4`.

Funnel (each candidate counted at the first gate that stopped it):
`27 971 → DATA −4 → 27 967 → FEATURE −25 → 27 942 → MODEL −27 925 → 17 → REGIME −7 → 10 →
CONSISTENCY −10 → 0 → CONFIDENCE/RISK/LOCK 0 → FINAL 0`.

**Oracle feasibility (research only, isolated in `tools/`, unreachable from the API, engine, scanner
and Telegram — proven by `tests/test_research_isolation.py`): FEASIBLE.** 461 of 27 037 decidable
minutes (905 flat excluded) cleared every gate with a *perfect* direction; what still blocks a
perfect model is CONFIDENCE (26 120×) and market context (REGIME 6 222×, CONSISTENCY 3 507×).
Conclusion: the architecture is satisfiable; the learned model is the binding constraint in the
production path; the honest answer stays NO TRADE and **no threshold was changed**.

Near-miss mining (`docs/gate_diagnostics.json`): 2 023 minutes blocked by the model layer alone;
19 165 minutes exactly one gate away (CONSISTENCY); 297 one REGIME away.

## RESEARCH

* **Gate variants A–D** on identical chronological minutes (`docs/RESEARCH_VARIANTS.md`):
  A (production) 0 · B (technical context routed through confidence) 0 · C (regime as a −6 pt penalty)
  0 · D (confidence sweep 60–95): **17 signals at ≥ 60** → 10 decided, 7 wins, hit rate 0.70,
  **Wilson 95 % LB 0.3968**, max losing streak 4; ≥ 70: 7 signals / 5 decided / 0.2307; ≥ 75: 1
  signal / 0 decided; ≥ 80/85/90/95: 0. **No variant adopted** — 10 decided trades cannot clear the
  published bar, and variants are never adopted for producing more signals.
* **Phases 4–9** (`docs/RESEARCH_REPORT.md/json`): 33 experiment cells, identical chronological folds
  with embargo; LogisticRegression / RandomForest / LightGBM plus optional challengers reported
  UNAVAILABLE (never faked); feature families causality-tested; regimes out-of-sample only;
  confidence buckets 50–60…95–100 with sample counts.
* **Threshold research:** bar = ≥ 100 signals **AND** Wilson 95 % LB > 0.50. Best cell reached
  47 signals with LB 0.7102, 0 cells selected a threshold → **NO QUALIFIED EDGE HAS BEEN
  DEMONSTRATED** (frozen wording, not a software failure).

## QUALIFIED RESEARCH CELLS

**0 of 33.** Every cell failed the evidence bar (never both enough simultaneous signals *and* a
lower bound above 0.50). The published consequence is exactly what the running system does: NO TRADE.

## PAPER TRADING

| item | value |
| --- | --- |
| mode | PAPER (only mode) · `PAPER_COLLECTION=true` records every signal *and* rejection |
| signals generated | **0** · coverage 0.00 % · scans recorded **903** at report time (the live scanner keeps recording one outcome per closed candle per enabled asset, so this number grows while the API runs; every recorded outcome so far is DATA_UNAVAILABLE or NO_TRADE) |
| decided trades | 0 · wins 0 · losses 0 · unresolved 0 · pending 0 · max losing streak 0 |
| no-trade rate | **17.17 %** = 155 NO_TRADE / 903 recorded scans; the remaining **748** scans are DATA_UNAVAILABLE (no trading decision was ever possible). One definition, used by the ledger evidence, the dashboard and `PAPER_TRADING_REPORT.md`: NO_TRADE scans ÷ all scans, with the status breakdown shown next to it |
| evidence bar | **NOT PASSED** — “only 0 decided paper trades (need 100)”, “Wilson 95 % lower bound not above 0.50” |
| mechanics proven | PENDING → WIN / LOSS / UNRESOLVED from verified candles only (scene H: 3 settled) · duplicate scan → LOCKED with no second row (scene G) · provenance on every row · DAILY never forced (scene J) · AUTO SELECT drops unavailable assets (scene I) |

`PAPER_TRADING_REPORT.md` is regenerated from the live database
(`POST /api/research/paper-report`, exercised live). Offline validation metrics are shown in a
separate panel and are never mixed with paper results.

## TELEGRAM

Gate = real + fresh + READY_MARKET + qualified + paper + unlocked. Verified matrix: production +
real + READY_MARKET + qualified → attempts delivery; READY_DEMO → `BLOCKED_NOT_MARKET`;
simulated/replay-only → `BLOCKED_NOT_MARKET`; unqualified → `BLOCKED_NOT_QUALIFIED`; development +
simulated → `BLOCKED_SIMULATED`; disabled → `DISABLED` with **zero HTTP calls**. NO TRADE and
DATA UNAVAILABLE are never sent. Message format enforced by tests and by scenario L:
`🚨 QUOTEX M1 PAPER SIGNAL`, asset, `💎 M1`, `🕐 ENTRY HH:MM:SS IST`, `🔺 CALL` / `🔻 PUT`,
`🎯 MODEL CONFIDENCE`, `🤖 MODEL AGREEMENT`, `⏳ EXPIRY 1 MIN`, `📡 DATA: REAL` (the data badge is
authoritative — a replay is never labelled REAL, and a REPLAY signal is never sent as REAL).
No bot token exists in this environment: **NOT_CONFIGURED** is reported and delivery is proven
against a stub transport (http 200, 1 call, format checked), never faked.

## FINAL CLASSIFICATION

**PAPER_TRADING_READY**

| classification | meaning | this build |
| --- | --- | --- |
| RESEARCH_ONLY | pipeline works on historical data only; no real-data model and/or no validated real dataset | no — real data validated, READY_MARKET artifact |
| **PAPER_TRADING_READY** | real data validated + READY_MARKET artifact + gates enforced, but a continuously updating feed (and accumulated paper evidence) is still missing | **yes** |
| LIVE_DATA_READY_FOR_PAPER_SIGNALING | fresh real feed + READY_MARKET + a gate that cleared the evidence bar + enough decided paper trades | no — no live feed, no qualifying gate, no accumulated evidence |

The system is **not** labelled profitable, guaranteed, sure-shot, risk-free or 100 %-accurate:
nothing measured supports such a claim, and the correct behaviour today is NO TRADE. Acceptance in
this report means **software complete** — pipeline, gates, timing, ledger, provenance, security,
tests, docs — never “profitable” and never “live ready”.

## KNOWN EXTERNAL BLOCKERS

1. **No continuously updating real feed.** Every dataset is a static snapshot (newest candle
   06:48 UTC vs a 180 s replay budget), so a *live* signal is structurally impossible today. The
   broker adapter is complete but `NOT_CONFIGURED` (no `QUOTEX_WS_URL` / `QUOTEX_SSID` and no
   upstream tick handler), and the external-push adapter (`POST /api/data/ingest`,
   `INGESTION_MODE=external`) is tested and waiting for a producer.
2. **OTC instruments have no public data source.** `USDBDT-OTCq`, `EURCAD-OTCq` stay DATA UNAVAILABLE;
   standard FX is never presented as OTC.
3. **Public delayed feed** (Yahoo) is rate-limited and, in this environment, returns flat/stale bars
   (EURUSD/GBPUSD ≈ 99.8 % flat) → labelled REPLAY, never treated as live or as a Quotex feed.
4. **Optional research dependencies unavailable:** XGBoost not installed, neural challenger untrained
   → reported UNAVAILABLE in every artefact (never fabricated).
5. **Telegram credentials absent** → `NOT_CONFIGURED`; gate and message format proven with a stub
   transport.
6. **Evidence cannot accumulate** without a live feed: 0 decided paper trades, evidence bar NOT
   PASSED.

## OPERATOR REQUIREMENTS

1. **A continuously updating real feed** — either
   (a) broker WebSocket: set `QUOTEX_WS_URL` + `QUOTEX_SSID` and attach the tick handler in
   `backend/data/providers/broker_ws.py`, or
   (b) external collector: `INGESTION_MODE=external` and POST verified candles to `/api/data/ingest`
   (`realtime=true` for live, `replay=true` for backfill; simulated data is refused outside
   development).
   Verify with `GET /api/data/live-status` — the target state is `FRESH` for the assets you trade.
2. **OTC coverage** (optional): a CSV export or feed for the OTC pairs — nothing is invented for them.
3. **Telegram**: `ENABLE_TELEGRAM=true`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (environment only).
4. **Evidence**: let the scanner run; the system keeps answering NO TRADE until the published bar
   (≥ 100 decided paper trades AND Wilson 95 % LB > 0.50) is cleared. By design, not a defect.
5. **Model improvement** (optional): retrain on your own validated exports; promotion requires
   out-of-sample improvement over `v1-market`, never a training-accuracy gain. Re-run
   `python3 tools/model_manifest.py --write` after any deliberate artifact replacement.

## EXACT WINDOWS RUN COMMANDS

```powershell
# ---------- PowerShell ----------
cd C:\path\to\quotex-signal-ai
py -3.13 -m pip install -r backend\requirements.txt

# (optional) make the trained market model active if artifacts\current is missing
py -3.13 tools\activate_model.py                      # list versions + state + integrity
py -3.13 tools\activate_model.py v1-market            # copy it into artifacts\current (+ hash manifest)

# start the API + built-in single-page dashboard
py -3.13 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
#   dashboard  http://localhost:8000/preview
#   API docs   http://localhost:8000/docs

# verification battery (all read-only unless noted)
py -3.13 -m pytest tests -q
py -3.13 tools\verify_scenarios.py                    # A-M matrix -> docs\VERIFICATION_PAPER_PHASE.md
py -3.13 tools\probe_endpoints.py                     # every route over HTTP (add --include-mutating)
node tools\smoke_dashboard.mjs                        # dashboard DOM contract
py -3.13 tools\model_manifest.py                      # artifact integrity report
py -3.13 -m pip install pip-audit ; py -3.13 -m pip_audit -r backend\requirements.txt

# audit / research (resumable, writes only its own artefacts)
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
py -3.13 tools\verify_scenarios.py
py -3.13 tools\probe_endpoints.py
py -3.13 tools\gate_study.py scan --minutes 0 --workers 2
py -3.13 tools\gate_study.py analyse
python tools\model_manifest.py --write
```

---

### What this final pass added (all verified above)

1. **`tests/test_final_acceptance.py`** — 25 contract tests over the acceptance criteria (one engine,
   no client-side invention, AUTO/DAILY behaviour, provenance round-trip, evidence rule + Wilson,
   §14/§15 widget contract, banned claims, Telegram gating, feed budgets, gaps reported, artifact
   integrity, one shared no-trade definition, root serving the dashboard to browsers and JSON to API
   clients). `pytest tests -q` → **285 passed** after the Phase-42 feed suite.
2. **Provenance on the emit path** — `backend/data/provenance.py::window_version` +
   `model_version` / `feature_version` / `dataset_version` / `feed_status` in `_build_signal`, attached
   to the ledger row (E2E proven, determinism unit-tested).
3. **Artifact integrity** — path-safe `model_dir`, hash manifest + pre-load verification, refusal on
   mismatch, `artifact_integrity` in `/api/model/status`, `tools/model_manifest.py`, manifest written
   by `train` / `activate_model`.
4. **`tools/probe_endpoints.py`** — route-table-driven live HTTP probe of the whole API (read-only by
   default, restores the scanner state), which also caught a real defect: `record_signal` used to
   swallow *any* `IntegrityError` as a lock conflict; it now re-raises anything that is not a genuine
   “same candle twice” conflict (an unrecognised constraint failure can no longer masquerade as a
   locked duplicate).
5. **One definition of the no-trade rate** — `PAPER_TRADING_REPORT.md` used `1 − coverage`, which
   folded DATA_UNAVAILABLE scans into the rejection rate. It now reports the ledger's definition
   (NO_TRADE ÷ all scans) plus the status breakdown, and a test keeps the two artefacts in agreement.
6. **§14/§15 wording** — the served dashboard and the Next components now use the exact card and NO
   TRADE strings of the specification (and the copied message carries the same block).
7. **Scenario matrix re-lettered A–L** with the honest absence named explicitly (`F-real`), and
   `docs/VERIFICATION_PAPER_PHASE.md` regenerated.
8. **Root content negotiation** — `GET /` now returns the single-page dashboard to browsers (the
   sandbox preview frame included) and the unchanged JSON index to API clients, so a human opening the
   served port sees the product instead of raw JSON. Covered by a test.
9. **Docs de-staled** — README/STATUS/ARCHITECTURE/this report carry the final values; the Phase-40
   `DELIVERY_REPORT.md` is kept and marked superseded.

---

# PHASE 42 — LIVE DATA FEED

**Goal of this phase:** receive a *continuously updating, real* M1 stream from a source the operator
supplies or is authorised to use, validate it, build only closed M1 candles, store them with
provenance, publish an honest freshness verdict, and let the existing engine scan them — without
inventing a provider, without scraping, without bypassing authentication, and without ever presenting
a public feed as Quotex OTC data.

## FEED ADAPTER

| piece | role | state |
| --- | --- | --- |
| `backend/data/feedsource.py` | transport + parser: `WebSocketFeedSource` (lazy `websocket-client` import, header / in-message auth, `recv(timeout)` → idle vs drop, exponential backoff, redacted `health()`) and `parse_feed_message()` → tick · candle · ignored · invalid · auth_error | complete |
| `backend/data/providers/broker_ws.py` | live provider: state machine `NOT_CONFIGURED` / `CONNECTED` / `DISCONNECTED` / `AUTH_FAILED`, bounded tick buffer, `push_tick` / `drain_ticks` / `push_candle` → shared validator + store, `latest_tick`, `latest_candles`, `fetch`, `subscribe`, `stats()`, `health()`, `note_*()` | complete |
| `backend/data/feed.py` | the **single** M1 builder: open = first tick, high/low = running extremes, close = last tick, volume only when supplied; CLOSED only after the minute ends; `persist_candles` switch; full row + receipt returned | unchanged, reused |
| `backend/signals/feed_state.py` | LIVE FEED summary read from **stored evidence only**: state, data, detail, provider, latest closed M1 (UTC + IST), age, budgets, `assets_streaming`, `candles_in_progress` | complete |
| `tools/probe_live_feed.py` | read-only probe → `CONNECTED` / `NOT_CONFIGURED` / `AUTH_FAILED` / `NO_DATA` / `INVALID_DATA` (exit 0 / 3 / 4 / 6 / 7), `--self-test` on labelled fixtures, `--json` | complete |
| `tools/start_live_feed.py` | the bridge: load `.env` → connect → subscribe → parse → build M1 → deliver (`--target api` = `POST /api/data/ingest`, `--target store` = local SQLite) with heartbeat + health logs, reconnect, graceful Ctrl+C | complete |
| `tools/fixture_feed_server.py` | **verification only** — local fixture WebSocket (`--i-know-this-is-a-fixture` required) used to prove the chain end to end; never a provider, never wired into the API | complete (new) |
| `websocket-client>=1.7` | added to `backend/requirements.txt`; the import stays lazy so the API runs without it | added |

## FEED CONFIGURATION

* `.env` (repository): `QUOTEX_WS_URL` **unset**, `QUOTEX_SSID` **unset** → the honest state is
  `NOT_CONFIGURED`. No endpoint was guessed and no credentials exist in the repository.
* `.env.example`: documented `QUOTEX_WS_URL`, `QUOTEX_SSID` plus optional
  `LIVE_FEED_AUTH_HEADER` / `_AUTH_SCHEME` / `_AUTH_IN_MESSAGE` / `_SUBSCRIBE_TEMPLATE`,
  `LIVE_FEED_ASSETS`, `LIVE_FEED_INCLUDE_OTC`, `LIVE_FEED_SYMBOL_MAP`, `LIVE_FEED_TARGET` /
  `_API_URL` / `_PROVIDER_LABEL` / `_RECV_TIMEOUT` / `_LOG_SECONDS` / `_MAX_SECONDS` /
  `_RETENTION_MINUTES`. Values are empty; examples are comments.
* `.env` loader hardened: `KEY=value   # comment` now yields `value` (a `#` inside a quoted value is
  kept verbatim), so an example can never be loaded as a credential.
* `LIVE_FEED_SETUP.md` — 8 beginner steps (what to obtain → where in `.env` → test the connection →
  start the bridge → verify FRESH → start paper collection → press GENERATE SIGNAL → what
  DATA UNAVAILABLE / NO TRADE / PAPER SIGNAL mean) with exact Windows PowerShell commands and a
  troubleshooting table.

## CONNECTION

| check | command | result |
| --- | --- | --- |
| configuration | `python3 tools/probe_live_feed.py` | **NOT_CONFIGURED** (exit 3) — `QUOTEX_WS_URL` is not set |
| plumbing (labelled fixtures) | `python3 tools/probe_live_feed.py --self-test` | **CONNECTED** — 10 frames, 7 data, 5 ticks, 1 M1 candle closed (`2026-10-02T12:00:00+00:00` o=149.1 h=149.14 l=149.08 c=149.08), 1 rejected, 2 ignored, 1 unknown symbol, 1 future stamp |
| bridge without a feed | `python3 tools/start_live_feed.py` | `LIVE FEED NOT CONFIGURED` + the operator steps, exit 3 — no fake fallback |
| **end-to-end live** (verification only) | fixture socket → `tools/start_live_feed.py --target api` → a **disposable** instance (`SIGNAL_DB_PATH=/tmp/…`) | `connection established` → `M1 candle closed USDJPY 2026-10-03T14:23:00+00:00 o=149.092 h=149.165 l=148.763 c=148.953` → `candle stored … http 200 accepted=1 rejected=0`; the run also surfaced and fixed a real defect (see below) |
| repository API | `GET /api/health` | `live_feed.state = NOT_CONFIGURED`, `data = NO_DATA`, provider `null` — the production database stays honest |

**Defect found by the live run (fixed):** the bridge sent the candle row's own `asset` field inside
the `candles[]` element, which the ingest schema rejects with `extra="forbid"` → HTTP 422. The payload
now contains exactly the fields `CandleIn` accepts, and a test validates every delivered candle with
the API's own model. The delivery contract is otherwise unchanged.

## LATEST M1

* repository database: **none** — `latest_candle_utc = null`, `latest_candle_ist = null`,
  `assets_streaming = []`, detail *"no authorised feed endpoint and no realtime candles …"*.
* disposable verification instance (fixture rows, labelled `fixture_ws_selftest`): latest closed M1
  `2026-10-03T14:23:00+00:00` = **19:53:00 IST**, age **7.7 s**, `assets_streaming = ["USDJPY"]`.
* operator CSV history (unchanged, replay): USDJPY last candle `2026-10-02T06:48:00+00:00`
  (12:18:00 IST), 28 292 rows for USDJPY and 28 285 for USDCHF.

## FRESHNESS

* budgets: **120 s** realtime / **180 s** replay (`live-status.budgets_seconds`), both published in
  the API and in the dashboard tooltip.
* `FRESH` requires a *closed* realtime candle inside the budget. An unclosed candle is never FRESH —
  it is reported under `candles_in_progress` (verified: adding a forming USDCHF candle keeps the
  state `FRESH` and lists `["USDCHF"]`).
* state transitions verified on a temp database: no endpoint → `NOT_CONFIGURED` / `NO_DATA`;
  closed candle 4 minutes old → `CONNECTED` / `STALE`; 40 minutes old → `DISCONNECTED`
  (`DISCONNECT_AFTER_SECONDS = 300`); closed realtime candle < 120 s → `FRESH`.
* replay and simulated rows are excluded from the live verdict entirely (proven: a fresh replay row
  and a fresh simulated row leave the feed **not** FRESH).
* current repository verdict: 7 assets `UNAVAILABLE`, 0 FRESH / 0 STALE / 0 INVALID.

## ASSETS

* 7 enabled assets; real operator history: **USDJPY**, **USDCHF** (REPLAY, `csv`);
  AUDUSD/EURUSD/GBPUSD: dataset invalid, not used for signals.
* OTC: **USDBDT-OTCq**, **EURCAD-OTCq** → `DATA UNAVAILABLE`. No OTC instrument was invented,
  renamed or mapped from a normal FX pair, and the bridge excludes OTC unless the operator's feed
  really carries those instruments (`LIVE_FEED_INCLUDE_OTC`, off by default).
* symbol mapping is explicit (`LIVE_FEED_SYMBOL_MAP`); an unknown instrument is counted and logged,
  never guessed.

## MODEL

`v1-market` — `READY_MARKET`, artifact integrity **OK** (4 files checked, 0 mismatched), 44 causal
features, unchanged this phase. No threshold, gate or weight was touched. `READY_MARKET` remains a
statement about the artifact, never about profitability.

## PAPER

Live repository state: **1 002 scans recorded** (847 `DATA_UNAVAILABLE`, 155 `NO_TRADE`), 0 qualified
signals, 0 pending, 0 decided, `freshness_counts` 0 FRESH / 0 STALE / 0 INVALID / 7 UNAVAILABLE.
Evidence bar **NOT PASSED** — *"only 0 decided paper trades (need 100)"* and *"Wilson 95 % lower
bound not above 0.50"*. The scanner still evaluates every newly closed candle with the same engine as
the button; the two verification processes that pushed fixtures wrote only into disposable databases.

## SIGNAL ENGINE

Unchanged and single: `POST /api/signal/generate` → same 13-step pipeline as the paper scanner and the
daily job. Live check this phase: `AUTO` → **DATA UNAVAILABLE**; `USDJPY` → **DATA UNAVAILABLE**
(reason `DATA UNAVAILABLE`, feed badge `REPLAY`, chain `live_store FAILED → csv OK`). No frontend
signal logic, no random direction, no forced CALL/PUT.

## TESTS

| check | result |
| --- | --- |
| `python3 -m pytest tests -q` | **285 passed** (11.2 s) |
| `tests/test_live_feed.py` (**new**, 50 tests) | tick → M1 (O/H/L/C + no invented volume), minute-boundary roll-over, duplicate tick dropped, duplicate candle updated, invalid OHLC rejected, future stamp rejected, unclosed candle never persisted/stored, gaps reported not filled, provider states + auth failure + reconnect backoff, secrets redacted from every payload, probe verdicts + read-only proof, bridge aggregation/strict-schema/rejection counters, LIVE FEED state machine, `live-status` contract, external ingestion (accepted / simulated-blocked / realtime+replay 422 / unverified broker 409 / unknown asset 422), API `live_feed`, engine identity |
| `python3 tools/verify_scenarios.py` | **15/16 passed + 1 honest absence** (`F-real`) → `docs/VERIFICATION_PAPER_PHASE.md` (scenario **M** added: a feed that stops reads `DISCONNECTED`, freshness is not FRESH, the scan answers `DATA_UNAVAILABLE` and the ledger stays empty) |
| `python3 tools/probe_live_feed.py --self-test` | **CONNECTED** (plumbing proven, fixtures labelled) |
| `python3 tools/start_live_feed.py` (no `.env` feed) | `LIVE FEED NOT CONFIGURED`, exit 3 |
| `python3 tools/probe_endpoints.py` | **33 ok · 0 failed · 6 skipped** |
| `node tools/smoke_dashboard.mjs` | all assertions passed (incl. the new LIVE FEED header/chip contract, `FRESH` + `LATEST M1 18:43:00 IST` + `AGE 42s` case) |
| `cd frontend && npx tsc --noEmit` | clean |
| `cd frontend && npm run build` | **PASS** (Next 16.3.8, 3/3 static pages) |
| `pip-audit -r backend/requirements.txt` | **no known vulnerabilities** (with `websocket-client>=1.7` added) |

## FRONTEND

The single-page dashboard keeps its layout. The header summary and the feed chip now read
`LIVE FEED <NOT_CONFIGURED | CONNECTED | DISCONNECTED | FRESH> · LATEST M1 HH:MM:SS IST · AGE Ns`
(next to DATA / MODEL / PAPER / TELEGRAM / EVIDENCE / CLASSIFICATION) with a tooltip explaining the
vocabulary and the 120 s / 180 s budgets. `backend/static/index.html` = **91 947 B**. No redesign, no
new panel, no colour-scheme change.

## SECURITY

* session values are env-only; they are registered for redaction at parse time and stripped from
  URLs, headers, log lines, `health()`, `stats()` and error strings (unit-tested with a live socket
  stub and with a query-string session);
* `POST /api/data/ingest` still refuses `simulated: true` outside development (409), a
  `realtime` + `replay` contradiction (422), an unverified `broker_ws`/`quotex` claim (409) and any
  unknown field (422) — the fixture path can never be filed as market data;
* a caller cannot overwrite provenance: the ingest path stamps `provider`, `source_label` and the
  realtime/simulated/replay flags from **its own** request, and the API never returns a credential;
* `pip-audit` clean; no new attack surface (the bridge and the fixture server are CLI tools, not
  routes; the probe is read-only).

## FINAL CLASSIFICATION

**PAPER_TRADING_READY** — unchanged. This phase completes the *data-availability* machinery; it does
**not** claim, and cannot claim, that the strategy is profitable or live-ready. Two facts keep the
classification where it is: no authorised feed is connected (`NOT_CONFIGURED`), and the evidence bar
is **NOT PASSED** (0 decided paper trades, Wilson lower bound not above 0.50). The live feed solves
data availability only; every gate, threshold and evidence requirement is untouched.

## OPERATOR ACTION REQUIRED

1. Obtain, from an authorised source, a **WebSocket endpoint** (+ session value if required) that
   streams your instruments as ticks or closed candles.
2. Put it in `.env` — `QUOTEX_WS_URL=…`, `QUOTEX_SSID=…`; adjust `LIVE_FEED_AUTH_*`,
   `LIVE_FEED_SUBSCRIBE_TEMPLATE`, `LIVE_FEED_SYMBOL_MAP`, `LIVE_FEED_ASSETS` only if your feed needs
   them (`LIVE_FEED_SETUP.md`, steps 1–2).
3. Verify with `python3 tools/probe_live_feed.py` and require the verdict **CONNECTED** before
   trusting anything else. `AUTH_FAILED` / `NO_DATA` / `INVALID_DATA` each name their own cause — the
   probe never hides an error.
4. Start the bridge (`python3 tools/start_live_feed.py`) next to the API and confirm
   `GET /api/data/live-status` reports **FRESH** (age < 120 s) and the dashboard header shows
   `LIVE FEED FRESH · LATEST M1 … IST · AGE …s`.
5. Leave paper collection running (`PAPER_COLLECTION=true`, scanner on) and let the ledger accumulate
   **decided** trades — 100+ with a Wilson 95 % lower bound above 0.50 — before any promotion
   decision. Until then, NO TRADE is the expected and correct answer.
6. If the instruments you need are OTC pairs, confirm your feed actually carries them; otherwise they
   stay `DATA UNAVAILABLE` by design (this project will not relabel a normal FX pair as OTC).
7. Telegram stays off until you enable it deliberately (`ENABLE_TELEGRAM=true`, token + chat id in the
   environment); the gate still blocks every non-real, non-fresh, non-READY_MARKET or NO_TRADE output.
