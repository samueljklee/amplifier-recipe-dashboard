"""Flask app serving the recipe dashboard REST API and web UI."""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, Flask, jsonify, render_template, request

from .git_tracker import get_commits_since, match_tasks_to_commits
from .plan_parser import parse_plan
from .session_scanner import RecipeSession, scan_all_sessions

logger = logging.getLogger(__name__)

# Module-level state for background refresh
_sessions: list[RecipeSession] = []
_sessions_lock = threading.Lock()
_projects_dir: Path | None = None

# Template variable pattern: {{variable_name}}
_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _refresh_sessions() -> None:
    """Re-scan all recipe sessions from disk."""
    global _sessions
    new_sessions = scan_all_sessions(_projects_dir)
    with _sessions_lock:
        _sessions = new_sessions


def _background_refresh(interval: float = 15.0) -> None:
    """Background thread: refresh sessions periodically."""
    while True:
        try:
            _refresh_sessions()
        except Exception:
            logger.exception("Error refreshing sessions")
        time.sleep(interval)


def _get_sessions() -> list[RecipeSession]:
    """Thread-safe accessor for current session list."""
    with _sessions_lock:
        return list(_sessions)


def _find_session(session_id: str) -> RecipeSession | None:
    """Find a session by ID (prefix match supported)."""
    for s in _get_sessions():
        if s.session_id == session_id or s.session_id.startswith(session_id):
            return s
    return None


# Keys to exclude from context summary (internal/noisy)
_CONTEXT_SKIP_KEYS = {"recipe", "session", "step", "stage", "_approval_message"}


def _summarize_context(ctx: dict) -> dict:
    """Extract meaningful context values. No truncation -- frontend handles that."""
    summary = {}
    for k, v in ctx.items():
        if k in _CONTEXT_SKIP_KEYS:
            continue
        if isinstance(v, str):
            summary[k] = v
        elif isinstance(v, bool):
            summary[k] = v
        elif isinstance(v, (int, float)):
            summary[k] = v
        elif isinstance(v, list):
            summary[k] = _summarize_list(v)
        elif isinstance(v, dict):
            summary[k] = _summarize_dict(v)
        else:
            summary[k] = str(v)
    return summary


def _summarize_list(items: list) -> str:
    """Serialize a list for display. No truncation."""
    if len(items) == 0:
        return "[]"
    if all(isinstance(i, (str, int, float, bool)) for i in items):
        return "\n".join(str(i) for i in items)
    parts = []
    for item in items:
        if isinstance(item, dict):
            compact = ", ".join(f"{k}: {v}" for k, v in item.items())
            parts.append(f"{{{compact}}}")
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _summarize_dict(d: dict) -> str:
    """Serialize a dict for display. No truncation."""
    if len(d) == 0:
        return "{}"
    return "\n".join(f"{k}: {v}" for k, v in d.items())


def _format_completed_tasks(tasks: object) -> list[dict]:
    """Format completed_tasks list for the API."""
    if not isinstance(tasks, list):
        return []
    result = []
    for i, task in enumerate(tasks):
        if isinstance(task, str):
            title = _extract_task_title(task)
            result.append({"index": i, "title": title, "report": task})
        elif isinstance(task, dict):
            result.append(
                {
                    "index": i,
                    "title": str(task.get("title", f"Task {i + 1}")),
                    "report": str(task.get("report", str(task))),
                }
            )
    return result


def _extract_task_title(markdown: str) -> str:
    """Extract title from first ## heading in a task report."""
    m = re.search(
        r"^#{1,3}\s+(?:Task\s+(?:Complete|Done)[:\s]*)?\s*(.+)",
        markdown,
        re.MULTILINE,
    )
    return m.group(1).strip() if m else f"Task report ({len(markdown)} chars)"


def _format_step_output(context: dict, output_key: str) -> str | None:
    """Get the value of a step's output from the context, formatted for display."""
    if not output_key or output_key not in context:
        return None
    val = context[output_key]
    if isinstance(val, str):
        return val
    if isinstance(val, (bool, int, float)):
        return str(val)
    if isinstance(val, list):
        return _summarize_list(val)
    if isinstance(val, dict):
        return _summarize_dict(val)
    return str(val)


def _resolve_template_vars(description: str, context: dict) -> dict:
    """Extract {{var}} references from a description and resolve their values."""
    if not description:
        return {}
    resolved = {}
    for match in _TEMPLATE_VAR_RE.finditer(description):
        var_name = match.group(1)
        if var_name in context and var_name not in _CONTEXT_SKIP_KEYS:
            val = context[var_name]
            if isinstance(val, str):
                resolved[var_name] = val
            elif isinstance(val, (bool, int, float)):
                resolved[var_name] = str(val)
            elif isinstance(val, list):
                resolved[var_name] = _summarize_list(val)
            elif isinstance(val, dict):
                resolved[var_name] = _summarize_dict(val)
            else:
                resolved[var_name] = str(val)
    return resolved


def _build_step_list(s: RecipeSession) -> list[dict]:
    """Build enriched step list with skipped detection."""
    completed = set(s.completed_steps)
    # Find the highest-index completed step to detect skips
    max_completed_idx = -1
    for rs in s.recipe_steps:
        if rs.id in completed:
            max_completed_idx = max(max_completed_idx, rs.index)

    steps = []
    for rs in s.recipe_steps:
        is_completed = rs.id in completed
        # A step is skipped if: it has a condition, it's not completed,
        # but a later step IS completed (meaning the engine evaluated and skipped it)
        is_skipped = not is_completed and rs.condition and rs.index < max_completed_idx

        steps.append(
            {
                "id": rs.id,
                "type": rs.step_type,
                "index": rs.index,
                "output_key": rs.output_key,
                "description": rs.description,
                "condition": rs.condition,
                "output_value": _format_step_output(s.context, rs.output_key),
                "completed": is_completed,
                "skipped": is_skipped,
                "resolved_variables": _resolve_template_vars(
                    rs.description + " " + rs.condition, s.context
                ),
            }
        )
    return steps


def _session_to_dict(s: RecipeSession) -> dict:
    """Convert RecipeSession to API-friendly dict."""
    # Extract parent session info from context if available
    session_ctx = s.context.get("session", {})
    parent_id = session_ctx.get("parent_id", "") if isinstance(session_ctx, dict) else ""

    # Extract rich context fields for the frontend
    recipe_ctx = s.context.get("recipe", {})
    recipe_desc = recipe_ctx.get("description", "") if isinstance(recipe_ctx, dict) else ""
    stage_ctx = s.context.get("stage", {})
    current_stage = stage_ctx.get("name", "") if isinstance(stage_ctx, dict) else ""

    return {
        "session_id": s.session_id,
        "recipe_name": s.recipe_name,
        "recipe_version": s.recipe_version,
        "started": s.started,
        "status": s.status,
        "project_slug": s.project_slug,
        "project_path": s.project_path,
        "completed_steps": s.completed_steps,
        "completed_stages": s.completed_stages,
        "total_steps": s.total_steps,
        "is_staged": s.is_staged,
        "progress": s.progress_fraction,
        "plan_path": s.plan_path,
        "working_dir": s.working_dir,
        "parent_id": parent_id,
        "session_dir": str(s.session_dir),
        "context_summary": _summarize_context(s.context),
        # Phase 1: status-related fields
        "cancellation_status": s.cancellation_status,
        "pending_approval_stage": s.pending_approval_stage,
        "pending_approval_prompt": s.pending_approval_prompt,
        "approval_history": s.approval_history,
        "stage_approvals": s.stage_approvals,
        # Phase 1: enrichment fields
        "recipe_description": recipe_desc,
        "current_stage": current_stage,
        # Phase 2: completed tasks
        "completed_tasks": _format_completed_tasks(s.context.get("completed_tasks", [])),
        "execution_summary": s.context.get("execution_summary", ""),
        "final_review": s.context.get("final_review", ""),
        "verification_results": s.context.get("verification_results", ""),
        "approval_prep": s.context.get("approval_prep", ""),
        "completion_report": s.context.get("completion_report", ""),
        "recipe_steps": _build_step_list(s),
    }


def _parse_since(since_str: str | None) -> datetime | None:
    """Parse a 'since' param like '1d', '7d', '30d' into a datetime."""
    if not since_str:
        return None
    try:
        if since_str.endswith("d"):
            days = int(since_str[:-1])
            return datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)
        if since_str.endswith("h"):
            hours = int(since_str[:-1])
            return datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=hours)
    except (ValueError, TypeError):
        return None
    return None


def create_app(projects_dir: Path | None = None) -> Flask:
    """Create and configure the Flask application."""
    global _projects_dir
    _projects_dir = projects_dir

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )

    bp = Blueprint("dashboard", __name__)

    @bp.route("/")
    def index():
        return render_template("index.html")

    @bp.route("/api/projects")
    def api_projects():
        """List unique project slugs with session counts, respecting time filter."""
        sessions = _get_sessions()
        since = _parse_since(request.args.get("since"))
        if since:
            since_iso = since.isoformat()
            sessions = [s for s in sessions if s.started >= since_iso]
        projects: dict[str, int] = {}
        for s in sessions:
            slug = s.project_slug or "unknown"
            projects[slug] = projects.get(slug, 0) + 1

        def _short_name(slug: str) -> str:
            """'Users-jane-repo-myproject' -> 'repo-myproject'
            Keep hyphens as-is since we can't distinguish path separators
            from hyphens in directory names."""
            # Strip 'Users-<username>-' prefix, keep the rest verbatim
            cleaned = re.sub(r"^Users-[^-]+-", "", slug)
            return cleaned or slug

        project_list = [
            {"slug": slug, "count": count, "short_name": _short_name(slug)}
            for slug, count in projects.items()
        ]
        project_list.sort(key=lambda p: p["short_name"].lower())
        return jsonify({"projects": project_list})

    @bp.route("/api/sessions")
    def api_sessions():
        """List all recipe sessions, most recent first."""
        sessions = _get_sessions()
        # Optional filter by project slug
        project = request.args.get("project")
        if project:
            sessions = [s for s in sessions if project in s.project_slug]
        # Optional filter by status
        status = request.args.get("status")
        if status:
            sessions = [s for s in sessions if s.status == status]
        # Optional time filter
        since = _parse_since(request.args.get("since"))
        if since:
            since_iso = since.isoformat()
            sessions = [s for s in sessions if s.started >= since_iso]
        return jsonify(
            {
                "sessions": [_session_to_dict(s) for s in sessions],
                "count": len(sessions),
            }
        )

    @bp.route("/api/session/<session_id>")
    def api_session_detail(session_id: str):
        """Get detailed state for a single session."""
        session = _find_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        return jsonify(_session_to_dict(session))

    @bp.route("/api/session/<session_id>/tasks")
    def api_session_tasks(session_id: str):
        """Get task-level progress for a session with a plan file."""
        session = _find_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        plan_path = session.plan_path
        if not plan_path:
            return jsonify({"tasks": [], "info": "This recipe does not use a plan file."})

        tasks = parse_plan(plan_path)
        if not tasks:
            return jsonify({"error": f"No tasks found in {plan_path}", "tasks": []})

        # Get git commits
        working_dir = session.working_dir
        commits = []
        if working_dir:
            commits = get_commits_since(working_dir, since=session.started)

        task_commits = match_tasks_to_commits(commits, len(tasks))

        # Build task list with status
        task_list = []
        last_done_idx = -1
        for task in tasks:
            commit = task_commits.get(task.number)
            if commit:
                last_done_idx = task.number

        for task in tasks:
            commit = task_commits.get(task.number)
            if commit:
                status = "done"
            elif task.number == last_done_idx + 1:
                status = "active"
            else:
                status = "pending"

            task_list.append(
                {
                    "number": task.number,
                    "description": task.description,
                    "status": status,
                    "commit_hash": commit.hash if commit else None,
                    "commit_subject": commit.subject if commit else None,
                    "commit_time": commit.timestamp if commit else None,
                }
            )

        done_count = sum(1 for t in task_list if t["status"] == "done")
        return jsonify(
            {
                "tasks": task_list,
                "total": len(task_list),
                "done": done_count,
                "progress": done_count / len(task_list) if task_list else 0,
                "plan_path": plan_path,
                "recent_commits": [
                    {"hash": c.hash, "subject": c.subject, "timestamp": c.timestamp}
                    for c in commits[:10]
                ],
            }
        )

    @bp.route("/api/refresh", methods=["POST"])
    def api_refresh():
        """Force an immediate session rescan."""
        _refresh_sessions()
        return jsonify({"status": "refreshed", "count": len(_get_sessions())})

    app.register_blueprint(bp)

    # Initial scan
    _refresh_sessions()

    # Start background refresh thread
    t = threading.Thread(target=_background_refresh, daemon=True)
    t.start()

    return app
