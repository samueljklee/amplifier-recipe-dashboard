"""System service management (systemd on Linux, launchd on macOS).

Ported from muxplex/service.py — platform-dispatching pattern for
amplifier-recipe-dashboard background service.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERVICE_NAME = "amplifier-recipe-dashboard"
_LAUNCHD_LABEL = "com.amplifier-recipe-dashboard"

_SYSTEMD_UNIT_DIR: Path = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_UNIT_PATH: Path = _SYSTEMD_UNIT_DIR / f"{_SERVICE_NAME}.service"

_LAUNCHD_PLIST_DIR: Path = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST_PATH: Path = _LAUNCHD_PLIST_DIR / f"{_LAUNCHD_LABEL}.plist"

_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=Amplifier Recipe Dashboard
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5s
TimeoutStopSec=10
KillMode=mixed
Environment=PATH={safe_path}

[Install]
WantedBy=default.target
"""

_LAUNCHD_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{dashboard_bin}</string>
        <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{safe_path}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/{service_name}.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{service_name}.err</string>
</dict>
</plist>
"""

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def _is_darwin() -> bool:
    """Return True if running on macOS."""
    return sys.platform == "darwin"


def _resolve_dashboard_bin() -> str:
    """Return the amplifier-recipe-dashboard binary path.

    Prefers the ``amplifier-recipe-dashboard`` executable on PATH;
    falls back to ``<sys.executable> -m amplifier_recipe_dashboard``.
    """
    which = shutil.which("amplifier-recipe-dashboard")
    if which:
        return which
    return f"{sys.executable} -m amplifier_recipe_dashboard"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _prompt_host_if_localhost() -> None:
    """Prompt the user to change host from 127.0.0.1 to 0.0.0.0 for service use."""
    from .settings import load_settings, patch_settings

    settings = load_settings()
    if settings.get("host") == "127.0.0.1":
        try:
            answer = (
                input("Host is 127.0.0.1 — change to 0.0.0.0 so the service is reachable? [Y/n] ")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("y", ""):
            patch_settings({"host": "0.0.0.0"})
            print("  Host updated to 0.0.0.0")
            if settings.get("auth") == "none":
                print(
                    "  \033[33m!\033[0m Warning: auth is 'none' with 0.0.0.0"
                    " — consider enabling auth"
                )


# ---------------------------------------------------------------------------
# Private implementations — systemd (Linux)
# ---------------------------------------------------------------------------


def _systemd_install() -> None:
    dashboard_bin = _resolve_dashboard_bin()
    safe_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    exec_start = f"{dashboard_bin} serve"
    unit_content = _SYSTEMD_UNIT_TEMPLATE.format(exec_start=exec_start, safe_path=safe_path)
    _SYSTEMD_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_UNIT_PATH.write_text(unit_content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", _SERVICE_NAME], check=True)
    print(f"  Service installed and started ({_SYSTEMD_UNIT_PATH})")
    _prompt_host_if_localhost()


def _systemd_uninstall() -> None:
    subprocess.run(["systemctl", "--user", "stop", _SERVICE_NAME])
    subprocess.run(["systemctl", "--user", "disable", _SERVICE_NAME])
    _SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print("  Service uninstalled")


def _systemd_start() -> None:
    subprocess.run(["systemctl", "--user", "start", _SERVICE_NAME], check=True)
    print("  Service started")


def _systemd_stop() -> None:
    subprocess.run(["systemctl", "--user", "stop", _SERVICE_NAME])
    print("  Service stopped")


def _systemd_restart() -> None:
    subprocess.run(["systemctl", "--user", "restart", _SERVICE_NAME], check=True)
    print("  Service restarted")


def _systemd_status() -> None:
    subprocess.run(["systemctl", "--user", "status", _SERVICE_NAME, "--no-pager"])


def _systemd_logs() -> None:
    try:
        subprocess.run(["journalctl", "--user", "-u", _SERVICE_NAME, "-f"])
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Private implementations — launchd (macOS)
# ---------------------------------------------------------------------------


def _launchd_install() -> None:
    dashboard_bin = _resolve_dashboard_bin()
    base_path = os.environ.get("PATH", "/usr/bin:/bin")
    safe_path = f"/opt/homebrew/bin:/usr/local/bin:{base_path}"
    plist_content = _LAUNCHD_PLIST_TEMPLATE.format(
        label=_LAUNCHD_LABEL,
        dashboard_bin=dashboard_bin,
        safe_path=safe_path,
        service_name=_SERVICE_NAME,
    )
    _LAUNCHD_PLIST_DIR.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PLIST_PATH.write_text(plist_content)
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(_LAUNCHD_PLIST_PATH)],
        check=True,
    )
    print(f"  Service installed and started ({_LAUNCHD_PLIST_PATH})")
    _prompt_host_if_localhost()


def _launchd_uninstall() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"])
    _LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
    print("  Service uninstalled")


def _launchd_start() -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(_LAUNCHD_PLIST_PATH)],
        check=True,
    )
    print("  Service started")


def _launchd_stop() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"])
    print("  Service stopped")


def _launchd_restart() -> None:
    _launchd_stop()
    _launchd_start()


def _launchd_status() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "print", f"gui/{uid}/{_LAUNCHD_LABEL}"])


def _launchd_logs() -> None:
    log_path = f"/tmp/{_SERVICE_NAME}.log"
    try:
        subprocess.run(["tail", "-f", log_path])
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Public API — platform-dispatching wrappers
# ---------------------------------------------------------------------------


def service_install() -> None:
    """Install the dashboard service unit for the current user."""
    if _is_darwin():
        _launchd_install()
    else:
        _systemd_install()


def service_uninstall() -> None:
    """Remove the dashboard service unit for the current user."""
    if _is_darwin():
        _launchd_uninstall()
    else:
        _systemd_uninstall()


def service_start() -> None:
    """Start the dashboard service."""
    if _is_darwin():
        _launchd_start()
    else:
        _systemd_start()


def service_stop() -> None:
    """Stop the dashboard service."""
    if _is_darwin():
        _launchd_stop()
    else:
        _systemd_stop()


def service_restart() -> None:
    """Restart the dashboard service."""
    if _is_darwin():
        _launchd_restart()
    else:
        _systemd_restart()


def service_status() -> None:
    """Print the current status of the dashboard service."""
    if _is_darwin():
        _launchd_status()
    else:
        _systemd_status()


def service_logs() -> None:
    """Stream or print logs for the dashboard service."""
    if _is_darwin():
        _launchd_logs()
    else:
        _systemd_logs()
