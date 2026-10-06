/**
 * Dashboard smoke harness — executes the single-page dashboard's own JavaScript
 * against canned API payloads, with a minimal DOM stub, so the panel renderers
 * are genuinely exercised without a browser.
 *
 *   node tools/smoke_dashboard.mjs
 *
 * Exits non-zero on the first failed assertion. Covers:
 *   * MODEL QUALITY panel  (READY_MARKET + NOT VALIDATED FOR PROFITABILITY, both
 *     the "no research run" and the "research run present" branches)
 *   * DATA PROVENANCE panel (REPLAY asset + DATA UNAVAILABLE asset with `tried`)
 */
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../backend/static/index.html", import.meta.url), "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/);
if (!script) throw new Error("no <script> block found in index.html");

// ------------------------------------------------------------------ DOM stub
const nodes = new Map();
function makeNode(id) {
  return {
    id,
    innerHTML: "",
    textContent: "",
    title: "",
    className: "",
    value: "",
    checked: false,
    options: [],
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    appendChild() {},
    querySelectorAll: () => [],
    querySelector: () => null,
    setAttribute() {},
    getAttribute: () => null,
    scrollIntoView() {},
  };
}
const document = {
  getElementById: (id) => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  },
  addEventListener() {},
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: () => makeNode("created"),
  body: makeNode("body"),
};

// ------------------------------------------------------------- fetch stub
const payloads = new Map();
globalThis.__payloads = payloads;
const fetchStub = async (path) => {
  const key = String(path).split("?")[0];
  if (!payloads.has(key)) {
    return { ok: false, status: 404, text: async () => JSON.stringify({ detail: "not stubbed: " + key }) };
  }
  return { ok: true, status: 200, text: async () => JSON.stringify(payloads.get(key)) };
};

const context = {
  document,
  fetch: fetchStub,
  window: { addEventListener() {}, location: { origin: "http://test" }, setTimeout, clearTimeout },
  console,
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout,
  clearTimeout,
  Date,
  Math,
  JSON,
  Number,
  String,
  Object,
  Array,
  Promise,
  isNaN,
  parseInt,
  parseFloat,
  Intl,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
};
context.globalThis = context;
vm.createContext(context);

// exposed hook so the harness can call renderers directly
const instrumented = script[1] + "\n;globalThis.__hooks = { loadSystem, loadModelQuality, loadProvenance, loadModelStatus, loadHistory, loadPaper };";
vm.runInContext(instrumented, context);

// ---------------------------------------------------------------- fixtures
payloads.set("/api/model/status", {
  state: "READY_MARKET",
  version: "v1-market",
  data_source_class: "MARKET",
  row_count: 56396,
  asset_list: ["USDJPY", "USDCHF"],
  data_period: { start_utc: "2026-09-04T07:52:00+00:00", end_utc: "2026-10-02T06:48:00+00:00" },
  trained_at_utc: "2026-10-02T07:02:50+00:00",
  validation_metrics: { accuracy: 0.3677, macro_f1: 0.3664, roc_auc_macro: 0.5638, directional_win_rate: 34.52 },
  holdout_metrics: { accuracy: 0.3673, macro_f1: 0.3658, roc_auc_macro: 0.5622, samples: 11280 },
});
payloads.set("/api/signals/performance", {
  signal_performance: { generated: 0, wins: 0, losses: 0, pending: 0, unresolved: 0, decided: 0, win_rate: null },
});
payloads.set("/api/system", {
  app: "Quotex Signal AI", version: "1.0.0", mode: "PAPER", environment: "production",
  paper_trading: true,
  time: { ist_date: "02 Oct 2026", ist: "2026-10-02T18:37:47+05:30", ist_clock: "18:37:47",
          last_closed_candle_ist: "18:36:00", next_candle_ist: "18:38:00",
          entry_time_ist: "18:38:00", expiry_time_ist: "18:39:00", seconds_to_next_candle: 12.0 },
  data: { status: "PARTIAL", available_count: 2, total_count: 7, simulated_count: 0,
          real_market_count: 2, assets: [], providers: [], message: "partial" },
  feed: { status: "UNAVAILABLE", counts: { FRESH: 0, STALE: 0, INVALID: 0, UNAVAILABLE: 7 },
          latest_candle_utc: null, latest_candle_ist: null, age_seconds: null },
  model: { state: "READY_MARKET", message: "trained on operator-supplied real market data", version: "v1-market" },
  telegram: { enabled: false, configured: false, allow_simulated: false },
  scheduler: { running: false },
  research: { experiments: 33, qualified_cells: 0, classification: "PAPER_TRADING_READY" },
  live_feed: { state: "NOT_CONFIGURED", data: "NO_DATA", provider: null,
               latest_candle_utc: null, latest_candle_ist: null, age_seconds: null,
               assets_streaming: [], detail: "no authorised feed endpoint and no realtime candles" },
  readiness: { state: { system: "READY", data: "UNAVAILABLE", live_feed: "NOT_CONFIGURED",
                        model: "READY_MARKET", paper: "ACTIVE",
                        telegram: "NOT_CONFIGURED", evidence: "NOT PASSED",
                        classification: "PAPER_TRADING_READY" } },
});
payloads.set("/api/data/status", {
  assets: [
    { asset: "USDJPY", status: "OK", badge: "REPLAY", source_class: "MARKET", provider: "csv", rows: 28292,
      latest_candle_open_utc: "2026-10-02T06:48:00+00:00", freshness_seconds: 2500, validation: { status: "WARNING", interval_minutes: 1.0, missing_candles: 32 } },
    { asset: "USDBDT-OTCq", status: "DATA_UNAVAILABLE", badge: "DATA UNAVAILABLE", reason: "DATA UNAVAILABLE", tried: [{ provider: "broker_ws", status: "NOT_CONFIGURED" }] },
  ],
  counts: { total: 2, available: 1, unavailable: 1, real_market: 1, simulated: 0 },
  datasets_dir: "/data/csv", data_source_mode: "auto", environment: "production",
});

const assert = (condition, message) => {
  if (!condition) throw new Error("ASSERTION FAILED: " + message);
};

// ------------------------------------------- system status summary (§24/§25)
await context.__hooks.loadSystem();
const statusLine = document.getElementById("status-summary").textContent;
for (const token of ["SYSTEM READY", "DATA UNAVAILABLE", "LIVE FEED NOT_CONFIGURED",
                     "LATEST M1", "AGE", "MODEL READY_MARKET", "PAPER ACTIVE",
                     "TELEGRAM NOT_CONFIGURED", "EVIDENCE NOT PASSED", "CLASSIFICATION PAPER_TRADING_READY"]) {
  assert(statusLine.includes(token), "status summary must show " + token + " (got: " + statusLine + ")");
}
assert(document.getElementById("status-summary").title.includes("never a profitability claim"),
       "the status summary must carry the READY_MARKET disclaimer");
const feedChip = document.getElementById("chip-feed").textContent;
assert(feedChip.includes("UNAVAILABLE"), "feed chip must show the freshness verdict (got: " + feedChip + ")");
assert(document.getElementById("chip-feed").title.includes("120s realtime"),
       "the feed chip must explain the freshness budget");
assert(feedChip.includes("LIVE FEED"), "the feed chip must name the LIVE FEED state (got: " + feedChip + ")");
assert(document.getElementById("chip-feed").title.includes("NOT_CONFIGURED / CONNECTED / DISCONNECTED / FRESH"),
       "the feed chip must explain the LIVE FEED vocabulary");

// ------------------------------------------- LIVE FEED: streaming (spec §16)
{
  const system = payloads.get("/api/system");
  system.live_feed = { state: "FRESH", data: "FRESH", provider: "operator_bridge",
                       latest_candle_utc: "2026-10-02T13:13:00+00:00", latest_candle_ist: "18:43:00",
                       age_seconds: 42.0, assets_streaming: ["USDJPY", "USDCHF"],
                       detail: "newest realtime candle closed 42s ago (budget 120s)" };
  system.readiness.state.live_feed = "FRESH";
  system.feed = { status: "FRESH", counts: { FRESH: 2, STALE: 0, INVALID: 0, UNAVAILABLE: 5 },
                  latest_candle_utc: "2026-10-02T13:13:00+00:00", latest_candle_ist: "2026-10-02T18:43:00+05:30",
                  age_seconds: 42.0 };
  await context.__hooks.loadSystem();
  const line = document.getElementById("status-summary").textContent;
  const chip = document.getElementById("chip-feed").textContent;
  assert(line.includes("LIVE FEED FRESH"), "the summary must show LIVE FEED FRESH (got: " + line + ")");
  assert(line.includes("LATEST M1 18:43:00 IST"), "the summary must show the latest M1 in IST (got: " + line + ")");
  assert(line.includes("AGE 42s"), "the summary must show the age in seconds (got: " + line + ")");
  assert(chip.includes("FRESH") && chip.includes("LIVE FEED FRESH"),
         "the chip must turn green-ish and name FRESH (got: " + chip + ")");
}

// ------------------------------------------- MODEL QUALITY: no research run
payloads.set("/api/research/latest", { available: false, reason: "no research run has been executed yet" });
await context.__hooks.loadModelQuality();
let quality = document.getElementById("model-quality").innerHTML;
const chip = document.getElementById("mq-status-chip").textContent;
assert(quality.includes("MODEL STATUS: READY_MARKET"), "model state must be shown");
assert(quality.includes("NOT VALIDATED FOR PROFITABILITY"), "profitability verdict must be present");
assert(quality.includes("v1-market"), "model version must be shown");
assert(quality.includes("56,396") && quality.includes("USDJPY, USDCHF"), "training rows + assets must be shown");
assert(quality.includes("0.3673"), "holdout metrics must be shown");
assert(quality.includes("no research run yet"), "missing research run must be stated, not hidden");
assert(chip.startsWith("READY_MARKET"), "status chip must carry the state");
console.log("MODEL QUALITY (no run)      ✓ " + chip);

// ----------------------------------- MODEL QUALITY: research run with cells
payloads.set("/api/research/latest", {
  available: true,
  run_id: "lab-TEST",
  dataset: { dataset_version: "ds-test", rows: 56577 },
  experiments_with_evidence: [{
    experiment_id: "M1_EXPIRY_OUTCOME-h1-base-lightgbm",
    selected_threshold: 0.85,
    holdout_gate: { signals: 120, hit_rate: 0.58 },
  }],
  experiments_without_evidence: [],
  summary: { experiment_count: 28, cells_with_a_selected_threshold: 1, cells_without_evidence_of_edge: 12,
             paper_signals_total: 120, paper_wins_total: 70, paper_losses_total: 50 },
});
await context.__hooks.loadModelQuality();
quality = document.getElementById("model-quality").innerHTML;
assert(quality.includes("lab-TEST"), "run id must be shown");
assert(quality.includes("M1_EXPIRY_OUTCOME-h1-base-lightgbm"), "evidence cell must be listed");
assert(quality.includes("gate 0.85"), "selected gate must be shown");
console.log("MODEL QUALITY (with run)    ✓ evidence cell rendered");

// ------------------------------------------- MODEL QUALITY: honest no-trade
payloads.set("/api/research/latest", {
  available: true, run_id: "lab-TEST2", dataset: { dataset_version: "ds-test" },
  experiments_with_evidence: [], experiments_without_evidence: ["x", "y"],
  summary: { experiment_count: 28, cells_with_a_selected_threshold: 0, cells_without_evidence_of_edge: 12,
             paper_signals_total: 0, paper_wins_total: 0, paper_losses_total: 0 },
});
await context.__hooks.loadModelQuality();
quality = document.getElementById("model-quality").innerHTML;
assert(quality.includes("No confidence gate cleared the validation evidence bar"),
       "zero-evidence runs must say so explicitly");
console.log("MODEL QUALITY (no evidence) ✓ explicit NO TRADE messaging");

// ------------------------------------------------------ DATA PROVENANCE
await context.__hooks.loadProvenance();
const provenance = document.getElementById("provenance").innerHTML;
assert(provenance.includes("USDJPY") && provenance.includes("REPLAY"), "replay asset must render");
assert(provenance.includes("USDBDT-OTCq") && provenance.includes("DATA UNAVAILABLE"),
       "unavailable asset must render its badge");
assert(provenance.includes("broker_ws"), "tried providers must be listed for unavailable assets");
console.log("DATA PROVENANCE             ✓ replay + unavailable rows render");

// ------------------------------------------------ MODEL QUALITY failure path
payloads.delete("/api/model/status");
await context.__hooks.loadModelQuality();
quality = document.getElementById("model-quality").innerHTML;
assert(quality.includes("Model quality unavailable"), "API failure must degrade gracefully");
console.log("MODEL QUALITY (backend down)✓ graceful error message");

// ------------------------------------------------ LIVE PAPER TRADING (§7/§8)
payloads.set("/api/paper/summary", {
  mode: "PAPER",
  current_ist: "14:42:10",
  data_freshness: "FRESH",
  freshness_counts: { FRESH: 1, STALE: 0, INVALID: 0, UNAVAILABLE: 6 },
  latest_m1: { asset: "USDJPY", provider: "operator_csv",
               open_time_utc: "2026-10-02T09:11:00+00:00", close_time_utc: "2026-10-02T09:12:00+00:00" },
  next_m1: { open_time_ist: "2026-10-02T14:43:00+05:30", seconds_away: 42.5 },
  paper: { signals_today: 0, pending: 0, wins: 0, losses: 0, unresolved: 0 },
  scans: { coverage: 0, no_trade_rate: 1, scans_recorded: 12 },
  live: { assets: [
    { asset: "USDJPY", provider: "operator_csv", freshness_status: "FRESH", age_seconds: 41.2, rows: 320,
      gaps: { missing_minutes: 2 }, is_simulated: false, is_replay: true },
    { asset: "USDBDT-OTCq", provider: null, freshness_status: "UNAVAILABLE", age_seconds: null, rows: 0,
      gaps: null, is_simulated: false, is_replay: false },
  ] },
});
payloads.set("/api/paper/card", { kind: "NO TRADE", reason: "Confidence 61.20% is below threshold 85.0%",
                                  blocked_by: ["CONFIDENCE ≥ THRESHOLD"], asset: "USDJPY", status: "NO_TRADE" });
payloads.set("/api/research/paper-evidence", {
  total_decided_trades: 0, wins: 0, losses: 0, win_rate: null, wilson_95_lower_bound: null,
  max_losing_streak: 0, days: 0, assets: [], evidence_bar: "NOT PASSED",
  evidence_rule: { minimum_qualifying_signals: 100, wilson_95_lower_bound_above: 0.5 },
});
payloads.set("/api/paper/ledger", { rows: [] });
await context.__hooks.loadPaper();
const paperCard = document.getElementById("paper-card").innerHTML;
const kpis = document.getElementById("paper-kpis").innerHTML;
const feeds = document.getElementById("paper-feeds").innerHTML;
const evidence = document.getElementById("paper-evidence").innerHTML;
const ledgerBody = document.getElementById("paper-ledger-body").innerHTML;
assert(paperCard.includes("NO TRADE") && paperCard.includes("below threshold 85.0%"),
       "the paper card must show NO TRADE with the real reason");
assert(paperCard.includes("blocked by"), "blocking requirements must be surfaced");
assert(kpis.includes("no-trade rate") && kpis.includes("coverage"), "coverage + no-trade rate KPIs required");
assert(kpis.includes("paper signals today"), "paper signal counter required");
assert(feeds.includes("FRESH") && feeds.includes("UNAVAILABLE"), "both feed states must render");
assert(feeds.includes("REPLAY") && feeds.includes("2"), "flags and gap counts must render");
assert(evidence.includes("EVIDENCE BAR") && evidence.includes("NOT PASSED"),
       "evidence bar state must be explicit");
assert(ledgerBody.includes("No qualified paper signal"), "an empty ledger must say so, never fake a row");
console.log("LIVE PAPER TRADING          ✓ card, KPIs, feeds, evidence, ledger");

// a qualified signal must render the §8 card with every required field
payloads.set("/api/paper/card", { kind: "PAPER SIGNAL", asset: "USDJPY", time_ist: "09:12:00",
  direction: "CALL", confidence: 88.4, model_agreement: 0.72, expiry_minutes: 1, data: "REAL",
  provider: "operator_csv", model: "v1-market", status: "PENDING", result: "PENDING" });
await context.__hooks.loadPaper();
const signalCard = document.getElementById("paper-card").innerHTML;
for (const field of ["PAPER SIGNAL", "USDJPY", "09:12:00 IST", "CALL", "88.4%", "1 MIN", "REAL",
                     "operator_csv", "v1-market"]) {
  assert(signalCard.includes(field), "signal card must include " + field);
}
assert(!signalCard.includes("95% guaranteed") && !signalCard.toLowerCase().includes("sure shot"),
       "no guarantee language is allowed anywhere");
console.log("REAL-TIME SIGNAL CARD       ✓ all §8 fields present, no guarantee language");

// backend down -> the panel must say so instead of inventing numbers
payloads.delete("/api/paper/summary");
await context.__hooks.loadPaper();
assert(document.getElementById("paper-card").innerHTML.includes("could not be loaded"),
       "paper panel must degrade gracefully when the backend is down");
assert(document.getElementById("paper-top-chip").textContent.includes("unavailable"),
       "the chip must report the outage");
console.log("LIVE PAPER (backend down)   ✓ graceful error message");

console.log("\nDashboard smoke harness: all assertions passed.");
