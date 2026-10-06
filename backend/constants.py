"""Static system constants: asset universe, quality bands, confidence weights.

Nothing in here is a fabricated market measurement — these are *policy*
constants (thresholds, weights, labels) documented in the README.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TIMEFRAME = "M1"
TIMEZONE = "Asia/Kolkata"

# ---------------------------------------------------------------- quality bands
QUALITY_BANDS = (
    (90.0, "VERY_HIGH", "VERY HIGH"),
    (85.0, "HIGH", "HIGH CONFIDENCE"),
    (75.0, "MODERATE", "MODERATE"),
    (0.0, "NO_TRADE", "NO TRADE"),
)
TRADABLE_MIN_QUALITY = 85.0

DEFAULT_MIN_CONFIDENCE = 85.0
PREFERRED_CONFIDENCE = 90.0

# ------------------------------------------------------- confidence composition
# Weights are a documented policy choice, normalised to 1.0. They are *not*
# tuned to manufacture high scores; the pipeline rejects instead of inflating.
CONFIDENCE_WEIGHTS: dict[str, float] = {
    "model": 0.26,          # ensemble probability of the selected direction
    "model_agreement": 0.14,
    "trend": 0.11,
    "momentum": 0.10,
    "candle": 0.10,
    "structure": 0.09,      # support / resistance context
    "volatility": 0.07,     # volatility suitability for a 1-minute expiry
    "regime": 0.08,         # market regime suitability
    "freshness": 0.05,      # data freshness
}

# Hard gates — any failure means NO TRADE regardless of confidence.
GATES = {
    "min_candles": 120,             # candles required to compute the M1 feature block
    "max_data_lag_seconds": 120,    # for realtime feeds
    "max_replay_lag_seconds": 180,  # for delayed/replay feeds (Yahoo 1m is delayed)
    "min_model_agreement": 0.60,    # fraction of models voting with the winner
    "max_model_spread": 0.18,       # max std-dev across model probabilities
    "min_direction_probability": 0.58,
    "min_volatility_pct": 0.00002,  # ATR% floor: too dead to trade an M1 expiry
    "max_volatility_pct": 0.0045,   # ATR% ceiling: news-spike / abnormal
    "max_anomaly_flags": 0,         # IsolationForest veto
    "min_structure_evidence": -0.25,  # structure evidence (room to the opposing level) floor
}


@dataclass(frozen=True)
class AssetSpec:
    symbol: str              # canonical id used by the API + UI
    label: str               # display label
    flag: str
    kind: str                # "otc" | "forex"
    price_decimals: int
    enabled: bool = True
    yahoo_symbol: str | None = None    # public delayed feed (if any)
    quotex_symbol: str | None = None   # symbol sent to the broker adapter
    simulated_pip: float = 0.0001       # tick size for the simulated feed
    simulated_vol_bps: float = 3.0      # 1-min sigma in basis points for simulated feed
    seed: int = 0
    notes: str = ""


# The OTC pairs have no public market-data feed. Without a configured broker
# feed (QUOTEX_* env / broker adapter) the engine reports DATA UNAVAILABLE for
# them instead of inventing prices.
ASSETS: tuple[AssetSpec, ...] = (
    AssetSpec("USDBDT-OTCq", "USDBDT-OTCq", "🇧🇩", "otc", 4, True, None, "USDBDT_otc", 0.0001, 3.1, 1101,
              "OTC pair — requires a broker feed; no public data source."),
    AssetSpec("EURCAD-OTCq", "EURCAD-OTCq", "🇪🇺", "otc", 5, True, None, "EURCAD_otc", 0.00001, 2.4, 1102,
              "OTC pair — requires a broker feed; no public data source."),
    AssetSpec("EURUSD", "EURUSD", "🇪🇺", "forex", 5, True, "EURUSD=X", "EURUSD", 0.00001, 2.2, 1103),
    AssetSpec("GBPUSD", "GBPUSD", "🇬🇧", "forex", 5, True, "GBPUSD=X", "GBPUSD", 0.00001, 2.6, 1104),
    AssetSpec("USDJPY", "USDJPY", "🇯🇵", "forex", 3, True, "JPY=X", "USDJPY", 0.001, 2.5, 1105),
    AssetSpec("AUDUSD", "AUDUSD", "🇦🇺", "forex", 5, True, "AUDUSD=X", "AUDUSD", 0.00001, 2.3, 1106),
    AssetSpec("USDCHF", "USDCHF", "🇨🇭", "forex", 5, True, "CHF=X", "USDCHF", 0.00001, 2.1, 1107),
)

ASSET_MAP: dict[str, AssetSpec] = {a.symbol: a for a in ASSETS}
# Symbols are case-sensitive in the broker ("USDBDT-OTCq"), so lookups are made
# case-insensitive instead of forcing everything to upper case.
_ASSET_LOOKUP: dict[str, str] = {a.symbol.upper(): a.symbol for a in ASSETS}
DEFAULT_ENABLED_ASSETS: tuple[str, ...] = tuple(a.symbol for a in ASSETS if a.enabled)


def canonical_symbol(symbol: str | None) -> str | None:
    """Resolve any case variant of a known symbol to its canonical form."""
    if symbol is None:
        return None
    return _ASSET_LOOKUP.get(str(symbol).strip().upper())


def is_known_asset(symbol: str | None) -> bool:
    return canonical_symbol(symbol) is not None

FEED_MODES = ("LIVE", "REPLAY")     # LIVE = realtime feed, REPLAY = delayed/simulated timeline
PROVIDER_LIVE = "live"
PROVIDER_DELAYED = "delayed"
PROVIDER_SIMULATED = "simulated"
PROVIDER_UNAVAILABLE = "unavailable"

# Feature-block version. Bump it whenever the feature maths changes: models
# trained under an older version are refused by the model service.
FEATURE_VERSION = "m1-features-1.0"

# Model lifecycle states (spec §4). READY_MARKET is the only state that may
# authorise a production signal.
MODEL_STATE_NOT_READY = "NOT_READY"
MODEL_STATE_READY_DEMO = "READY_DEMO"
MODEL_STATE_READY_MARKET = "READY_MARKET"
MODEL_STATES = (MODEL_STATE_NOT_READY, MODEL_STATE_READY_DEMO, MODEL_STATE_READY_MARKET)

# Deployment environment. In PRODUCTION only real market data + a READY_MARKET
# model may produce a signal; DEVELOPMENT additionally allows the labelled demo
# pipeline to run so the software can be exercised without a data licence.
ENV_PRODUCTION = "production"
ENV_DEVELOPMENT = "development"
TRADING_ENVS = (ENV_PRODUCTION, ENV_DEVELOPMENT)

DATA_SOURCE_MODES = ("auto", "real_only", "live_only", "simulated", "csv")

# Human labels for the four UI data badges (spec §1/§12).
DATA_BADGES = ("LIVE", "REPLAY", "SIMULATED", "DATA UNAVAILABLE")

DISCLAIMER = (
    "Confidence is a model-derived score, not a guarantee. Signals can and do lose. "
    "Paper trading only — no order is ever sent to a broker by this build."
)
