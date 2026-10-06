# GATE AUDIT — why the real operator history produced no signal

- window: `2026-09-04 12:09:00+00:00` → `2026-10-02 06:47:00+00:00` (27971 minutes, USDJPY, operator_csv (REAL operator export))
- generated: 2026-10-02T13:02:17+00:00
- engine: **unchanged production gate stack**, `persist=False` (nothing was written)
- scan outcomes: {"NO_TRADE": 27967, "DATA_UNAVAILABLE": 4}

## 1. Per-gate result

| gate | evaluated | not reached | passed | failed | pass % (of evaluated) | top failure reason |
| --- | --- | --- | --- | --- | --- | --- |
| DATA | 27946 | 25 | 27942 | 4 | 99.99 | Stale data: newest candle closed 180005s ago (limit 180s for a replay feed) (2) |
| FEATURE | 27967 | 4 | 27942 | 25 | 99.91 | Feature block incomplete: # missing (ret_1, ret_3, ret_5, ret_10…) (25) |
| MODEL | 27942 | 29 | 17 | 27925 | 0.06 | Directional probability too low: # < # (17705) |
| ANOMALY | 27942 | 29 | 27626 | 316 | 98.87 | Feature vector outside the training distribution (316) |
| REGIME | 27942 | 29 | 21188 | 6754 | 75.83 | Unfavorable market regime: choppy/mean-reverting (ADX # efficiency # (6438) |
| CONSISTENCY | 27942 | 29 | 2386 | 25556 | 8.54 | Conflicting indicators: only 1/4 rule groups (structure) support PUT; need # — opposing: trend, momentum, candle (2946) |
| CONFIDENCE | 27942 | 29 | 0 | 27942 | 0.0 | Confidence # is below threshold # (27942) |
| RISK | 0 | 27971 | 0 | 0 | None | — (0) |
| LOCK | 27942 | 29 | 27942 | 0 | 100.0 | — (0) |
| FINAL | 27971 | 0 | 0 | 27971 | 0.0 | — (0) |

### Failure-reason distribution (top 5 per gate)

**DATA**

- `2×` Stale data: newest candle closed 180005s ago (limit 180s for a replay feed)
- `2×` Stale data: newest candle closed 179885s ago (limit 180s for a replay feed)

**FEATURE**

- `25×` Feature block incomplete: # missing (ret_1, ret_3, ret_5, ret_10…)

**MODEL**

- `17705×` Directional probability too low: # < #
- `5404×` Conflicting models: weighted agreement # < #
- `4500×` Models report uncertainty: NEUTRAL probability # is the top class — this is a coin-flip setup, not a trade
- `316×` Anomaly detector flagged the current market state as out-of-distribution

**ANOMALY**

- `316×` Feature vector outside the training distribution

**REGIME**

- `6438×` Unfavorable market regime: choppy/mean-reverting (ADX # efficiency #
- `316×` Abnormal market regime: anomaly detector veto

**CONSISTENCY**

- `2946×` Conflicting indicators: only 1/4 rule groups (structure) support PUT; need # — opposing: trend, momentum, candle
- `2660×` Conflicting indicators: only 1/4 rule groups (structure) support CALL; need # — opposing: trend, momentum, candle
- `2453×` Conflicting indicators: only 1/4 rule groups (trend) support CALL; need # — opposing: momentum, candle, structure
- `2294×` Conflicting indicators: only 0/4 rule groups (none) support CALL; need # — opposing: trend, momentum, candle, structure
- `2086×` Conflicting indicators: only 0/4 rule groups (none) support PUT; need # — opposing: trend, momentum, candle, structure

**CONFIDENCE**

- `27942×` Confidence # is below threshold #

## 2. Cumulative filtering

```
ALL candidates            27971
DATA                        27967   (blocked here: 4)
FEATURE                     27942   (blocked here: 25)
MODEL                          17   (blocked here: 27925)
ANOMALY                        17   (blocked here: 0)
REGIME                         10   (blocked here: 7)
CONSISTENCY                     0   (blocked here: 10)
CONFIDENCE                      0   (blocked here: 0)
RISK                            0   (blocked here: 0)
LOCK                            0   (blocked here: 0)
FINAL                           0   (blocked here: 0)
```

## 3. Oracle feasibility (research only)

- verdict: **FEASIBLE**
- minutes where the oracle was applicable: 27037 (flat settlement candles: 905)
- oracle signals (perfect direction *and* every remaining gate cleared): 461

| oracle stage | evaluated | passed | failed |
| --- | --- | --- | --- |
| DATA | 27037 | 27037 | 0 |
| FEATURE | 27037 | 27037 | 0 |
| MODEL | 27037 | 27037 | 0 |
| ANOMALY | 27037 | 27037 | 0 |
| REGIME | 27037 | 20815 | 6222 |
| CONSISTENCY | 27037 | 3285 | 23752 |
| CONFIDENCE | 27037 | 917 | 26120 |
| RISK | 461 | 0 | 461 |
| LOCK | 27037 | 27037 | 0 |
| FINAL | 27037 | 461 | 26576 |

Top oracle blockers (perfect direction, unchanged gates):

- `26120×` CONFIDENCE: Confidence # is below threshold #
- `6222×` REGIME: Unfavorable market regime: choppy/mean-reverting (ADX # efficiency #
- `3507×` CONSISTENCY: Structure blocks the trade: too little room before the opposing level
- `1918×` CONSISTENCY: Conflicting indicators: only 1/4 rule groups (structure) support PUT; need # — opposing: trend, momentum, candle
- `1857×` CONSISTENCY: Conflicting indicators: only 1/4 rule groups (structure) support CALL; need # — opposing: trend, momentum, candle
- `1584×` CONSISTENCY: Conflicting indicators: only 1/4 rule groups (trend) support CALL; need # — opposing: momentum, candle, structure
- `1427×` CONSISTENCY: Conflicting indicators: only 2/4 rule groups (trend, momentum) support CALL; need # — opposing: candle, structure
- `1419×` CONSISTENCY: Conflicting indicators: only 0/4 rule groups (none) support CALL; need # — opposing: trend, momentum, candle, structure

## 4. Safe gate variants (research only — identical chronological minutes)

| variant | signals | coverage % | decided | wins | losses | hit rate | Wilson 95 % LB | max losing streak |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A — production | 0 | 0.0 | 0 | 0 | 0 | None | None | 0 |
| B — soft technical context | 0 | 0.0 | 0 | 0 | 0 | None | None | 0 |
| C — regime as penalty | 0 | 0.0 | 0 | 0 | 0 | None | None | 0 |
| D @ conf ≥ 60 | 17 | 0.061 | 10 | 7 | 3 | 0.7 | 0.3968 | 4 |
| D @ conf ≥ 65 | 17 | 0.061 | 10 | 7 | 3 | 0.7 | 0.3968 | 4 |
| D @ conf ≥ 70 | 7 | 0.025 | 5 | 3 | 2 | 0.6 | 0.2307 | 3 |
| D @ conf ≥ 75 | 1 | 0.004 | 0 | 0 | 0 | None | None | 1 |
| D @ conf ≥ 80 | 0 | 0.0 | 0 | 0 | 0 | None | None | 0 |
| D @ conf ≥ 85 | 0 | 0.0 | 0 | 0 | 0 | None | None | 0 |
| D @ conf ≥ 90 | 0 | 0.0 | 0 | 0 | 0 | None | None | 0 |
| D @ conf ≥ 95 | 0 | 0.0 | 0 | 0 | 0 | None | None | 0 |

**No variant is adopted on this evidence.** Variant B/C are research constructions; adopting one
would require untouched out-of-sample validation through the experiment suite, exactly like any
other model change. Producing more signals is explicitly *not* a reason to adopt anything.

## 5. Verdict

- gate audit: **COMPLETE** (27971 minutes, 10 gates)
- primary bottleneck: **MODEL (stopped 27925 of 27971 candidates)**
- oracle verdict: **FEASIBLE**
- production resolution: **NO TRADE** on every audited minute — unchanged, and correct.

## 5. Near-miss mining — the minutes that came closest

Mined from all 27971 audited minutes (gate_study.jsonl.gz). No threshold was 
changed and no scan was re-run to produce this table — it is a property of the audit data.

- minutes where **only the model layer** (MODEL/CONFIDENCE) failed: **2023**
- minutes where the oracle (perfect direction, research only) cleared every gate: **461**
- minutes with exactly one non-model gate failing: CONSISTENCY 19165, DATA 4, FEATURE 25, REGIME 297

| # | minute (UTC) | confidence | failing gates | blocking reason |
| --- | --- | --- | --- | --- |
| 1 | 2026-09-23 13:46:00+00:00 | 79.25670000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.55 < 0.58 |
| 2 | 2026-09-16 21:01:00+00:00 | 78.83820000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.50 < 0.58 |
| 3 | 2026-09-10 14:08:00+00:00 | 76.71610000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.50 < 0.58 |
| 4 | 2026-09-14 19:48:00+00:00 | 75.88300000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.46 < 0.58 |
| 5 | 2026-10-01 00:02:00+00:00 | 75.63960000000002 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.52 < 0.58 |
| 6 | 2026-09-11 20:54:00+00:00 | 75.6187 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.48 < 0.58 |
| 7 | 2026-09-21 21:29:00+00:00 | 75.57310000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.52 < 0.58 |
| 8 | 2026-09-17 20:34:00+00:00 | 75.5384 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.50 < 0.58 |
| 9 | 2026-09-29 03:16:00+00:00 | 75.24390000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.49 < 0.58 |
| 10 | 2026-09-10 10:25:00+00:00 | 74.54330000000002 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.49 < 0.58 |
| 11 | 2026-09-10 22:07:00+00:00 | 74.4008 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.49 < 0.58 |
| 12 | 2026-09-23 13:48:00+00:00 | 74.3757 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.51 < 0.58 |
| 13 | 2026-09-14 20:05:00+00:00 | 74.19250000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.49 < 0.58 |
| 14 | 2026-10-01 14:32:00+00:00 | 74.07100000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.48 < 0.58 |
| 15 | 2026-09-06 23:50:00+00:00 | 73.91470000000001 | MODEL, CONFIDENCE, FINAL | Directional probability too low: 0.46 < 0.58 |
