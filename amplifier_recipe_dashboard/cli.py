"""CLI entry point for the Amplifier Recipe Dashboard."""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import secrets as _secrets
import shutil
import subprocess
import sys
import threading
import webbrowser

# ---------------------------------------------------------------------------
# Install info helpers (shared by doctor + upgrade)
# ---------------------------------------------------------------------------


def _get_install_info() -> dict:
    """Detect how amplifier-recipe-dashboard was installed using PEP 610 direct_url.json.

    Returns dict with keys:
      source: 'git' | 'editable' | 'pypi' | 'unknown'
      version: installed version string
      commit: installed commit sha (git only)
      url: git repo URL (git only)
    """
    info: dict = {
        "source": "unknown",
        "version": "0.0.0",
        "commit": None,
        "url": None,
    }

    try:
        from importlib.metadata import distribution

        dist = distribution("amplifier-recipe-dashboard")
        info["version"] = dist.metadata["Version"]

        du_text = dist.read_text("direct_url.json")
        if du_text:
            du = json.loads(du_text)
            if "vcs_info" in du:
                info["source"] = "git"
                info["commit"] = du["vcs_info"].get("commit_id", "")
                info["url"] = du.get("url", "")
            elif "dir_info" in du and du["dir_info"].get("editable"):
                info["source"] = "editable"
            else:
                info["source"] = "unknown"
        else:
            # No direct_url.json → probably PyPI
            info["source"] = "pypi"
    except Exception:  # noqa: BLE001
        pass

    return info


def _check_for_update(info: dict) -> tuple[bool, str]:
    """Check if an update is available. Returns (update_available, message).

    For git: compares installed commit_id against remote HEAD sha.
    For pypi: compares installed version against latest PyPI version.
    For editable: always returns (False, "editable install").
    """
    import urllib.request

    if info["source"] == "editable":
        return False, "editable install — manage updates manually"

    if info["source"] == "git":
        try:
            result = subprocess.run(
                ["git", "ls-remote", info["url"], "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return True, "could not check remote — upgrading to be safe"

            remote_sha = result.stdout.strip().split()[0] if result.stdout.strip() else ""
            local_sha = info["commit"] or ""

            if not remote_sha:
                return True, "could not read remote sha — upgrading to be safe"

            if local_sha == remote_sha:
                return False, f"up to date (commit {local_sha[:8]})"
            else:
                return True, f"update available ({local_sha[:8]} → {remote_sha[:8]})"
        except Exception:  # noqa: BLE001
            return True, "check failed — upgrading to be safe"

    if info["source"] == "pypi":
        try:
            req = urllib.request.Request(
                "https://pypi.org/pypi/amplifier-recipe-dashboard/json",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                data = json.loads(resp.read())
                latest = data["info"]["version"]
                if latest == info["version"]:
                    return False, f"up to date (v{info['version']})"
                else:
                    return (
                        True,
                        f"update available (v{info['version']} → v{latest})",
                    )
        except Exception:  # noqa: BLE001
            return True, "could not check PyPI — upgrading to be safe"

    # Unknown source
    return True, "unknown install source — upgrading to be safe"


# ---------------------------------------------------------------------------
# Stale port killer (crash-loop guard for service restarts)
# ---------------------------------------------------------------------------


def _kill_stale_port_holder(port: int) -> None:
    """Kill any existing process on *port* to prevent EADDRINUSE crash-loops.

    On service restart, the old process may still be holding the port.
    Uses ``lsof -ti :<port>`` to find occupants, sends SIGTERM, then waits 1s
    for the port to free.  Silently swallows all errors.
    """
    import signal
    import time

    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            my_pid = os.getpid()
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    pid = int(pid_str.strip())
                    if pid != my_pid:
                        os.kill(pid, signal.SIGTERM)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
            time.sleep(1)  # Brief wait for the port to be released
    except Exception:  # noqa: BLE001
        pass  # lsof not available or other error — proceed


# ---------------------------------------------------------------------------
# Doctor command
# ---------------------------------------------------------------------------


def doctor() -> None:
    """Run diagnostic checks and report system status."""
    from pathlib import Path

    ok = "\033[32m✓\033[0m"  # green check
    fail = "\033[31m✗\033[0m"  # red x
    warn = "\033[33m!\033[0m"  # yellow warning

    print("\namplifier-recipe-dashboard doctor\n")

    # 1. Python version
    py_version = platform.python_version()
    py_ok = tuple(int(x) for x in py_version.split(".")[:2]) >= (3, 11)
    print(f"  {ok if py_ok else fail} Python {py_version}" + ("" if py_ok else " (3.11+ required)"))

    # 2. Dashboard version + install source
    info = _get_install_info()
    source_label = info["source"]
    if info["commit"]:
        source_label += f" @ {info['commit'][:8]}"
    print(f"  {ok} amplifier-recipe-dashboard {info['version']} (installed via {source_label})")

    # 3. Update available check
    try:
        update_available, update_msg = _check_for_update(info)
        if update_available:
            print(f"  {warn} Update: {update_msg}")
            print("    Run: amplifier-recipe-dashboard upgrade")
        else:
            print(f"  {ok} {update_msg}")
    except Exception:  # noqa: BLE001
        print(f"  {warn} Could not check for updates")

    # 4. Settings file status
    from .settings import SETTINGS_PATH

    if SETTINGS_PATH.exists():
        try:
            text = SETTINGS_PATH.read_text()
            json.loads(text)
            print(f"  {ok} Settings: {SETTINGS_PATH}")
        except json.JSONDecodeError:
            print(f"  {fail} Settings: {SETTINGS_PATH} (invalid JSON)")
    else:
        print(f"  {warn} Settings: {SETTINGS_PATH} (not yet created — will use defaults)")

    # 5. Projects directory
    projects_dir = Path.home() / ".amplifier" / "projects"
    if projects_dir.exists() and os.access(projects_dir, os.R_OK):
        print(f"  {ok} Projects dir: {projects_dir}")
    elif projects_dir.exists():
        print(f"  {fail} Projects dir: {projects_dir} (not readable)")
    else:
        print(f"  {warn} Projects dir: {projects_dir} (does not exist)")

    # 6. Session count
    session_count = 0
    try:
        if projects_dir.exists():
            for project_dir in projects_dir.iterdir():
                sessions_dir = project_dir / "sessions"
                if sessions_dir.is_dir():
                    session_count += sum(1 for d in sessions_dir.iterdir() if d.is_dir())
        print(f"  {ok} Sessions found: {session_count}")
    except Exception:  # noqa: BLE001
        print(f"  {warn} Sessions: could not scan")

    # 7. Auth mode vs host warning
    from .settings import load_settings

    cfg = load_settings()
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 8181)
    auth = cfg.get("auth", "none")
    if host == "0.0.0.0" and auth == "none":
        print(
            f"  {warn} Host is 0.0.0.0 with auth='none'"
            " — anyone on the network can access the dashboard"
        )
    else:
        print(f"  {ok} Listening: {host}:{port} (auth={auth})")

    # 8. Service status
    print(f"  {ok} Platform: {sys.platform} ({platform.machine()})")
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.amplifier-recipe-dashboard.plist"
        if plist.exists():
            uid = os.getuid()
            result = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/com.amplifier-recipe-dashboard"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"  {ok} Service: launchd agent running")
            else:
                print(f"  {warn} Service: launchd agent installed but not running")
        else:
            print(
                f"  {warn} Service: not installed (run: amplifier-recipe-dashboard service install)"
            )
    else:
        systemd_user = (
            Path.home() / ".config" / "systemd" / "user" / "amplifier-recipe-dashboard.service"
        )
        if systemd_user.exists():
            print(f"  {ok} Service: systemd user unit installed ({systemd_user})")
        else:
            print(
                f"  {warn} Service: not installed (run: amplifier-recipe-dashboard service install)"
            )

    print()  # trailing newline


# ---------------------------------------------------------------------------
# Auth helpers (show-password, reset-secret)
# ---------------------------------------------------------------------------


def show_password() -> None:
    """Print the current dashboard password or indicate PAM mode."""
    from .auth import load_password, pam_available

    auth_setting = os.environ.get("DASHBOARD_AUTH", "").lower()
    if auth_setting != "password" and pam_available():
        print("Auth mode: PAM — no password file used")
        return
    pw = load_password()
    if pw:
        print(f"Password: {pw}")
    else:
        print("No password file found. Start the dashboard with auth enabled to auto-generate one.")


def reset_secret() -> None:
    """Regenerate the signing secret and warn that all sessions are now invalid."""
    from .auth import get_secret_path

    path = get_secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = _secrets.token_urlsafe(32)
    path.write_text(secret + "\n")
    path.chmod(0o600)
    print(f"Secret written to {path}")
    print("Warning: all active sessions are now invalid.")


# ---------------------------------------------------------------------------
# Upgrade command
# ---------------------------------------------------------------------------


def upgrade(*, force: bool = False) -> None:
    """Upgrade amplifier-recipe-dashboard to the latest version and restart the service."""
    from .service import service_install, service_start, service_stop

    print("\namplifier-recipe-dashboard upgrade\n")

    # 1. Show current install info
    info = _get_install_info()
    commit_suffix = f" (commit {info['commit'][:8]})" if info["commit"] else ""
    print(f"  Installed: v{info['version']}{commit_suffix} via {info['source']}")

    # 2. Editable install → bail out
    if info["source"] == "editable":
        print("  Editable install detected — manage updates manually.")
        return

    # 3. Check for update (unless --force)
    if not force:
        update_available, message = _check_for_update(info)
        print(f"  Status: {message}")

        if not update_available:
            print(
                "\n  Already up to date."
                " Use 'amplifier-recipe-dashboard upgrade --force'"
                " to reinstall anyway.\n"
            )
            return
    else:
        print("  Status: --force specified — skipping version check")

    # 4. Stop service if running
    service_was_running = False
    try:
        if sys.platform == "darwin":
            label = "com.amplifier-recipe-dashboard"
            uid = os.getuid()
            result = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                service_was_running = True
                print("  Stopping service...")
                service_stop()
        else:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "amplifier-recipe-dashboard"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                service_was_running = True
                print("  Stopping service...")
                service_stop()
    except Exception:  # noqa: BLE001
        pass  # service module may not be available

    # 5. Run upgrade command
    print("  Installing latest version...")
    uv_path = shutil.which("uv")

    if info["source"] == "git" and info.get("url"):
        # Git install: use uv tool install git+<url> --force
        if uv_path:
            cmd = [uv_path, "tool", "install", f"git+{info['url']}", "--force"]
        else:
            pip_path = shutil.which("pip") or shutil.which("pip3")
            if not pip_path:
                print("  ERROR: neither uv nor pip found — cannot upgrade")
                return
            cmd = [pip_path, "install", "--upgrade", f"git+{info['url']}"]
    elif info["source"] == "pypi":
        # PyPI install: uv tool install --upgrade
        if uv_path:
            cmd = [uv_path, "tool", "install", "amplifier-recipe-dashboard", "--upgrade"]
        else:
            pip_path = shutil.which("pip") or shutil.which("pip3")
            if not pip_path:
                print("  ERROR: neither uv nor pip found — cannot upgrade")
                return
            cmd = [pip_path, "install", "--upgrade", "amplifier-recipe-dashboard"]
    else:
        # Unknown source — try PyPI as fallback
        if uv_path:
            cmd = [uv_path, "tool", "install", "amplifier-recipe-dashboard", "--upgrade"]
        else:
            pip_path = shutil.which("pip") or shutil.which("pip3")
            if not pip_path:
                print("  ERROR: neither uv nor pip found — cannot upgrade")
                return
            cmd = [pip_path, "install", "--upgrade", "amplifier-recipe-dashboard"]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: install failed:\n{result.stderr}")
        return
    print("  Installed successfully")

    # 6. Restart service if it was running
    if service_was_running:
        print("  Regenerating service file...")
        try:
            service_install()
        except Exception:  # noqa: BLE001
            pass
        print("  Restarting service...")
        try:
            service_start()
        except Exception:  # noqa: BLE001
            print("  Warning: could not restart service")

    # 7. Run doctor to verify
    print("\n  Verifying...")
    doctor()


# ---------------------------------------------------------------------------
# Settings helpers (config subcommands)
# ---------------------------------------------------------------------------


def config_list() -> None:
    """Show all settings with current values."""
    from .settings import DEFAULT_SETTINGS, SETTINGS_PATH, load_settings

    settings = load_settings()
    print(f"\namplifier-recipe-dashboard config ({SETTINGS_PATH})\n")

    for key in DEFAULT_SETTINGS:
        value = settings.get(key)
        default = DEFAULT_SETTINGS[key]
        is_default = value == default
        marker = "" if is_default else " (modified)"
        if isinstance(value, str):
            display = f'"{value}"'
        elif value is None:
            display = "null"
        elif isinstance(value, bool):
            display = "true" if value else "false"
        else:
            display = str(value)
        print(f"  {key}: {display}{marker}")
    print()


def config_get(key: str) -> None:
    """Show one setting value."""
    from .settings import DEFAULT_SETTINGS, load_settings

    if key not in DEFAULT_SETTINGS:
        print(f"Unknown setting: {key}", file=sys.stderr)
        print(f"Valid keys: {', '.join(sorted(DEFAULT_SETTINGS.keys()))}", file=sys.stderr)
        sys.exit(1)

    settings = load_settings()
    value = settings.get(key)
    if isinstance(value, str):
        print(value)
    elif value is None:
        print("null")
    elif isinstance(value, bool):
        print("true" if value else "false")
    else:
        print(value)


def config_set(key: str, raw_value: str) -> None:
    """Set a setting value. Auto-detects type from the default."""
    from .settings import DEFAULT_SETTINGS, patch_settings

    if key not in DEFAULT_SETTINGS:
        print(f"Unknown setting: {key}", file=sys.stderr)
        print(f"Valid keys: {', '.join(sorted(DEFAULT_SETTINGS.keys()))}", file=sys.stderr)
        sys.exit(1)

    default = DEFAULT_SETTINGS[key]

    try:
        if isinstance(default, bool):
            value: object = raw_value.lower() in ("true", "1", "yes", "on")
        elif isinstance(default, int):
            value = int(raw_value)
        elif default is None:
            value = None if raw_value.lower() in ("null", "none", "") else raw_value
        else:
            value = raw_value
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Invalid value for {key}: {e}", file=sys.stderr)
        sys.exit(1)

    patch_settings({key: value})
    print(f"  {key}: {value}")


def config_reset(key: str | None = None) -> None:
    """Reset one or all settings to defaults."""
    import copy

    from .settings import DEFAULT_SETTINGS, SETTINGS_PATH, patch_settings, save_settings

    if key is not None:
        if key not in DEFAULT_SETTINGS:
            print(f"Unknown setting: {key}", file=sys.stderr)
            print(
                f"Valid keys: {', '.join(sorted(DEFAULT_SETTINGS.keys()))}",
                file=sys.stderr,
            )
            sys.exit(1)
        patch_settings({key: DEFAULT_SETTINGS[key]})
        print(f"  {key} reset to: {DEFAULT_SETTINGS[key]}")
    else:
        save_settings(copy.deepcopy(DEFAULT_SETTINGS))
        print(f"  All settings reset to defaults ({SETTINGS_PATH})")


# ---------------------------------------------------------------------------
# Serve command
# ---------------------------------------------------------------------------


def serve(args: argparse.Namespace) -> None:
    """Start the Amplifier Recipe Dashboard server.

    Resolution order: CLI flag (if not None) > settings.json > hardcoded default.
    """
    import uvicorn

    from .settings import load_settings

    settings = load_settings()
    host = args.host if args.host is not None else settings.get("host", "127.0.0.1")
    port = args.port if args.port is not None else settings.get("port", 8181)
    auth = getattr(args, "auth", None)
    auth = auth if auth is not None else settings.get("auth", "none")
    session_ttl = getattr(args, "session_ttl", None)
    session_ttl = session_ttl if session_ttl is not None else settings.get("session_ttl", 604800)
    auto_open = settings.get("auto_open", True) if not args.no_open else False
    debug = args.debug
    refresh_interval = settings.get("refresh_interval", 15)

    log_level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    # Prevent crash-loop on restart: kill any stale process holding the port
    _kill_stale_port_holder(port)

    # Configure the app with settings before uvicorn starts
    from .server import create_app

    create_app(refresh_interval=refresh_interval, auth=auth, session_ttl=session_ttl)

    url = f"http://{host}:{port}"
    print(f"Recipe Dashboard starting at {url}")

    if auto_open:

        def _open() -> None:
            import time

            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    print(f"Serving on {url} (Ctrl+C to stop)")
    uvicorn.run(
        "amplifier_recipe_dashboard.server:app",
        host=host,
        port=port,
        log_level="debug" if debug else "info",
    )


# ---------------------------------------------------------------------------
# Argparse setup
# ---------------------------------------------------------------------------


def _add_serve_flags(parser: argparse.ArgumentParser) -> None:
    """Add --host, --port, --auth, --session-ttl, --no-open, --debug flags to a parser.

    All default to None so serve() can distinguish 'not passed' from
    'passed the default value'.
    """
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host (default: from settings.json, then 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port (default: from settings.json, then 8181)",
    )
    parser.add_argument(
        "--auth",
        choices=["none", "pam", "password"],
        default=None,
        help="Auth method: none, pam, or password (default: from settings.json, then none)",
    )
    parser.add_argument(
        "--session-ttl",
        type=int,
        default=None,
        dest="session_ttl",
        help="Session TTL in seconds (default: from settings.json, then 604800)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        default=False,
        dest="no_open",
        help="Don't auto-open browser",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="amplifier-recipe-dashboard",
        description="Amplifier Recipe Dashboard — live recipe progress viewer",
    )
    # Bare command (no subcommand) defaults to serve
    _add_serve_flags(parser)

    sub = parser.add_subparsers(dest="command")

    # --- serve ---
    serve_parser = sub.add_parser("serve", help="Start the server (default)")
    _add_serve_flags(serve_parser)

    # --- config ---
    config_parser = sub.add_parser("config", help="View and manage settings")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_sub.add_parser("list", help="Show all settings (default)")
    config_get_parser = config_sub.add_parser("get", help="Show one setting")
    config_get_parser.add_argument("key", help="Setting key")
    config_set_parser = config_sub.add_parser("set", help="Set a setting value")
    config_set_parser.add_argument("key", help="Setting key")
    config_set_parser.add_argument("value", help="New value")
    config_reset_parser = config_sub.add_parser("reset", help="Reset to defaults")
    config_reset_parser.add_argument("key", nargs="?", help="Setting key (omit to reset all)")

    # --- service ---
    service_parser = sub.add_parser("service", help="Manage the background service")
    service_sub = service_parser.add_subparsers(dest="service_command")
    service_sub.add_parser("install", help="Install + enable + start the service")
    service_sub.add_parser("uninstall", help="Stop + disable + remove the service")
    service_sub.add_parser("start", help="Start the service")
    service_sub.add_parser("stop", help="Stop the service")
    service_sub.add_parser("restart", help="Stop + start the service")
    service_sub.add_parser("status", help="Show service status")
    service_sub.add_parser("logs", help="Tail service logs")

    # --- doctor ---
    sub.add_parser("doctor", help="Check dependencies and system status")

    # --- show-password ---
    sub.add_parser("show-password", help="Show the current dashboard password")

    # --- reset-secret ---
    sub.add_parser("reset-secret", help="Regenerate signing secret (invalidates sessions)")

    # --- upgrade ---
    upgrade_parser = sub.add_parser(
        "upgrade",
        aliases=["update"],
        help="Upgrade to latest version and restart service",
    )
    upgrade_parser.add_argument(
        "--force",
        action="store_true",
        help="Force reinstall even if already up to date",
    )

    args = parser.parse_args()

    # Dispatch
    if args.command == "config":
        cmd = getattr(args, "config_command", None)
        if cmd == "get":
            config_get(args.key)
        elif cmd == "set":
            config_set(args.key, args.value)
        elif cmd == "reset":
            config_reset(getattr(args, "key", None))
        else:
            # Default: list (no subcommand or explicit "list")
            config_list()
    elif args.command == "service":
        from .service import (
            service_install,
            service_logs,
            service_restart,
            service_start,
            service_status,
            service_stop,
            service_uninstall,
        )

        cmd = getattr(args, "service_command", None)
        if cmd == "install":
            service_install()
        elif cmd == "uninstall":
            service_uninstall()
        elif cmd == "start":
            service_start()
        elif cmd == "stop":
            service_stop()
        elif cmd == "restart":
            service_restart()
        elif cmd == "status":
            service_status()
        elif cmd == "logs":
            service_logs()
        else:
            service_parser.print_help()
    elif args.command == "doctor":
        doctor()
    elif args.command == "show-password":
        show_password()
    elif args.command == "reset-secret":
        reset_secret()
    elif args.command in ("upgrade", "update"):
        upgrade(force=getattr(args, "force", False))
    else:
        # Default (bare command or explicit "serve")
        serve(args)


if __name__ == "__main__":
    main()
