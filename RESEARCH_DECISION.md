# Model quality & live paper-trading validation — decision report

- generated: 2026-10-02T13:01:00.025913+00:00
- classification: **PAPER_TRADING_READY**
- reasons: validated real market data present for ['USDJPY', 'USDCHF']; active artifact v1-market is READY_MARKET (56,396 rows of operator data); full-history gate audit (GATE_AUDIT.json): 27971 audited minutes produced 0 signal(s); even a perfect-direction oracle clears every gate 461/27037 times (FEASIBLE) — no threshold was changed; no continuously updating feed: every dataset is a static snapshot, so the freshness gate (120 s realtime / 180 s replay) can never pass for a signal

This report separates what was measured from what is claimed. It never labels the system profitable, and a confidence score is never described as accuracy.

## DATA QUALITY

| asset | badge | rows | resolution | newest candle (UTC) | age | validation | gaps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| USDJPY | REPLAY | 28292 | 1.0 min | 2026-10-02T06:48:00+00:00 | 22320 s | WARNING | 32 |
| USDCHF | REPLAY | 28285 | 1.0 min | 2026-10-02T06:49:00+00:00 | 22260 s | WARNING | 37 |
| EURUSD | DATA UNAVAILABLE | 0 | — min | — | — | DATA_UNAVAILABLE | — |
| GBPUSD | DATA UNAVAILABLE | 0 | — min | — | — | DATA_UNAVAILABLE | — |
| USDBDT-OTCq | DATA UNAVAILABLE | 0 | — min | — | — | DATA_UNAVAILABLE | — |
| EURCAD-OTCq | DATA UNAVAILABLE | 0 | — min | — | — | DATA_UNAVAILABLE | — |

Provider adapters (spec §11):

- `csv` — status **REPLAY_ONLY**, realtime_capable False, subscribe False — 5 dataset file(s) in /home/user/backend/data/csv — operator snapshots, not a live feed; signals need candles newer than the freshness budget
- `broker_ws` — status **NOT_CONFIGURED**, realtime_capable True, subscribe True — set QUOTEX_WS_URL / QUOTEX_SSID and attach the tick handler to stream real candles
- `public_feed` — status **REPLAY_ONLY**, realtime_capable False, subscribe False — public delayed data — treated as REPLAY, never as a live Quotex/OTC feed; M1 bars may be re-sampled or delayed by the upstream service
- `simulated` — status **READY**, realtime_capable True, subscribe True — synthetic feed for pipeline demonstration — never market data, never a production signal source

## MODEL QUALITY

- active artifact: `v1-market` (MARKET, feature version `m1-features-1.0`)
- trained: 2026-10-02T07:02:50+00:00 · rows: 56,396 · assets: USDJPY, USDCHF
- data period: 2026-09-04T07:52:00+00:00 → 2026-10-02T06:48:00+00:00
- walk-forward (out-of-fold): accuracy 0.3677, macro F1 0.3664, ROC-AUC 0.5638, directional win rate 34.5%
- untouched holdout: accuracy 0.3673, macro F1 0.3658, ROC-AUC 0.5622, samples 11280
- interpretation: at ~1/3 accuracy on three classes the model is **not** demonstrating a directional edge; ROC-AUC near 0.5 means it barely separates CALL from PUT.

## GATE AUDIT AND ORACLE FEASIBILITY

- window: 27971 real M1 minutes (2026-09-04 12:09:00+00:00 → 2026-10-02 06:47:00+00:00), provider operator_csv (REAL operator export)
- scan outcomes over the window: {'NO_TRADE': 27967, 'DATA_UNAVAILABLE': 4}
- per-gate result (unchanged production gate stack, nothing persisted):

| gate | evaluated | passed | failed | pass % | top failure reason |
| --- | --- | --- | --- | --- | --- |
| DATA | 27946 | 27942 | 4 | 99.99 | Stale data: newest candle closed 180005s ago (limit 180s for a replay feed) |
| FEATURE | 27967 | 27942 | 25 | 99.91 | Feature block incomplete: # missing (ret_1, ret_3, ret_5, ret_10…) |
| MODEL | 27942 | 17 | 27925 | 0.06 | Directional probability too low: # < # |
| ANOMALY | 27942 | 27626 | 316 | 98.87 | Feature vector outside the training distribution |
| REGIME | 27942 | 21188 | 6754 | 75.83 | Unfavorable market regime: choppy/mean-reverting (ADX # efficiency # |
| CONSISTENCY | 27942 | 2386 | 25556 | 8.54 | Conflicting indicators: only 1/4 rule groups (structure) support PUT; need # — opposing: t |
| CONFIDENCE | 27942 | 0 | 27942 | 0.0 | Confidence # is below threshold # |
| RISK | 0 | 0 | 0 | — | — |
| LOCK | 27942 | 27942 | 0 | 100.0 | — |
| FINAL | 27971 | 0 | 27971 | 0.0 | — |

- oracle feasibility (research only — perfect direction known in advance): **FEASIBLE** · 461 of 27037 applicable minutes cleared every gate
- the gate that still blocks even a perfect model: **CONFIDENCE: Confidence # is below threshold #** — model quality is not the binding constraint on this history
- no production threshold was changed by this audit; a variant may only be adopted with defensible out-of-sample evidence, never because it produces more signals

## VALIDATION QUALITY

- research run `lab-20261002T080023Z` on dataset `ds-d21e6369e945` (56,577 rows)
- experiments: **33** · with a qualifying gate: **0** · without evidence of edge: **33**
- held out: no shuffling, expanding-window folds with an embargo ≥ the label horizon, one untouched chronological holdout applied once per cell
- thresholds: chosen on validation folds only under a published rule (≥ 100 signals and a Wilson 95 % lower bound above 0.50); 'no threshold' is a valid, honest outcome
- **no cell cleared the evidence bar**, so the correct live behaviour of the running system is NO TRADE — that is the system working as specified, not a defect

## PAPER-TRADING RESULTS

- live paper trades generated: 0 · decided: 0 · wins 0 / losses 0 · pending 0 · unresolved 0
- win rate: — (no decided paper trades: the feed is a static snapshot, so the freshness gate blocks signalling, and every outcome would have to come from a verified candle close anyway)
- offline paper replay over the holdout: 0 signals across all cells, 0 wins / 0 losses (descriptive only; never mixed with live paper results)

## SIGNAL COVERAGE AND NO-TRADE RATE

- signal coverage on live data: **0 signals** — the newest operator candle is hours old (static snapshot), which fails the configured 120 s / 180 s freshness budgets
- no-trade rate on live data: **100 %**, with the reason surfaced in the API/UI (`DATA SOURCE IS REAL` / freshness), never a fabricated CALL/PUT
- offline: 33 of 33 experiment cells chose no threshold, i.e. the evidence-based default outside those cells is also NO TRADE

## KNOWN LIMITATIONS

- operator datasets are **static snapshots**: they can train and evaluate a model, but they cannot produce a fresh signal, so live paper trading cannot start until a continuously updating feed exists
- OTC instruments have no public data source; they report DATA UNAVAILABLE until a broker/operator connection is wired (never simulated prices dressed as OTC quotes)
- the public delayed feed is rate-limited and re-sampled; it is labelled REPLAY and must not be treated as a Quotex feed
- two assets of ~8 days of M1 data is a small, correlated sample; metrics carry wide uncertainty and class balance shifts between sessions
- XGBoost is unavailable in this environment and the neural challenger is untrained; both are recorded as unavailable rather than silently skipped
- every metric here is offline validation or replay; none of it is a forecast, and none of it is combined with live paper results

## CLASSIFICATION

**PAPER_TRADING_READY**

| classification | meaning |
| --- | --- |
| RESEARCH_ONLY | pipeline works on historical data only; no real-data model and/or no validated real dataset |
| PAPER_TRADING_READY | real data validated + READY_MARKET artifact + gates enforced, but a continuously updating feed (or accumulated paper evidence) is still missing |
| LIVE_DATA_READY_FOR_PAPER_SIGNALING | fresh real feed + READY_MARKET + a gate that cleared the evidence bar + enough decided paper trades to report |

The system is **not** labelled profitable: nothing measured here supports that claim, and the correct behaviour today is NO TRADE.
