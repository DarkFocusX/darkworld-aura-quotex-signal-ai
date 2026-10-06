# Paper-trading report

- generated: 2026-10-03T14:31:24+00:00
- mode: **PAPER** (no live execution exists in this build)
- collection period: no signals collected yet
- assets in the ledger: —
- data providers feeding the ledger: —

> Confidence is a model-derived score, never a guarantee of profit. Offline validation metrics and live paper results are reported in separate sections and are never combined.

## 1. Live paper results

| metric | value |
| --- | --- |
| signals issued | 0 |
| decided trades | 0 |
| wins | 0 |
| losses | 0 |
| unresolved | 0 |
| pending | 0 |
| win rate | — |
| Wilson 95 % lower bound | — |
| maximum losing streak | 0 |
| scans recorded | 1018 |
| coverage (signals / scans) | 0.00 % |
| no-trade rate (NO_TRADE scans / scans) | 15.23 % |
| scans by status | DATA_UNAVAILABLE 863 · NO_TRADE 155 |
| locked duplicate attempts | 0 |
| distinct days | 0 |
| distinct assets | 0 |

**Evidence bar:** NOT PASSED — rule: ≥ 100 decided signals and a Wilson 95 % lower bound above 0.50.

Shortfall: only 0 decided paper trades (need 100); Wilson 95 % lower bound not above 0.50.

This report does **not** label the system profitable: nothing here meets the published bar.

## 2. Confidence distribution (ledger rows)

| bucket | signals | decided | win rate |
| --- | --- | --- | --- |
| 50–60 % | 0 | 0 | — |
| 60–70 % | 0 | 0 | — |
| 70–75 % | 0 | 0 | — |
| 75–80 % | 0 | 0 | — |
| 80–85 % | 0 | 0 | — |
| 85–90 % | 0 | 0 | — |
| 90–95 % | 0 | 0 | — |
| 95–100 % | 0 | 0 | — |

## 3. Regime distribution (scan evidence)

No signals were issued, so no regime distribution exists yet.

## 4. Data quality and feed state

| asset | freshness | provider | rows | gaps | simulated | replay |
| --- | --- | --- | --- | --- | --- | --- |
| USDBDT-OTCQ | UNAVAILABLE | — | 0 | — | no | no |
| EURCAD-OTCQ | UNAVAILABLE | — | 0 | — | no | no |
| EURUSD | UNAVAILABLE | — | 0 | — | no | no |
| GBPUSD | UNAVAILABLE | — | 0 | — | no | no |
| USDJPY | UNAVAILABLE | — | 0 | — | no | no |
| AUDUSD | UNAVAILABLE | — | 0 | — | no | no |
| USDCHF | UNAVAILABLE | — | 0 | — | no | no |

Provider adapters:

- `broker_ws` — None · Set QUOTEX_WS_URL / QUOTEX_SSID (or run tools/start_live_feed.py) to stream real candles; without a verified stream this adapter stays NOT_CONFIGURED.
- `yahoo` — None · Public delayed feed for the non-OTC FX pairs; commonly rate-limited.
- `csv` — None · Primary REAL-data path: drop backend/data/csv/<ASSET>.csv or POST /api/data/import.
- `simulated` — None · Deterministic demo series. Always flagged is_simulated=true.

## 5. Offline validation (separate from the numbers above)

- active artifact: `v1-market` — READY_MARKET (production usable: True)
- these figures come from the research grid / training report and are **never** mixed into the live paper win rate above

## 6. Known limitations

- paper outcomes require a verified closing candle; anything else stays UNRESOLVED rather than guessed
- a simulated feed can exercise the pipeline but can never back a production signal
- a stale or absent feed produces NO TRADE / DATA UNAVAILABLE — never a fabricated CALL/PUT
- the model is not validated for profitability; no profit claim is made anywhere in this report
