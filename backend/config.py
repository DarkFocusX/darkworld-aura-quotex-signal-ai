"""Runtime configuration.

Precedence:  environment variables  >  values persisted via POST /api/config  >  defaults.

Secrets (Telegram) are ONLY read from the environment — they are never written
to the database, never returned by the API and never hard-coded.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    DATA_SOURCE_MODES,
    DEFAULT_ENABLED_ASSETS,
    DEFAULT_MIN_CONFIDENCE,
    ENV_PRODUCTION,
    TRADING_ENVS,
)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
CSV_DIR = DATA_DIR / "csv"
ARTIFACTS_DIR = BASE_DIR / "models" / "artifacts"
DB_PATH = Path(os.environ.get("SIGNAL_DB_PATH", str(BASE_DIR / "database" / "signals.db")))
REPORTS_DIR = BASE_DIR / "research" / "reports"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (no external dependency). Existing env vars win."""
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] not in {'"', "'"}:
            # `KEY=value   # comment` is a comment, not part of the value (a '#' inside a
            # quoted value is kept verbatim). Without this, an example comment in
            # .env.example would be loaded as the value itself.
            hash_index = value.find("#")
            if hash_index > 0 and value[hash_index - 1] in " \t":
                value = value[:hash_index].rstrip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Settings:
    """Mutable-at-runtime settings (persisted in the `config` table)."""

    # signal policy
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    expiry_minutes: int = 1
    timeframe: str = "M1"
    timezone: str = "Asia/Kolkata"
    mode: str = "PAPER"                 # PAPER | SIGNAL_ONLY (never LIVE in this build)
    auto_select: bool = True
    daily_mode: bool = True
    selected_asset: str = "AUTO"        # "AUTO" or an asset symbol
    enabled_assets: list[str] = field(default_factory=lambda: list(DEFAULT_ENABLED_ASSETS))
    data_source_mode: str = "auto"      # auto | live_only | simulated | csv
    max_signals_per_day: int = 40
    min_entry_seconds_remaining: int = 8   # do not fire with <8s left in the minute
    telegram_enabled: bool = False
    telegram_allow_simulated: bool = False
    scheduler_enabled: bool = True
    outcome_evaluation_enabled: bool = True
    # production policy
    environment: str = ENV_PRODUCTION      # production | development
    min_model_agreement: float = 0.60      # agreement gate, configurable (spec §5)
    require_real_data: bool = True         # production: only real market data may signal
    require_market_model: bool = True      # production: READY_MARKET only

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _defaults_from_env() -> Settings:
    s = Settings()
    s.min_confidence = _env_float("MIN_CONFIDENCE", s.min_confidence)
    s.mode = os.environ.get("TRADING_MODE", s.mode).upper()
    s.data_source_mode = os.environ.get("DATA_SOURCE_MODE", s.data_source_mode).lower()
    if s.data_source_mode not in DATA_SOURCE_MODES:
        s.data_source_mode = "auto"
    s.telegram_enabled = _env_bool("ENABLE_TELEGRAM", s.telegram_enabled)
    s.telegram_allow_simulated = _env_bool("TELEGRAM_ALLOW_SIMULATED", s.telegram_allow_simulated)
    s.auto_select = _env_bool("AUTO_SELECT", s.auto_select)
    s.daily_mode = _env_bool("DAILY_SIGNAL_ENABLED", s.daily_mode)
    s.scheduler_enabled = _env_bool("SCHEDULER_ENABLED", s.scheduler_enabled)
    environment = os.environ.get("TRADING_ENV", s.environment).strip().lower()
    s.environment = environment if environment in TRADING_ENVS else ENV_PRODUCTION
    s.min_model_agreement = _env_float("MIN_MODEL_AGREEMENT", s.min_model_agreement)
    s.require_real_data = _env_bool("REQUIRE_REAL_DATA", s.require_real_data)
    s.require_market_model = _env_bool("REQUIRE_MARKET_MODEL", s.require_market_model)
    expiry = int(os.environ.get("EXPIRY_MINUTES", str(s.expiry_minutes)) or s.expiry_minutes)
    s.expiry_minutes = max(1, min(5, expiry))
    raw_assets = os.environ.get("ENABLED_ASSETS", "").replace(" ", "")
    if raw_assets:
        s.enabled_assets = [a for a in raw_assets.split(",") if a]
    return s


def telegram_credentials() -> dict[str, Any]:
    """Telegram secrets come from the environment only."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID") or ""
    return {
        "token": token.strip(),
        "chat_id": chat_id.strip(),
        "enabled": _env_bool("ENABLE_TELEGRAM", False),
        "configured": bool(token.strip() and chat_id.strip()),
        "api_base": os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        "timeout": _env_float("TELEGRAM_TIMEOUT", 8.0),
    }


_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def model_dir(version: str | None = None) -> Path:
    """Artifact directory for `version` (default: the active one).

    The version is a folder name, never a path: a version string containing separators,
    `..` or an absolute-style prefix is rejected instead of silently resolving outside
    `backend/models/artifacts` (spec §17 — path safety).
    """
    if version is None or version == "":
        return ARTIFACTS_DIR / "current"
    if not _VERSION_RE.match(version) or ".." in version:
        raise ValueError(f"invalid model version {version!r}: expected a plain folder name")
    candidate = (ARTIFACTS_DIR / version).resolve()
    if ARTIFACTS_DIR.resolve() not in candidate.parents:
        raise ValueError(f"invalid model version {version!r}: outside the artifacts directory")
    return candidate


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, default=str, separators=(",", ":"))
