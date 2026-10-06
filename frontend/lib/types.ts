/** Shared API types for the Quotex Signal AI dashboard. */

export type Direction = "CALL" | "PUT";
export type Status =
  | "SIGNAL"
  | "NO_TRADE"
  | "DATA_UNAVAILABLE"
  | "MODEL_NOT_READY"
  | "LOCKED"
  | "ERROR"
  | "STUDY_ONLY"
  | "PROBING";

export interface Feed {
  provider?: string;
  source_label?: string;
  mode?: string;
  badge?: string;
  realtime?: boolean;
  simulated?: boolean;
  delayed_seconds?: number | null;
  candles?: number;
  has_volume?: boolean;
  latest_candle_open_utc?: string | null;
  latest_candle_close_utc?: string | null;
  latest_candle_ist?: string | null;
  lag_seconds?: number | null;
  max_lag_seconds?: number | null;
  notes?: string;
  fallback_chain?: { provider: string; status: string; reason?: string }[];
}

export interface CheckResult {
  check: string;
  passed: boolean;
  severity: string;
  detail: string;
  evidence?: Record<string, unknown>;
}

export interface ChecksBundle {
  checks: CheckResult[];
  passed: boolean;
  blocking: string[];
  reasons: string[];
  risk?: { allowed: boolean; reasons: string[]; meta?: Record<string, unknown> };
  execution?: { status: string; adapter: string; detail: string };
  telegram?: { status: string; detail: string };
}

export interface ConfidenceComponent {
  score: number;
  weight: number;
  contribution: number;
  aligned: boolean;
  evidence: number;
  detail: Record<string, any>;
}

export interface Components {
  confidence: number;
  quality: string;
  quality_label: string;
  tradable: boolean;
  components: Record<string, ConfidenceComponent>;
  weights: Record<string, number>;
  notes: string[];
}

export interface ModelVote {
  model: string;
  p_put: number;
  p_neutral: number;
  p_call: number;
  weight: number;
  vote: string;
  status: string;
  detail?: string | null;
}

export interface Ensemble {
  p_put: number;
  p_neutral: number;
  p_call: number;
  direction: string | null;
  direction_probability: number;
  directional_probability_normalised: number;
  agreement: number;
  spread: number;
  agree_models?: string[];
  agreeing_models: string[];
  no_trade_votes: number;
  anomaly_flag: boolean;
  anomaly_detail?: string | null;
  temperature?: number;
  votes: ModelVote[];
  provenance?: Record<string, any>;
}

export interface Signal {
  signal_uid: string;
  asset: string;
  flag?: string;
  label?: string;
  timeframe: string;
  direction: Direction;
  confidence: number;
  quality: string;
  quality_label: string;
  entry_time_utc?: string;
  entry_time_ist: string;
  expiry_minutes: number;
  expiry_time_ist: string;
  data_timestamp_utc?: string;
  data_timestamp_ist?: string;
  price?: number | null;
  model_probability?: number;
  model_agreement?: number;
  anomaly_flag?: boolean;
  feed_source?: string;
  feed_mode?: string;
  feed_provider?: string;
  feed_badge?: string;
  // provenance (spec §9): emitted by the engine on every built signal
  model_version?: string | null;
  feature_version?: string | null;
  dataset_version?: string | null;
  feed_status?: string | null;
  source_class?: string;
  source_status?: string;
  is_simulated?: boolean;
  is_replay?: boolean;
  is_real_market_data?: boolean;
  environment?: string;
  realtime?: boolean;
  components?: Components | null;
  model_votes?: Ensemble | null;
  status?: string;
  result?: string;
  scan_mode?: string;
  telegram_status?: string;
  execution?: { status: string; adapter: string; detail: string };
  telegram?: { status: string; detail: string };
  created_at_utc?: string;
}

export interface FeaturesPayload {
  price: number;
  features: Record<string, number>;
  extras: {
    candle_time_ist?: string;
    atr14?: number;
    atr_pct?: number;
    ema9?: number;
    ema21?: number;
    ema50?: number;
    ema_stack?: string;
    rsi7?: number;
    rsi14?: number;
    adx14?: number;
    plus_di?: number;
    minus_di?: number;
    macd_hist?: number;
    efficiency_ratio_10?: number;
    efficiency_ratio_20?: number;
    volume_z?: number;
    has_volume?: boolean;
    median_atr_pct_200?: number;
    consecutive_run?: number;
    support_resistance?: Record<string, number>;
    last_candle?: Record<string, any>;
    prev_candle?: Record<string, any>;
  };
  missing: string[];
}

export interface Session {
  ist_time: string;
  ist_date: string;
  utc_time: string;
  timezone: string;
  current_candle_ist: string;
  next_candle_ist: string;
  seconds_to_next_candle: number;
}

export interface GenerateResponse {
  status: Status;
  scan_mode?: string;
  requested_asset?: string;
  assets_scanned?: string[];
  asset?: string | null;
  timeframe?: string;
  timezone?: string;
  direction?: Direction | null;
  entry_time?: string | null;
  entry_time_ist?: string | null;
  expiry_time_ist?: string | null;
  expiry_minutes?: number;
  confidence?: number | null;
  quality?: string | null;
  quality_label?: string | null;
  data_timestamp?: string | null;
  data_timestamp_ist?: string | null;
  model_agreement?: number | null;
  signal?: Signal | null;
  reason?: string | null;
  reason_detail?: string | null;
  next_valid_scan_ist?: string | null;
  min_confidence?: number;
  tradable_min_quality?: number;
  session?: Session;
  feed?: Feed | null;
  features?: FeaturesPayload | null;
  components?: Components | null;
  checks?: ChecksBundle | null;
  ensemble?: Ensemble | null;
  model_status?: ModelStatus;
  environment?: string;
  production_policy?: { require_real_data: boolean; require_market_model: boolean; min_model_agreement: number };
  scan_summary?: {
    scanned: number;
    qualified: number;
    rejected: number;
    data_unavailable: number;
    locked: number;
    model_not_ready: number;
    assets: { scanned: string[]; qualified: string[]; rejected: string[]; unavailable: string[] };
    best_candidate: { asset: string; confidence?: number | null; quality?: string | null; direction?: string | null } | null;
    note?: string;
  } | null;
  eligibility?: {
    allowed: boolean;
    decision: string;
    environment: string;
    label: string;
    reason: string | null;
    blocked_by: string[];
    requirements: {
      requirement: string;
      passed: boolean;
      status: string;
      downgraded: boolean;
      detail: string;
      evidence: Record<string, any>;
    }[];
  } | null;
  data_validation?: Record<string, any> | null;
  rejections?: { asset: string; status: string; reason: string; detail?: string | null; confidence?: number | null; blocked_by?: string[] }[];
  all_scans?: Record<string, any>[];
  generated_at_ist?: string;
  generated_at_utc?: string;
  disclaimer?: string;
  // daily mode
  day_ist?: string;
  emitted_today?: { count: number; best: any | null };
  study?: {
    best: any | null;
    asset: string | null;
    candidates: any[];
    scanned_assets: string[];
    in_sample?: boolean | null;
    in_sample_note?: string | null;
    note: string;
  };
}

export interface ModelStatus {
  state: string;
  message: string;
  ready: boolean;
  version: string;
  model_version?: string;
  data_source?: string;
  data_source_class?: string;
  row_count?: number;
  asset_list?: string[];
  data_period?: { start_utc?: string; end_utc?: string };
  feature_version?: string;
  validation_metrics?: Record<string, number>;
  holdout_metrics?: Record<string, number>;
  production_usable?: boolean;
  trained_at_utc?: string | null;
  loaded_at_utc?: string | null;
  training_rows?: number;
  horizon_minutes?: number;
  temperature?: number;
  data_provenance?: {
    source?: string;
    is_simulated?: boolean;
    notes?: string;
    rows?: number;
    period_start_utc?: string;
    period_end_utc?: string;
    assets?: { asset: string; provider: string; is_simulated: boolean; rows: number }[];
  };
  models: { model: string; status: string; weight: number; detail?: string }[];
  unavailable: { model: string; status: string; detail: string }[];
  anomaly_detector?: { available: boolean; reason: string };
  neural_challenger?: { available: boolean; reason: string; architecture?: string | null };
  last_error?: string | null;
}

export interface HistoryRow {
  signal_uid: string;
  time_ist: string;
  entry_time_ist: string;
  asset: string;
  direction: Direction;
  confidence: number;
  quality: string;
  result: "PENDING" | "WIN" | "LOSS" | "VOID";
  status: string;
  result_reason?: string | null;
  is_simulated?: boolean;
  feed_source?: string;
  expiry_minutes?: number;
  telegram_status?: string;
}

export interface PerformancePayload {
  signal_performance: {
    generated: number;
    wins: number;
    losses: number;
    voids: number;
    pending: number;
    decided: number;
    unresolved: number;
    win_rate: number | null;
    max_losing_streak: number;
    note: string;
  };
  model_validation: {
    available: boolean;
    version: string;
    trained_at_utc?: string | null;
    data_provenance?: Record<string, any>;
    out_of_fold?: Record<string, any> | null;
    holdout?: Record<string, any> | null;
    note: string;
  };
  separation_note: string;
}

export interface ScanRow {
  id: number;
  scanned_at_ist: string;
  asset?: string | null;
  status: string;
  reason?: string | null;
  confidence?: number | null;
  feed_source?: string | null;
  is_simulated?: number | boolean;
  scan_mode?: string | null;
}

export interface SystemStatus {
  app: string;
  version: string;
  time: {
    ist_clock: string;
    ist_date: string;
    ist: string;
    next_candle_ist: string;
    seconds_to_next_candle: number;
    current_candle_ist?: string;
    last_closed_candle_ist?: string;
    entry_time_ist?: string;
    expiry_time_ist?: string;
    expiry_minutes?: number;
    countdown_seconds?: number;
  };
  session: Session;
  mode: string;
  paper_trading: boolean;
  data: {
    status: string;
    message: string;
    data_source_mode: string;
    available_count: number;
    total_count: number;
    simulated_count: number;
    real_market_count?: number;
    environment?: string;
    providers: Record<string, any>[];
    assets: {
      asset: string;
      available: boolean;
      provider?: string;
      source_label?: string;
      badge?: string;
      display_status?: string;
      source_class?: string;
      source_status?: string;
      simulated?: boolean;
      is_replay?: boolean;
      is_real_market_data?: boolean;
      last_update_utc?: string | null;
      lag_seconds?: number | null;
      max_lag_seconds?: number | null;
      reason?: string;
      candles?: number;
    }[];
  };
  model: ModelStatus;
  scheduler: { running: boolean; runs: number; last_run_utc?: string | null; last_scan_status?: string | null };
  telegram: { enabled: boolean; configured: boolean; allow_simulated: boolean };
  disclaimer: string;
}

export interface ConfigPayload {
  settings: {
    min_confidence: number;
    min_model_agreement?: number;
    environment?: string;
    require_real_data?: boolean;
    require_market_model?: boolean;
    expiry_minutes: number;
    timeframe: string;
    timezone: string;
    mode: string;
    auto_select: boolean;
    daily_mode: boolean;
    selected_asset: string;
    enabled_assets: string[];
    data_source_mode: string;
    max_signals_per_day: number;
    min_entry_seconds_remaining: number;
    telegram_enabled: boolean;
    telegram_allow_simulated: boolean;
    scheduler_enabled: boolean;
    outcome_evaluation_enabled: boolean;
  };
  telegram: { enabled: boolean; configured: boolean; token_set: boolean; chat_id_set: boolean; allow_simulated: boolean };
  limits: { min_confidence_range: number[]; expiry_minutes_range: number[]; modes: string[]; data_source_modes: string[] };
  execution_modes: { mode: string; available: boolean; detail: string }[];
  scheduler: { running: boolean };
  disclaimer: string;
}

export interface AssetInfo {
  symbol: string;
  label: string;
  flag: string;
  kind: string;
  enabled: boolean;
  notes?: string;
  has_public_feed: boolean;
}

export interface Candle {
  time_utc: string;
  time_ist: string;
  epoch: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface MarketPayload {
  status: string;
  asset: string;
  feed?: Feed;
  live_price?: number | null;
  candles: Candle[];
  features?: FeaturesPayload | null;
  feature_error?: { reason: string; detail: string };
  reason?: string;
  provider?: string;
  detail?: string;
}

// ---------------------------------------------------------------- data status
export interface DatasetValidation {
  ok: boolean;
  status: string;
  rows: number;
  first_candle_utc?: string | null;
  last_candle_utc?: string | null;
  span_hours?: number | null;
  duplicates: number;
  non_monotonic: number;
  missing_candles: number;
  missing_ratio: number;
  session_breaks: number;
  longest_gap_minutes: number;
  interval_minutes?: number | null;
  invalid_ohlc: number;
  non_finite: number;
  negative_prices: number;
  has_volume: boolean;
  stale_seconds?: number | null;
  flat_candles?: number;
  flat_ratio?: number;
  errors: string[];
  warnings: string[];
  min_required_candles: number;
}

export interface DatasetStatus {
  asset: string;
  status: string;
  badge?: string;
  display_status?: string;
  provider?: string;
  source_label?: string;
  source_class?: string;
  source_status?: string;
  timeframe?: string;
  rows?: number;
  candles_available?: number;
  latest_candle_open_utc?: string | null;
  latest_candle_close_utc?: string | null;
  last_update_utc?: string | null;
  freshness_seconds?: number | null;
  is_realtime?: boolean;
  is_replay?: boolean;
  is_simulated?: boolean;
  is_real_market_data?: boolean;
  has_volume?: boolean;
  validation?: DatasetValidation | null;
  provenance?: {
    source_class?: string;
    source_label?: string;
    filename?: string | null;
    imported_at_utc?: string | null;
    rows?: number;
    sha256?: string | null;
    operator_label?: string;
    notes?: string;
    first_candle_utc?: string | null;
    last_candle_utc?: string | null;
  } | null;
  providers_tried?: { provider: string; status: string; detail?: string }[];
  notes?: string;
  reason?: string;
  detail?: string | null;
  tried?: { provider: string; status: string; detail?: string }[];
  as_of_utc?: string;
}

export interface DataStatusPayload {
  assets: DatasetStatus[];
  counts: {
    total: number;
    available: number;
    unavailable: number;
    real_market: number;
    simulated: number;
    replay: number;
    stale: number;
  };
  data_source_mode?: string | null;
  environment?: string;
  datasets_dir?: string;
  note?: string;
  as_of_utc?: string;
}

export interface DataImportResponse {
  status: string;
  asset?: string;
  reason?: string | null;
  rows?: number;
  validation?: DatasetValidation | null;
  provenance?: Record<string, any> | null;
  warnings?: string[];
  dry_run?: boolean;
  path?: string;
}


// ------------------------------------------------------------ research lab (§16)
export interface ResearchSummary {
  run_id?: string | null;
  generated_at_utc?: string | null;
  dataset?: Record<string, any> | null;
  experiment_count: number;
  cells_with_a_selected_threshold: number;
  cells_without_evidence_of_edge: number;
  paper_signals_total: number;
  paper_wins_total: number;
  paper_losses_total: number;
  paper_win_rate_pooled: number | null;
  note: string;
}

export interface ResearchGate {
  signals: number;
  wins: number;
  losses: number;
  hit_rate: number | null;
  loss_rate: number | null;
  max_losing_streak: number;
}

export interface ResearchSelection {
  experiment_id: string;
  target: string;
  horizon_minutes: number;
  feature_set: string;
  models: string[];
  selected_threshold: number | null;
  selection_reason: string;
  holdout_gate: ResearchGate;
  paper_replay: ResearchGate & { no_trade_rate: number | null };
}

export interface ResearchLatest {
  available: boolean;
  reason?: string;
  run_id?: string;
  generated_at_utc?: string;
  config?: Record<string, any>;
  dataset?: Record<string, any>;
  run_notes?: Record<string, any>;
  summary?: ResearchSummary;
  experiments_with_evidence?: ResearchSelection[];
  experiments_without_evidence?: string[];
  report_markdown?: string | null;
  disclaimer?: string;
}

// ------------------------------------------------- continuous M1 + paper trading
export interface LiveFeedRow {
  asset: string;
  provider: string | null;
  latest_candle: {
    open_time_utc: string;
    close_time_utc: string;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number | null;
    received_at_utc: string;
  } | null;
  age_seconds: number | null;
  resolution_minutes: number | null;
  rows: number;
  gaps: { missing_minutes: number; sample: string[] } | null;
  is_simulated: boolean;
  is_replay: boolean;
  freshness_status: "FRESH" | "STALE" | "INVALID" | "UNAVAILABLE";
  budget_seconds: number | null;
  detail: string | null;
}

export interface PaperSummary {
  generated_at_utc: string;
  current_ist: string;
  mode: string;
  data_freshness: "FRESH" | "STALE" | "INVALID" | "UNAVAILABLE";
  freshness_counts: Record<string, number>;
  latest_m1: { asset: string; provider: string | null; open_time_utc: string; close_time_utc: string } | null;
  next_m1: { open_time_utc: string; open_time_ist: string; seconds_away: number } | null;
  paper: {
    signals_today: number;
    pending: number;
    wins: number;
    losses: number;
    unresolved: number;
  };
  scans: { coverage: number | null; no_trade_rate: number | null; scans_recorded: number };
  live: { assets: LiveFeedRow[] };
  disclaimer?: string;
}

export interface PaperCard {
  kind: "PAPER SIGNAL" | "NO TRADE";
  asset?: string | null;
  time_ist?: string;
  direction?: string | null;
  confidence?: number | null;
  model_agreement?: number | null;
  expiry_minutes?: number | null;
  data?: string;
  provider?: string | null;
  model?: string | null;
  status?: string | null;
  result?: string | null;
  reason?: string | null;
  detail?: string | null;
  blocked_by?: string[];
}

export interface PaperEvidence {
  total_decided_trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  wilson_95_lower_bound: number | null;
  max_losing_streak: number;
  unresolved_trades: number;
  pending_trades: number;
  days: number;
  assets: string[];
  evidence_bar: "PASSED" | "NOT PASSED";
  evidence_rule: { minimum_qualifying_signals: number; wilson_95_lower_bound_above: number };
  disclaimer?: string;
}

export interface PaperLedgerRow {
  signal_id: string;
  asset: string;
  entry_time_utc: string;
  entry_time_ist: string | null;
  direction: string | null;
  entry_price: number | null;
  expiry_minutes: number | null;
  expiry_time_utc: string | null;
  model_version: string | null;
  confidence: number | null;
  model_agreement: number | null;
  feed_provider: string | null;
  feed_status: string | null;
  is_simulated: boolean;
  status: "PENDING" | "WIN" | "LOSS" | "UNRESOLVED" | "LOCKED" | "REJECTED";
  result: string | null;
  result_reason: string | null;
}

export interface PaperLedger {
  rows: PaperLedgerRow[];
  summary: Record<string, any>;
}
