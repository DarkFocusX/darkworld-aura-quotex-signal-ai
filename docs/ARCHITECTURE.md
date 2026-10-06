# Architecture

## Layering (strict)

```
                 ┌───────────────────────────────┐
   browser ────▶ │  frontend/ (Next.js + TS)     │   single page, mobile-first
                 │  or backend/static/index.html │   (same page served at /preview)
                 └──────────────┬────────────────┘
                                │  /api/*  (server-side rewrite; one origin)
                 ┌──────────────▼────────────────┐
                 │  backend/main.py  (FastAPI)   │   HTTP surface, payload shapes
                 └──────────────┬────────────────┘
      ┌─────────────────────────┼───────────────────────────┐
      ▼                         ▼                           ▼
┌───────────┐            ┌─────────────┐             ┌───────────────┐
│ signals/  │            │ validation/ │             │ database/     │
│ engine    │◀──────────▶│ checks      │             │ SQLite        │
│ confidence│            │ time_series │             │ signals/scans │
└─────┬─────┘            │ metrics     │             └───────────────┘
      │                  └─────────────┘
      ├─────────────┬──────────────┬───────────────┬──────────────┐
      ▼             ▼              ▼               ▼              ▼
┌──────────┐  ┌──────────┐  ┌───────────┐  ┌───────────┐  ┌────────────┐
│ data/    │  │ features/│  │ models/   │  │ risk/     │  │ telegram/  │
│ providers│  │ indicators│ │ ensemble  │  │ policy    │  │ notifier   │
│ registry │  │ pipeline  │ │ service   │  │           │  │            │
└──────────┘  │ rules     │  │ anomaly   │  └───────────┘  └────────────┘
              └───────────┘  │ neural    │
                             └───────────┘

   research/ (offline training + validation)  ──writes──▶  models/artifacts/<version>
   execution/ (PAPER · SIGNAL_ONLY)  ← separate consumer of the signal engine
```

Key separations:

* **Signal engine ≠ execution engine.** Signals are produced, stored and notified independently of any
  execution venue. `execution/paper.py` records paper trades; `LIVE` raises `NotImplementedError`.
* **Research ≠ runtime.** `research/train.py` only writes artifacts; the runtime only reads them.
* **Data providers ≠ fabrication.** Every frame carries `FeedMeta` provenance; simulated data is
  impossible to mistake for market data.
* **Validation ≠ signalling.** `validation/checks.py` can only *reject*; it never influences direction.
* **Provenance travels with the data.** `FeedMeta` carries `provider`, `timeframe`, `is_simulated`,
  `is_replay`, `is_market_data`, `source_class`, `source_status` and `last_update_utc`; the same
  fields are stored on the signal row, returned by `/api/data/status` and rendered as one of the four
  badges (`LIVE` / `REPLAY` / `SIMULATED` / `DATA UNAVAILABLE`).

## Request lifecycle of one scan

1. `POST /api/signal/generate` → `SignalEngine.generate()`
2. Target assets resolved (`AUTO SELECT` → all enabled assets)
3. Per asset: `registry.get_frame()` walks the provider chain, recording every attempt
4. `data_check()` — candles, gaps, staleness for the feed type (`LIVE` 120 s, replay 180 s)
5. `latest_features()` — 44 causal features + a display snapshot
6. `ModelService.predict()` — per-model probabilities → weighted ensemble → agreement/spread → anomaly
7. Rule scores (trend, momentum, candle, structure, volatility, regime, freshness)
8. `compute_confidence()` — weighted 0–100 + quality band
9. `run_all_checks()` — model / feature / regime / confidence / consistency gates
10. `RiskPolicy` — daily cap, expiry bounds; `db.signal_exists_for_candle()` — signal lock
11. Emit: persist (unique index on `asset + entry_time_utc`), paper execution, Telegram, scan log
12. Rejections are written to `scans` with the exact blocking reason — visible in the UI

Steps 4, 9 and 10 additionally feed `evaluate_eligibility()`, and the per-asset outcome
(`SIGNAL` / `NO_TRADE` / `LOCKED` / `DATA_UNAVAILABLE` / `MODEL_NOT_READY` / `ERROR`) plus the
AUTO-SELECT counters (`scanned`, `qualified`, `rejected`, `data_unavailable`) are returned in
`scan_summary` so the page can explain exactly why nothing was emitted.

## Operator data ingestion (spec §1–§3)

```
 operator CSV ──▶ POST /api/data/import ──▶ csv_text_to_frame()   (parse only, in memory)
                        │                        │
                        │                        ├─ NUL/control chars, >25 MB text, >500k rows  → rejected
                        │                        └─ ISO-8601 / epoch s / epoch ms → UTC DatetimeIndex
                        ▼
                 validate_candles()  ── resolution · duplicates · ordering · OHLC geometry ·
                        │                finite/negative values · history · flat feed · gaps
                        │                (report status: VALID | WARNING | INVALID)
                        ├── INVALID ──▶ nothing is written; response carries the full report
                        ▼
                 CsvProvider.write_dataset()   → backend/data/csv/<ASSET>.csv (atomic replace)
                        │
                        └─ provenance.record_import()  → backend/data/provenance.json
                                                           source_class, filename, sha256, rows,
                                                           period, has_volume, operator_label,
                                                           validation report
```

At scan time the chain is walked in order (`auto`: csv → broker_ws → public delayed → simulated) and
the **whole file** is re-validated before the tail is used — a dataset that stops passing validation
becomes `DATA UNAVAILABLE` even if the last 300 rows look fine. In `production` the engine refuses a
synthetic result outright (step 1b of `scan_asset`), so an instrument with no real feed reads
`DATA UNAVAILABLE` and is never analysed from simulated candles. `development` keeps the labelled
simulated feed.

## Production eligibility policy (spec §5)

`signals/policy.py::evaluate_eligibility()` is the last gate before emission and can only ever
*remove* signals. Nine named requirements, each returned with pass/fail, evidence and (in
development) a `downgraded` flag:

| # | Requirement | Blocking condition |
| --- | --- | --- |
| 1 | DATA SOURCE IS REAL | source class is not `MARKET` or the feed is simulated |
| 2 | DATA IS FRESH | `freshness_seconds` > 120 s (live) / 180 s (replay) |
| 3 | MODEL READY_MARKET | model state is `READY_DEMO` / `NOT_READY` |
| 4 | ENOUGH CANDLES | fewer than `GATES["min_candles"]` (120) closed candles |
| 5 | MANDATORY GATES PASS | any blocking check in the validation suite |
| 6 | CONFIDENCE ≥ THRESHOLD | confidence below `min_confidence` (default 85) |
| 7 | AGREEMENT ≥ THRESHOLD | ensemble agreement below `min_model_agreement` (default 0.60) |
| 8 | NO ANOMALY VETO | IsolationForest flagged the feature vector |
| 9 | SIGNAL LOCK PASSES | a signal already exists for this asset + candle |

`development` downgrades requirements 1 and 3 to warnings, labels the decision `DEVELOPMENT`, and the
resulting signal/alert is banner-marked so it can never be mistaken for a production signal. A
lock-only block is reported as `LOCKED` (not `NO TRADE`) because the setup itself was qualified.

## Why NO TRADE is the default outcome

The system has five ways to refuse and only one way to accept. Empirical notes from the shipped demo
artifact (trained on **simulated** data): most scans fail the `MODEL CHECK` (directional probability or
agreement) or the `CONFIDENCE CHECK`. That is expected behaviour for a next-candle problem — the gates
are doing their job.

## Failure semantics

| Failure | Behaviour |
| --- | --- |
| Feed unavailable | `status: DATA_UNAVAILABLE`, reason + providers tried; **no signal** |
| Feed stale | `DATA_UNAVAILABLE` with the measured lag and the limit |
| Model artifacts missing | `MODEL_NOT_READY` + the exact training command |
| One model raises during inference | its vote is marked `ERROR` and excluded; fewer than 2 usable votes blocks the signal |
| Database failure while storing | `status: ERROR` — a signal is never reported that was not stored |
| Telegram failure | stored `telegram_status` (`FAILED` + reason); the signal itself is unaffected |
| Scheduler crash | logged to `events`; the API keeps serving |

## Data model (SQLite)

| Table | Purpose |
| --- | --- |
| `signals` | one row per emitted signal with the full evidence bundle + outcome |
| `scans` | every scan attempt, including rejections (transparency log) |
| `config_kv` | runtime settings patched via `POST /api/config` |
| `daily_best` | best qualified setup per IST trading day |
| `events` | internal event log (scheduler, errors) |

The unique index `idx_signal_lock(asset, entry_time_utc)` is what makes "one signal per candle per
asset" a database guarantee rather than a convention. `init_db()` also performs in-place migrations
(added columns + backfill) so existing databases survive upgrades.

## Provider adapter interface (spec §11)

Every feed behind the registry implements the same small contract, so a real broker feed can be dropped in
without touching the engine:

| Method | Meaning here |
| --- | --- |
| `connect()` | verify the adapter is wired (never invents a session) |
| `disconnect()` | release resources (no-op for pull sources) |
| `health()` | `provider, connected, status, realtime_capable, supports_subscribe, is_simulated, detail` |
| `latest_tick(asset)` | newest closed candle as a tick dict, or `None` when unavailable/stale |
| `latest_candles(asset, limit)` | the `CandleFrame` used by the engine (provenance attached) |
| `subscribe(asset, cb)` | push registration — replay sources honestly answer `supported: false` |

Adapter states are explicit: `STREAMING` (broker ticks arriving), `AWAITING_TICK_STREAM`,
`NOT_CONFIGURED` (broker), `REPLAY_ONLY` (csv / public feed), `READY` (simulated, never market data),
`UNAVAILABLE`, and for the live adapter `CONNECTED` / `DISCONNECTED` / `AUTH_FAILED`. `GET /api/data/status` exposes the per-provider view, and the data-quality block grades each
dataset (rows, resolution, gaps, session breaks, duplicates, invalid OHLC, flat ratio, age) — the same numbers
the UI shows.

## Research layer (spec §1–§9)

```
backend/research/
  targets.py        label builders (M1_EXPIRY_OUTCOME, NEXT_CANDLE_DIRECTION) + realised_outcome()
  feature_lab.py    base (m1-features-1.0) and extended (m1-features-lab-1.0) causal feature sets
  lab.py            folds, models, sweep, calibration, replay, walk-forward paper, persistence
  run_experiments.py  CLI for the grid
  decision_report.py  RESEARCH_DECISION.md generator (classification by published rule)
  experiments/      index.json / latest.json + one JSON (+ signals .jsonl) per cell
```

Guarantees encoded in the research layer:

* labels only ever reference bars **after** the decision candle (`open[i+1]` → `close[i+h]`), and rows
  without a complete future window stay NaN;
* every cell uses the same chronological folds with embargo ≥ horizon and one untouched holdout;
* thresholds are selected on validation folds only under a fixed rule (≥ 100 signals, Wilson 95 % lower
  bound > 0.50); "no threshold" is a first-class result, and the paper replay then emits nothing;
* outcomes in the replay are read from the dataset's entry/expiry prices — never from the prediction;
* results are published as measured: no ranking, no blended live/offline numbers, no profitability claim.

---

## Continuous M1 ingestion + paper collection (phase 2)

```
backend/data/store.py          rolling M1 store (candles table, PK asset+provider+ts_utc, 7-day retention)
backend/data/feed.py           ContinuousFeed (connect/subscribe/tick/health) + asset_live_status()
backend/data/provenance.py     per-import provenance records + window_version() = win-<sha256[:12]>
                               (deterministic id of the data window a signal was produced from)
backend/models/integrity.py    artifact hash manifest, verified before anything is deserialised
backend/signals/ingestion.py   process-wide feed + scanner lifecycle (ENABLE_INGESTION / INGESTION_MODE)
backend/signals/scanner.py     one evaluation per newly closed candle — the SAME SignalEngine as the button
backend/signals/ledger.py      paper ledger, provenance columns, Wilson bound, streaks, evidence verdict
backend/signals/outcomes.py    verified settlement only (fill at entry OPEN, settle on the close at entry+expiry−1)
backend/signals/paper.py       live_status / panel / card payloads behind the LIVE PAPER TRADING panel
backend/research/paper_report.py  PAPER_TRADING_REPORT.md generator
```

Guarantees encoded here:

* the store adapter is **clock-aware** — a scan at time *X* only ever sees candles that had already
  closed by *X*, which is what makes a chronological replay honest (no future leakage into features);
* missing candles are reported as gaps and **never filled**; a duplicate tick (same minute + price) is dropped;
* the same (asset, candle) is evaluated at most once — scanner memory *and* a database-level per-candle
  signal lock, so a manual click and the automatic scanner can never double-fire;
* outcomes are read from verified candles only; anything unverifiable stays `UNRESOLVED` rather than guessed;
* the evidence bar (≥ 100 decided paper trades **and** Wilson 95 % lower bound > 0.50) is published as a
  verdict — `PASSED` / `NOT PASSED` — and is never softened to make a result look better;
* every emitted signal carries its provenance — `model_version`, `feature_version`,
  `dataset_version` (the `win-…` window id of `backend/data/provenance.py`), `feed_provider` and
  `feed_status` — attached to the ledger row by the emission path, never re-derived later;
* model artifacts are hash-verified (`manifest.json`) *before* loading; a mismatched or substituted
  file is refused with `ARTIFACT INTEGRITY MISMATCH` instead of being deserialised, and a model
  version string can never be a filesystem path.

## Live feed layer (phase 42)

```
backend/data/feedsource.py        transport + parser: WebSocketFeedSource (lazy websocket-client import,
                                  header / in-message auth, backoff, idle timer, redacted health) and
                                  parse_feed_message() → tick | candle | ignored | invalid | auth_error
backend/data/providers/broker_ws.py  live adapter: state machine, bounded tick buffer, push_tick/drain_ticks,
                                  push_candle → validate + store, latest_tick/latest_candles/fetch,
                                  stats(), health(), note_*() — never echoes a session value
backend/signals/feed_state.py     live_feed_state(): the LIVE FEED summary read from stored evidence only
tools/probe_live_feed.py          read-only probe → CONNECTED | NOT_CONFIGURED | AUTH_FAILED | NO_DATA |
                                  INVALID_DATA (exit 0/3/4/6/7) · --self-test on labelled fixtures
tools/start_live_feed.py          the bridge CLI: connect → subscribe → parse → build M1 → deliver
                                  (api | store) with heartbeat/health logs, reconnect, graceful Ctrl+C
tools/fixture_feed_server.py      verification-only local fixture socket (never a provider)
```

Guarantees encoded here:

* **one M1 builder.** Ticks go through `ContinuousFeed` (open = first tick, high/low = running
  extremes, close = last tick, volume only when the feed supplies it). A candle is CLOSED only after
  the minute ends; an unfinished candle is never persisted and never counted as FRESH;
* **everything is validated before it is stored** — symbol, timestamp, UTC, M1 resolution, OHLC
  geometry, monotonicity, duplicate (dropped for ticks, updated for re-sent candles), future
  timestamps rejected; a rejected message is counted and logged, never repaired;
* **provenance per row** — provider, `source_label`, realtime / simulated / replay flags, ingest
  time; the API stamps the *caller's* label, so a fixture or a replay can never be filed as market data;
* **the LIVE FEED state is evidence, not intention**: `FRESH` requires a *closed* realtime candle
  inside the 120 s budget (180 s replay); a silent endpoint is `NOT_CONFIGURED`, a stream that stopped
  more than 5 minutes ago is `DISCONNECTED`, and a configured-but-silent socket is `CONNECTED` with
  `NO_DATA`. Replay and simulated rows are excluded from the live verdict entirely;
* **secrets never leave the process**: session values are registered for redaction at parse time and
  stripped from URLs, headers, logs, health payloads and error strings;
* **the API never refuses to start** because a feed is missing — startup writes one `FEED` event and
  keeps serving; `POST /api/signal/generate` answers `DATA_UNAVAILABLE` instead of inventing a candle;
* **one long-lived builder per external producer**: `POST /api/data/ingest` keeps a per-(provider,
  realtime/simulated/replay) `ContinuousFeed`, so a tick-pushing collector closes real M1 candles
  across requests instead of restarting the minute every call.

## Offline research tooling and the oracle-isolation rule

```
tools/gate_study.py             gate audit · oracle feasibility · variants A–D (one read-only pass)
tools/blocking_reasons.py       per-minute blocker histogram on a bounded window
tools/find_qualified_minute.py  read-only sweep of the real history with the production gates unchanged
tools/harness_model.py          model-layer double used only by research/harness scenarios
tools/verify_scenarios.py       acceptance scenario harness (A–M) on isolated temporary databases
tools/probe_endpoints.py        live HTTP probe of every API route (read-only by default)
tools/model_manifest.py         write/report the artifact hash manifest
tests/test_research_isolation.py  proves the oracle can never reach production inference
tests/test_final_acceptance.py    contract suite over the acceptance criteria (one engine, wording,
                               provenance round-trip, evidence rule, artifact integrity)
```

`tools/gate_study.py` is the **only** code in the repository that reads the future to choose a
direction (the oracle diagnostic). It is one-way dependent: it imports production code, exposes no API,
and is never imported by anything under `backend/`. The isolation tests walk the backend sources,
the import graph and the FastAPI route table and fail the build if that ever changes — a signal can
therefore never be produced with knowledge of the future.

## Long-history research and the rolling-store retention rule

`backend/data/store.py::upsert_candles` prunes rows older than `retention_minutes` (default: one week)
**relative to wall-clock now**. That is correct for a live rolling store, but it silently truncates a
historical replay that spans more than the retention window — a 28-day audit would score the oldest
minutes as `DATA` failures even though the export contains them. Every long-history tool therefore
ingests with `retention_minutes=0` (`tools/gate_study.py`, `tools/find_qualified_minute.py`,
`tools/blocking_reasons.py`), so the audit measures the **gate stack**, not the retention policy:

```
store.upsert_candles(rows, retention_minutes=0)     # research/replay: keep the whole window
store.upsert_candles(rows)                          # live feed: rolling retention (default)
```

A second, subtler trap: `LiveStoreProvider.fetch(now)` used to read the *newest* N rows of the store and
then apply the caller's cutoff, so a scan clocked in the past received an empty window and fell through
the provider chain — every historical minute looked like `No verified market data available`. The
provider now asks the store for a window ending at the caller's clock (`store.candles(..., until=now)`),
which is the same rows for live scans and the correct rows for a replay. Both defects were found by
running the audit and reading its output rather than trusting it. The first run reported `DATA`
failures for 27 792 of 27 971 minutes; that was a measurement bug, not a market finding. The corrected
full-history run reports `DATA` passing 27 942 of 27 946 evaluated minutes (99.99 % — the four failures
are the Sunday 23:00 UTC week-open minutes, where the newest candle really is ~50 h old because the FX
market was closed), `FEATURE` 99.91 %, `MODEL` 0.06 %, `REGIME` 75.83 %, `CONSISTENCY` 8.54 %,
`CONFIDENCE` 0.00 %, `LOCK` 100 %, and a final count of **0 signals**, with the oracle clearing every
gate 461 of 27 037 applicable minutes (`FEASIBLE`).

### Artefact map

```
GATE_AUDIT.md / GATE_AUDIT.json     per-gate funnel + oracle feasibility + variants A–D (full history)
docs/RESEARCH_VARIANTS.md           variant detail: signals / coverage / hit rate / Wilson LB / streaks
docs/RESEARCH_REPORT.md             phases 4–9 mined from the stored experiment records (read-only)
docs/gate_diagnostics.json          near-miss mining over the completed audit (--from-study)
docs/qualified_minute*.json         read-only sweeps of the whole history — superseded by GATE_AUDIT.json
                                    (they predate the clock-aware store query, so their zero means
                                     "could not be evaluated", not "no setup exists")
docs/VERIFICATION_PAPER_PHASE.md    §18 scenarios A–M incl. honest absences
PAPER_TRADING_REPORT.md             live paper collection: counts, coverage, calibration buckets, verdict
```
