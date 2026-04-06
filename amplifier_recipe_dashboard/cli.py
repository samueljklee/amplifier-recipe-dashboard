"""CLI entry point for the Amplifier Recipe Dashboard."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import webbrowser

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
    auto_open = settings.get("auto_open", True) if not args.no_open else False
    debug = args.debug
    refresh_interval = settings.get("refresh_interval", 15)

    log_level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    # Configure the app with settings before uvicorn starts
    from .server import create_app

    create_app(refresh_interval=refresh_interval)

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
    """Add --host, --port, --no-open, --debug flags to a parser.

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

    # --- placeholder subcommands (phases 2 & 3) ---
    sub.add_parser("service", help="Manage the background service (not yet implemented)")
    sub.add_parser("doctor", help="Check dependencies and system status (not yet implemented)")
    sub.add_parser("upgrade", help="Upgrade to latest version (not yet implemented)")

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
        print("service: not yet implemented")
    elif args.command == "doctor":
        print("doctor: not yet implemented")
    elif args.command == "upgrade":
        print("upgrade: not yet implemented")
    else:
        # Default (bare command or explicit "serve")
        serve(args)


if __name__ == "__main__":
    main()
