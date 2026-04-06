"""
Server-side settings management for amplifier-recipe-dashboard.

Settings are stored at ~/.config/amplifier-recipe-dashboard/settings.json.
"""

import copy
import json
from pathlib import Path

SETTINGS_PATH = Path.home() / ".config" / "amplifier-recipe-dashboard" / "settings.json"

DEFAULT_SETTINGS: dict = {
    "host": "127.0.0.1",
    "port": 8181,
    "auto_open": True,
    "refresh_interval": 15,
    "auth": "none",
    "device_name": "",
}


def load_settings() -> dict:
    """Load settings from disk, merging saved values over defaults.

    Returns DEFAULT_SETTINGS if the file does not exist or contains corrupt JSON.
    Unknown keys in the file are ignored.
    """
    result = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        text = SETTINGS_PATH.read_text()
        data = json.loads(text)
        for key in DEFAULT_SETTINGS:
            if key in data:
                result[key] = data[key]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return result


def save_settings(data: dict) -> None:
    """Save settings to disk, merging *data* with defaults first.

    Creates parent directories as needed. Writes JSON with indent=2 and a
    trailing newline.
    """
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in data:
            merged[key] = data[key]
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2) + "\n")


def patch_settings(patch: dict) -> dict:
    """Merge known keys from *patch* into the current settings, save, and return result.

    Unknown keys in *patch* are silently ignored.
    """
    current = load_settings()
    for key in DEFAULT_SETTINGS:
        if key in patch:
            current[key] = patch[key]
    save_settings(current)
    return current
