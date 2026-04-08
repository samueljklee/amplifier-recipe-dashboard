"""Tests for amplifier_recipe_dashboard.settings."""

from __future__ import annotations

from pathlib import Path

from amplifier_recipe_dashboard.settings import (
    DEFAULT_SETTINGS,
    load_settings,
    patch_settings,
    save_settings,
)

# ---------------------------------------------------------------------------
# load_settings defaults
# ---------------------------------------------------------------------------


def test_load_settings_returns_defaults_when_no_file(tmp_config_dir: Path):
    result = load_settings()
    assert result == DEFAULT_SETTINGS


# ---------------------------------------------------------------------------
# save / load roundtrip
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_config_dir: Path):
    custom = {**DEFAULT_SETTINGS, "port": 9999, "host": "0.0.0.0"}
    save_settings(custom)
    loaded = load_settings()
    assert loaded["port"] == 9999
    assert loaded["host"] == "0.0.0.0"


# ---------------------------------------------------------------------------
# patch_settings
# ---------------------------------------------------------------------------


def test_patch_settings_updates_specified_keys(tmp_config_dir: Path):
    result = patch_settings({"port": 7777})
    assert result["port"] == 7777
    # Other keys remain at default
    assert result["host"] == DEFAULT_SETTINGS["host"]


def test_patch_settings_ignores_unknown_keys(tmp_config_dir: Path):
    result = patch_settings({"port": 8080, "unknown_key": "should-be-ignored"})
    assert result["port"] == 8080
    assert "unknown_key" not in result


# ---------------------------------------------------------------------------
# Type coercion / mixed types
# ---------------------------------------------------------------------------


def test_type_coercion_bool(tmp_config_dir: Path):
    result = patch_settings({"auto_open": False})
    assert result["auto_open"] is False
    loaded = load_settings()
    assert loaded["auto_open"] is False


def test_type_coercion_int(tmp_config_dir: Path):
    result = patch_settings({"refresh_interval": 60})
    assert result["refresh_interval"] == 60
    loaded = load_settings()
    assert loaded["refresh_interval"] == 60


def test_type_coercion_string(tmp_config_dir: Path):
    result = patch_settings({"device_name": "my-server"})
    assert result["device_name"] == "my-server"
    loaded = load_settings()
    assert loaded["device_name"] == "my-server"


# ---------------------------------------------------------------------------
# Reset to defaults
# ---------------------------------------------------------------------------


def test_reset_to_defaults(tmp_config_dir: Path):
    patch_settings({"port": 1234, "auto_open": False})
    save_settings(DEFAULT_SETTINGS)
    assert load_settings() == DEFAULT_SETTINGS
