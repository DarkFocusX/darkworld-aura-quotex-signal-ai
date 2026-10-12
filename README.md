# Quotex Signal AI — *DARKWORLD AURA*

A single-page **M1 signal dashboard** that runs a real analysis pipeline:

> **Press Generate Signal → the system fetches market data → verifies freshness → computes M1 features →
> runs a model ensemble → scores confidence → validates the setup → and either produces ONE signal
> or says NO TRADE.**

Built to be honest by construction: it never invents prices, never hard-codes confidence, never claims a
"sure shot", and prefers **NO TRADE** over a weak signal.

---

## ⚠️ Read this first

| This system **is** | This system is **not** |
| --- | --- |
| A working signal *engine* with real feature/model/validation code | A profit machine, a guarantee, or "95 % accuracy" marketing |
| Explicit about data provenance (live / delayed / **simulated**) | A source of real-time OTC quotes (Quotex has no public data API) |
| Paper / study trading only — no order ever reaches a broker | A broker integration |
| Reporting metrics that were actually computed from stored data | A source of invented backtests or win rates |

* **Confidence is a calculated model score, not a promise.** Signals can and do lose.
* If market data is unavailable → **DATA UNAVAILABLE**. If models are missing → **MODEL NOT READY**.
  If the setup is weak → **NO TRADE** with the exact reason.
* The default demo model is trained on **simulated** data and is labelled `READY_DEMO` everywhere.
* **Measured on the operator export: 0 signals out of 27 971 audited minutes.** The gate audit re-ran
  the unchanged gate stack over every minute of the real history (2026-09-04 12:09 → 2026-10-02 06:47
  UTC, USDJPY) and found no setup that clears it. The learned model is the first wall (MODEL passes
  0.06 %), and even when a research-only oracle supplies the *actual* next direction the confidence and
  context gates still block 26 576 of 27 037 applicable minutes — 461 clear everything (oracle verdict
  **FEASIBLE**, i.e. the architecture is satisfiable, the model is the binding constraint). So the
  honest answer on this data is **NO TRADE**, and the classification is **PAPER_TRADING_READY**
  (structurally ready, waiting for a continuously updating real feed and for evidence). See
  `GATE_AUDIT.md`, `docs/RESEARCH_REPORT.md` and `STATUS.md`.

---

## 1. What you get

* **Single-page dashboard** (Next.js + TypeScript + Tailwind) — mobile-first, dark trading aesthetic,
  large signal card, large Generate button, live IST clock, history, validation panels, settings.
* **FastAPI backend** with the full endpoint surface (`/api/signal/generate`, `/api/model/status`, …).
* **ML pipeline**: LightGBM + RandomForest + LogisticRegression ensemble, optional XGBoost/neural
  challenger, IsolationForest anomaly veto, temperature calibration, walk-forward + holdout validation.
* **Confidence engine** combining nine weighted components, hard gates, and quality bands.
* **Signal lock** (one signal per asset per candle, enforced by a DB unique index).
* **Daily mode** (best qualified setup of the IST trading day, including a labelled study replay).
* **Outcome tracking** that only records WIN/LOSS from verified candles, and a **transparency log** of
  every rejected scan.
* **Telegram notifications** (env-var credentials, no secrets in code, blocked for NO TRADE and for
  simulated signals by default).
* **103 automated tests** covering timezones, confidence maths, gates, locking, staleness, NO_TRADE
  logic, Telegram formatting, the API surface, and time-series validation.

---

## 2. Quick start

### 2.1 Backend + API + built-in preview page (fastest path)

```bash
cd /home/user
pip install -r backend/requirements.txt

# (optional) create the demo model so the engine has something to score with
python -m backend.research.train --source simulated --rows 20000 --folds 3 --version v1-demo

# start the API
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

If a trained version already exists but `backend/models/artifacts/current` is missing (a copied or
restored checkout without the symlink), point the engine at it — the helper validates the artifact's
provenance first and refuses to activate an incomplete one:

```bash
python3 tools/activate_model.py                    # list the versions found + their state
python3 tools/activate_model.py v1-market          # copy it into place (survives zip/checkout)
python3 tools/activate_model.py v1-market --link   # symlink instead, where symlinks are supported
```

Open **http://localhost:8000/preview** → the single-page dashboard (same `index.html` file that is
committed at `backend/static/index.html`, served by FastAPI). **http://localhost:8000/** shows the
same page to a browser; the port root answers JSON only to clients that do not ask for HTML, so
`curl http://localhost:8000/` still returns the machine-readable API index.
Interactive API docs: **http://localhost:8000/docs**.

### 2.2 Full Next.js frontend

```bash
cd frontend
npm install
npm run dev            # http://localhost:3000  (proxies /api/* to the backend on :8000 server-side)
```

The Next.js server rewrites `/api/*` to `http://127.0.0.1:8000`, so the browser only ever talks to one
origin (no CORS problems, and the preview host allow-list is already configured in `next.config.mjs`).

### 2.3 Environment file

```bash
cp .env.example .env      # optional; every value has a safe default
```

---

### 2.4 Windows (PowerShell / CMD)

The same commands, as an operator on Windows would type them. The substance of each one was
exercised while building this project (backend boot, pipeline import, tests, training, frontend
build); the shells themselves are listed because a Windows operator needs them verbatim.

PowerShell:

```powershell
cd C:\path\to\quotex-signal-ai
py -3.13 -m pip install -r backend\requirements.txt
Copy-Item .env.example .env                      # optional
py -3.13 -m backend.research.train --source csv --assets USDJPY,USDCHF --folds 3 --version v1-market
py -3.13 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
#   dashboard:  http://localhost:8000/preview
#   docs:       http://localhost:8000/docs
```

CMD (`cmd.exe`):

```bat
cd /d C:\path\to\quotex-signal-ai
py -3.13 -m pip install -r backend\requirements.txt
copy .env.example .env
py -3.13 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Verify and test (either shell):

```powershell
py -3.13 -c "import backend.main; print('pipeline import OK')"
py -3.13 -m pytest tests -q
cd frontend; npm install; npm run build; npm run dev   # http://localhost:3000
```

Notes for Windows: use `py -3.13 -m pip` (not bare `pip`) when several Pythons are installed; if
`python` is not on `PATH`, re-run the installer with *Add python.exe to PATH*; the SQLite file lives
at `backend\database\signals.db` unless `SIGNAL_DB_PATH` overrides it; the operator CSV goes in
`backend\data\csv\` and is loaded with `--source csv` (see §4).

---

## 3. How the signal pipeline works

```
POST /api/signal/generate
        │
        ├── 1. resolve assets            AUTO SELECT → every enabled asset, else the chosen one
        ├── 2. fetch verified candles    provider chain (csv → broker → public → simulated*)
        ├── 3. DATA CHECK                candle count, gaps, staleness for the feed type
        ├── 4. FEATURE CHECK             44 causal M1 features (EMA/RSI/MACD/ATR/BB/ADX/S-R/…)
        ├── 5. MODEL CHECK               ensemble probabilities, agreement, spread, anomaly veto
        ├── 6. CONFIDENCE ENGINE         weighted score over 9 components → 0–100
        ├── 7. REGIME CHECK              volatility bounds, ADX/efficiency regime, anomalies
        ├── 8. CONSISTENCY CHECK         ≥3 of 4 rule groups must align with the model direction
        ├── 9. CONFIDENCE CHECK          confidence ≥ threshold (default 85)
        ├── 10. RISK POLICY + SIGNAL LOCK  daily cap, expiry range, one signal per candle
        │
        └── emit ONE signal (or NO TRADE with the first blocking reason + next valid scan time)
```

\* simulated fallback is labelled and can be disabled with `DATA_SOURCE_MODE=live_only`.

### Quality bands

| Confidence | Band | Tradable |
| --- | --- | --- |
| 90–100 | VERY HIGH | ✅ |
| 85–89 | HIGH | ✅ |
| 75–84 | MODERATE | ❌ |
| < 75 | NO TRADE | ❌ |

### Confidence composition (documented policy weights)

```
confidence = 0.26·model + 0.14·model_agreement + 0.11·trend + 0.10·momentum + 0.10·candle
           + 0.09·structure + 0.07·volatility + 0.08·regime + 0.05·freshness
```

**Uncertainty is never converted into confidence.** A neutral-heavy ensemble (the class that means
"coin flip") lowers the model component twice — once through the directional probability mapping and
once through an explicit neutral-mass penalty — which is what pushes most setups into NO TRADE.

### Direction comes from the models, context can only veto

The ensemble proposes CALL/PUT; the rule scores (trend, momentum, candle, structure) each contribute
0–100 *agreement* scores. A rule group disagreeing costs confidence, and if fewer than three of the four
groups align, the setup is rejected outright (`SIGNAL CONSISTENCY CHECK`).

---

## 4. Data sources and the anti-fabrication rules

The provider chain is configured by `DATA_SOURCE_MODE`:

| Mode | Chain | Notes |
| --- | --- | --- |
| `auto` (default) | CSV → broker feed → public delayed feed → **simulated** | Every fallback step is recorded in `feed.fallback_chain` and shown in the UI |
| `live_only` | CSV → broker feed → public delayed feed | **No synthetic fallback** — unavailable stays unavailable |
| `csv` | local CSV files only | For your own exported candles |
| `simulated` | simulated only | Demo/testing |

**Enforced rules**

1. A simulated frame is always `is_simulated = true`; signals carry `is_simulated = 1` and the UI shows
   a `SIMULATED FEED — NOT MARKET DATA` badge.
2. OTC instruments (e.g. `USDBDT-OTCq`) have **no public data source**. Without a configured broker feed
   the engine returns `DATA_UNAVAILABLE` — it never invents OTC prices.
3. The public delayed feed is treated as **replay** data (marked `REPLAY`, never "LIVE").
4. Telegram delivery of simulated signals is blocked unless `TELEGRAM_ALLOW_SIMULATED=true`.
5. In **production** (`TRADING_ENV=production`, the default) the engine refuses a synthetic feed
   outright: the asset reads `DATA UNAVAILABLE — No verified market data … the configured chain fell
   back to 'simulated'`. Development mode (`TRADING_ENV=development`) keeps the labelled simulated
   feed so the pipeline stays demonstrable.
6. Display states are the four in the spec: `LIVE` | `REPLAY` | `SIMULATED` | `DATA UNAVAILABLE`.

### Using your own real data (the supported path)

Two equivalent ways to get operator candles in:

```bash
# A. copy the file in, then register it (validated, nothing is trusted blindly)
copy my_export.csv backend\data\csv\EURUSD.csv
curl -X POST http://127.0.0.1:8000/api/data/import ^
     -H "Content-Type: application/json" ^
     -d "{\"asset\":\"EURUSD\",\"from_file\":true,\"source_class\":\"MARKET\"}"

# B. POST the CSV body directly (paste / upload from the dashboard panel)
curl -X POST http://127.0.0.1:8000/api/data/import ^
     -H "Content-Type: application/json" ^
     -d "{\"asset\":\"EURUSD\",\"source_class\":\"MARKET\",\"csv_text\":\"time,open,high,low,close,volume\n2026-10-01T06:30:00Z,1.0699,1.0704,1.0699,1.0701,138.3\n...\"}"
```

```csv
time,open,high,low,close,volume
2026-10-01T06:30:00Z,1.06990,1.07040,1.06990,1.07010,138.3
```

Accepted time formats: epoch seconds/ms or ISO-8601 (assumed UTC unless an offset is present). Stored in
**UTC**, displayed in **IST**.

Every import is validated *before* it can reach the model — the report is returned and shown in the
dashboard (`GET /api/data/status`):

| Check | Rejected when |
| --- | --- |
| Resolution | candles are not a 1-minute grid (e.g. a feed silently serving 2-minute candles) |
| Duplicates | the same timestamp appears twice |
| Ordering | timestamps are not strictly increasing |
| OHLC geometry | `high < low`, or high/low not enclosing open/close |
| Values | non-finite / negative prices, missing columns |
| History | too few candles for the M1 feature block |
| Flat feed | > 50 % of candles have `open = high = low = close` (a frozen/stale quote, not price action) |
| Freshness | the newest candle is older than the feed budget (120 s live / 180 s replay) — reported, and the engine returns `DATA_UNAVAILABLE` for a stale snapshot |

A dataset that fails validation is **never served to the model**: `GET /api/data/status` reports
`DATA UNAVAILABLE` with the exact errors, and the training pipeline skips the asset.

> A static CSV snapshot supports **training and study**. It cannot produce live signals once it is
> older than the freshness budget — live signals need a feed that keeps appending candles (see the
> broker adapter) or a freshly exported file.

### Connecting a live feed (the continuous-M1 path)

The bridge is finished; it is the *endpoint* that is missing. Two supported sources exist.

**a) Twelve Data — realtime standard FX (an API key is the only requirement)**

```bash
# 1. .env  →  TWELVE_DATA_API_KEY=your-key        (optional: TWELVE_DATA_SYMBOLS=…)
python3 tools/probe_live_feed.py --provider twelve_data   # CONNECTED | NOT_CONFIGURED | AUTH_FAILED | NO_DATA | INVALID_DATA
python3 tools/probe_live_feed.py --provider twelve_data --self-test   # no key: labelled fixtures
python3 tools/start_twelve_data_feed.py --show-config     # resolved setup, key redacted
python3 tools/start_twelve_data_feed.py                   # connect → subscribe → parse → build M1 → deliver
curl http://127.0.0.1:8000/api/data/live-status           # FRESH / STALE / INVALID / UNAVAILABLE per asset
```

`EUR/USD→EURUSD`, `GBP/USD→GBPUSD`, `USD/JPY→USDJPY`, `USD/CHF→USDCHF`, `AUD/USD→AUDUSD` are the
only instruments accepted, from an explicit map; anything else is reported and **not** subscribed.
Rows are stored with `provider=twelve_data`, `source_class=MARKET`, `realtime=true`,
`is_simulated=false`, `is_replay=false`, and FRESH still means *a validated closed M1 candle inside
the 120 s realtime budget* — a socket or a heartbeat is never data. Twelve Data carries no OTC
instrument, so `USDBDT-OTCq` / `EURCAD-OTCq` stay `DATA UNAVAILABLE`: standard FX is never
substituted for an OTC pair. The key comes from the environment only (never hard-coded, never
stored, never returned by the API, never logged — `apikey=` and every query string are redacted).

**b) Your own authorised feed (broker-style)**

```bash
# 1. .env  →  QUOTEX_WS_URL=wss://your-feed-host/...   QUOTEX_SSID=your-session
python3 tools/probe_live_feed.py        # CONNECTED | NOT_CONFIGURED | AUTH_FAILED | NO_DATA | INVALID_DATA
python3 tools/probe_live_feed.py --self-test   # no credentials: proves the plumbing on labelled fixtures
python3 tools/start_live_feed.py        # connect → subscribe → parse → build M1 → deliver to the API
curl http://127.0.0.1:8000/api/data/live-status   # FRESH / STALE / INVALID / UNAVAILABLE per asset
```

* `backend/data/feedsource.py` — WebSocket transport + tolerant message parser (ticks, closed
  candles, epoch/ISO times, auth errors, heartbeats), session redaction, reconnect with backoff.
* `backend/data/providers/broker_ws.py` — adapter: state machine, tick buffer, validation,
  `latest_tick` / `latest_candles`, provenance, redacted `health()`.
* `backend/data/providers/twelve_data.py` — adapter + vendor protocol for `twelve_data`: env-only
  key, the documented subscribe frame, the explicit instrument map, control-frame classification
  (`heartbeat` / `subscribe-status` / `error`) and the OTC refusal. It rides on the `broker_ws`
  adapter and reuses the shared transport, parser and M1 builder — it adds no second pipeline.
* `tools/start_twelve_data_feed.py` — the Twelve Data bridge (same transport, parser, M1 builder,
  delivery, heartbeat, reconnect and redaction as `start_live_feed.py`).
* `backend/signals/feed_state.py` — store-evidence-only LIVE FEED summary (state, latest closed M1 in
  UTC **and** IST, age, budget, assets streaming) shown in the dashboard header.
* `tools/fixture_feed_server.py` — verification only: a local fixture socket
  (`--i-know-this-is-a-fixture`) to prove the whole chain without credentials. Generated numbers,
  never market data, never wired into the API.

An alternative supported path needs no bridge: POST verified ticks or candles to
`POST /api/data/ingest` (see the API section for the exact schema). Without an endpoint the system
reports `LIVE FEED NOT_CONFIGURED` / `DATA UNAVAILABLE` — it never invents a candle, and it never
presents a normal FX pair as an OTC instrument. Step-by-step operator instructions:
**`LIVE_FEED_SETUP.md`**.

---

## 5. Training and validation

```bash
# real model — operator-supplied market data ONLY (refuses to train on anything else)
python -m backend.research.train --assets EURUSD,GBPUSD,USDJPY,USDCHF ^
    --source csv --rows 40000 --folds 3 --neutral-atr 0.25 ^
    --version v1-market --require-market

# demo model (simulated data — always loads as READY_DEMO, never usable in production)
python -m backend.research.train --assets USDBDT-OTCq,EURCAD-OTCq --source simulated ^
    --rows 20000 --folds 3 --version v2-demo
```

`--source` is one of `auto | simulated | csv | real_only | live`; `--require-market` aborts the run
unless the assembled dataset is classified `MARKET`. Data classification is four-way —
`SIMULATED | FIXTURE | MARKET | MIXED` — and a `MIXED` dataset is explicitly marked *not eligible for
production signals*. Assets whose candles are too short/invalid are skipped with a message instead of
crashing the run.

After training the engine must be told to reload:

```bash
curl -X POST http://127.0.0.1:8000/api/model/reload
# or just restart the API
```

What the trainer does:

1. builds the **identical** feature block used at inference time (no train/serve skew);
2. labels the next candle with a **neutral band** (`±0.25 × ATR` by default) so coin-flip moves are
   labelled NEUTRAL instead of being forced into CALL/PUT;
3. runs **expanding-window walk-forward validation** with an embargo ≥ horizon (no shuffling anywhere);
4. keeps the final **20 % as an untouched holdout** and reports holdout metrics separately;
5. fits a **calibration temperature** on out-of-fold probabilities;
6. fits an **IsolationForest** anomaly detector;
7. writes `models.pkl`, `scaler.pkl`, `isolation_forest.pkl`, `metadata.json` into
   `backend/models/artifacts/<version>/`, points `artifacts/current` at it (a symlink where the
   platform supports one, otherwise a copy — re-run `python3 tools/activate_model.py <version>` after
   moving the project), and writes
   `backend/research/reports/validation_<stamp>.{json,md}`;
8. records the **data provenance** (provider, simulated flag, period, rows) inside the artifact — the
   UI reads it, so a simulated-trained model can never masquerade as a market model;
9. persists the §3 metadata block in `metadata.json`: `model_version`, `feature_version`,
   `training_timestamp`, `data_source`, `data_source_class`, `data_period{start_utc,end_utc}`,
   `row_count`, `asset_list`, `validation_metrics`, `holdout_metrics`.

Model states (spec §4):

| State | Meaning | Usable for production signals |
| --- | --- | --- |
| `READY_MARKET` | trained on operator-supplied real market data | yes, if every other gate also passes |
| `READY_DEMO` | trained on simulated or fixture data | **never** — production blocks it |
| `NOT_READY` | no artifact / load failure | no |

Reported metrics (all computed, never invented): accuracy, per-class precision/recall/F1, confusion
matrix, ROC-AUC (one-vs-rest), Brier score, calibration table + expected calibration error, directional
win rate, max losing streak, and a confidence-gate threshold sweep.

> **Model validation (offline) and signal performance (live paper) are reported in two separate panels
> and must never be combined.** The API even returns a `separation_note` saying so.

### Model-quality research lab

Beyond the production trainer, `backend/research/run_experiments.py` runs the research grid described in
spec §1–§9 and writes every cell to `backend/research/experiments/`:

```bash
python -m backend.research.run_experiments --assets USDJPY,USDCHF --source csv --rows 40000
python -m backend.research.run_experiments --quick          # smoke-sized grid
```

What the lab measures, per cell:

| Dimension | Values |
| --- | --- |
| Target | `M1_EXPIRY_OUTCOME` (fill `open[i+1]` → settle `close[i+h]`) **and** `NEXT_CANDLE_DIRECTION` (kept for comparison — never silently replaced) |
| Horizon | 1, 2, 3, 5 minutes |
| Features | `base` (m1-features-1.0) and `extended` (m1-features-lab-1.0: RSI/MACD slopes, wick & body ratios, multi-horizon momentum, S/R distance, time-of-day, …) |
| Models | logistic regression, random forest, LightGBM, calibrated ensemble (XGBoost recorded as unavailable, neural challenger untrained). The full benchmark runs on the headline horizon (default 1 min); the other horizons are measured with `--core-models` (default LightGBM) so the grid stays affordable on a 2-core box |
| Regimes | trend / range / high-vol / low-vol / anomaly-filtered variants (studied on the headline horizon with the core family) |

The walk-forward paper loop (spec §9) retrains the *fastest comparable family* in every cell — the
procedure (retrain → validate → move forward → test again) is what is measured, and the family actually
used is recorded in each cell's step report. Per-cell checkpoints in `experiments/.partial/` make the grid
resumable after an interruption; a resumed run never mixes cells computed under a different policy.

Rules the lab enforces (and tests verify):

1. **No leakage** — expanding-window folds with an embargo ≥ the label horizon, one untouched chronological
   holdout per cell, no shuffling anywhere; features are causal (future candles cannot change past rows).
2. **Thresholds are chosen on validation folds only**, under a published rule: the smallest gate with ≥ 100
   validation signals whose Wilson 95 % lower bound on the hit rate clears 0.50. If nothing qualifies, the
   answer is *no threshold* — and the correct live behaviour is NO TRADE.
3. **One holdout test per cell**, reported honestly; `--quick` runs a smoke grid. Cells are checkpointed
   as they finish (`.partial/` under `experiments/`), so an interrupted run resumes instead of restarting.
4. **No “best model” ranking** — every cell is reported as measured (the index says so explicitly).
5. **Calibration is reported as buckets with sample counts** — a bucket with too few observations is
   described as unproven, never as reliable.

The dashboard grows a **MODEL QUALITY** panel (spec §15) that shows the active artifact, the offline
walk-forward/holdout metrics, live paper counts and the research evidence side by side — headed by the
model state (`READY_MARKET` / `READY_DEMO` / `NOT_READY`) and, underneath, `PERFORMANCE: NOT VALIDATED FOR
PROFITABILITY` until enough decided paper trades say otherwise.

The API exposes the run read-only:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/research/experiments` | Every experiment of the latest (or `?run_id=`) run, with pooled summary |
| GET | `/api/research/latest` | Compact view for the dashboard MODEL QUALITY panel |
| GET | `/api/research/experiments/{id}` | One cell in full (folds, sweep, calibration, replay) |
| GET | `/api/research/experiments/{id}/signals` | Chronological paper-replay signals for a cell |

### Decision report

```bash
python -m backend.research.decision_report     # writes RESEARCH_DECISION.md
```

The report states DATA QUALITY / MODEL QUALITY / VALIDATION QUALITY / PAPER-TRADING RESULTS / SIGNAL
COVERAGE / NO-TRADE RATE / KNOWN LIMITATIONS and exactly one classification — `RESEARCH_ONLY`,
`PAPER_TRADING_READY` or `LIVE_DATA_READY_FOR_PAPER_SIGNALING` — derived from a published rule over the
evidence files, never from optimism. It never labels the system profitable.

---

## 6. API reference

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Subsystem health (db, model, scheduler, telegram, execution modes) |
| GET | `/api/time/ist` | Server-synchronised IST clock, next candle, scheduler state |
| GET | `/api/assets` | Asset universe (`?probe=true` runs a live availability probe) |
| GET | `/api/market/{asset}` | Candles + feed provenance + feature snapshot for the newest closed candle |
| POST | `/api/signal/generate` | Run the pipeline. Body: `{"asset":"AUTO","mode":"NOW"\|"DAILY"}` |
| GET | `/api/signal/latest` | Most recent stored signal + last scan |
| GET | `/api/signals/history` | Compact history rows (Time/Asset/Direction/Confidence/Result/Status) |
| GET | `/api/signals/performance` | Signal performance **and** model validation (separated) |
| GET | `/api/signals/scans` | Transparency log of every scan, including rejections |
| GET | `/api/signals/daily` | Daily mode status (emitted best + study replay) |
| GET | `/api/signals/study/{asset}` | Replay today's closed candles through the pipeline |
| POST | `/api/signals/evaluate` | Resolve open signals from verified candles |
| GET | `/api/model/status` | Model state, per-model status, anomaly/neural availability |
| GET | `/api/model/metrics` | Metrics stored in the artifact |
| POST | `/api/model/reload` | Reload artifacts after training |
| GET/POST | `/api/config` | Read / patch runtime settings (secrets are rejected: environment only) |
| GET | `/api/system` | Everything the header needs in one call |
| GET | `/api/events` | Internal event log |
| POST | `/api/data/import` | Operator CSV ingestion (text, or `from_file` for a file already in `backend/data/csv`) |
| GET | `/api/data/status` | Per-asset data status: badge, rows, freshness, validation report, provenance, provider chain |
| GET | `/api/data/provenance` | Raw import register (file, sha256, rows, period, operator label) |
| GET | `/api/research/experiments` | Everything the research grid measured (no "best model" ranking) |
| GET | `/api/research/latest` | Compact research view powering the MODEL QUALITY panel |
| GET | `/api/research/experiments/{id}` | One experiment cell in full · `…/signals` for its replay log |
| GET | `/api/data/live-status` | Per-asset feed verdict: `FRESH` / `STALE` / `INVALID` / `UNAVAILABLE`, provider, latest candle, age, resolution, rows, gaps, realtime/simulated/replay flags |
| GET | `/api/health` / `/api/system` | … plus `live_feed`: `NOT_CONFIGURED` / `CONNECTED` / `DISCONNECTED` / `FRESH`, newest closed M1 (UTC + IST), age, assets streaming |
| POST | `/api/data/ingest` | Operator pushes verified ticks/candles (the supported external path); refuses unknown assets, simulated data outside development, and realtime+replay combinations |
| POST | `/api/data/ingest/start` · `/stop` | Start/stop the continuous feed + automatic paper scanner |
| GET/POST | `/api/paper/scanner` | Scanner state / start-stop (`PAPER_COLLECTION` records every scan) |
| POST | `/api/paper/scan` | Scan the newest closed candle with the same engine as the button |
| POST | `/api/paper/settle` | Resolve due paper signals from verified candles only |
| GET | `/api/paper/summary` · `/card` · `/ledger` | LIVE PAPER TRADING panel payloads (KPIs, real-time card, ledger) |
| GET | `/api/research/paper-evidence` | Decided trades, wins, losses, win rate, Wilson 95 % lower bound, coverage, losing streak, days, assets + the published evidence bar |
| POST | `/api/research/paper-report` | Regenerate `PAPER_TRADING_REPORT.md` |

### Signal response (success)

```json
{
  "status": "SIGNAL",
  "asset": "USDBDT-OTCq",
  "timeframe": "M1",
  "direction": "CALL",
  "entry_time": "06:30:00",
  "timezone": "Asia/Kolkata",
  "expiry_minutes": 1,
  "confidence": 91.4,
  "quality": "VERY_HIGH",
  "data_timestamp": "2026-10-02T00:59:00+00:00",
  "model_agreement": 0.91,
  "signal": { "...": "full evidence bundle incl. components, model votes, checks" }
}
```

### Signal response (rejected)

```json
{
  "status": "NO_TRADE",
  "reason": "Confidence 71.4% is below threshold 85.0%",
  "reason_detail": "models disagree with short-term market structure — best of 4 scanned assets (EURUSD at 71.4%)",
  "confidence": 71.4,
  "next_valid_scan_ist": "12:31:00"
}
```

---

## 7. Telegram

```bash
ENABLE_TELEGRAM=true
TELEGRAM_BOT_TOKEN=123456:ABC...        # secrets come from the environment only
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_ALLOW_SIMULATED=false          # keep false: demo signals stay on the dashboard
```

Message format sent on a qualified signal:

```
🚨 QUOTEX M1 SIGNAL

🇧🇩 USDBDT-OTCq
💎 M1

🕐 ENTRY:
06:30:00 IST

🔺 CALL

🎯 MODEL CONFIDENCE:
91.4%

⏳ EXPIRY:
1 MIN

📡 DATA:
REAL

Model confidence is a calculated score, not a guarantee of profit.
```

A message is sent **only** when all three hold at once: the feed is real market data (`📡 DATA: REAL`),
the model is `READY_MARKET`, and the eligibility policy returned `ALLOW_SIGNAL`. Everything else is
blocked: `BLOCKED_NOT_MARKET`, `BLOCKED_NOT_QUALIFIED`, `BLOCKED_SIMULATED`.

`NO_TRADE`, `DATA_UNAVAILABLE`, `READY_DEMO` and simulated signals are **never** sent — the single
exception is an explicit development deployment (`TRADING_ENV=development` *and*
`TELEGRAM_ALLOW_SIMULATED=true`), where the message is prefixed with
`⚠️ DEVELOPMENT MODE — this alert would be blocked in production …`.

Delivery status (`SENT`, `DISABLED`, `NOT_CONFIGURED`, `BLOCKED_*`, `FAILED`) is stored on the signal
row and shown in the validation panel. The bot token is redacted from every error string.

---

## 8. Project structure

```
backend/
  main.py            FastAPI app (all endpoints, single source of truth for payloads)
  scheduler.py       background loop: scan each M1 candle + resolve outcomes
  config.py          env/settings plumbing, .env loader (no secrets in code)
  constants.py       asset universe, gates, quality bands, confidence weights
  timeutil.py        Asia/Kolkata conversions (browser timezone is never used)
  settings_service.py runtime settings merge + validation
  data/
    base.py          CandleFrame/FeedMeta contracts + provenance
    registry.py      provider chain + transparent fallback recording
    providers/       csv, yahoo (public delayed), simulated (labelled), broker_ws (live adapter),
                     twelve_data (realtime standard FX: env-only key + vendor protocol)
    feedsource.py    live-feed transport: WebSocket client, message parser, redaction, backoff
    feed.py          ContinuousFeed: ticks → M1 builder (O/H/L/C, no invented volume) → validation → store
    store.py         rolling candle store (provenance per row, gaps reported never filled)
  features/
    indicators.py    EMA/RSI/MACD/ATR/Bollinger/ADX/efficiency/pivots (causal)
    pipeline.py      44-feature M1 block + forward labels
    rules.py         trend/momentum/candle/structure/volatility/regime/freshness scores
  models/
    ensemble.py      3-class (PUT/NEUTRAL/CALL) weighted aggregation, temperature calibration
    service.py       artifact loading, status reporting, inference (never fakes a probability)
    anomaly.py       IsolationForest veto
    neural.py        optional LSTM/GRU/Transformer challenger (interface + honest unavailability)
  validation/
    checks.py        the six pre-signal gates
    time_series.py   walk-forward + holdout splits, embargo, leakage probe
    metrics.py       confusion matrix, P/R/F1, ROC-AUC, Brier, calibration, streaks
  signals/
    engine.py        the pipeline: fetch → check → features → models → confidence → emit/reject
    confidence.py    weighted confidence engine
    daily.py         daily mode + study replay (in-sample aware)
    tracker.py       outcome resolution from verified candles only
  risk/policy.py     daily cap, entry timing, expiry policy
  execution/         PAPER + SIGNAL_ONLY adapters; LIVE is deliberately not implemented
  telegram/          message formatting + Bot API delivery (env credentials)
  signals/           engine · confidence · daily · tracker · scanner · ledger · outcomes · paper · ingestion
  data/store.py      rolling M1 store (candles table) + verified_price()
  data/feed.py       ContinuousFeed + asset_live_status() (FRESH / STALE / INVALID / UNAVAILABLE)
  database/db.py     SQLite schema, migrations, signal lock, performance aggregates
  research/          train.py · run_experiments.py · decision_report.py · paper_report.py · retrain.py
  static/index.html  single-file dashboard served at /preview (incl. LIVE PAPER TRADING panel)
frontend/            Next.js 16 + TS + Tailwind dashboard (+ lightweight-charts), same panels
tools/               gate_study.py (gate audit + oracle + variants; scan --minutes N --workers 2
                     [--stride K] [--kind full|audit_only] [--no-resume] · analyse) ·
                     verify_scenarios.py (acceptance A–M) · probe_endpoints.py (live HTTP route probe) ·
                     probe_live_feed.py (live-feed verdict, read-only) · start_live_feed.py (the feed bridge) ·
                     fixture_feed_server.py (verification-only fixture socket) ·
                     model_manifest.py (artifact hash manifest) · find_qualified_minute.py (full-history sweeps) ·
                     blocking_reasons.py (per-minute diagnostics + --from-study near-miss mining) ·
                     research_report.py (phases 4–9) · harness_model.py · activate_model.py (point
                     artifacts/current at a trained version) · smoke_dashboard.mjs
tests/               330 tests (live feed: transport/parser/M1/M1-boundary/validation/probe/bridge/
                     LIVE FEED state; twelve_data: key redaction/apikey masking/subscribe frame/
                     instrument map/control frames/M1 finalisation/provenance/freshness/OTC refusal/
                     ingestion + AUTO SELECT with no regressions; feed, candles, freshness, scanner,
                     lock, outcomes, evidence, Telegram gate matrix, research isolation,
                     IST minute boundaries, final-acceptance contract suite, artifact integrity)
```

---

## 9. Testing

```bash
python -m pytest tests -q            # 285 tests (incl. tests/test_live_feed.py — 50 feed contracts)
python3 tools/verify_scenarios.py    # acceptance scenarios A–M (isolated temp databases)
python3 tools/probe_endpoints.py     # every API route over HTTP (add --include-mutating)
node tools/smoke_dashboard.mjs       # executes the page's own JS against canned payloads
python3 tools/model_manifest.py      # artifact hash-manifest report (integrity of artifacts/current)
cd frontend && npx tsc --noEmit      # frontend type check
cd frontend && npm run build         # production build
```

Coverage includes: IST conversion & DST-safe boundaries, epoch/ISO parsing, asset validation,
config validation (including rejection of secrets through the API), feature causality
(*future rows cannot change past features*), leakage probe, confidence monotonicity and threshold
rejection, all six gates, walk-forward/embargo/holdout invariants, duplicate-signal locking,
stale-data rejection, MODEL_NOT_READY handling, OTC-without-feed unavailability, Telegram formatting
and failure modes, the tracker never inventing WIN/LOSS, and the HTTP API contract.

`tests/test_final_acceptance.py` adds the final acceptance contracts: one engine for button /
scanner / daily job, no client-side direction invention, AUTO counters, DAILY never forcing a signal
(and writing no row), daily cap, data-window provenance determinism + ledger round-trip, the evidence
rule and the Wilson bound (7/10 = 0.3968), the exact §14/§15 card and NO-TRADE wording, no marketing
claims anywhere in the served UI, Telegram off ⇒ zero HTTP calls, demo/stale ineligibility, `/api/system`
state keys, the 120 s / 180 s freshness budgets, gaps reported and never filled, scan-log outcomes,
and artifact integrity (path-safe model versions, hash manifest, refusal on mismatch).

`tests/test_final_phase.py` adds the operator-data path: CSV parsing (ISO / epoch s / epoch ms),
duplicate + non-monotonic rejection, invalid OHLC geometry, wrong resolution, flat/stale feed
detection, insufficient history, freshness budgets, the nine production eligibility requirements
(each one proven able to block a signal), demo-model rejection, the closed-candle boundary, dataset
registry reporting, import-endpoint hardening (payload cap, path-like filenames, unknown assets,
dry-run), CORS defaults and bot-token redaction.

`tools/smoke_dashboard.mjs` loads `backend/static/index.html`, extracts its script, runs it against a DOM
stub with canned API payloads (including one failure response) and asserts the MODEL QUALITY and DATA
PROVENANCE panels render the state, the version, the offline metrics, the evidence cells, the explicit
"no gate cleared" wording and the graceful backend-down message.

`tests/test_research_phase.py` covers the model-quality phase: M1 expiry labelling (theoretical fill,
settle at the horizon close, NaN without a complete future window), the next-candle target staying a
separate experiment, multi-horizon labels, extended-feature causality (mutating future candles cannot
change early rows), embargo invariants in the folds, the Wilson-bound threshold rule ("no threshold" is a
valid outcome), calibration-count consistency, chronological price-based replay, the provider adapter
contract, data-quality grading, artifact provenance fields, READY_MARKET vs READY_DEMO separation and the
experiment-index round trip.

---

## 10. Configuration reference

| Env var | Default | Meaning |
| --- | --- | --- |
| `MIN_CONFIDENCE` | `85` | Tradable threshold (UI-configurable 50–99) |
| `EXPIRY_MINUTES` | `1` | Expiry (1–5) |
| `TRADING_MODE` | `PAPER` | `PAPER` or `SIGNAL_ONLY` (LIVE is not implemented) |
| `DATA_SOURCE_MODE` | `auto` | `auto` / `live_only` / `csv` / `simulated` |
| `ENABLED_ASSETS` | all 7 | Comma-separated symbols |
| `AUTO_SELECT` | `true` | Scan all enabled assets, keep the best |
| `DAILY_SIGNAL_ENABLED` | `true` | Daily mode |
| `SCHEDULER_ENABLED` | `true` | Scan + evaluate every M1 candle |
| `TRADING_ENV` | `production` | `production` (real data + READY_MARKET required) or `development` |
| `MIN_MODEL_AGREEMENT` | `0.60` | Minimum ensemble agreement for a signal |
| `ENABLE_TELEGRAM` | `false` | Telegram delivery |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | Secrets (environment only) |
| `TELEGRAM_ALLOW_SIMULATED` | `false` | Allow demo-feed signals on Telegram |
| `ENABLE_INGESTION` | `false` | Start the continuous feed + automatic paper scanner with the API |
| `INGESTION_MODE` | `external` | `external` (real provider only) or `simulated` (labelled lab feed, dev only) |
| `PAPER_COLLECTION` | `false` | Record every scan — qualified **and** rejected — for the evidence tracker |
| `INGESTION_TICK_SECONDS` | `1.0` | Feed/scanner loop cadence |
| `RETRAIN_INTERVAL_DAYS` | `7` | Controlled retraining cadence (never per candle) |
| `RETRAIN_MIN_NEW_ROWS` | `2000` | New rows required before a retrain is even considered |
| `RETRAIN_MIN_IMPROVEMENT` | `0.0` | Minimum out-of-sample improvement required to promote a candidate |
| `QUOTEX_WS_URL`, `QUOTEX_SSID` | — | Live-feed endpoint + session (environment only; empty ⇒ `LIVE FEED NOT_CONFIGURED`) |
| `TWELVE_DATA_API_KEY` | — | Twelve Data realtime standard-FX key (environment only; empty ⇒ `TWELVE DATA NOT CONFIGURED`) |
| `TWELVE_DATA_WS_URL` / `TWELVE_DATA_SYMBOLS` | `wss://ws.twelvedata.com/v1/quotes/price` / `EUR/USD,GBP/USD,USD/JPY,USD/CHF,AUD/USD` | Twelve Data endpoint + the instruments it subscribes to (only mapped standard FX is accepted; OTC is never available) |
| `LIVE_FEED_ASSETS` | enabled assets | Which assets the bridge subscribes to (OTC excluded unless `LIVE_FEED_INCLUDE_OTC=true`) |
| `LIVE_FEED_AUTH_HEADER` / `_AUTH_SCHEME` / `_AUTH_IN_MESSAGE` | `Authorization` / — / `false` | How the session is presented to your feed |
| `LIVE_FEED_SUBSCRIBE_TEMPLATE` | — | JSON subscribe message with `{asset}` / `{ssid}` |
| `LIVE_FEED_SYMBOL_MAP` | — | Feed symbol → known asset (never guessed) |
| `LIVE_FEED_TARGET` / `_API_URL` / `_PROVIDER_LABEL` | `api` / `http://127.0.0.1:8000` / `operator_bridge` | Where the bridge delivers and the provenance label it writes |
| `LIVE_FEED_CONNECT_TIMEOUT` / `_RECV_TIMEOUT` / `_LOG_SECONDS` / `_MAX_SECONDS` / `_RETENTION_MINUTES` | `10` / `5` / `30` / `0` / `10080` | Timeouts, heartbeat logs, run window, live-row retention |
| `SIGNAL_DB_PATH` | `backend/database/signals.db` | SQLite location |
| `ALLOWED_ORIGINS` | same-origin + localhost | Explicit CORS origin list (`*` only if you ask for it) |
| `ALLOW_PREVIEW_ORIGINS` | `true` | Allow the sandbox preview host pattern (`https://<port>-<id>.e2b.app`) |
| `ENABLE_API_DOCS` | `true` | Set `false` to switch off `/docs`, `/redoc`, `/openapi.json` |

Runtime settings can also be patched via `POST /api/config` and are persisted in SQLite
(`config_kv`); the API refuses secret-looking keys outright.

---

## 11. Paper trading, evidence and research artefacts

Continuous collection runs through the supported external path — the operator pushes verified
ticks/candles, the engine proves one candle at a time, and nothing is ever invented:

```bash
# 1. push real candles (replay = both flags must agree; simulated is refused outside development)
curl -X POST http://127.0.0.1:8000/api/data/ingest -H 'Content-Type: application/json' \
  -d '{"asset":"USDJPY","provider":"operator_csv","replay":true,
       "candles":[{"ts_utc":"2026-10-02T06:48:00+00:00","open":147.1,"high":147.2,"low":147.0,"close":147.15,"volume":12}]}'

# 2. what the feed verdict is right now
curl http://127.0.0.1:8000/api/data/live-status      # FRESH / STALE / INVALID / UNAVAILABLE + age + budget
curl http://127.0.0.1:8000/api/health                # includes live_feed: state / data / latest M1 / age

# 3. run the automatic per-candle paper scanner (never re-evaluates a candle)
curl -X POST http://127.0.0.1:8000/api/data/ingest/start
curl http://127.0.0.1:8000/api/paper/scanner

# 4. resolve due signals from verified candles only → PENDING / WIN / LOSS / UNRESOLVED
curl -X POST http://127.0.0.1:8000/api/paper/settle

# 5. the evidence tracker (same numbers as the dashboard's LIVE PAPER TRADING panel)
curl http://127.0.0.1:8000/api/research/paper-evidence
curl -X POST http://127.0.0.1:8000/api/research/paper-report   # rebuild PAPER_TRADING_REPORT.md
```

`PAPER_COLLECTION=true` records **every** scan — qualified and rejected — so coverage, no-trade
rate and the rejection reasons are measurable, and each ledger row keeps the exact candle,
provider, model version and feature version that produced it.

Evidence bar (published, not adjustable): **≥ 100 decided trades AND Wilson 95 % lower bound >
0.50**. Until it clears, the honest answer stays NO TRADE. Current state is written to
`PAPER_TRADING_REPORT.md` (mode PAPER, 0 decided trades, coverage 0.00 %, evidence bar **NOT
PASSED**) and never mixed with the offline validation numbers.

Research artefacts produced by the read-only tooling in `tools/`:

| Artefact | What it answers |
| --- | --- |
| `GATE_AUDIT.md` + `GATE_AUDIT.json` | Per-gate pass rates and failure distribution over the real history, plus the cumulative funnel and the oracle feasibility class |
| `docs/RESEARCH_VARIANTS.md` | Gate-policy variants A–D (production / hard data+model+RISK / regime penalties / threshold sweep) under identical chronological validation |
| `docs/RESEARCH_REPORT.md` | Phases 4–9: targets, model families + challengers (UNAVAILABLE reported, never faked), features, regimes, calibration, thresholds |
| `docs/VERIFICATION_PAPER_PHASE.md` | The §18 scenario harness (A–M), including honest absences |
| `FINAL_DELIVERY_REPORT.md` | **The current delivery report** (Phase 41 final acceptance): build, backend, frontend, API, tests, TypeScript, build, security, data, model, gate audit, research, qualified cells, paper trading, Telegram, classification, external blockers, operator requirements and exact Windows commands |
| `DELIVERY_REPORT.md` | Phase-40 delivery report — **superseded** by the file above, kept for history:  build, API, tests, security, data, model, gate audit, research, paper trading, Telegram, classification, blockers, operator requirements and exact run commands |
| `docs/gate_diagnostics.json` | Near-miss mining over the *completed* audit (`blocking_reasons.py --from-study`): which real minutes came closest, and why they still failed |
| `tmp_gate_study_shards/` | Working state of the audit (`shard_<generation>_<worker>.jsonl.gz`). It lives inside the workspace on purpose: a full-history pass runs for tens of minutes, and `scan` resumes from whatever is already there instead of starting over |
| `docs/qualified_minute*.json` | Full-history sweeps of the unchanged gate stack — *superseded* by `GATE_AUDIT.json`: they were produced before the store adapter became clock-aware for historical clocks, so treat their zero as "not evaluable", not as a gate finding |

Retraining is controlled, never continuous: `RETRAIN_INTERVAL_DAYS` + `RETRAIN_MIN_NEW_ROWS` gate
whether a candidate is even trained, and a candidate is promoted only when it beats the current
artifact's out-of-sample metric by `RETRAIN_MIN_IMPROVEMENT` — a promotion always writes a
reproducibility record (dataset fingerprint, features version, folds, timings).

---

## 12. Honest limitations

1. **The shipped demo model is trained on simulated data.** The dashboard says `READY_DEMO` and every
   signal carries a simulated badge. Retrain on your own CSV/broker data for a market model.
2. **Real M1 edge is hard — the measured truth on this dataset.** The active `v1-market` artifact
   (USDJPY + USDCHF, ~56 k M1 rows) scores ≈ 1/3 accuracy on three classes with ROC-AUC ≈ 0.56 and a
   directional win rate of ≈ 34.5 % on the untouched holdout. The research grid found **no confidence
   gate that cleared the validation evidence bar**, so the correct behaviour is NO TRADE. Expect most
   scans — realistically all of them on a stale snapshot — to end in NO TRADE. That is the design.
3. **Classification today is `PAPER_TRADING_READY`, not live-ready.** Real M1 data has been ingested and
   validated, but the datasets are static snapshots: freshness can never pass, so nothing can be signalled
   until a continuously updating feed is connected. See `RESEARCH_DECISION.md`.
4. **OTC instruments need your own feed.** Nothing in this repository can legitimately provide OTC quotes.
5. **Public delayed feed** (Yahoo) is often rate-limited (HTTP 429) and is treated as replay data.
6. **No live execution.** `LIVE` mode raises `NotImplementedError` by design; a licensed broker adapter
   would be a separate, explicit integration.
7. **Signals are not financial advice.** You are responsible for anything you do with this software.

---

## 13. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `MODEL_NOT_READY` | Run `python -m backend.research.train …`, then `POST /api/model/reload` |
| `DATA_UNAVAILABLE` on OTC pairs | Expected: configure a broker feed or supply CSV candles |
| Everything is `NO_TRADE` | The gates are working. Check the confidence components and the scan log |
| Dashboard shows "Backend offline" | The API is not running on the same origin as the page |
| `HTTP 429` in the feed chain | Public feed rate-limited; the engine falls back (or reports unavailability) |
| Telegram status `NOT_CONFIGURED` | Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, restart the API |

---

*Built for study and paper trading. Confidence is a model score — never a guarantee.*
