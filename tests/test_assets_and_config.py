"""Asset validation and configuration guards."""
from __future__ import annotations

import pytest

from backend.constants import ASSET_MAP, canonical_symbol, is_known_asset
from backend.settings_service import validate_updates


def test_canonical_symbol_is_case_insensitive_for_otc_suffix():
    assert canonical_symbol("USDBDT-OTCq") == "USDBDT-OTCq"
    assert canonical_symbol("usdbdt-otcq") == "USDBDT-OTCq"
    assert canonical_symbol("USDBDT-OTCQ") == "USDBDT-OTCq"
    assert canonical_symbol("  eurcad-otcq ") == "EURCAD-OTCq"
    assert canonical_symbol("EURUSD") == "EURUSD"


def test_unknown_symbols_are_rejected():
    assert canonical_symbol("FAKEUSD") is None
    assert not is_known_asset("BTCUSDT")
    assert not is_known_asset("")


def test_asset_universe_contains_requested_pairs():
    for symbol in ("USDBDT-OTCq", "EURCAD-OTCq", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"):
        assert symbol in ASSET_MAP


@pytest.mark.parametrize(
    "patch,should_fail",
    [
        ({"min_confidence": 84}, False),
        ({"min_confidence": 40}, True),
        ({"min_confidence": 120}, True),
        ({"expiry_minutes": 0}, True),
        ({"expiry_minutes": 3}, False),
        ({"mode": "LIVE"}, True),
        ({"mode": "paper"}, False),
        ({"selected_asset": "NOTREAL"}, True),
        ({"selected_asset": "auto"}, False),
        ({"data_source_mode": "magic"}, True),
        ({"enabled_assets": ["EURUSD", "NOPE"]}, True),
        ({"enabled_assets": []}, True),
    ],
)
def test_config_validation(patch, should_fail):
    clean, errors = validate_updates(patch)
    assert bool(errors) is should_fail, (patch, errors)
    if not should_fail:
        assert clean


def test_secrets_cannot_be_set_through_the_api():
    clean, errors = validate_updates({"telegram_bot_token": "123:abc", "chat_id": "999"})
    assert not clean
    assert len(errors) == 2
    assert all("environment" in error for error in errors)


def test_enabled_assets_are_canonicalised():
    clean, errors = validate_updates({"enabled_assets": ["usdbdt-otcq", "eurusd"]})
    assert not errors
    assert clean["enabled_assets"] == ["USDBDT-OTCq", "EURUSD"]
