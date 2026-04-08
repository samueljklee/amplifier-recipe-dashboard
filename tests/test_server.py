"""Tests for amplifier_recipe_dashboard.server — FastAPI routes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """Create a TestClient with an empty projects directory.

    We mock scan_all_sessions so the app doesn't need real Amplifier data.
    """
    with patch(
        "amplifier_recipe_dashboard.server.scan_all_sessions",
        return_value=[],
    ):
        from amplifier_recipe_dashboard.server import create_app

        app = create_app(projects_dir=tmp_path, refresh_interval=999)
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


def test_index_returns_200(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200


def test_index_contains_hostname_in_title(client: TestClient):
    import socket

    hostname = socket.gethostname().split(".")[0]
    resp = client.get("/")
    assert hostname in resp.text


def test_index_contains_favicon_link(client: TestClient):
    resp = client.get("/")
    assert "favicon" in resp.text


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


def test_api_sessions_returns_json_with_sessions_key(client: TestClient):
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "sessions" in data
    assert isinstance(data["sessions"], list)


def test_api_projects_returns_json_with_projects_key(client: TestClient):
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert "projects" in data
    assert isinstance(data["projects"], list)


def test_api_refresh_returns_refreshed(client: TestClient):
    resp = client.post("/api/refresh")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "refreshed"


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


def test_auth_mode_returns_mode(client: TestClient):
    resp = client.get("/auth/mode")
    assert resp.status_code == 200
    assert "mode" in resp.json()


def test_login_page_returns_200(client: TestClient):
    resp = client.get("/login")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/static/app.js", "/static/style.css", "/static/favicon.svg"])
def test_static_files_return_200(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 200
