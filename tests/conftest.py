"""Shared fixtures for amplifier-recipe-dashboard tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all config-dir helpers in auth.py and SETTINGS_PATH in settings.py to tmp_path."""
    config = tmp_path / "config"
    config.mkdir()

    monkeypatch.setattr("amplifier_recipe_dashboard.auth._config_dir", lambda: config)
    monkeypatch.setattr(
        "amplifier_recipe_dashboard.auth.get_password_path", lambda: config / "password"
    )
    monkeypatch.setattr(
        "amplifier_recipe_dashboard.auth.get_secret_path", lambda: config / "secret"
    )
    monkeypatch.setattr(
        "amplifier_recipe_dashboard.settings.SETTINGS_PATH", config / "settings.json"
    )
    return config
