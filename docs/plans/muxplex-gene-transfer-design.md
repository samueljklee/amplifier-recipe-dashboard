# Muxplex Gene Transfer Design

## Goal

Transfer 8 battle-tested patterns from Brian's muxplex repo into the Amplifier Recipe Dashboard — upgrading it from a lean local-only dev tool into a production-grade, always-on monitoring service with a rich CLI, persistent configuration, authentication, and dynamic browser signals.

## Background

The recipe dashboard (`amplifier-recipe-dashboard`) is a Flask + Waitress + Click app that displays Amplifier recipe session progress. It works well for its original use case — manually launch it, check session status, close it — but lacks the operational maturity for persistent deployment, remote access, or self-management.

Brian's [muxplex](https://github.com/bkrabach/muxplex) is a production-hardened tmux web dashboard that has solved all of these problems: service management across macOS/Linux, PAM authentication for remote access, dynamic browser tab signals, persistent settings, self-upgrade, and a comprehensive doctor command. The architecture is close enough (Python web dashboard, vanilla JS frontend, `uv tool install` distribution) that patterns can transfer with minimal adaptation.

## Approach

**Migrate the framework first, then layer features.** The dashboard currently runs on Flask + Waitress + Click (~480 lines of Python). We migrate to FastAPI + uvicorn + argparse first because 7 of the 8 target patterns are ASGI-native in muxplex. Paying the small migration tax up front avoids writing Flask adapter code for every subsequent feature.

The migration is low-risk because:
- The Flask surface area is small (6 routes, 1 background thread, no ORM, no sessions)
- All business logic (`session_scanner.py`, `plan_parser.py`, `git_tracker.py`) is framework-agnostic
- The frontend (`app.js`, `style.css`) is vanilla JS with no server-side rendering dependency

## Architecture

After all 8 features are implemented, the component layout:

```
amplifier_recipe_dashboard/
├── cli.py              ← argparse multi-command tree (serve, config, service, doctor, upgrade)
├── server.py           ← FastAPI app, routes, async background refresh
├── settings.py         ← Persistent settings (~/.config/.../settings.json)
├── auth.py             ← Starlette middleware (PAM/password, localhost bypass)
├── service.py          ← Platform-dispatching service management (launchd/systemd)
├── session_scanner.py  ← (unchanged) Filesystem scan for recipe sessions
├── plan_parser.py      ← (unchanged) Parse task headers from plan files
├── git_tracker.py      ← (unchanged) Match git commits to task numbers
├── static/
│   ├── app.js          ← + favicon badge, dynamic title, poll-driven updates
│   ├── style.css       ← (unchanged)
│   └── favicon.svg     ← New base favicon asset
└── templates/
    ├── index.html       ← Simplified (html.replace instead of Jinja2)
    └── login.html       ← New auth login page
```

**Dependency changes in `pyproject.toml`:**

| Remove         | Add              | Purpose                          |
|----------------|------------------|----------------------------------|
| `flask`        | `fastapi`        | Web framework                    |
| `waitress`     | `uvicorn`        | ASGI server                      |
| `click`        | —                | Replaced by stdlib `argparse`    |
| —              | `itsdangerous`   | Signed session cookies for auth  |
| —              | `python-pam` (optional) | PAM auth on Linux/macOS   |

## Components

### 1. Framework Migration (Flask → FastAPI)

**What changes:**

- **Routes** — 6 Flask routes become FastAPI routes. `@app.route("/api/sessions")` becomes `@app.get("/api/sessions")`. Return dicts directly; FastAPI handles JSON serialization.
- **App factory** — `create_app()` factory becomes a module-level `app = FastAPI()`.
- **Background refresh** — Replace `threading.Timer` daemon with a `@app.on_event("startup")` async task using `asyncio.sleep(15)`. Replace `threading.Lock` with `asyncio.Lock` for the shared `_sessions` state.
- **Template serving** — Drop Jinja2. Use the muxplex pattern: `html.replace()` for server-side injection (hostname, etc.) and return `HTMLResponse`. Static files served via `StaticFiles` mount.
- **CLI launcher** — Swap Click for argparse. Call `uvicorn.run()` instead of `waitress.serve()`.

**What stays the same:** All business logic — `session_scanner.py`, `plan_parser.py`, `git_tracker.py`, `app.js`, `style.css`. These don't touch the web framework.

### 2. Settings System

**New file: `settings.py`** (~100 lines) — direct port of muxplex's pattern.

**Location:** `~/.config/amplifier-recipe-dashboard/settings.json`

**Default settings:**

| Key                | Default       | Type   |
|--------------------|---------------|--------|
| `host`             | `127.0.0.1`  | str    |
| `port`             | `8181`        | int    |
| `auto_open`        | `true`        | bool   |
| `refresh_interval` | `15`          | int    |
| `auth`             | `"none"`      | str    |

**Priority chain:** CLI flag → settings.json → default. CLI flags default to `None` (not their final defaults), so `None` means "not passed, use settings file."

**Config subcommand:**
- `config list` — all settings with `(modified)` markers for non-default values
- `config get <key>` — single value
- `config set <key> <value>` — type-coerced (bool/int/str based on default type)
- `config reset [key]` — reset one key or all to defaults

**`patch_settings()`** only writes keys that exist in `DEFAULT_SETTINGS`, preserving forward compatibility when new settings are added later.

**Why this comes before service management:** The service file runs `amplifier-recipe-dashboard serve` with zero flags and reads everything from `settings.json`. Without persistent settings, there's no way to configure a headless service.

### 3. Dynamic Favicon Badge + Hostname Title

**Dynamic favicon:**

Copy muxplex's self-contained vanilla JS pattern (`_drawFaviconBadge` + `updateFaviconBadge`, ~76 lines) into `app.js`.

Badge trigger conditions (adapted from muxplex's bell detection to recipe status):

| Badge      | Condition                                    |
|------------|----------------------------------------------|
| Amber dot  | Sessions in `running` state                  |
| Red dot    | Sessions in `waiting` state (pending approval — needs human attention) |
| No badge   | All sessions `done`, `idle`, `failed`, `cancelled`, or `stalled` |

- Called after every poll cycle (existing 10s/15s polling in `app.js`)
- Uses the lazy-cached `_faviconImage` pattern to avoid network fetches on every poll
- Requires a base favicon asset (SVG or simple PNG)

**Dynamic title:**

- **Server-side:** `socket.gethostname().split(".")[0]` → inject into HTML via `html.replace()`
- **Baseline format:** `"hostname — Recipe Dashboard"`
- **Active format:** `"2 running · 1 waiting — hostname — Recipe Dashboard"` (updated client-side after each poll, falls back to baseline when idle)

### 4. PAM Authentication

**New file: `auth.py`** — Starlette `BaseHTTPMiddleware`, direct transfer from muxplex's `auth.py`.

> **Note:** This section needs a security review during implementation. The auth middleware, localhost bypass logic, cookie signing, and PAM integration should be validated by a security-focused review before shipping.

**Auth cascade (checked in order):**

1. `client.host` in `{127.0.0.1, ::1}` → **pass through** (socket-level check, not HTTP header — can't be spoofed)
2. Request path is `/login` or `/auth/*` → **pass through** (login page must load)
3. Static file extensions (`.css`, `.js`, `.svg`, `.png`, `.ico`) → **pass through** (login page assets)
4. `muxplex_session` cookie → verify via `itsdangerous.TimestampSigner` with TTL → **pass through**
5. `Authorization: Basic` header → decode → check credentials (PAM or password fallback)
6. No auth → redirect browser to `/login` or return `401` JSON for API calls

**Auth mode resolution at startup (same as muxplex):**

1. Settings say `auth: "password"` → password mode
2. `python-pam` importable → PAM mode (uses OS login credentials)
3. `DASHBOARD_PASSWORD` env var → password mode
4. `~/.config/amplifier-recipe-dashboard/password` file → use it
5. Auto-generate password → write to file, print to stderr on first run

**Login page:** New `login.html` (~100 lines). Server injects auth mode and hostname.

**Conditional activation:** Middleware only mounts when `settings.auth != "none"`. When running on localhost with default settings, there is zero auth overhead. The `service install` command prompts about enabling auth when the host is changed to `0.0.0.0`.

**Dependencies:** `python-pam` (optional — soft import with graceful fallback), `itsdangerous` (for signed session cookies).

### 5. Service Management

**New file: `service.py`** (~250 lines) — port of muxplex's platform-dispatching pattern.

**Platform support:**

|                      | macOS (primary)                                              | Linux                                                        |
|----------------------|--------------------------------------------------------------|--------------------------------------------------------------|
| **Daemon system**    | `launchd` user agent                                         | `systemd` user service                                       |
| **Unit file**        | `~/Library/LaunchAgents/com.amplifier-recipe-dashboard.plist` | `~/.config/systemd/user/amplifier-recipe-dashboard.service`  |
| **Install**          | `launchctl bootstrap gui/{uid}`                               | `systemctl --user enable --now`                              |
| **Uninstall**        | `launchctl bootout gui/{uid}/...`                             | `systemctl --user stop && disable`                           |
| **Logs**             | `tail -f /tmp/amplifier-recipe-dashboard.log`                 | `journalctl --user -u amplifier-recipe-dashboard -f`         |

**CLI subcommands:**

- `service install` — write unit file, enable, start. Prompts: "Host is 127.0.0.1 — change to 0.0.0.0?" (if yes, suggests enabling auth)
- `service uninstall` — stop, disable, remove unit file
- `service start | stop | restart` — lifecycle commands
- `service status` — running/stopped + PID
- `service logs` — tail the service output

**Key design points:**
- Service file runs `amplifier-recipe-dashboard serve` with **no flags** — all config from `settings.json`
- **Crash-loop guard** — before every `uvicorn.run()`, run `lsof -ti :<port>` → SIGTERM stale holder → sleep 1s. Prevents `EADDRINUSE` after crash recovery.
- macOS is the primary platform (user's current environment)

### 6. Doctor Command

**New subcommand: `doctor`** — single command that diagnoses the health of the installation.

**Example output:**

```
$ amplifier-recipe-dashboard doctor

✓ Python 3.12.0
✓ amplifier-recipe-dashboard v0.2.0 (git @ d73756e)
✓ Update check: up to date
✓ Settings: ~/.config/amplifier-recipe-dashboard/settings.json
✓ ~/.amplifier/projects/ exists (5 projects)
✓ Recipe sessions found: 12 sessions across 3 projects
! Auth: none (host is 127.0.0.1 — OK for localhost)
✓ Service: installed, running (PID 48201)
✓ Listening: 127.0.0.1:8181
```

**Checks in order:**

1. Python version (≥3.10)
2. Dashboard version + install source (PEP 610 detection — git, editable, pypi)
3. Update available? (`git ls-remote` for git installs, PyPI JSON for pip installs)
4. Settings file exists and is valid JSON
5. `~/.amplifier/projects/` exists and is readable
6. Session count across projects (quick filesystem scan)
7. Auth mode vs host configuration (warn if `0.0.0.0` with `auth: none`)
8. Service status (installed? running? PID?)
9. Listening address and port

**Output styling:** Colored terminal output using ANSI escape codes — green `✓`, red `✗`, yellow `!`. Zero external dependencies.

### 7. Multi-Command CLI Restructure

Promote the single flat Click command into an argparse subcommand tree:

```
amplifier-recipe-dashboard                          → serve (default, no subcommand)
amplifier-recipe-dashboard serve [--port] [--host] [--no-open] [--debug]

amplifier-recipe-dashboard config list
amplifier-recipe-dashboard config get <key>
amplifier-recipe-dashboard config set <key> <value>
amplifier-recipe-dashboard config reset [key]

amplifier-recipe-dashboard service install
amplifier-recipe-dashboard service uninstall
amplifier-recipe-dashboard service start | stop | restart
amplifier-recipe-dashboard service status
amplifier-recipe-dashboard service logs

amplifier-recipe-dashboard doctor
amplifier-recipe-dashboard upgrade [--force]
```

**Key design detail:** The `_add_serve_flags(parser)` helper adds `--port`, `--host`, `--no-open`, `--debug` to both the root parser AND the `serve` subparser. Both of these work identically:

```
amplifier-recipe-dashboard --port 9000
amplifier-recipe-dashboard serve --port 9000
```

The bare command (no subcommand) defaults to `serve` — preserving backward compatibility with how the tool works today.

**argparse over Click:** Click's decorator model gets awkward with nested subcommands and shared flag helpers. argparse gives direct control over the parser tree and is stdlib (one less dependency).

### 8. Upgrade Command

**New subcommand: `upgrade [--force]`** — port of muxplex's three-phase pattern.

**Phase 1 — Detect install source** via PEP 610 (`direct_url.json` in dist metadata):

| Source     | Detection                             | Update check                          |
|------------|---------------------------------------|---------------------------------------|
| `git`      | `vcs_info` present in `direct_url.json` | `git ls-remote HEAD` → compare SHA  |
| `pypi`     | No `direct_url.json` or URL points to PyPI | `pypi.org/pypi/.../json` → compare version |
| `editable` | `dir_info.editable = true`            | Skip — print "editable install, manage manually" |

**Phase 2 — Upgrade:**

1. Stop service (if installed and running)
2. `uv tool install git+https://... --force` (git) or `uv tool install ... --upgrade` (pypi)
3. Restart service (if it was running)

**Phase 3 — Verify:**

Run `doctor` automatically after upgrade to confirm everything's healthy.

**The `--force` flag** skips the "already up to date" check and reinstalls regardless.

## Data Flow

**Startup sequence (after all features):**

1. `cli.py` parses args → resolves settings (CLI flags → `settings.json` → defaults)
2. `server.py` creates FastAPI app → mounts `StaticFiles`
3. If `settings.auth != "none"` → mount `AuthMiddleware` from `auth.py`
4. Crash-loop guard: check for stale process on target port
5. `@app.on_event("startup")` launches async background refresh task
6. `uvicorn.run()` starts ASGI server
7. If `auto_open` and not running as service → open browser

**Request flow (non-localhost with auth enabled):**

```
Browser → uvicorn → AuthMiddleware
  ├─ localhost? → pass through
  ├─ exempt path? → pass through
  ├─ valid cookie? → pass through
  ├─ valid Basic auth? → set cookie + pass through
  └─ else → redirect to /login (browser) or 401 (API)
→ FastAPI route → return JSON or HTML
```

**Background refresh (async):**

```
Loop every {refresh_interval} seconds:
  async with _sessions_lock:
    scan filesystem → update _sessions dict
```

## Source Mapping

Which muxplex files map to which dashboard files for each feature:

| Feature                  | Muxplex Source             | Dashboard Target          | Transfer Type       |
|--------------------------|----------------------------|---------------------------|---------------------|
| Framework migration      | `main.py` (FastAPI app)    | `server.py`               | Rewrite to match    |
| Settings system          | `settings.py` (119 lines) | `settings.py` (new)       | Near-direct port    |
| Favicon badge            | `app.js:1863–1939`        | `static/app.js`           | Copy + adapt triggers |
| Dynamic title            | `main.py` (hostname inject) + `app.js` | `server.py` + `static/app.js` | Copy pattern |
| PAM auth                 | `auth.py` (232 lines)     | `auth.py` (new)           | Near-direct port    |
| Service management       | `service.py` (277 lines)  | `service.py` (new)        | Near-direct port    |
| Doctor command           | `cli.py:350–530`          | `cli.py`                  | Adapt checks        |
| Multi-command CLI         | `cli.py` (817 lines)      | `cli.py`                  | Structure + helpers  |
| Upgrade command          | `cli.py:530–650`          | `cli.py`                  | Near-direct port    |

## Error Handling

- **Port in use:** Crash-loop guard (`lsof -ti :<port>` → SIGTERM) before binding. Clear error message if port is still occupied after guard.
- **Settings file corrupt:** `settings.py` falls back to defaults, prints warning. `doctor` reports the issue.
- **Auth failures:** Redirect to login page (browser) or 401 JSON (API). Never expose stack traces.
- **PAM unavailable:** Graceful fallback to password-file mode. `doctor` reports which auth mode is active.
- **Service install fails:** Platform detection → clear error if neither launchd nor systemd available.
- **Upgrade fails:** Service restart is in a try/finally — if upgrade crashes, the service state is reported. `doctor` runs post-upgrade to surface any issues.

## Testing Strategy

- **Framework migration:** Verify all 6 API routes return identical JSON structures. Manual smoke test of the frontend.
- **Settings:** Unit tests for priority chain (CLI flag > file > default), type coercion, and `patch_settings()` forward compatibility.
- **Favicon/title:** Manual browser verification. Validate badge logic against session states.
- **Auth:** Security review of middleware cascade. Test localhost bypass, cookie signing/verification, PAM integration. Delegate to security-focused review.
- **Service:** Manual test on macOS (primary). Verify install/uninstall/start/stop/restart/status/logs cycle.
- **Doctor:** Verify each check reports correctly for both healthy and broken states.
- **CLI:** Verify backward compatibility — bare command still launches serve. Verify flag inheritance on root and serve subparsers.
- **Upgrade:** Test with git install source. Verify service stop/restart around upgrade. Verify `--force` bypasses up-to-date check.

## Implementation Order

```
1. Framework Migration (Flask → FastAPI)    ← foundation everything else builds on
2. Multi-Command CLI Restructure            ← needed before adding subcommands
3. Settings System                          ← needed before service management
4. Dynamic Favicon Badge + Hostname Title   ← quick win, no dependencies
5. Doctor Command                           ← useful during development of remaining features
6. Service Management                       ← depends on settings
7. PAM Authentication                       ← depends on FastAPI + settings; needs security review
8. Upgrade Command                          ← depends on service management + doctor
```

## Open Questions

- **Base favicon asset:** Generate a simple geometric SVG, or commission something? Muxplex uses a monitor icon.
- **PAM security review:** The auth middleware is a direct port of muxplex's battle-tested code, but should still get a security-focused review before shipping, particularly the localhost bypass logic and cookie TTL configuration.
- **Session cookie name:** Muxplex uses `muxplex_session`. Rename to `recipe_dashboard_session` or keep the pattern name?
- **Windows support:** Service management only covers macOS (launchd) and Linux (systemd). Windows is out of scope for now — document as a limitation.
