"""Settings service — merges defaults, env vars and DB-persisted overrides."""
from __future__ import annotations

from typing import Any

from .config import Settings, _defaults_from_env, telegram_credentials
from .constants import (
    ASSET_MAP,
    DATA_SOURCE_MODES,
    MODEL_STATES,
    TRADING_ENVS,
    canonical_symbol,
)
from .database import db

_SECRET_KEYS = {"telegram_bot_token", "bot_token", "telegram_chat_id", "chat_id"}

_MUTABLE_FIELDS = {
    "min_confidence": float,
    "expiry_minutes": int,
    "mode": str,
    "auto_select": bool,
    "daily_mode": bool,
    "selected_asset": str,
    "enabled_assets": list,
    "telegram_enabled": bool,
    "telegram_allow_simulated": bool,
    "data_source_mode": str,
    "max_signals_per_day": int,
    "min_entry_seconds_remaining": int,
    "scheduler_enabled": bool,
    "outcome_evaluation_enabled": bool,
    "environment": str,
    "min_model_agreement": float,
    "require_real_data": bool,
    "require_market_model": bool,
}


def _defaults() -> dict[str, Any]:
    return _defaults_from_env().as_dict()


def get_settings() -> Settings:
    merged = db.get_config(_defaults())
    settings = Settings()
    for key, caster in _MUTABLE_FIELDS.items():
        if key not in merged:
            continue
        value = merged[key]
        try:
            if caster is bool:
                value = bool(value) if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}
            elif caster is list:
                value = [str(v) for v in value] if isinstance(value, list) else [str(value)]
            else:
                value = caster(value)
        except (TypeError, ValueError):
            continue
        setattr(settings, key, value)
    settings.timeframe = "M1"
    settings.timezone = "Asia/Kolkata"
    return settings


def validate_updates(updates: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate + coerce a config patch. Returns (clean, errors)."""
    clean: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in updates.items():
        if key in _SECRET_KEYS:
            errors.append(f"{key}: secrets must be provided through environment variables, not the API")
            continue
        if key not in _MUTABLE_FIELDS:
            errors.append(f"{key}: unknown setting")
            continue
        caster = _MUTABLE_FIELDS[key]
        try:
            if key == "min_confidence":
                value = float(raw)
                if not 50.0 <= value <= 99.0:
                    errors.append("min_confidence must be between 50 and 99")
                    continue
            elif key == "expiry_minutes":
                value = int(raw)
                if not 1 <= value <= 5:
                    errors.append("expiry_minutes must be between 1 and 5")
                    continue
            elif key == "mode":
                value = str(raw).upper()
                if value not in {"PAPER", "SIGNAL_ONLY"}:
                    errors.append("mode must be PAPER or SIGNAL_ONLY (live execution is not implemented)")
                    continue
            elif key == "data_source_mode":
                value = str(raw).lower()
                if value not in DATA_SOURCE_MODES:
                    errors.append(f"data_source_mode must be one of {list(DATA_SOURCE_MODES)}")
                    continue
            elif key == "selected_asset":
                if str(raw).upper() == "AUTO":
                    value = "AUTO"
                else:
                    value = canonical_symbol(str(raw)) or ""
                    if not value:
                        errors.append(f"selected_asset '{raw}' is not in the asset universe")
                        continue
            elif key == "enabled_assets":
                if not isinstance(raw, list):
                    errors.append("enabled_assets must be a list")
                    continue
                resolved = [canonical_symbol(str(a)) for a in raw]
                unknown = [a for a, r in zip(raw, resolved) if not r]
                if unknown:
                    errors.append(f"enabled_assets contains unknown symbols: {unknown}")
                    continue
                value = [r for r in resolved if r]
                if not value:
                    errors.append("enabled_assets cannot be empty")
                    continue
            elif key == "environment":
                value = str(raw).lower()
                if value not in TRADING_ENVS:
                    errors.append(f"environment must be one of {list(TRADING_ENVS)}")
                    continue
            elif key == "min_model_agreement":
                value = float(raw)
                if not 0.30 <= value <= 0.99:
                    errors.append("min_model_agreement must be between 0.30 and 0.99")
                    continue
            elif key == "max_signals_per_day":
                value = max(1, min(500, int(raw)))
            elif key == "min_entry_seconds_remaining":
                value = max(3, min(55, int(raw)))
            elif caster is bool:
                value = bool(raw) if isinstance(raw, bool) else str(raw).lower() in {"1", "true", "yes", "on"}
            else:
                value = caster(raw)
        except (TypeError, ValueError):
            errors.append(f"{key}: could not parse value {raw!r}")
            continue
        clean[key] = value
    return clean, errors


def update_settings(updates: dict[str, Any]) -> dict[str, Any]:
    clean, errors = validate_updates(updates)
    if clean:
        db.set_config(clean)
    return {"updated": clean, "errors": errors, "settings": public_config()}


def public_config() -> dict[str, Any]:
    settings = get_settings()
    creds = telegram_credentials()
    merged = db.get_config(_defaults())
    return {
        "settings": settings.as_dict(),
        "env_overrides_present": sorted(k for k in ("MIN_CONFIDENCE", "TRADING_MODE", "DATA_SOURCE_MODE",
                                                    "ENABLE_TELEGRAM", "EXPIRY_MINUTES") if k in merged or True),
        "telegram": {
            "enabled": settings.telegram_enabled,
            "env_enabled": creds["enabled"],
            "configured": creds["configured"],
            "token_set": bool(creds["token"]),
            "chat_id_set": bool(creds["chat_id"]),
            "allow_simulated": settings.telegram_allow_simulated,
        },
        "limits": {
            "min_confidence_range": [50, 99],
            "expiry_minutes_range": [1, 5],
            "modes": ["PAPER", "SIGNAL_ONLY"],
            "data_source_modes": list(DATA_SOURCE_MODES),
            "environments": list(TRADING_ENVS),
            "min_model_agreement_range": [0.3, 0.99],
        },
    }
