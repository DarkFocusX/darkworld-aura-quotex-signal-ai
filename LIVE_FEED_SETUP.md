# LIVE FEED SETUP — connecting a continuous real M1 feed

This project ships complete and honest with **no feed**. Every panel works, the model is
loaded, the scanner runs — and the answer is **DATA UNAVAILABLE** until a real, continuously
updating M1 source is connected. This document is the operator's checklist for connecting one.

> **Read this first.** A feed only solves the *data availability* problem. It does **not** make
> the strategy profitable. The evidence bar (≥ 100 decided paper trades **and** Wilson 95 %
> lower bound > 0.50) stays exactly as it is, and NO TRADE stays the correct answer until that
> bar is cleared with real results.

Nothing in this project invents a feed, guesses credentials, scrapes a source or presents a
public FX feed as Quotex OTC data. You supply an endpoint you are authorised to use.

If you only need the **standard FX pairs**, skip to *Twelve Data — realtime standard FX* below:
that path needs nothing but an API key. Everything after STEP 5 (paper collection, GENERATE
SIGNAL, the meaning of the answers) applies to both paths unchanged.

---

## Twelve Data — realtime standard FX (the shortest path)

Twelve Data publishes a public realtime WebSocket and this project already speaks it:

    wss://ws.twelvedata.com/v1/quotes/price?apikey=YOUR_API_KEY

Nothing else about the architecture changes. The same parser, the same tick validation, the
same `ContinuousFeed` M1 builder, the same rolling store, the same `SignalEngine`, the same
AUTO SELECT, the same confidence threshold. There is no second engine and no second builder.

### 1. Put your key in `.env` (the only required line)

```ini
TWELVE_DATA_API_KEY=your-twelve-data-api-key
```

Optional — both already default to the values below:

```ini
TWELVE_DATA_WS_URL=wss://ws.twelvedata.com/v1/quotes/price
TWELVE_DATA_SYMBOLS=EUR/USD,GBP/USD,USD/JPY,USD/CHF,AUD/USD
```

### 2. Verify, then start the bridge

```powershell
python3 tools\probe_live_feed.py --provider twelve_data              # ~30 s listen
python3 tools\probe_live_feed.py --provider twelve_data --self-test  # no key: labelled fixtures
python3 tools\start_twelve_data_feed.py --show-config                # resolved setup, key redacted
python3 tools\start_twelve_data_feed.py                              # connect → subscribe → build M1
python3 tools\start_twelve_data_feed.py --target store               # write straight into SQLite
python3 tools\start_twelve_data_feed.py --dry-run                    # connect + log, deliver nothing
python3 tools\start_twelve_data_feed.py --assets EUR/USD,USD/JPY     # a subset
```

The probe prints the same verdicts as STEP 3 (`NOT_CONFIGURED`, `AUTH_FAILED`, `NO_DATA`,
`INVALID_DATA`, `CONNECTED`) and is read-only. `--show-config` prints the setup with the key
reduced to `api_key_set: true/false`.

### 3. Verify FRESH — exactly as in STEP 5

```powershell
curl http://127.0.0.1:8000/api/data/live-status
```

Per asset you are looking for `"provider": "twelve_data"`, `"freshness_status": "FRESH"`,
`"realtime": true`, `"is_simulated": false`, `"is_replay": false`, `"budget_seconds": 120`.

### What is Twelve-Data-specific

| | |
| --- | --- |
| Authentication | the key rides in the **query string** (`apikey=…`) — no header session is ever sent |
| Subscription | one frame for the whole set: `{"action":"subscribe","params":{"symbols":"EUR/USD,GBP/USD,USD/JPY,USD/CHF,AUD/USD"}}` |
| Instrument map | explicit and final: `EUR/USD→EURUSD`, `GBP/USD→GBPUSD`, `USD/JPY→USDJPY`, `USD/CHF→USDCHF`, `AUD/USD→AUDUSD`. An instrument that is not in it is reported and **not** subscribed — nothing is guessed |
| Control frames | `heartbeat`, `subscribe-status`/`unsubscribe-status` and `error` are classified: an ack/heartbeat is never data, and an `error` with code 401/403 is `AUTH_FAILED` (a rate limit is not a dead session) |
| Provenance | every stored row carries `provider=twelve_data`, `source_class=MARKET`, `realtime=true`, `is_simulated=false`, `is_replay=false` |
| FRESH | a **validated closed M1 candle** inside the existing **120 s** realtime budget. A socket, a heartbeat or a subscribe acknowledgement is *not* data |
| Delivery | `--target api` (default) POSTs each closed M1 candle to `POST /api/data/ingest`; `--target store` writes it straight into the rolling store |
| Rejected input | malformed / incomplete / invalid-geometry / non-finite / non-positive / future values and duplicates are counted and dropped, never repaired; an unfinished candle is never persisted |
| The pipeline | unchanged: one parser, one validator, one M1 builder, one store, one engine, one AUTO SELECT, one confidence threshold, one signal lock |

### OTC is not available — by design

Twelve Data publishes standard FX and carries **no** `USDBDT-OTCq` and **no** `EURCAD-OTCq`:
those are broker-internal instruments with no public source. This project will not map a normal
pair onto an OTC symbol, will not substitute one for the other and will never simulate OTC data.
Both OTC pairs keep reporting `DATA UNAVAILABLE`; the adapter refuses to subscribe to them and
refuses to serve them even if a mislabelled row existed in the store.

### Security

The key is read from `TWELVE_DATA_API_KEY` only. It is never hard-coded, never committed, never
written to SQLite or the config table, never returned by the API, never printed and never placed
in an exception — the shared redactor masks `apikey=`, every query string and the bare value,
and the provider exposes only `api_key_set: true/false`. Rotating the key means editing `.env`
and restarting **both** processes (the API process needs it too for `--target api`, because it
validates a `twelve_data` claim against its own configuration).

---

## STEP 1 — What you must obtain from your authorised data source

From whoever provides your feed (your broker's data service, your licensed vendor, or your own
collector), you need:

| item | what it is | example (illustrative only) |
| --- | --- | --- |
| **Endpoint URL** | the WebSocket address the feed streams from | `wss://your-feed-host.example.com/stream` |
| **Session value** | session id / token, *if* the feed requires one | `1a2b3c4d…` |
| **Auth style** | is the session sent in a header, or inside a subscribe message? | header `Authorization: Bearer <session>` |
| **Subscribe message** | how to ask for an instrument (only if the feed needs one) | `{"action":"subscribe","asset":"{asset}"}` |
| **Instrument names** | what the feed calls your pairs | `USDJPY`, `XAU/USD`, `US30` |

Also confirm that your licence covers the instruments you want to run. **OTC pairs**
(`USDBDT-OTCq`, `EURCAD-OTCq`) exist only inside a broker: if your feed does not carry them,
they stay DATA UNAVAILABLE. Never map a normal FX pair onto an OTC symbol to make it look
available.

The bridge understands ticks and closed candles delivered as JSON:

* a **tick**: `{"symbol":"USDJPY","price":149.123,"ts":"2026-10-02T12:00:05Z"}`
* a **candle**: `{"symbol":"USDJPY","open":149.100,"high":149.140,"low":149.080,"close":149.120,"ts":"2026-10-02T12:01:00Z"}`
  (a candle must carry open **and** high **and** low **and** close — a partial bar is rejected,
  never completed with a guess)

Field names are tolerant (`price`/`tick`/`p`/`last`, `symbol`/`asset`/`pair`, `ts`/`time`/`timestamp`,
open/high/low/close with `o/h/l/c` shortcuts). Anything the parser cannot verify is counted and
reported, never stored.

## STEP 2 — Where to put it in `.env`

In the project folder:

```powershell
copy .env.example .env
notepad .env
```

Fill in the two required lines (nothing else is mandatory):

```ini
QUOTEX_WS_URL=wss://your-feed-host.example.com/stream
QUOTEX_SSID=your-session-value
```

If your feed authenticates differently, add only what applies:

```ini
# session in a header other than Authorization / with a prefix
LIVE_FEED_AUTH_HEADER=Authorization
LIVE_FEED_AUTH_SCHEME=Bearer

# or send the session inside the subscribe message instead
LIVE_FEED_AUTH_IN_MESSAGE=true
LIVE_FEED_SUBSCRIBE_TEMPLATE={"action":"subscribe","asset":"{asset}","ssid":"{ssid}"}

# if your feed names instruments differently from this project
LIVE_FEED_SYMBOL_MAP=XAU/USD=XAUUSD,US30=US30

# which assets to bridge (default: the enabled assets, OTC excluded)
LIVE_FEED_ASSETS=USDJPY,USDCHF

# delivery + logging (defaults shown — set them only if you need to)
LIVE_FEED_TARGET=api                 # api (POST /api/data/ingest) | store (local SQLite)
LIVE_FEED_API_URL=http://127.0.0.1:8000
LIVE_FEED_PROVIDER_LABEL=operator_bridge   # provenance written on every stored row
LIVE_FEED_CONNECT_TIMEOUT=10         # socket connect timeout, seconds
LIVE_FEED_RECV_TIMEOUT=5             # socket read timeout, seconds
LIVE_FEED_LOG_SECONDS=30             # heartbeat log interval
LIVE_FEED_MAX_SECONDS=0              # stop after N seconds (0 = run until Ctrl+C)
LIVE_FEED_RETENTION_MINUTES=10080    # rolling-store retention for live rows (7 days)
```

Secrets live in the environment only. They are never printed, never returned by an API and
never committed — every log line and error message is passed through a redactor.

## STEP 3 — How to test the connection (before starting the bridge)

```powershell
python3 tools\probe_live_feed.py                 # ~30 seconds of listening
python3 tools\probe_live_feed.py --seconds 90    # listen longer
python3 tools\probe_live_feed.py --asset USDJPY  # watch one instrument
python3 tools\probe_live_feed.py --self-test     # no credentials: plumbing check on fixtures
```

The probe prints one verdict, and never hides a problem:

| verdict | meaning | what to do |
| --- | --- | --- |
| `NOT_CONFIGURED` | `QUOTEX_WS_URL` is empty | do STEP 2 |
| `AUTH_FAILED` | the feed rejected the session | check `QUOTEX_SSID` / auth style (STEP 2) |
| `INVALID_DATA` | messages arrived but no usable price / incomplete OHLC | check `LIVE_FEED_SYMBOL_MAP`, ask your provider for the schema |
| `NO_DATA` | connected, but nothing arrived in the window | check the subscription template / instrument names |
| `CONNECTED` | real messages arrived, validated, and M1 candles were built | continue to STEP 4 |

The probe is read-only: it never writes a candle into the store.

## STEP 4 — How to start the feed bridge

Keep the API running in one window, the bridge in another:

```powershell
# window 1 — the API + dashboard
cd C:\path\to\quotex-signal-ai
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

# window 2 — the bridge
cd C:\path\to\quotex-signal-ai
python3 tools\start_live_feed.py
```

Useful variants:

```powershell
python3 tools\start_live_feed.py --dry-run        # connect + log, deliver nothing
python3 tools\start_live_feed.py --target store   # write straight into the local store
python3 tools\start_live_feed.py --assets USDJPY --seconds 900
```

The bridge prints one line per closed candle and a heartbeat every 30 seconds:

```
[13:41:00Z] INFO    connection established (wss://your-feed-host.example.com/stream)
[13:41:00Z] INFO    M1 candle closed USDJPY 2026-10-02T13:40:00+00:00 o=149.101 h=149.139 l=149.098 c=149.127
[13:41:30Z] INFO    health state=CONNECTED messages=312 ticks=312 candles=1 delivered=1 rejected=0 unknown=0 reconnects=0 idle=2s rows={'USDJPY': 1}
```

Stop it with `Ctrl+C` — it closes the socket and prints a summary.

## STEP 5 — How to verify FRESH status

```powershell
curl http://127.0.0.1:8000/api/data/live-status
curl http://127.0.0.1:8000/api/health
```

You are looking for, per asset:

```json
{ "asset": "USDJPY", "provider": "operator_bridge", "freshness_status": "FRESH",
  "age_seconds": 43.2, "rows": 6, "resolution_minutes": 1.0, "realtime": true,
  "is_simulated": false, "is_replay": false, "gaps": [] }
```

`FRESH` requires the newest verified candle to have **closed** inside the published budget:
**120 seconds** for a realtime feed (180 s for a replay snapshot). In the dashboard header the
same facts appear as `LIVE FEED FRESH · LATEST M1 19:13:00 IST · AGE 43s`.

The possible states are honest by construction:

| state | meaning |
| --- | --- |
| `NOT_CONFIGURED` | no endpoint and no realtime candle — nothing has ever arrived |
| `CONNECTED` | candles are being written but none has closed yet, or the last one is outside the budget |
| `FRESH` | a realtime candle closed inside the last 120 s — the state you want |
| `DISCONNECTED` | candles were flowing and then stopped for more than 5 minutes |

A replay snapshot (`REPLAY`) and a simulated feed (`SIMULATED`) always keep their own label and
can never be displayed as LIVE.

## STEP 6 — How to start paper collection

Paper collection is on by default in this build (`PAPER_COLLECTION=true`), and the automatic
scanner evaluates **every newly closed M1 candle** with the same engine the GENERATE SIGNAL
button uses:

```powershell
# confirm the scanner is running
curl http://127.0.0.1:8000/api/paper/scanner

# start/stop it explicitly if needed
curl -X POST http://127.0.0.1:8000/api/paper/scanner -H "Content-Type: application/json" -d "{\"enabled\":true}"
```

Every scan — qualified *and* rejected — is recorded, so the evidence panel can show coverage,
the NO-TRADE rate and the Wilson bound without anyone choosing what to count.

```powershell
curl http://127.0.0.1:8000/api/research/paper-evidence
curl -X POST http://127.0.0.1:8000/api/research/paper-report   # rebuild PAPER_TRADING_REPORT.md
```

## STEP 7 — How to press GENERATE SIGNAL

Open **http://localhost:8000/preview** (or the Next dashboard on port 3000), leave the asset on
`AUTO`, confirm `M1`, and press **GENERATE SIGNAL**. The button calls the same backend pipeline:
fetch → freshness → features → ensemble → confidence → gates → one signal or NO TRADE. There is
no second algorithm and no client-side decision.

## STEP 8 — What the answers mean

| answer | meaning | what to do |
| --- | --- | --- |
| `DATA UNAVAILABLE` | no verified, fresh, real candle for that asset right now | steps 3–5: check the bridge and `live-status` |
| `NO TRADE` | the feed was fine; the setup did **not** clear every gate — the card shows confidence, the exact blocking check, the best candidate and the next scan | nothing. This is the honest and expected answer most of the time, and always when the evidence bar has not been cleared |
| `PAPER SIGNAL` | **every** gate passed: real + fresh + READY_MARKET + valid features + model + anomaly + regime + consistency + confidence + risk + lock. It is recorded as PENDING and settles to WIN / LOSS / UNRESOLVED from the verified closing candle | nothing to press — paper mode only, no order ever reaches a broker |
| `LOCKED` | that candle was already evaluated — a candle is never scanned twice | wait for the next candle |
| `MODEL_NOT_READY` | no READY_MARKET artifact is loaded | `python3 tools/activate_model.py v1-market` |

Confidence is a **calculated model score**, never a probability of profit and never an accuracy
claim. A `PAPER SIGNAL` means "the pipeline qualified this setup", not "this trade will win".

---

## Troubleshooting

| symptom | cause | fix |
| --- | --- | --- |
| `LIVE FEED NOT_CONFIGURED` | `.env` has no `QUOTEX_WS_URL` | STEP 2, then re-run |
| `TWELVE DATA NOT CONFIGURED` | `.env` has no `TWELVE_DATA_API_KEY` | add the key, then re-run |
| Twelve Data: `AUTH_FAILED` | the key is wrong, expired or out of credits | check `TWELVE_DATA_API_KEY` in both processes |
| Twelve Data: `NO_DATA` | nothing arrived in the window (market closed, or nothing was subscribed) | start it during market hours; check `TWELVE_DATA_SYMBOLS` |
| Twelve Data: `INVALID_DATA` | messages arrived but carried no usable price | ask the probe with `--json` for the per-frame counters |
| Twelve Data: header shows `CONNECTED` / `NO_DATA` | the key is set but no closed candle has landed yet | wait for the next minute boundary, then check `live-status` |
| probe: `AUTH_FAILED` | wrong session value or wrong auth style | STEP 2 (`LIVE_FEED_AUTH_*`) |
| probe: `NO_DATA` | subscribed to nothing / instrument names differ | `LIVE_FEED_SUBSCRIBE_TEMPLATE`, `LIVE_FEED_ASSETS`, `LIVE_FEED_SYMBOL_MAP` |
| dashboard: `LIVE FEED CONNECTED` but data `STALE` | the bridge stopped, or the upstream market is closed | restart the bridge; check the heartbeat lines |
| OTC pairs still DATA UNAVAILABLE | your feed does not carry those instruments | nothing to fix — this project will not fabricate them |
| `pip` error about `websocket-client` | the WebSocket client is missing | `python3 -m pip install -r backend\requirements.txt` |
