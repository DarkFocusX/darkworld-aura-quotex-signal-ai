"""Shared pytest fixtures.

Tests never touch the production SQLite file: each test module that needs the
database points `backend.database.db.DB_PATH` at a temporary file.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("DATA_SOURCE_MODE", "simulated")
os.environ.setdefault("ENABLE_TELEGRAM", "false")


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Isolated SQLite database for a test."""
    from backend.database import db as db_module

    db_file = tmp_path / "test_signals.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_file, raising=True)
    db_module.init_db(db_file)
    yield db_file
    db_module.init_db(db_file)


@pytest.fixture()
def settings_simulated(temp_db):
    """Engine settings pinned to the simulated feed with the default threshold."""
    from backend.settings_service import get_settings

    settings = get_settings()
    settings.data_source_mode = "simulated"
    settings.min_confidence = 85.0
    settings.expiry_minutes = 1
    settings.max_signals_per_day = 40
    settings.auto_select = False
    settings.selected_asset = "USDBDT-OTCq"
    settings.enabled_assets = ["USDBDT-OTCq", "EURCAD-OTCq", "EURUSD"]
    return settings


@pytest.fixture()
def model_ready() -> bool:
    from backend.models.service import get_model_service

    return get_model_service().ready


def pytest_configure(config):  # pragma: no cover - informational
    try:
        from backend.models.service import get_model_service

        status = get_model_service().status()
        config.stash  # noqa: B018 - keep reference for clarity
        print(f"\n[conftest] model state: {status['state']} — {status['message'][:120]}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[conftest] model service unavailable: {exc}")
