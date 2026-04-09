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
RUNNING_AGE_SECONDS = 300
IDLE_AGE_SECONDS = 1800
RECENT_ACTIVITY_LOOKBACK_SECONDS = IDLE_AGE_SECONDS
MAX_STEP_DURATION_SECONDS = 3600


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
    activity_mtime: float = 0.0  # freshest agent session activity (events.jsonl)
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

    # Time-based classification using freshest activity signal.
    # activity_mtime tracks agent session events.jsonl writes which happen
    # continuously during LLM calls, so it catches steps that are genuinely
    # running but haven't updated state.json recently.
    now = datetime.now(timezone.utc).timestamp()
    freshest = max(session.state_mtime, session.activity_mtime)
    age_seconds = now - freshest

    if age_seconds < RUNNING_AGE_SECONDS:  # 5 minutes
        return "running"
    elif age_seconds < IDLE_AGE_SECONDS:  # 30 minutes
        return "idle"
    else:
        return "stalled"


def _agent_sessions_dir(session: RecipeSession, projects_dir: Path) -> Path | None:
    """Derive the agent sessions directory for a recipe session.

    Amplifier maps project working-directory paths to project slugs by
    replacing ``/`` with ``-``, e.g.::

        /Users/sam/repo/my-project  →  -Users-sam-repo-my-project

    Agent sessions live at ``<projects_dir>/<slug>/sessions/``.

    The slug MUST be derived from ``project_path`` (which includes the
    leading ``-`` from the root ``/``).  The ``project_slug`` field on the
    session comes from the *recipe-sessions* directory tree which omits the
    leading dash and therefore does not match the agent-sessions directory.
    We fall back to ``project_slug`` only when ``project_path`` is absent
    (older state files).
    """
    if session.project_path:
        slug = session.project_path.replace("/", "-")
    elif session.project_slug:
        slug = session.project_slug
    else:
        return None
    sessions_dir = projects_dir / slug / "sessions"
    return sessions_dir if sessions_dir.is_dir() else None


def _freshest_agent_activity(sessions_dir: Path | None, cutoff: float) -> float:
    """Return the most recent ``events.jsonl`` mtime under *sessions_dir*.

    The freshness signal comes from ``events.jsonl`` itself, not the parent
    session directory. A directory mtime only changes when entries are created,
    removed, or renamed, so ongoing writes to ``events.jsonl`` do not refresh
    it. Returns ``0.0`` when no recent activity is found.
    """
    if sessions_dir is None:
        return 0.0

    best = 0.0
    try:
        with os.scandir(sessions_dir) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                events_path = Path(entry.path) / "events.jsonl"
                try:
                    mtime = events_path.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                if mtime > best:
                    best = mtime
    except OSError:
        pass
    return best


def _enrich_activity_mtime(sessions: list[RecipeSession], projects_dir: Path) -> None:
    """Set ``activity_mtime`` on sessions from agent events.jsonl files.

    Scans the project-level ``sessions/`` directory (where Amplifier agent
    sessions write ``events.jsonl``) and caches the result per project so
    the filesystem scan happens at most once per project per poll cycle.

    A plausibility guard prevents unrelated sessions (e.g. an interactive
    Amplifier session) from boosting old recipe sessions: agent activity is
    only attributed to a recipe session when the gap between the activity
    and the recipe's last ``state.json`` write is within
    ``MAX_STEP_DURATION_SECONDS``.  This stops a 5-day-old recipe from appearing
    active just because a new session started in the same project directory.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - RECENT_ACTIVITY_LOOKBACK_SECONDS

    # Cache per project_path so we scan each project's sessions dir only once.
    project_activity: dict[str, float] = {}

    for s in sessions:
        key = s.project_path or s.project_slug
        if not key:
            continue
        if key not in project_activity:
            sdir = _agent_sessions_dir(s, projects_dir)
            project_activity[key] = _freshest_agent_activity(sdir, cutoff)

        activity = project_activity[key]
        if activity <= s.activity_mtime:
            continue

        # Plausibility guard: only attribute this activity to the recipe
        # session if it falls within a reasonable window of the last
        # state.json write.  A gap of hours/days means the activity is
        # from an unrelated session that happens to share the same project.
        if activity > s.state_mtime and (activity - s.state_mtime) > MAX_STEP_DURATION_SECONDS:
            continue

        s.activity_mtime = activity
        # Reclassify — the freshest signal may change idle → running
        s.status = classify_status(s)


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
    base_dir = projects_dir or DEFAULT_PROJECTS_DIR
    recipe_base = base_dir / "{project}" / "recipe-sessions"
    if not recipe_base.is_dir():
        return []

    sessions: list[RecipeSession] = []
    for slug_dir in sorted(recipe_base.iterdir()):
        inner = slug_dir / "recipe-sessions"
        if not inner.is_dir():
            continue
        for session_dir in sorted(inner.iterdir(), reverse=True):
            session = load_session(session_dir, project_slug=slug_dir.name)
            if session:
                sessions.append(session)

    # Enrich with agent session activity before linking/propagation.
    # This checks events.jsonl in the project's sessions/ directory —
    # agent sessions write events.jsonl continuously during LLM calls,
    # which is a much fresher signal than recipe state.json (step-level).
    _enrich_activity_mtime(sessions, base_dir)

    # Link parent↔child and propagate status
    _link_parent_child(sessions)

    sessions.sort(key=lambda s: s.started, reverse=True)
    return sessions
