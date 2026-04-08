"""Tests for amplifier_recipe_dashboard.auth — security-critical."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from amplifier_recipe_dashboard.auth import (
    create_session_cookie,
    generate_and_save_password,
    load_or_create_secret,
    pam_available,
    resolve_auth_mode,
    verify_session_cookie,
)

# ---------------------------------------------------------------------------
# Session cookie signing / verification
# ---------------------------------------------------------------------------


def test_create_session_cookie_returns_non_empty_string():
    cookie = create_session_cookie("test-secret")
    assert isinstance(cookie, str)
    assert len(cookie) > 0


def test_verify_session_cookie_valid():
    secret = "test-secret"
    cookie = create_session_cookie(secret)
    assert verify_session_cookie(secret, cookie, ttl_seconds=3600) is True


def test_verify_session_cookie_rejects_garbage():
    assert verify_session_cookie("test-secret", "totally-garbage-cookie", ttl_seconds=3600) is False


def test_verify_session_cookie_rejects_expired():
    secret = "test-secret"
    cookie = create_session_cookie(secret)
    # itsdangerous uses integer timestamps, so we need >1s of integer difference
    time.sleep(2.1)
    assert verify_session_cookie(secret, cookie, ttl_seconds=1) is False


# ---------------------------------------------------------------------------
# resolve_auth_mode
# ---------------------------------------------------------------------------


def test_resolve_auth_mode_none(tmp_config_dir: Path):
    mode, password = resolve_auth_mode("none")
    assert mode == "none"
    assert password == ""


def test_resolve_auth_mode_password_env_var(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "env-pw-123")
    mode, password = resolve_auth_mode("password")
    assert mode == "password"
    assert password == "env-pw-123"


def test_resolve_auth_mode_password_auto_generates(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    mode, password = resolve_auth_mode("password")
    assert mode == "password"
    assert len(password) > 0


# ---------------------------------------------------------------------------
# pam_available
# ---------------------------------------------------------------------------


def test_pam_available_returns_bool():
    result = pam_available()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# load_or_create_secret
# ---------------------------------------------------------------------------


def test_load_or_create_secret_returns_non_empty(tmp_config_dir: Path):
    secret = load_or_create_secret()
    assert isinstance(secret, str)
    assert len(secret) > 0


def test_load_or_create_secret_idempotent(tmp_config_dir: Path):
    first = load_or_create_secret()
    second = load_or_create_secret()
    assert first == second


# ---------------------------------------------------------------------------
# generate_and_save_password
# ---------------------------------------------------------------------------


def test_generate_and_save_password(tmp_config_dir: Path):
    pw = generate_and_save_password()
    assert isinstance(pw, str)
    assert len(pw) > 0
    # Verify the file was actually created
    pw_path = tmp_config_dir / "password"
    assert pw_path.exists()
    assert pw_path.read_text().strip() == pw
