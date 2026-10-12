"""FastAPI application — the Quotex Signal AI HTTP surface.

    uvicorn backend.main:app --host 0.0.0.0 --port 8000

Everything the dashboard shows comes from these endpoints. The signal engine is
independent of any execution venue and of the frontend.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .config import load_dotenv, telegram_credentials
from .constants import (ASSETS, ASSET_MAP, DISCLAIMER, ENV_DEVELOPMENT, TIMEZONE,
                        TRADABLE_MIN_QUALITY, canonical_symbol)
from .data.base import MarketDataUnavailable
from .data.feed import ContinuousFeed
from .data import provenance
from .data.providers.twelve_data import (PROVIDER_NAME as TWELVE_DATA_PROVIDER,
                                         configured as twelve_data_configured)
from .data.registry import registry
from .database import db
from .execution import available_modes
from .models.service import get_model_service, reset_model_service
from .scheduler import scheduler
from .settings_service import get_settings, public_config, update_settings
from .signals import ledger, outcomes, paper as paper_state, tracker
from .signals import feed_state
from .signals.daily import clear_study_cache, daily_status, study_scan
from .signals.ingestion import ingestion
from .signals.scanner import PaperScanner, paper_collection_enabled
from .signals.engine import get_engine, reset_engine
from .timeutil import floor_minute, ist_iso, now_ist, now_utc, seconds_to_next_minute, to_ist, utc_iso

PROJECT_ROOT = Path(__file__).resolve().parents[1]
from .validation.checks import max_lag_for
from .data.base import data_lag_seconds


# Upload guard rails (security review §14): the CSV arrives as text, so cap both
# the byte size and the row count before anything is parsed into memory.
MAX_CSV_CHARS = 25_000_000  # ~25 MB of text; the parser additionally caps rows at 500k


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    db.init_db()
    ledger.ensure_schema()
    get_model_service()
    if get_settings().scheduler_enabled:
        scheduler.start()
    # Continuous M1 ingestion + paper scanner start only when explicitly asked for
    # (ENABLE_INGESTION=true). Nothing about the default deployment changes.
    if os.environ.get("ENABLE_INGESTION", "").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            if os.environ.get("INGESTION_MODE", "external").strip().lower() == "simulated":
                ingestion.start_simulated()
            else:
                ingestion.start()
        except Exception as exc:  # noqa: BLE001 — a feed problem must not stop the API
            ingestion.last_error = f"{type(exc).__name__}: {exc}"
    # Startup feed check (spec §18): report the LIVE FEED state without ever refusing to
    # start. A missing feed is a configuration fact, not a reason to keep the API down.
    try:
        feed_line = feed_state.live_feed_state()
        db.log_event("FEED", f"LIVE FEED {feed_line['state']} — {feed_line['detail']}",
                     level="INFO" if feed_line["state"] in {"FRESH", "CONNECTED"} else "WARNING",
                     meta={"state": feed_line["state"], "data": feed_line["data"],
                           "provider": feed_line["provider"]})
    except Exception as exc:  # noqa: BLE001
        db.log_event("FEED", f"live-feed check failed: {type(exc).__name__}", level="WARNING")

    yield
    ingestion.stop()
    scheduler.stop()


# API docs are convenient while commissioning and can be switched off for a
# hardened deployment (ENABLE_API_DOCS=false).
_DOCS = os.environ.get("ENABLE_API_DOCS", "true").lower() not in {"0", "false", "no", "off"}

app = FastAPI(
    title="Quotex Signal AI",
    version=__version__,
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
    description=(
        "M1 signal engine with strict validation gates. Paper/study use only — "
        "confidence is a model score, never a guarantee."
    ),
    lifespan=lifespan,
)

def _cors_settings() -> tuple[list[str], str | None]:
    """Origins allowed to call this API from a browser.

    Security review (§14): the earlier blanket `https?://.*` regex let any web
    page drive the engine (generate signals, import data, change settings) from
    a visitor's browser. Default is now same-origin plus local development, and
    a wildcard is only used when the operator explicitly asks for it.
    """
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if raw == "*":
        return ["*"], None
    explicit = [o.strip() for o in raw.split(",") if o.strip()]
    local = ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000", "http://127.0.0.1:8000"]
    preview = None
    if os.environ.get("ALLOW_PREVIEW_ORIGINS", "true").lower() in {"1", "true", "yes", "on"}:
        # sandbox live-preview hosts (https://<port>-<sandbox-id>.e2b.app)
        preview = r"^https://[0-9]+-[a-z0-9-]+\.e2b\.app$"
    return list(dict.fromkeys(explicit + local)), preview


_origins, _origin_regex = _cors_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(MarketDataUnavailable)
async def _market_data_handler(_: Request, exc: MarketDataUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"status": "DATA_UNAVAILABLE", "reason": exc.reason,
                 "asset": exc.asset, "provider": exc.provider, "detail": exc.detail},
    )


# ------------------------------------------------------------------- schemas
class GenerateRequest(BaseModel):
    asset: str | None = Field(default=None, description="Asset symbol or AUTO")
    mode: Literal["NOW", "DAILY"] = "NOW"
    persist: bool = True


class ConfigPatch(BaseModel):
    # Unknown fields are rejected outright so nobody can post secrets or typos.
    model_config = ConfigDict(extra="forbid")

    min_confidence: float | None = Field(default=None, ge=50, le=99)
    expiry_minutes: int | None = Field(default=None, ge=1, le=5)
    mode: Literal["PAPER", "SIGNAL_ONLY"] | None = None
    auto_select: bool | None = None
    daily_mode: bool | None = None
    selected_asset: str | None = None
    enabled_assets: list[str] | None = None
    telegram_enabled: bool | None = None
    telegram_allow_simulated: bool | None = None
    data_source_mode: Literal["auto", "real_only", "live_only", "simulated", "csv"] | None = None
    max_signals_per_day: int | None = Field(default=None, ge=1, le=500)
    min_entry_seconds_remaining: int | None = Field(default=None, ge=3, le=55)
    scheduler_enabled: bool | None = None
    outcome_evaluation_enabled: bool | None = None
    environment: Literal["production", "development"] | None = None
    min_model_agreement: float | None = Field(default=None, ge=0.30, le=0.99)
    require_real_data: bool | None = None
    require_market_model: bool | None = None


class DataImportRequest(BaseModel):
    """Operator CSV import (spec §2).

    The CSV arrives as TEXT: it is parsed in memory, validated, and only written
    to the dataset folder when it passes. No raw file upload, no user-controlled
    path — the file name is derived from the validated asset symbol.
    """

    model_config = ConfigDict(extra="forbid")

    asset: str = Field(description="Asset symbol, e.g. EURUSD or USDBDT-OTCq")
    csv_text: str | None = Field(default=None, description="Full CSV payload as text")
    from_file: bool = Field(default=False, description="Register an existing backend/data/csv/<ASSET>.csv")
    source_class: Literal["MARKET", "SIMULATED", "FIXTURE"] = "MARKET"
    operator_label: str | None = Field(default=None, max_length=200)
    filename: str | None = Field(default=None, max_length=120,
                                  description="Advisory only — the stored file name is derived from the asset symbol")
    dry_run: bool = False
    force: bool = False

    @field_validator("filename")
    @classmethod
    def _no_paths(cls, value: str | None) -> str | None:
        """A client-supplied file name must never look like a path (defence in depth)."""
        if value and ("/" in value or "\\" in value or ".." in value):
            raise ValueError("filename must be a plain file name with no path separators")
        return value


class TickIn(BaseModel):
    """One verified tick from the operator's own feed process."""

    model_config = ConfigDict(extra="forbid")

    price: float
    time_utc: datetime | None = None
    volume: float | None = None


class CandleIn(BaseModel):
    """One verified, *closed* M1 candle."""

    model_config = ConfigDict(extra="forbid")

    ts_utc: datetime | None = None
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    resolution_minutes: float = Field(default=1.0, gt=0, le=60)


class IngestRequest(BaseModel):
    """Push verified data into the continuous store (spec §1).

    The provider label is supplied by the caller and is preserved verbatim on every
    stored row. A caller may not claim a provider that is not actually connected:
    `broker_ws` requires a streaming broker adapter, `simulated` is only accepted in
    a development environment, and public/delayed feeds must be declared as replay.
    """

    model_config = ConfigDict(extra="forbid")

    asset: str
    ticks: list[TickIn] = Field(default_factory=list, max_length=2000)
    candles: list[CandleIn] = Field(default_factory=list, max_length=2000)
    provider: str = Field(default="operator", max_length=40)
    source_label: str | None = Field(default=None, max_length=200)
    realtime: bool = False
    simulated: bool = False
    replay: bool = True

    @field_validator("asset")
    @classmethod
    def _known_asset(cls, value: str) -> str:
        canonical = canonical_symbol(value) or value
        if canonical not in ASSET_MAP:
            raise ValueError(f"unknown asset {value!r}")
        return canonical


class ScannerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class PaperScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: str | None = None
    at_utc: datetime | None = None


# -------------------------------------------------------------------- helpers
def _signal_public(signal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not signal:
        return None
    keys = (
        "signal_uid", "asset", "flag", "label", "timeframe", "direction", "confidence", "quality",
        "quality_label", "entry_time_utc", "entry_time_ist", "entry_time_ist_full", "entry_date_ist",
        "expiry_minutes", "expiry_time_ist",
        "expiry_time_utc", "data_timestamp_utc", "data_timestamp_ist", "price", "model_probability",
        "model_agreement", "anomaly_flag", "feed_source", "feed_mode", "feed_provider",
        "feed_badge", "display_status", "source_class", "source_status",
        "is_simulated", "is_replay", "is_real_market_data", "environment", "model_market_ready",
        "realtime", "created_at_utc", "status", "result", "result_reason",
        "scan_mode", "telegram_status",
    )
    out = {k: signal.get(k) for k in keys}
    out["components"] = signal.get("components")
    out["model_votes"] = signal.get("model_votes")
    out["disclaimer"] = DISCLAIMER
    return out


def _history_row(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_uid": signal.get("signal_uid"),
        "time_ist": (signal.get("entry_time_ist") or "")[:5],
        "entry_time_ist": signal.get("entry_time_ist"),
        "asset": signal.get("asset"),
        "direction": signal.get("direction"),
        "confidence": signal.get("confidence"),
        "quality": signal.get("quality"),
        "result": signal.get("result"),
        "status": signal.get("status"),
        "result_reason": signal.get("result_reason"),
        "is_simulated": signal.get("is_simulated"),
        "feed_source": signal.get("feed_source"),
        "expiry_minutes": signal.get("expiry_minutes"),
        "telegram_status": signal.get("telegram_status"),
        "created_at_utc": signal.get("created_at_utc"),
    }


def _time_payload(now: datetime | None = None) -> dict[str, Any]:
    """Authoritative clock for the UI (spec §8).

    The browser never computes trading times: it renders these values, subtracting
    only the local elapsed seconds so the display stays smooth between polls.
    """
    now = now or now_utc()
    ist = to_ist(now)
    remaining = seconds_to_next_minute(now)
    current_candle = floor_minute(now)
    last_closed = current_candle - timedelta(minutes=1)
    next_candle = current_candle + timedelta(minutes=1)
    entry = next_candle
    settings = get_settings()
    return {
        "timezone": TIMEZONE,
        "ist": ist_iso(now),
        "ist_clock": ist.strftime("%H:%M:%S"),
        "ist_date": ist.strftime("%d %b %Y"),
        "ist_offset": ist.strftime("%z"),
        "utc": utc_iso(now),
        "epoch": int(now.timestamp()),
        # M1 execution timing
        "current_candle_ist": to_ist(current_candle).strftime("%H:%M:%S"),
        "last_closed_candle_ist": to_ist(last_closed).strftime("%H:%M:%S"),
        "last_closed_candle_utc": utc_iso(last_closed),
        "next_candle_ist": to_ist(next_candle).strftime("%H:%M:%S"),
        "entry_time_ist": to_ist(entry).strftime("%H:%M:%S"),
        "expiry_minutes": settings.expiry_minutes,
        "expiry_time_ist": to_ist(entry + timedelta(minutes=settings.expiry_minutes)).strftime("%H:%M:%S"),
        "seconds_to_next_candle": round(remaining, 1),
        "countdown_seconds": round(remaining, 1),
        "countdown_label": f"{int(remaining // 60):02d}:{remaining % 60:04.1f}",
        "server_weekday": ist.strftime("%A"),
        "authoritative": "backend",
        "note": (
            "Only CLOSED candles are used for prediction. The in-progress candle is never "
            "part of the feature vector."
        ),
    }


def _data_status() -> dict[str, Any]:
    settings = get_settings()
    out: dict[str, Any] = {
        "data_source_mode": settings.data_source_mode,
        "providers": registry.provider_report(),
        "assets": [],
    }
    for spec in ASSETS:
        if not spec.enabled:
            continue
        try:
            frame = registry.get_frame(spec.symbol, limit=120, data_source_mode=settings.data_source_mode)
        except MarketDataUnavailable as exc:
            out["assets"].append({
                "asset": spec.symbol, "available": False, "reason": exc.reason,
                "provider": exc.provider, "badge": "DATA UNAVAILABLE",
                "display_status": "DATA UNAVAILABLE", "detail": exc.detail,
            })
            continue
        lag = data_lag_seconds(frame.meta)
        out["assets"].append({
            "asset": spec.symbol,
            "available": True,
            "provider": frame.meta.provider,
            "source_label": frame.meta.source_label,
            "source_class": frame.meta.source_class,
            "source_status": frame.meta.source_status,
            "badge": frame.meta.badge,
            "display_status": frame.meta.display_status,
            "simulated": frame.meta.is_simulated,
            "is_replay": frame.meta.is_replay,
            "is_real_market_data": frame.meta.is_market_data,
            "timeframe": frame.meta.timeframe,
            "mode": frame.meta.mode,
            "candles": frame.meta.candles,
            "lag_seconds": None if lag == float("inf") else round(lag, 1),
            "max_lag_seconds": max_lag_for(frame.meta),
            "latest_candle_ist": ist_iso(frame.meta.latest_candle_open_utc) if frame.meta.latest_candle_open_utc else None,
            "last_update_utc": frame.meta.last_update_utc,
            "live_price": frame.live_price,
        })
    available = [a for a in out["assets"] if a["available"]]
    real = [a for a in available if a.get("is_real_market_data")]
    out["available_count"] = len(available)
    out["total_count"] = len(out["assets"])
    out["simulated_count"] = len([a for a in available if a.get("simulated")])
    out["real_market_count"] = len(real)
    out["environment"] = settings.environment
    if not available:
        out["status"] = "UNAVAILABLE"
        out["message"] = "No market data available for any enabled asset"
    elif not real:
        out["status"] = "SIMULATED_ONLY"
        out["message"] = (
            "Only the simulated demo feed is available — no production signal can be produced "
            "from it. Import real candles (POST /api/data/import) to enable production signals."
        )
    elif len(real) < len(available):
        out["status"] = "PARTIAL"
        out["message"] = (
            f"{len(real)} of {len(available)} assets are on real market data; "
            f"{len(available) - len(real)} are simulated"
        )
    else:
        out["status"] = "OK"
        out["message"] = "Real market data available"
    return out


# ------------------------------------------------------------------- endpoints
@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    model_status = get_model_service().status()
    try:
        db.init_db()
        history = len(db.list_history(limit=1))
        db_status = {"ok": True, "path": str(db.DB_PATH), "has_signals": history > 0}
    except Exception as exc:  # noqa: BLE001
        db_status = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    telegram = telegram_credentials()
    return {
        "status": "ok" if db_status["ok"] else "degraded",
        "app": "Quotex Signal AI",
        "version": __version__,
        "time_ist": ist_iso(),
        "timezone": TIMEZONE,
        "mode": settings.mode,
        "environment": settings.environment,
        "min_confidence": settings.min_confidence,
        "min_model_agreement": settings.min_model_agreement,
        "tradable_min_quality": TRADABLE_MIN_QUALITY,
        "production_policy": {
            "require_real_data": settings.require_real_data,
            "require_market_model": settings.require_market_model,
        },
        "database": db_status,
        "model": {"state": model_status["state"], "ready": model_status["ready"],
                  "message": model_status["message"], "version": model_status["version"],
                  "simulated_trained": bool(model_status.get("data_provenance", {}).get("is_simulated"))},
        "telegram": {"enabled": settings.telegram_enabled, "configured": telegram["configured"]},
        "live_feed": _live_feed_brief(),
        "scheduler": scheduler.status(),
        "execution": available_modes(),
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/time/ist")
def time_ist() -> dict[str, Any]:
    payload = _time_payload()
    engine = get_engine()
    payload["session"] = engine.session()
    payload["scheduler"] = scheduler.status()
    return payload


@app.get("/api/assets")
def assets(probe: bool = Query(default=False, description="run a live availability probe per asset")):
    settings = get_settings()
    out = []
    for spec in ASSETS:
        entry: dict[str, Any] = {
            "symbol": spec.symbol,
            "label": spec.label,
            "flag": spec.flag,
            "kind": spec.kind,
            "price_decimals": spec.price_decimals,
            "enabled": spec.enabled,
            "is_selected": settings.selected_asset == spec.symbol,
            "notes": spec.notes,
            "has_public_feed": bool(spec.yahoo_symbol),
        }
        if probe:
            entry["probe"] = registry.probe(spec.symbol, settings.data_source_mode)
        out.append(entry)
    return {
        "assets": out,
        "enabled_assets": settings.enabled_assets,
        "selected_asset": settings.selected_asset,
        "auto_select": settings.auto_select,
        "timeframe": settings.timeframe,
    }


@app.post("/api/data/import")
def data_import(request: DataImportRequest) -> dict[str, Any]:
    """Ingest operator candle data (spec §2).

    Body: {"asset": "EURUSD", "csv_text": "time,open,high,low,close,volume\n...",
           "source_class": "MARKET", "dry_run": false}

    Set `from_file: true` to validate + register a CSV that was copied into
    backend/data/csv/. Everything is validated before it can reach the model:
    duplicates, gaps, invalid OHLC, non-monotonic timestamps, wrong resolution
    and insufficient history are all rejected with a structured report.
    """
    if not request.from_file:
        payload = request.csv_text or ""
        if not payload.strip():
            raise HTTPException(status_code=422, detail="csv_text is empty — send the CSV body or set from_file=true")
        if len(payload) > MAX_CSV_CHARS:
            raise HTTPException(
                status_code=413,
                detail=f"CSV payload too large ({len(payload):,} chars, limit {MAX_CSV_CHARS:,}). "
                       "Import in per-asset files or place the file in the dataset folder and use from_file=true.",
            )

    if request.from_file:
        result = registry.register_file(
            request.asset,
            source_class=request.source_class,
            operator_label=request.operator_label or "",
            force=request.force,
        )
        if result.get("status") == "REGISTERED":
            clear_study_cache()
        return result

    if not request.csv_text:
        return {
            "status": "REJECTED",
            "asset": request.asset,
            "reason": "Provide csv_text, or set from_file=true to register an existing dataset file.",
            "validation": None,
        }
    result = registry.import_csv(
        request.asset,
        request.csv_text,
        source_class=request.source_class,
        operator_label=request.operator_label or "",
        filename=request.filename,
        dry_run=request.dry_run,
    )
    if result.get("status") == "IMPORTED":
        clear_study_cache()
    return result


@app.get("/api/data/status")
def data_status() -> dict[str, Any]:
    """Per-asset data provenance + validation (spec §2)."""
    settings = get_settings()
    payload = registry.dataset_status_all(now=now_utc())
    payload["data_source_mode"] = settings.data_source_mode
    payload["environment"] = settings.environment
    payload["datasets_dir"] = str(registry.csv.directory)
    payload["note"] = (
        "Rows, freshness, validation and provenance come from the datasets actually on disk. "
        "Nothing here is estimated or inferred."
    )
    return payload


@app.get("/api/data/provenance")
def data_provenance() -> dict[str, Any]:
    """Raw provenance records for the DATA PROVENANCE panel (spec §12)."""
    records = provenance.all_records()
    return {
        "count": len(records),
        "records": [{"asset": asset, **record} for asset, record in sorted(records.items())],
        "store": str(provenance.PROVENANCE_PATH),
    }


@app.get("/api/market/{asset}")
def market(asset: str, limit: int = Query(default=180, ge=20, le=500)) -> dict[str, Any]:
    symbol = canonical_symbol(asset)
    if not symbol:
        raise HTTPException(status_code=400, detail=f"Unknown asset '{asset}'. See GET /api/assets.")
    settings = get_settings()
    try:
        # always pull enough history for the feature block, then trim the chart payload
        frame = registry.get_frame(symbol, limit=max(limit, 300), data_source_mode=settings.data_source_mode)
    except MarketDataUnavailable as exc:
        return JSONResponse(status_code=503, content={
            "status": "DATA_UNAVAILABLE", "asset": symbol, "reason": exc.reason,
            "provider": exc.provider, "detail": exc.detail,
        })
    lag = data_lag_seconds(frame.meta)
    from .data.base import frame_to_records
    from .features.pipeline import FeatureError, latest_features

    payload: dict[str, Any] = {
        "status": "OK",
        "asset": symbol,
        "timeframe": "M1",
        "timezone": TIMEZONE,
        "feed": {
            **frame.meta.public_dict(),
            "latest_candle_ist": ist_iso(frame.meta.latest_candle_open_utc) if frame.meta.latest_candle_open_utc else None,
            "lag_seconds": None if lag == float("inf") else round(lag, 1),
            "max_lag_seconds": max_lag_for(frame.meta),
        },
        "live_price": frame.live_price,
        "candles": frame_to_records(frame, limit=limit),
    }
    try:
        snapshot = latest_features(frame.df, registry.asset(symbol))
        payload["features"] = snapshot.as_dict()
    except FeatureError as exc:
        payload["features"] = None
        payload["feature_error"] = {"reason": exc.reason, "detail": exc.detail}
    return payload


@app.post("/api/signal/generate")
def generate_signal(request: GenerateRequest) -> dict[str, Any]:
    settings = get_settings()
    raw_symbol = request.asset or settings.selected_asset or "AUTO"
    symbol = "AUTO" if str(raw_symbol).upper() == "AUTO" else (canonical_symbol(str(raw_symbol)) or "")
    if not symbol:
        raise HTTPException(status_code=400, detail={
            "status": "INVALID_ASSET",
            "reason": f"Unknown asset '{request.asset}'",
            "known_assets": sorted(ASSET_MAP),
        })

    if request.mode == "DAILY":
        payload = daily_status(None if symbol == "AUTO" else symbol)
        payload["scan_mode"] = "DAILY"
        payload["mode"] = settings.mode
        payload["disclaimer"] = DISCLAIMER
        payload["session"] = get_engine().session()
        payload["model_status"] = get_model_service().status()
        return payload

    try:
        result = get_engine().generate(symbol, mode="NOW", persist=request.persist)
    except Exception as exc:  # noqa: BLE001 — never invent a signal on failure
        raise HTTPException(status_code=500, detail={
            "status": "ERROR",
            "reason": "Signal engine failure",
            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }) from exc
    if result.get("signal"):
        result["signal"] = _signal_public(result["signal"])
    result["mode"] = settings.mode
    return result


@app.get("/api/signal/latest")
def latest_signal(asset: str | None = None) -> dict[str, Any]:
    signal = db.get_latest_signal(canonical_symbol(asset) if asset else None)
    last_scan = db.list_scans(limit=1)
    return {
        "signal": _signal_public(signal),
        "last_scan": last_scan[0] if last_scan else None,
        "session": get_engine().session(),
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/signals/history")
def signals_history(limit: int = Query(default=30, ge=1, le=200), asset: str | None = None) -> dict[str, Any]:
    rows = db.list_history(limit=limit, asset=canonical_symbol(asset) if asset else None)
    return {
        "count": len(rows),
        "signals": [_history_row(row) for row in rows],
        "disclaimer": "WIN/LOSS is only recorded once the verified closing candle exists.",
    }


@app.get("/api/signals/performance")
def signals_performance(include_simulated: bool = True) -> dict[str, Any]:
    perf = db.get_signal_performance(include_simulated=include_simulated)
    model = get_model_service()
    return {
        "signal_performance": {**perf, "note": "Live/paper signals emitted by this engine only."},
        "model_validation": {
            "available": model.ready,
            "version": model.metadata.version,
            "data_source_class": model.metadata.source_class,
            "asset_list": model.metadata.asset_list or model.metadata.data_provenance.get("assets", []),
            "row_count": model.metadata.training_rows,
            "data_period": model.metadata.data_period,
            "trained_at_utc": model.metadata.trained_at_utc,
            "data_provenance": model.metadata.data_provenance,
            "out_of_fold": (model.metadata.metrics or {}).get("out_of_fold"),
            "holdout": (model.metadata.metrics or {}).get("holdout"),
            "validation_metrics": model.metadata.validation_metrics,
            "holdout_metrics": model.metadata.holdout_metrics,
            "note": "Model validation metrics describe offline evaluation and are NOT live win rates.",
        },
        "separation_note": (
            "MODEL VALIDATION (offline, walk-forward/holdout) and SIGNAL PERFORMANCE (live paper trades) "
            "are intentionally reported separately and must never be combined."
        ),
    }


@app.get("/api/signals/scans")
def recent_scans(limit: int = Query(default=25, ge=1, le=200)) -> dict[str, Any]:
    return {"scans": db.list_scans(limit=limit)}


@app.get("/api/signals/daily")
def daily(asset: str | None = None) -> dict[str, Any]:
    payload = daily_status(canonical_symbol(asset) if asset and asset.upper() != "AUTO" else None)
    payload["session"] = get_engine().session()
    payload["min_confidence"] = get_settings().min_confidence
    payload["disclaimer"] = DISCLAIMER
    return payload


@app.get("/api/signals/study/{asset}")
def study(asset: str, day: str | None = None, max_rows: int = Query(default=240, ge=30, le=600)) -> dict[str, Any]:
    symbol = canonical_symbol(asset)
    if not symbol:
        raise HTTPException(status_code=400, detail=f"Unknown asset '{asset}'")
    return study_scan(symbol, day_ist=day, max_rows=max_rows)


@app.post("/api/signals/evaluate")
def evaluate() -> dict[str, Any]:
    return tracker.evaluate_open_signals()


# --------------------------------------------------------------- research lab
@app.get("/api/research/experiments")
def research_experiments(limit: int = Query(default=200, ge=1, le=2000),
                         run_id: str | None = None) -> dict[str, Any]:
    """Every experiment of the latest (or a named) lab run — no 'best model' ranking."""
    from .research.lab import load_index, summarise

    index = load_index(limit=limit, run_id=run_id)
    if not index.get("available"):
        return {**index, "summary": None, "disclaimer": DISCLAIMER}
    return {
        **index,
        "summary": summarise(index),
        "disclaimer": (
            "Offline validation measurements only. Not a promise of future performance, never combined "
            "with live paper results, and deliberately not ranked into a 'best model'."
        ),
    }


@app.get("/api/research/latest")
def research_latest() -> dict[str, Any]:
    """Compact view of the newest lab run for the MODEL QUALITY panel."""
    from .research.lab import load_index, summarise

    index = load_index()
    if not index.get("available"):
        return {"available": False, "reason": index.get("reason"), "disclaimer": DISCLAIMER}
    summary = summarise(index)
    rows = index["experiments"]
    selected = [r for r in rows if r.get("selected_threshold") is not None]
    return {
        "available": True,
        "run_id": index["run_id"],
        "generated_at_utc": index["generated_at_utc"],
        "config": index["config"],
        "dataset": index["dataset"],
        "run_notes": index.get("run_notes", {}),
        "summary": summary,
        "experiments_with_evidence": [
            {
                "experiment_id": r["experiment_id"],
                "target": r["target"],
                "horizon_minutes": r["horizon_minutes"],
                "feature_set": r["feature_set"],
                "models": r["models"],
                "selected_threshold": r["selected_threshold"],
                "selection_reason": r["selection_reason"],
                "holdout_gate": r["holdout_gate"],
                "paper_replay": r["paper_replay"],
            }
            for r in selected
        ],
        "experiments_without_evidence": [r["experiment_id"] for r in rows if r.get("selected_threshold") is None],
        "report_markdown": index.get("report_markdown"),
        "disclaimer": (
            "A selected threshold means only that the validation folds cleared the stated evidence bar; the "
            "holdout column is the single out-of-sample test of that choice."
        ),
    }


@app.get("/api/research/experiments/{experiment_id}")
def research_experiment(experiment_id: str, run_id: str | None = None) -> dict[str, Any]:
    from .research.lab import load_experiment

    record = load_experiment(experiment_id, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No experiment '{experiment_id}' in the latest run")
    record["disclaimer"] = DISCLAIMER
    return record


@app.get("/api/research/experiments/{experiment_id}/signals")
def research_signals(experiment_id: str, run_id: str | None = None,
                     limit: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
    """Paper-replay signals for one experiment (chronological)."""
    from .research.lab import load_signals

    rows = load_signals(run_id, experiment_id, limit=limit) if run_id else []
    if not rows:
        from .research.lab import load_experiment

        record = load_experiment(experiment_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No experiment '{experiment_id}'")
        rows = (record.get("paper_replay", {}).get("signals_preview") or [])[:limit]
    return {
        "experiment_id": experiment_id,
        "count": len(rows),
        "signals": rows,
        "note": "Outcomes are read from the entry/expiry prices in the dataset, never from the prediction.",
    }


# ---------------------------------------------- continuous ingestion + paper phase
@app.get("/api/data/live-status")
def data_live_status(assets: str | None = Query(default=None, description="comma-separated, default all")) -> dict[str, Any]:
    """Spec §3 — per-asset continuous-feed status (FRESH / STALE / INVALID / UNAVAILABLE)."""
    requested = [a for a in (assets or "").replace(" ", "").split(",") if a] or None
    payload = ingestion.live_status(requested)
    payload["disclaimer"] = DISCLAIMER
    return payload


@app.post("/api/data/ingest")
def data_ingest(request: IngestRequest) -> dict[str, Any]:
    """Push verified ticks or closed candles into the continuous M1 store (spec §1, §2).

    Nothing is fabricated here: the caller supplies real values from its own feed
    process, and every stored row keeps the caller's provider label plus the
    simulated/replay flags it declared. Unlabelled "live" data is refused.
    """
    settings = get_settings()
    provider = request.provider.strip().lower() or "operator"
    if provider in {"broker_ws", "quotex", "quotes"} and not registry.broker.health().get("connected"):
        raise HTTPException(status_code=409, detail=(
            f"provider {provider!r} is not connected — configure QUOTEX_WS_URL and attach the tick "
            "handler, or declare the provider you are actually using"))
    if provider == TWELVE_DATA_PROVIDER and not twelve_data_configured():
        # The key is read from this process's environment only; it is never echoed back.
        raise HTTPException(status_code=409, detail=(
            f"provider {provider!r} is not configured — set TWELVE_DATA_API_KEY and run "
            "tools/start_twelve_data_feed.py, or declare the provider you are actually using"))
    if request.simulated and settings.environment != ENV_DEVELOPMENT:
        raise HTTPException(status_code=409, detail=(
            "simulated data is only accepted in a development environment; it can never be market data"))
    if request.realtime and (request.simulated or request.replay):
        raise HTTPException(status_code=422, detail="a simulated or replay feed cannot be declared realtime")

    feed = ingestion.feed or _external_feed(
        provider,
        source_label=request.source_label or f"Operator feed ({provider})",
        realtime=request.realtime, simulated=request.simulated, replay=request.replay,
    )
    receipts: list[dict[str, Any]] = []
    label = request.source_label or f"Operator feed ({provider})"
    for tick in request.ticks:
        receipts.append(feed.ingest_tick(request.asset, tick.price, time_utc=tick.time_utc,
                                         volume=tick.volume, source_label=label))
    for candle in request.candles:
        payload = candle.model_dump()
        payload.setdefault("source_label", label)      # provenance of *this* producer
        payload["ts_utc"] = (candle.ts_utc or (datetime.now(tz=timezone.utc))).isoformat()
        receipts.append(feed.ingest_candle(request.asset, payload, provider=provider))
    accepted = sum(1 for r in receipts if r.get("accepted"))
    return {
        "asset": request.asset,
        "provider": provider,
        "accepted": accepted,
        "rejected": len(receipts) - accepted,
        "receipts": receipts[:50],
        "live_status": feed.live_status(request.asset),
        "note": ("Stored rows keep the provider label and the simulated/replay flags declared here; a "
                 "missing minute is reported as a gap and never invented."),
        "disclaimer": DISCLAIMER,
    }


@app.post("/api/data/ingest/start")
def data_ingest_start(mode: Literal["external", "simulated"] = Query(default="external")) -> dict[str, Any]:
    """Start the continuous ingestion service (and the paper scanner with it)."""
    settings = get_settings()
    if mode == "simulated" and settings.environment != ENV_DEVELOPMENT:
        raise HTTPException(status_code=409, detail=(
            "the simulated feed can only be started in a development environment"))
    try:
        status = ingestion.start_simulated() if mode == "simulated" else ingestion.start()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)[:300])
    return {"started": True, "mode": mode, "ingestion": status, "disclaimer": DISCLAIMER}


@app.post("/api/data/ingest/stop")
def data_ingest_stop() -> dict[str, Any]:
    return {"stopped": True, "ingestion": ingestion.stop()}


@app.get("/api/paper/scanner")
def paper_scanner_state() -> dict[str, Any]:
    return {"scanner": ingestion.status(),
            "paper_collection": paper_collection_enabled(),
            "evidence": ledger.evidence(),
            "disclaimer": DISCLAIMER}


@app.post("/api/paper/scanner")
def paper_scanner_control(action: ScannerAction) -> dict[str, Any]:
    """Start/stop the automatic paper scanner (never changes gates or thresholds)."""
    scanner = ingestion.scanner
    if scanner is None:
        ingestion.start()
        scanner = ingestion.scanner
    if scanner is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="scanner unavailable")
    if action.enabled:
        scanner.start()
    else:
        scanner.stop()
    return {"enabled": action.enabled, "scanner": scanner.status(), "disclaimer": DISCLAIMER}


@app.post("/api/paper/scan")
def paper_scan(request: PaperScanRequest | None = None) -> dict[str, Any]:
    """Evaluate the newest closed candle with the same engine the button uses."""
    request = request or PaperScanRequest()
    scanner = ingestion.scanner or PaperScanner(ingestion.feed)
    now = request.at_utc or utc_iso()
    assets = [request.asset] if request.asset else scanner.assets()
    if not assets:
        return {"status": "NO_TRADE", "reason": "no ingested candles to evaluate yet",
                "scanned": [], "disclaimer": DISCLAIMER}
    at = datetime.fromisoformat(str(now).replace("Z", "+00:00")) if isinstance(now, str) else now
    results = []
    for asset in assets:
        candle = scanner._closed_candle_for(asset, at)
        if candle is None:
            results.append({"asset": asset, "status": "NO_TRADE", "reason": "no closed candle yet"})
            continue
        decision = scanner.evaluate_asset(asset, candle, now=at)
        scanner._evaluated[asset] = candle
        results.append(decision)
    qualified = [r for r in results if r.get("status") == "SIGNAL"]
    primary = qualified[0] if qualified else (results[0] if results else None)
    return {
        "status": "SIGNAL" if qualified else (primary or {}).get("status", "NO_TRADE"),
        "scanned": results,
        "card": paper_state.card(now=at),
        "disclaimer": DISCLAIMER,
    }


@app.post("/api/paper/settle")
def paper_settle() -> dict[str, Any]:
    """Settle every due paper signal from verified candles (spec §6)."""
    receipt = outcomes.resolve_due()
    if ingestion.scanner:
        legacy = tracker.evaluate_open_signals()
        receipt["registry"] = legacy
    receipt["summary"] = ledger.summary()
    receipt["disclaimer"] = DISCLAIMER
    return receipt


@app.get("/api/paper/summary")
def paper_summary() -> dict[str, Any]:
    """Spec §7 — the LIVE PAPER TRADING panel payload."""
    payload = paper_state.panel(feed=ingestion.feed)
    payload["scanner"] = ingestion.status()
    return payload


@app.get("/api/paper/card")
def paper_card() -> dict[str, Any]:
    """Spec §8 — the real-time card (qualified paper signal, or NO TRADE + reason)."""
    return paper_state.card()


@app.get("/api/paper/ledger")
def paper_ledger(limit: int = Query(default=100, ge=1, le=1000),
                 day_ist: str | None = None, asset: str | None = None,
                 statuses: str | None = None) -> dict[str, Any]:
    """Spec §5 — the permanent paper-trading ledger."""
    selected = [s.strip().upper() for s in (statuses or "").replace(" ", "").split(",") if s.strip()] or None
    rows = ledger.rows(limit=limit, day_ist=day_ist, asset=asset, statuses=selected)
    return {
        "rows": rows,
        "summary": ledger.summary(day_ist=day_ist),
        "locked_attempts": ledger.locked_attempts(),
        "evidence": ledger.evidence(),
        "statuses": list(ledger.LEDGER_STATUSES),
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/research/paper-evidence")
def research_paper_evidence() -> dict[str, Any]:
    """Spec §12 — minimum-evidence tracker (the published rule, applied to the ledger)."""
    payload = ledger.evidence()
    payload["summary"] = ledger.summary()
    payload["disclaimer"] = DISCLAIMER
    return payload


@app.post("/api/research/paper-report")
def research_paper_report() -> dict[str, Any]:
    """Spec §15 — write PAPER_TRADING_REPORT.md from the ledger and the feeds."""
    from .research.paper_report import write_report

    path, payload = write_report()
    return {"path": str(path), "report": payload, "disclaimer": DISCLAIMER}


@app.get("/api/model/status")
def model_status() -> dict[str, Any]:
    return get_model_service().status()


@app.get("/api/model/metrics")
def model_metrics() -> dict[str, Any]:
    return get_model_service().metrics()


@app.post("/api/model/reload")
def model_reload() -> dict[str, Any]:
    reset_model_service()
    reset_engine()
    clear_study_cache()
    return {"reloaded": True, "status": get_model_service().status()}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    payload = public_config()
    payload["execution_modes"] = available_modes()
    payload["scheduler"] = scheduler.status()
    payload["disclaimer"] = DISCLAIMER
    return payload


@app.post("/api/config")
def post_config(patch: ConfigPatch) -> dict[str, Any]:
    updates = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not updates:
        return {"updated": {}, "errors": [], "settings": public_config()}
    result = update_settings(updates)
    if not result.get("errors"):
        clear_study_cache()
    if any(k in updates for k in ("scheduler_enabled",)):
        if get_settings().scheduler_enabled:
            scheduler.start()
        else:
            scheduler.stop()
    result["scheduler"] = scheduler.status()
    return result


# ----------------------------------------------------------------- operational summary
_SYSTEM_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_SYSTEM_CACHE_SECONDS = 60.0


def _feed_summary() -> dict[str, Any]:
    """Newest verified candle + freshness verdicts across the enabled universe (spec §4)."""
    try:
        live = ingestion.live_status(None)
    except Exception as exc:  # noqa: BLE001 - the summary must never break the endpoint
        return {"status": "UNAVAILABLE", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
    counts = live.get("counts") or {}
    worst = "UNAVAILABLE"
    for state in ("INVALID", "STALE", "FRESH"):
        if counts.get(state):
            worst = state if state != "FRESH" else ("FRESH" if worst == "UNAVAILABLE" else worst)
    assets = live.get("assets") or []
    dated = [a for a in assets if a.get("latest_candle")]
    newest = max(dated, key=lambda a: str(a["latest_candle"]["close_time_utc"]), default=None)
    return {
        "status": worst,
        "counts": counts,
        "worst_asset": worst,
        "newest_asset": (newest or {}).get("asset"),
        "latest_candle_utc": ((newest or {}).get("latest_candle") or {}).get("close_time_utc"),
        "latest_candle_ist": None,
        "age_seconds": (newest or {}).get("age_seconds"),
        "budgets_seconds": live.get("budgets_seconds"),
        "generated_at_utc": live.get("generated_at_utc"),
    }


def _paper_summary() -> dict[str, Any]:
    """Paper-trading + evidence state (spec §9, §10, §11)."""
    evidence = ledger.evidence()
    summary = ledger.summary(day_ist=None)
    return {
        "mode": "PAPER" if get_settings().mode == "PAPER" else get_settings().mode,
        "signals": evidence.get("qualified_signals"),
        "decided": evidence.get("total_decided_trades"),
        "wins": evidence.get("wins"),
        "losses": evidence.get("losses"),
        "pending": evidence.get("pending_trades"),
        "unresolved": evidence.get("unresolved_trades"),
        "coverage": evidence.get("coverage"),
        "no_trade_rate": evidence.get("no_trade_rate"),
        "wilson_95_lower_bound": evidence.get("wilson_95_lower_bound"),
        "max_losing_streak": evidence.get("max_losing_streak"),
        "collection_days": evidence.get("days"),
        "assets": evidence.get("assets"),
        "scans_recorded": evidence.get("scans_recorded"),
        "lock_attempts": evidence.get("locked_attempts"),
        "evidence_bar": evidence.get("evidence_bar"),
        "evidence_reasons": evidence.get("reasons"),
        "today": {"signals": summary.get("signals_today"), "decided": summary.get("decided_today"),
                  "pending": summary.get("pending"), "wins": summary.get("wins"),
                  "losses": summary.get("losses"), "unresolved": summary.get("unresolved"),
                  "locked": summary.get("locked"), "rejected": summary.get("rejected")},
    }


def _research_summary() -> dict[str, Any]:
    """Research state read from the stored artefacts (never recomputed, never invented)."""
    report: dict[str, Any] = {}
    audit: dict[str, Any] = {}
    decision = {"classification": None, "generated_at_utc": None}
    research_json = PROJECT_ROOT / "docs" / "research_report.json"
    audit_json = PROJECT_ROOT / "GATE_AUDIT.json"
    decision_md = PROJECT_ROOT / "RESEARCH_DECISION.md"
    if research_json.exists():
        try:
            report = json.loads(research_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
    if audit_json.exists():
        try:
            audit = json.loads(audit_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            audit = {}
    if decision_md.exists():
        try:
            text = decision_md.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("- classification:"):
                    decision["classification"] = line.split("**")[1] if "**" in line else line.split(":", 1)[1].strip()
                if line.startswith("- generated:"):
                    decision["generated_at_utc"] = line.split(":", 1)[1].strip()
        except OSError:
            pass
    cells = report.get("cells") or []
    qualified = [c for c in cells if c.get("threshold_selected") or c.get("qualifies")]
    window = audit.get("window") or {}
    oracle = audit.get("oracle_feasibility") or {}
    return {
        "experiments": report.get("experiments"),
        "cells": len(cells) or None,
        "qualified_cells": len(qualified) if cells else report.get("threshold_selected_cells"),
        "unqualified_cells": (len(cells) - len(qualified)) if cells else None,
        "threshold_selected_cells": report.get("threshold_selected_cells"),
        "max_signals_any_threshold": report.get("max_signals_any_threshold"),
        "best_wilson_lower_bound": report.get("best_wilson_lower_bound"),
        "evidence_bar": report.get("evidence_bar"),
        "gate_audit": {
            "minutes": window.get("minutes"),
            "window_start_utc": window.get("start_utc"),
            "window_end_utc": window.get("end_utc"),
            "production_signals": (audit.get("status_counts") or {}).get("SIGNAL", 0),
            "oracle_verdict": oracle.get("verdict"),
            "oracle_signals": oracle.get("oracle_signals"),
            "oracle_applicable": oracle.get("applicable"),
            "generated_at_utc": audit.get("generated_at_utc"),
        },
        "classification": decision["classification"],
        "decision_generated_at_utc": decision["generated_at_utc"],
        "conclusion": ("NO QUALIFIED EDGE HAS BEEN DEMONSTRATED — the production gate stack is "
                       "unchanged and every research variant stays available for future study."),
        "note": ("Artefacts are read as written; this endpoint never re-runs research and never "
                 "promotes a variant."),
    }


#: External producers that push *ticks* need one long-lived M1 builder per (provider,
#: realtime/simulated/replay) combination — otherwise every request would start a fresh
#: forming candle and no candle would ever close. Candles are unaffected (they are
#: complete on arrival), so this cache only matters for the tick path.
_EXTERNAL_FEEDS: dict[tuple[str, bool, bool, bool], Any] = {}


def _external_feed(provider: str, *, source_label: str, realtime: bool, simulated: bool,
                   replay: bool):
    from .data.feed import ContinuousFeed

    key = (provider, bool(realtime), bool(simulated), bool(replay))
    feed = _EXTERNAL_FEEDS.get(key)
    if feed is None:
        feed = ContinuousFeed(provider=provider, source_label=source_label, realtime=realtime,
                              is_simulated=simulated, is_replay=replay)
        _EXTERNAL_FEEDS[key] = feed
    return feed


def _live_feed_brief() -> dict[str, Any]:
    """One-line LIVE FEED state for /api/health — never raises, never guesses."""
    try:
        state = feed_state.live_feed_state()
    except Exception as exc:  # noqa: BLE001 - health must stay answerable
        return {"state": "UNKNOWN", "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {
        "state": state["state"],
        "data": state["data"],
        "detail": state["detail"],
        "provider": state["provider"],
        "latest_candle_utc": state["latest_candle_utc"],
        "latest_candle_ist": state["latest_candle_ist"],
        "age_seconds": state["age_seconds"],
        "assets_streaming": state["assets_streaming"],
    }


def _readiness(model_state: str, telegram: dict[str, Any], feed: dict[str, Any],
               paper: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    """Spec §25 — the one-line status summary the UI and the operator both read."""
    if telegram.get("configured") and telegram.get("enabled"):
        telegram_state = "CONFIGURED"
    elif telegram.get("configured"):
        telegram_state = "BLOCKED"
    else:
        telegram_state = "NOT_CONFIGURED"
    live_feed = feed.get("live_feed") or {}
    state = {
        "system": "READY",
        "data": feed.get("status") or "UNAVAILABLE",
        "live_feed": live_feed.get("state") or "NOT_CONFIGURED",
        "model": model_state,
        "paper": "ACTIVE" if paper.get("mode") == "PAPER" else "BLOCKED",
        "telegram": telegram_state,
        "evidence": paper.get("evidence_bar") or "NOT PASSED",
        "classification": research.get("classification"),
    }
    return {
        "state": state,
        "notes": [
            "READY_MARKET means the artifact was trained on operator-supplied real market data — "
            "it is NOT a profitability claim.",
            "A production signal requires REAL + FRESH + READY_MARKET + valid features + MODEL + "
            "ANOMALY + REGIME + CONSISTENCY + CONFIDENCE + RISK + LOCK; if any of them fails the "
            "answer is NO TRADE.",
            "Telegram additionally requires QUALIFIED + PAPER + UNLOCKED and is off by default.",
            "LIVE FEED FRESH means a realtime candle closed inside the 120 s budget; "
            "NOT_CONFIGURED / CONNECTED / DISCONNECTED describe the bridge, not the model.",
        ],
    }


@app.get("/api/system")
def system_status(refresh: bool = Query(default=False, description="bypass the 60 s summary cache")) -> dict[str, Any]:
    """Spec §24 — one complete operational summary.

    environment · mode · data · feed freshness · paper · telegram · scheduler · research ·
    latest candle · active artifact · version, plus the §25 readiness line.
    """
    import time as _time

    settings = get_settings()
    model = get_model_service().status()
    now_monotonic = _time.monotonic()
    cached = _SYSTEM_CACHE["value"]
    if cached is None or refresh or (now_monotonic - float(_SYSTEM_CACHE["at"])) > _SYSTEM_CACHE_SECONDS:
        feed = _feed_summary()
        if feed.get("latest_candle_utc"):
            try:
                feed["latest_candle_ist"] = ist_iso(datetime.fromisoformat(feed["latest_candle_utc"]))
            except (TypeError, ValueError):
                feed["latest_candle_ist"] = None
        telegram = {
            "enabled": settings.telegram_enabled,
            "configured": telegram_credentials()["configured"],
            "allow_simulated": settings.telegram_allow_simulated,
        }
        paper = _paper_summary()
        research = _research_summary()
        feed["live_feed"] = _live_feed_brief()
        cached = {"feed": feed, "telegram": telegram, "paper": paper, "research": research}
        _SYSTEM_CACHE.update({"at": now_monotonic, "value": cached})

    feed = cached["feed"]
    telegram = cached["telegram"]
    paper = cached["paper"]
    research = cached["research"]
    return {
        "app": "Quotex Signal AI",
        "version": __version__,
        "time": _time_payload(),
        "session": get_engine().session(),
        "environment": settings.environment,
        "mode": settings.mode,
        "paper_trading": settings.mode == "PAPER",
        "data": _data_status(),
        "feed": feed,
        "live_feed": feed.get("live_feed") or _live_feed_brief(),
        "model": model,
        "active_artifact": {"dir": model.get("artifact_dir"), "version": model.get("version"),
                            "state": model.get("state"), "market_ready": model.get("market_ready")},
        "paper": paper,
        "telegram": telegram,
        "scheduler": scheduler.status(),
        "research": research,
        "readiness": _readiness(str(model.get("state")), telegram, feed, paper, research),
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/events")
def events(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"events": [dict(r) for r in rows]}


# --------------------------------------------------------------- static preview
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/preview", StaticFiles(directory=str(_STATIC_DIR), html=True), name="preview")


@app.get("/")
def root(request: Request):
    """Browser gets the dashboard, an API client gets the JSON index.

    The single page lives at `/preview/`; a human (or the sandbox preview frame) opening the
    port root is redirected to it via content negotiation instead of being shown raw JSON.
    Programmatic callers — curl, the probe tools, tests — send no `text/html` accept header and
    keep the machine-readable index they have always had.
    """
    index = _STATIC_DIR / "index.html"
    wants_html = "text/html" in (request.headers.get("accept") or "")
    if wants_html and index.exists():
        return FileResponse(index, media_type="text/html")
    return {
        "app": "Quotex Signal AI",
        "version": __version__,
        "docs": "/docs",
        "preview": "/preview" if _STATIC_DIR.exists() else None,
        "endpoints": [
            "GET /api/health", "GET /api/time/ist", "GET /api/assets", "GET /api/market/{asset}",
            "POST /api/signal/generate", "GET /api/signal/latest", "GET /api/signals/history",
            "GET /api/signals/performance", "GET /api/signals/scans", "GET /api/signals/daily",
            "GET /api/signals/study/{asset}", "POST /api/signals/evaluate", "GET /api/model/status",
            "GET /api/model/metrics", "POST /api/model/reload", "GET /api/config", "POST /api/config",
            "GET /api/system", "GET /api/events",
        ],
        "disclaimer": DISCLAIMER,
    }
