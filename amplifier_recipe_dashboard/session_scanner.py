"""Discover and load recipe session state from the Amplifier filesystem.

Recipe sessions live at:
    ~/.amplifier/projects/{project}/recipe-sessions/<slug>/recipe-sessions/<id>/

Each session directory contains:
    state.json   - session checkpoint (progress, context, approvals)
    recipe.yaml  - copy of the original recipe YAML
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Default base path for recipe sessions
DEFAULT_PROJECTS_DIR = Path(os.path.expanduser("~/.amplifier/projects"))


@dataclass
class RecipeStep:
    """A single step from a recipe YAML."""

    id: str
    step_type: str  # "agent", "bash", "recipe", etc.
    index: int
    output_key: str = ""  # context key where this step's output is stored
    description: str = ""  # prompt preview or command preview
    condition: str = ""  # condition expression (e.g., "{{var}} == 'true'")


@dataclass
class RecipeSession:
    """Parsed recipe session state."""

    session_id: str
    recipe_name: str
    started: str
    project_slug: str
    project_path: str
    completed_steps: list[str] = field(default_factory=list)
    completed_stages: list[str] = field(default_factory=list)
    current_step_index: int = 0
    is_staged: bool = False
    recipe_version: str = ""
    context: dict = field(default_factory=dict)
    total_steps: int = 0
    recipe_steps: list[RecipeStep] = field(default_factory=list)
    session_dir: Path = field(default_factory=lambda: Path("."))
    state_mtime: float = 0.0
    status: str = "unknown"  # running, waiting, idle, done, stalled, cancelled, failed
    cancellation_status: str = ""
    pending_approval_stage: str = ""
    pending_approval_prompt: str = ""
    approval_history: list = field(default_factory=list)
    stage_approvals: dict = field(default_factory=dict)
    parent_session_id: str = ""
    child_session_ids: list[str] = field(default_factory=list)

    @property
    def plan_path(self) -> str | None:
        return self.context.get("plan_path")

    @property
    def working_dir(self) -> str | None:
        return self.context.get("working_dir") or self.project_path

    @property
    def progress_fraction(self) -> float:
        if self.total_steps == 0:
            return 0.0
        # Count skipped steps (conditional, not completed, but later step completed)
        completed = set(self.completed_steps)
        max_completed_idx = -1
        for rs in self.recipe_steps:
            if rs.id in completed:
                max_completed_idx = max(max_completed_idx, rs.index)
        skipped = sum(
            1
            for rs in self.recipe_steps
            if rs.id not in completed and rs.condition and rs.index < max_completed_idx
        )
        return (len(completed) + skipped) / self.total_steps


def classify_status(session: RecipeSession) -> str:
    """Classify session into one of 7 states based on state fields and mtime.

    States: running, waiting, idle, done, stalled, cancelled, failed.
    """
    # Check cancellation first
    if session.cancellation_status and session.cancellation_status != "none":
        return "cancelled"

    # Check if all steps are done or skipped
    # A recipe is "done" when every step is either completed or was skipped
    # (has a condition and a later step completed, meaning the engine evaluated and skipped it)
    if session.total_steps > 0:
        completed = set(session.completed_steps)
        if len(completed) >= session.total_steps:
            return "done"
        # Check if remaining steps were all skipped (conditional + later step completed)
        max_completed_idx = -1
        for rs in session.recipe_steps:
            if rs.id in completed:
                max_completed_idx = max(max_completed_idx, rs.index)
        all_accounted = True
        for rs in session.recipe_steps:
            if rs.id not in completed:
                # Not completed -- is it a skipped conditional step?
                if rs.condition and rs.index < max_completed_idx:
                    continue  # skipped, accounted for
                else:
                    all_accounted = False
                    break
        if all_accounted:
            return "done"

    # Check pending approval
    if session.pending_approval_stage:
        return "waiting"

    # Time-based classification
    now = datetime.now(timezone.utc).timestamp()
    age_seconds = now - session.state_mtime

    if age_seconds < 300:  # 5 minutes
        return "running"
    elif age_seconds < 1800:  # 30 minutes
        return "idle"
    else:
        return "stalled"


def _parse_recipe_yaml(recipe_path: Path) -> list[RecipeStep]:
    """Parse a recipe.yaml to extract step list."""
    try:
        data = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []

    if not isinstance(data, dict):
        return []

    steps = []
    # Handle staged recipes (stages → steps within)
    if "stages" in data:
        for stage in data["stages"]:
            for step in stage.get("steps", []):
                step_id = step.get("id", f"step-{len(steps)}")
                step_type = step.get("type", step.get("agent", "unknown"))
                output_key = step.get("output", "")
                desc = _step_description(step)
                steps.append(
                    RecipeStep(
                        id=step_id,
                        step_type=str(step_type),
                        index=len(steps),
                        output_key=output_key,
                        description=desc,
                        condition=str(step.get("condition", "")),
                    )
                )
    # Handle flat recipes
    elif "steps" in data:
        for i, step in enumerate(data["steps"]):
            step_id = step.get("id", f"step-{i}")
            step_type = step.get("type", step.get("agent", "unknown"))
            output_key = step.get("output", "")
            desc = _step_description(step)
            steps.append(
                RecipeStep(
                    id=step_id,
                    step_type=str(step_type),
                    index=i,
                    output_key=output_key,
                    description=desc,
                    condition=str(step.get("condition", "")),
                )
            )
    return steps


def _step_description(step: dict) -> str:
    """Extract a short description from a step definition."""
    # Try prompt (agent steps), then command (bash steps)
    for key in ("prompt", "command"):
        val = step.get(key, "")
        if isinstance(val, str) and val.strip():
            # First non-empty line, trimmed
            first_line = val.strip().splitlines()[0][:120]
            return first_line
    return ""


def load_session(session_dir: Path, project_slug: str) -> RecipeSession | None:
    """Load a single recipe session from its directory."""
    state_file = session_dir / "state.json"
    recipe_file = session_dir / "recipe.yaml"

    if not state_file.exists():
        return None

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    recipe_steps = _parse_recipe_yaml(recipe_file) if recipe_file.exists() else []

    # Extract approval/cancellation fields (top-level in state.json, not in context)
    cancellation = state.get("cancellation_status", "")
    if isinstance(cancellation, dict):
        cancellation = cancellation.get("status", str(cancellation))

    pending_stage = state.get("pending_approval_stage", "")
    pending_prompt = state.get("pending_approval_prompt", "")
    approval_hist = state.get("approval_history", [])
    stage_apprvls = state.get("stage_approvals", {})

    # Read parent_session_id from multiple possible locations (fallback chain)
    parent_sid = (
        state.get("parent_session_id")  # new canonical top-level field
        or state.get("context", {}).get("parent_session_id")  # legacy context-level
        or ""
    )

    session = RecipeSession(
        session_id=state.get("session_id", session_dir.name),
        recipe_name=state.get("recipe_name", "unknown"),
        started=state.get("started", ""),
        project_slug=project_slug,
        project_path=state.get("project_path", ""),
        completed_steps=state.get("completed_steps", []),
        completed_stages=state.get("completed_stages", []),
        current_step_index=state.get("current_step_index", 0),
        is_staged=state.get("is_staged", False),
        recipe_version=state.get("recipe_version", ""),
        context=state.get("context", {}),
        total_steps=len(recipe_steps) or len(state.get("completed_steps", [])) + 1,
        recipe_steps=recipe_steps,
        session_dir=session_dir,
        state_mtime=state_file.stat().st_mtime,
        cancellation_status=str(cancellation) if cancellation else "",
        pending_approval_stage=str(pending_stage) if pending_stage else "",
        pending_approval_prompt=str(pending_prompt) if pending_prompt else "",
        approval_history=approval_hist if isinstance(approval_hist, list) else [],
        stage_approvals=stage_apprvls if isinstance(stage_apprvls, dict) else {},
        parent_session_id=str(parent_sid) if parent_sid else "",
    )
    session.status = classify_status(session)
    return session


def _link_parent_child(sessions: list[RecipeSession]) -> None:
    """Build parent→child links and propagate status up the tree.

    After this, each parent session's child_session_ids is populated,
    and a parent whose own state.json is stale but has running/waiting
    children will be reclassified as 'running' instead of 'stalled'.
    """
    by_id: dict[str, RecipeSession] = {s.session_id: s for s in sessions}

    # Build child lists
    for s in sessions:
        if s.parent_session_id and s.parent_session_id in by_id:
            by_id[s.parent_session_id].child_session_ids.append(s.session_id)

    # Propagate status: walk children to determine if parent is still active.
    # A parent is "running" if ANY descendant is running or waiting,
    # regardless of the parent's own state_mtime.
    active_statuses = {"running", "waiting", "idle"}

    def _has_active_descendant(session_id: str, visited: set[str]) -> bool:
        """Recursively check if any descendant is active."""
        if session_id in visited:
            return False  # prevent cycles
        visited.add(session_id)
        parent = by_id.get(session_id)
        if not parent:
            return False
        for child_id in parent.child_session_ids:
            child = by_id.get(child_id)
            if not child:
                continue
            if child.status in active_statuses:
                return True
            if _has_active_descendant(child_id, visited):
                return True
        return False

    for s in sessions:
        if s.child_session_ids and s.status in ("stalled", "idle"):
            if _has_active_descendant(s.session_id, set()):
                s.status = "running"


def scan_all_sessions(projects_dir: Path | None = None) -> list[RecipeSession]:
    """Scan all recipe sessions across all projects."""
    base = (projects_dir or DEFAULT_PROJECTS_DIR) / "{project}" / "recipe-sessions"
    if not base.is_dir():
        return []

    sessions: list[RecipeSession] = []
    for slug_dir in sorted(base.iterdir()):
        inner = slug_dir / "recipe-sessions"
        if not inner.is_dir():
            continue
        for session_dir in sorted(inner.iterdir(), reverse=True):
            session = load_session(session_dir, project_slug=slug_dir.name)
            if session:
                sessions.append(session)

    # Link parent↔child and propagate status
    _link_parent_child(sessions)

    sessions.sort(key=lambda s: s.started, reverse=True)
    return sessions
