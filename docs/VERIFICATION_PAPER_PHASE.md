# Verification — continuous M1 data & paper-trading phase

- generated: 2026-10-03T14:29:41+00:00
- harness: `python3 tools/verify_scenarios.py` (isolated temporary databases)

| # | scenario | expected | observed | verdict |
| --- | --- | --- | --- | --- |
| A no real feed → DATA UNAVAILABLE | — | DATA_UNAVAILABLE + no candle | DATA_UNAVAILABLE · store row: None | ✅ PASS |
| B simulated data only → READY_DEMO, production blocked | — | no SIGNAL from simulated/demo material | feed FRESH · scan DATA_UNAVAILABLE · demo artifact state None | ✅ PASS |
| C stale real feed → DATA_UNAVAILABLE / STALE | — | STALE + DATA_UNAVAILABLE(stale) | STALE (114032s old) · scan DATA_UNAVAILABLE | ✅ PASS |
| D fresh real feed + weak model → NO_TRADE | — | FRESH + NO_TRADE with the real blocking reason | FRESH at 06:48:05Z · NO_TRADE · confidence 40.79650000000001 · 3 blocking check(s) | ✅ PASS |
| D2 fresh feed + model not READY_MARKET → MODEL_NOT_READY | — | MODEL_NOT_READY, no signal | MODEL_NOT_READY | ✅ PASS |
| E fresh real feed + model disagreement → NO_TRADE | — | NO_TRADE blocked by the model/agreement gate | NO_TRADE · confidence 51.00770000000001 · Conflicting models: weighted agreement 0.33 < 0.6 | ✅ PASS |
| F-real fresh real feed + production artifact → PAPER SIGNAL | — | SIGNAL from a real qualified setup | not demonstrable on this history: 6 real minutes with the production artifact, confidence 74.23–83.38 (threshold 85) | ⚠️ NOT DEMONSTRABLE — honest absence |
| F fresh real feed + qualified pipeline → PAPER SIGNAL | — | SIGNAL → ledger row PENDING → card fields → Telegram not sent | SIGNAL · real minute 2026-10-02 03:30:00+00:00 (PUT) · card PAPER SIGNAL PUT 86.17% · data REAL · ledger rows 1 · telegram NOT SENT · 1 scans | ✅ PASS |
| G same candle scanned twice → LOCKED | — | LOCKED, no second ledger row | LOCKED · locked attempts 1 · ledger rows 1 | ✅ PASS |
| H expired signal → WIN / LOSS / UNRESOLVED from the verified candle | — | WIN + LOSS + UNRESOLVED (no guessed outcome) | WIN / LOSS / UNRESOLVED | ✅ PASS |
| I AUTO SELECT → qualified candidate only | — | no signal; every unavailable asset is discarded, not guessed | scanned 1 · qualified 0 · rejected 0 · unavailable 1 · best {'asset': 'USDJPY', 'confidence': None, 'quality': None, 'direction': None} | ✅ PASS |
| J DAILY mode → one qualified signal maximum, else NO TRADE | — | NO_TRADE today (nothing qualified) | status NO_TRADE · selected None · signals today None | ✅ PASS |
| K Telegram + simulated/READY_DEMO → blocked | — | no HTTP call is attempted | DISABLED (unconfigured) · BLOCKED_NOT_MARKET (configured) · calls 0 | ✅ PASS |
| L Telegram + real qualified paper signal → attempts delivery | — | one POST to the Bot API in the §23 format (stub transport) | SENT · http 200 · calls 1 · format ok | ✅ PASS |
| M live feed stops → DISCONNECTED, nothing fabricated | — | DISCONNECTED + freshness ≠ FRESH + DATA_UNAVAILABLE + empty ledger | DISCONNECTED (age 114039s, window 300.0s) · freshness STALE · scan DATA_UNAVAILABLE · ledger rows 0 | ✅ PASS |
| API surface (spec §3, §7, §12, §15) | — | all endpoints 200 | {"/api/data/live-status": 200, "/api/paper/summary": 200, "/api/paper/card": 200, "/api/paper/ledger": 200, "/api/paper/scanner": 200, "/api/research/paper-evidence": 200, "POST /api/research/paper-report": 200} | ✅ PASS |

## How each scenario was exercised

- **A** empty store + production policy → the engine reports `DATA_UNAVAILABLE`; nothing is stored.
- **B** a SIMULATED feed → the engine may analyse it, but its verdict can never be a production signal, no ledger row is written for a market claim, and no Telegram call may be attempted.
- **C** REAL operator candles (hours old) → freshness says `STALE`, the scan refuses to analyse them.
- **D** REAL operator candles at a replay clock 5 s after the newest close (freshness genuinely `FRESH`) + the real READY_MARKET artifact → `NO_TRADE` with the exact blocking check. No threshold is lowered; no simulated data is involved.
- **D2** fresh feed + an unready (READY_DEMO) model → `MODEL_NOT_READY`; a stricter model blocks, never the other way round.
- **E** a deliberately disagreeing ensemble split → `NO_TRADE` from the agreement gate, proving the ensemble cannot be talked into a trade by one confident voter.
- **F** a qualified pipeline on REAL candles completes the emit path: signal built, card fields populated, ledger row `PENDING`, Telegram not sent. Only the learned model *layer* is the labelled harness double (`tools/harness_model.py`); prices, freshness, features, rule scores, every gate, the confidence arithmetic, eligibility, the ledger and the lock are production code. It is a plumbing demonstration, never a market claim.
- **F-real** the same pipeline with the production artifact → honest absence: no real minute clears the gates anywhere on this history, recorded rather than papered over. The full-history audit (`GATE_AUDIT.md`) is the measurement; this row is the scenario-level statement of it.
- **G** the same candle scanned again → `LOCKED`; no second ledger row is ever created.
- **H** REAL operator candles settle `WIN` / `LOSS` from verified prices; a signal whose expiry candle is missing stays `UNRESOLVED` with no invented price.
- **I** AUTO SELECT discards unavailable assets instead of guessing a direction for them.
- **J** DAILY mode: at most one best *qualified* signal per day, otherwise NO TRADE.
- **K** Telegram refuses SIMULATED / READY_DEMO material even when credentials are configured.
- **L** with a configured operator token the §23 message is delivered through the stub transport (no real token exists in this build — the delivery is proven, never faked).

## What F-real does NOT prove (and why it is still the right answer)

It does not show that the real model produces qualified setups — on this operator history it does not, and the honest absence is the result. The read-only full-history gate audit (`GATE_AUDIT.md` / `GATE_AUDIT.json`) re-ran the unchanged gate stack over every real minute and recorded the outcome; `docs/gate_diagnostics.json` mines that completed audit for the minutes that came closest and why they still failed. Until the evidence bar is met, this system is a **paper collection** system, not a live-ready one.
