"""Tests for amplifier_recipe_dashboard.session_scanner."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from amplifier_recipe_dashboard.session_scanner import (
    RecipeSession,
    _agent_sessions_dir,
    _enrich_activity_mtime,
    _freshest_agent_activity,
    classify_status,
)

# ---------------------------------------------------------------------------
# _agent_sessions_dir: slug derivation
# ---------------------------------------------------------------------------


def test_agent_sessions_dir_derives_slug_from_project_path(tmp_path: Path) -> None:
    """project_path produces the leading-dash slug that matches Amplifier's
    project directory convention (``/Users/sam`` → ``-Users-sam``)."""
    slug = "/Users/sam/repo/my-project".replace("/", "-")  # -Users-sam-repo-my-project
    (tmp_path / slug / "sessions").mkdir(parents=True)

    session = RecipeSession(
        session_id="r1",
        recipe_name="demo",
        started="2026-01-01",
        # project_slug intentionally differs — no leading dash
        project_slug="Users-sam-repo-my-project",
        project_path="/Users/sam/repo/my-project",
    )
    result = _agent_sessions_dir(session, tmp_path)
    assert result is not None
    assert result == tmp_path / slug / "sessions"


def test_agent_sessions_dir_falls_back_to_project_slug(tmp_path: Path) -> None:
    """When project_path is empty, fall back to project_slug."""
    (tmp_path / "some-slug" / "sessions").mkdir(parents=True)
    session = RecipeSession(
        session_id="r2",
        recipe_name="demo",
        started="2026-01-01",
        project_slug="some-slug",
        project_path="",
    )
    result = _agent_sessions_dir(session, tmp_path)
    assert result is not None
    assert result == tmp_path / "some-slug" / "sessions"


def test_agent_sessions_dir_returns_none_when_dir_missing(tmp_path: Path) -> None:
    session = RecipeSession(
        session_id="r3",
        recipe_name="demo",
        started="2026-01-01",
        project_slug="nonexistent",
        project_path="/nonexistent/path",
    )
    result = _agent_sessions_dir(session, tmp_path)
    assert result is None


def _write_events_file(path: Path, *, mtime: float) -> None:
    """Create an events.jsonl file with a controlled modification time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_freshest_agent_activity_uses_events_file_mtime_not_session_dir_mtime(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_dir = sessions_dir / "agent-session-1"
    session_dir.mkdir(parents=True)

    cutoff = time.time() - 1800
    fresh_events_mtime = time.time() - 5
    _write_events_file(session_dir / "events.jsonl", mtime=fresh_events_mtime)

    stale_dir_mtime = cutoff - 60
    os.utime(session_dir, (stale_dir_mtime, stale_dir_mtime))

    freshest = _freshest_agent_activity(sessions_dir, cutoff)

    assert freshest == pytest.approx(fresh_events_mtime, abs=1.0)


def test_enrich_activity_mtime_falls_back_to_project_slug_when_project_path_is_missing(
    tmp_path: Path,
) -> None:
    now = time.time()
    project_slug = "-tmp-demo-project"
    activity_mtime = now - 10
    _write_events_file(
        tmp_path / project_slug / "sessions" / "agent-session-1" / "events.jsonl",
        mtime=activity_mtime,
    )

    session = RecipeSession(
        session_id="recipe-1",
        recipe_name="demo",
        started="2026-04-09T12:00:00Z",
        project_slug=project_slug,
        project_path="",
        state_mtime=now - 600,
    )
    session.status = classify_status(session)

    assert session.status == "idle"

    _enrich_activity_mtime([session], tmp_path)

    assert session.activity_mtime == pytest.approx(activity_mtime, abs=1.0)
    assert session.status == "running"
