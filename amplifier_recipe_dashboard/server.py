"""FastAPI app serving the recipe dashboard REST API and web UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .git_tracker import get_commits_since, match_tasks_to_commits
from .plan_parser import parse_plan
from .session_scanner import RecipeSession, scan_all_sessions

logger = logging.getLogger(__name__)

# Module-level state for background refresh
_sessions: list[RecipeSession] = []
_sessions_lock = asyncio.Lock()
_projects_dir: Path | None = None
_refresh_interval: float = 15.0

# Auth state (populated by create_app when auth != "none")
_auth_mode: str = "none"
_auth_password: str = ""
_auth_secret: str = ""
_auth_ttl: int = 604800

# Template variable pattern: {{variable_name}}
_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")

# Background poll task reference
_poll_task: asyncio.Task[None] | None = None

# ---------------------------------------------------------------------------
# Frontend directory
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_HOSTNAME = socket.gethostname().split(".")[0]


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------


def _refresh_sessions() -> list[RecipeSession]:
    """Build a fresh session list from disk. Pure function — caller swaps state.

    Heavy I/O (~10 s for thousands of sessions) — call via asyncio.to_thread,
    then atomically swap into `_sessions` under the lock. Holding the lock
    around the swap (not around the scan) keeps reader latency minimal.
    """
    return scan_all_sessions(_projects_dir)


async def _get_sessions() -> list[RecipeSession]:
    """Async-safe accessor for current session list."""
    async with _sessions_lock:
        return list(_sessions)


async def _poll_loop() -> None:
    """Run session refresh every _refresh_interval seconds, catching all exceptions."""
    global _sessions
    while True:
        try:
            new_sessions = await asyncio.to_thread(_refresh_sessions)
            async with _sessions_lock:
                _sessions = new_sessions
        except Exception:
            logger.exception("Error refreshing sessions")
        await asyncio.sleep(_refresh_interval)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global _poll_task, _sessions

    # Initial scan
    _sessions = _refresh_sessions()

    # Start background poll loop
    _poll_task = asyncio.create_task(_poll_loop())
    yield

    # Cleanup: cancel the poll loop
    if _poll_task is not None:
        _poll_task.cancel()
        try:
            await _poll_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="amplifier-recipe-dashboard",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers (pure functions — unchanged business logic)
# ---------------------------------------------------------------------------


async def _find_session(session_id: str) -> RecipeSession | None:
    """Find a session by ID (prefix match supported)."""
    for s in await _get_sessions():
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


def _build_step_list_slim(s: RecipeSession) -> list[dict]:
    """Slim step list for the LIST endpoint.

    Inline child rendering (frontend `_renderInlineSubRecipe`) reads only
    `id`, `type`, `completed`, `skipped` per step. Output values, resolved
    template variables, descriptions, and conditions are detail-view-only
    and stripped here to keep the list payload compact.
    """
    completed = set(s.completed_steps)
    max_completed_idx = -1
    for rs in s.recipe_steps:
        if rs.id in completed:
            max_completed_idx = max(max_completed_idx, rs.index)

    steps = []
    for rs in s.recipe_steps:
        is_completed = rs.id in completed
        is_skipped = not is_completed and rs.condition and rs.index < max_completed_idx
        steps.append(
            {
                "id": rs.id,
                "type": rs.step_type,
                "completed": is_completed,
                "skipped": is_skipped,
            }
        )
    return steps


def _session_to_dict(s: RecipeSession) -> dict:
    """Convert RecipeSession to API-friendly dict."""
    # Parent session ID (read from dataclass, already resolved by scanner)
    parent_id = s.parent_session_id

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
        "child_session_ids": s.child_session_ids,
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


def _session_to_list_dict(s: RecipeSession) -> dict:
    """API-friendly dict for the LIST endpoint — strips heavy detail-only fields.

    The list endpoint feeds the discovery view, tree counts, parent/child
    lookup, and inline child step rendering. It does NOT need:
    - `context_summary` (detail-view only — `_renderContextSummary`)
    - `completed_tasks`, `execution_summary`, `final_review`,
      `verification_results`, `approval_prep`, `completion_report`
      (detail-view tabs only)
    - Heavy fields inside each `recipe_steps[*]` entry (output_value,
      resolved_variables, description, condition, output_key, index) —
      use `_build_step_list_slim()` for a stripped step list.

    Detail endpoint `/api/session/{id}` continues to return the full
    `_session_to_dict()` payload.
    """
    parent_id = s.parent_session_id
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
        "child_session_ids": s.child_session_ids,
        "session_dir": str(s.session_dir),
        "cancellation_status": s.cancellation_status,
        "pending_approval_stage": s.pending_approval_stage,
        "pending_approval_prompt": s.pending_approval_prompt,
        "approval_history": s.approval_history,
        "stage_approvals": s.stage_approvals,
        "recipe_description": recipe_desc,
        "current_stage": current_stage,
        "recipe_steps": _build_step_list_slim(s),
    }


def _parse_since(since_str: str | None) -> datetime | None:
    """Parse a 'since' param like '1d', '7d', '30d' into a datetime."""
    if not since_str:
        return None
    try:
        if since_str.endswith("d"):
            days = int(since_str[:-1])
            return datetime.now(timezone.utc) - timedelta(days=days)
        if since_str.endswith("h"):
            hours = int(since_str[:-1])
            return datetime.now(timezone.utc) - timedelta(hours=hours)
    except (ValueError, TypeError):
        return None
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index_page() -> HTMLResponse:
    """Serve index.html with hostname injected into the title."""
    html = (_TEMPLATE_DIR / "index.html").read_text()
    html = html.replace(
        "<title>Recipe Dashboard</title>",
        f"<title>{_HOSTNAME} — Recipe Dashboard</title>",
    )
    return HTMLResponse(html)


@app.get("/api/projects")
async def api_projects(since: str | None = Query(default=None)) -> dict:
    """List unique project slugs with session counts, respecting time filter."""
    sessions = await _get_sessions()
    since_dt = _parse_since(since)
    if since_dt:
        since_iso = since_dt.isoformat()
        sessions = [s for s in sessions if s.started >= since_iso]
    projects: dict[str, int] = {}
    for s in sessions:
        slug = s.project_slug or "unknown"
        projects[slug] = projects.get(slug, 0) + 1

    def _short_name(slug: str) -> str:
        """'Users-jane-repo-myproject' -> 'repo-myproject'"""
        cleaned = re.sub(r"^Users-[^-]+-", "", slug)
        return cleaned or slug

    project_list = [
        {"slug": slug, "count": count, "short_name": _short_name(slug)}
        for slug, count in projects.items()
    ]
    project_list.sort(key=lambda p: p["short_name"].lower())
    return {"projects": project_list}


@app.get("/api/sessions")
async def api_sessions(
    project: str | None = Query(default=None),
    status: str | None = Query(default=None),
    since: str | None = Query(default=None),
) -> dict:
    """List all recipe sessions, most recent first.

    Returns a slim payload (no detail-view fields). For full session data,
    use /api/session/{id}.
    """
    sessions = await _get_sessions()
    if project:
        sessions = [s for s in sessions if project in s.project_slug]
    if status:
        sessions = [s for s in sessions if s.status == status]
    since_dt = _parse_since(since)
    if since_dt:
        since_iso = since_dt.isoformat()
        sessions = [s for s in sessions if s.started >= since_iso]
    return {
        "sessions": [_session_to_list_dict(s) for s in sessions],
        "count": len(sessions),
    }


@app.get("/api/session/{session_id}")
async def api_session_detail(session_id: str) -> dict:
    """Get detailed state for a single session."""
    session = await _find_session(session_id)
    if not session:
        return {"error": "Session not found"}
    return _session_to_dict(session)


@app.get("/api/session/{session_id}/tasks")
async def api_session_tasks(session_id: str) -> dict:
    """Get task-level progress for a session with a plan file."""
    session = await _find_session(session_id)
    if not session:
        return {"error": "Session not found"}

    plan_path = session.plan_path
    if not plan_path:
        return {"tasks": [], "info": "This recipe does not use a plan file."}

    tasks = await asyncio.to_thread(parse_plan, plan_path)
    if not tasks:
        return {"error": f"No tasks found in {plan_path}", "tasks": []}

    # Get git commits
    working_dir = session.working_dir
    commits = []
    if working_dir:
        commits = await asyncio.to_thread(get_commits_since, working_dir, since=session.started)

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
            task_status = "done"
        elif task.number == last_done_idx + 1:
            task_status = "active"
        else:
            task_status = "pending"

        task_list.append(
            {
                "number": task.number,
                "description": task.description,
                "status": task_status,
                "commit_hash": commit.hash if commit else None,
                "commit_subject": commit.subject if commit else None,
                "commit_time": commit.timestamp if commit else None,
            }
        )

    done_count = sum(1 for t in task_list if t["status"] == "done")
    return {
        "tasks": task_list,
        "total": len(task_list),
        "done": done_count,
        "progress": done_count / len(task_list) if task_list else 0,
        "plan_path": plan_path,
        "recent_commits": [
            {"hash": c.hash, "subject": c.subject, "timestamp": c.timestamp} for c in commits[:10]
        ],
    }


@app.post("/api/refresh")
async def api_refresh() -> dict:
    """Force an immediate session rescan."""
    global _sessions
    new_sessions = await asyncio.to_thread(_refresh_sessions)
    async with _sessions_lock:
        _sessions = new_sessions
    return {"status": "refreshed", "count": len(await _get_sessions())}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    """Serve login.html with auth mode and hostname injected."""
    html = (_TEMPLATE_DIR / "login.html").read_text()
    html = html.replace("__HOSTNAME__", _HOSTNAME)
    # Inject auth config as a JS global before the closing </head>
    auth_script = (
        f'<script>window.DASHBOARD_AUTH = {{"mode": "{_auth_mode}",'
        f' "hostname": "{_HOSTNAME}"}};</script>'
    )
    html = html.replace("</head>", f"  {auth_script}\n</head>")
    return HTMLResponse(html)


@app.post("/auth/login")
async def auth_login(request: Request) -> Response:
    """Verify credentials, set signed cookie, return success/failure."""
    from .auth import authenticate_pam, create_session_cookie

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "detail": "Invalid request body"}, status_code=400)

    username = body.get("username", "")
    password = body.get("password", "")

    # Verify credentials
    authenticated = False
    if _auth_mode == "pam":
        authenticated = authenticate_pam(username, password)
    elif _auth_mode == "password":
        authenticated = password == _auth_password
    else:
        # auth == "none" — shouldn't reach here but allow through
        authenticated = True

    if not authenticated:
        return JSONResponse({"ok": False, "detail": "Invalid credentials"}, status_code=401)

    # Set signed session cookie
    cookie_value = create_session_cookie(_auth_secret)
    response = JSONResponse({"ok": True})
    max_age = _auth_ttl if _auth_ttl > 0 else None
    response.set_cookie(
        key="dashboard_session",
        value=cookie_value,
        httponly=True,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    return response


@app.get("/auth/logout")
async def auth_logout() -> RedirectResponse:
    """Clear session cookie and redirect to login."""
    response = RedirectResponse(url="/login", status_code=307)
    response.delete_cookie(key="dashboard_session", path="/")
    return response


@app.get("/auth/mode")
async def auth_mode() -> dict:
    """Return the current auth mode for frontend use."""
    return {"mode": _auth_mode}


# ---------------------------------------------------------------------------
# Static file serving — MUST come after all API routes (first-match-wins)
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Factory for CLI use
# ---------------------------------------------------------------------------


def create_app(
    projects_dir: Path | None = None,
    refresh_interval: float = 15.0,
    auth: str = "none",
    session_ttl: int = 604800,
) -> FastAPI:
    """Configure the module-level app for the given projects_dir.

    Returns the module-level ``app`` after setting globals used by routes.
    This mirrors the Flask create_app() pattern for backward compatibility
    with cli.py, while being a thin wrapper around the module-level FastAPI app.

    When *auth* is not "none", resolves the auth mode and attaches
    :class:`~amplifier_recipe_dashboard.auth.AuthMiddleware`.
    """
    global _projects_dir, _refresh_interval
    global _auth_mode, _auth_password, _auth_secret, _auth_ttl

    _projects_dir = projects_dir
    _refresh_interval = refresh_interval

    # --- Auth setup ---
    if auth != "none":
        from .auth import AuthMiddleware, load_or_create_secret, resolve_auth_mode

        mode, password = resolve_auth_mode(auth)
        secret = load_or_create_secret()

        _auth_mode = mode
        _auth_password = password
        _auth_secret = secret
        _auth_ttl = session_ttl

        if mode != "none":
            app.add_middleware(
                AuthMiddleware,
                auth_mode=mode,
                secret=secret,
                ttl_seconds=session_ttl,
                password=password,
            )
            logger.info("Auth middleware enabled: mode=%s, ttl=%ds", mode, session_ttl)
    else:
        _auth_mode = "none"
        _auth_password = ""
        _auth_secret = ""
        _auth_ttl = session_ttl

    return app
