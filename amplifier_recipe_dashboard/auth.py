"""
amplifier-recipe-dashboard authentication — password and signing secret management.

Ported from muxplex/auth.py — same middleware cascade, adapted for the dashboard.
"""

from __future__ import annotations

import base64
import logging
import secrets
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

_log = logging.getLogger(__name__)

# Soft import: python-pam is optional
try:
    import pam  # noqa: F401

    _PAM_AVAILABLE = True
except ImportError:
    _PAM_AVAILABLE = False


# ---------------------------------------------------------------------------
# Config directory
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    """Return ~/.config/amplifier-recipe-dashboard, creating it (mode 0700) if needed."""
    d = Path.home() / ".config" / "amplifier-recipe-dashboard"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Password file management
# ---------------------------------------------------------------------------


def get_password_path() -> Path:
    """Return the path to the password file."""
    return Path.home() / ".config" / "amplifier-recipe-dashboard" / "password"


def load_password() -> str | None:
    """Read the password file if it exists, return None otherwise."""
    path = get_password_path()
    if not path.exists():
        return None
    return path.read_text().strip()


def generate_and_save_password() -> str:
    """Generate a random password, write it to the password file (0600), return it."""
    pw = secrets.token_urlsafe(20)
    path = get_password_path()
    _config_dir()  # ensures dir exists with mode 0700
    path.write_text(pw + "\n")
    path.chmod(0o600)
    return pw


# ---------------------------------------------------------------------------
# Secret (signing key) management
# ---------------------------------------------------------------------------


def get_secret_path() -> Path:
    """Return the path to the signing secret file."""
    return Path.home() / ".config" / "amplifier-recipe-dashboard" / "secret"


def load_or_create_secret() -> str:
    """Load the signing secret from file, or create one if it doesn't exist."""
    path = get_secret_path()
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_urlsafe(32)
    _config_dir()  # ensures dir exists
    path.write_text(secret + "\n")
    path.chmod(0o600)
    return secret


# ---------------------------------------------------------------------------
# Session cookie signing / verification
# ---------------------------------------------------------------------------


def create_session_cookie(secret: str) -> str:
    """Create a signed, timestamped session cookie value."""
    signer = TimestampSigner(secret)
    return signer.sign("dashboard-session").decode()


def verify_session_cookie(secret: str, cookie: str, ttl_seconds: int) -> bool:
    """Verify a session cookie's signature and expiry.

    ttl_seconds=0 means session cookie — no server-side expiry check.
    """
    signer = TimestampSigner(secret)
    try:
        max_age = ttl_seconds if ttl_seconds > 0 else None
        signer.unsign(cookie, max_age=max_age)
        return True
    except (BadSignature, SignatureExpired):
        return False


# ---------------------------------------------------------------------------
# PAM authentication
# ---------------------------------------------------------------------------


def pam_available() -> bool:
    """Check whether the python-pam module is importable."""
    return _PAM_AVAILABLE


def authenticate_pam(username: str, password: str) -> bool:
    """Authenticate via PAM. Username must match the running process owner."""
    import os
    import pwd

    import pam as _pam

    running_user = pwd.getpwuid(os.getuid()).pw_name
    if username != running_user:
        return False
    return _pam.authenticate(username, password, service="login")


# ---------------------------------------------------------------------------
# Auth mode resolution
# ---------------------------------------------------------------------------


def resolve_auth_mode(settings_auth: str) -> tuple[str, str]:
    """Resolve the auth mode and password at startup.

    Returns (mode, password) where mode is "pam", "password", or "none".
    For "pam" mode, password is empty. For "none" mode, password is empty.
    """
    import os
    import sys

    # 0. Auth explicitly disabled
    if settings_auth == "none":
        return "none", ""

    # 1. Settings has auth: "password" → force password mode
    if settings_auth == "password":
        # Try env var first
        env_pw = os.environ.get("DASHBOARD_PASSWORD", "")
        if env_pw:
            return "password", env_pw
        # Try password file
        file_pw = load_password()
        if file_pw:
            return "password", file_pw
        # Auto-generate
        pw = generate_and_save_password()
        print(f"\n  Auto-generated password: {pw}", file=sys.stderr)
        print(f"  Saved to: {get_password_path()}\n", file=sys.stderr)
        return "password", pw

    # 2. python-pam importable → PAM mode
    if pam_available():
        return "pam", ""

    # 3. DASHBOARD_PASSWORD env var → password mode
    env_pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if env_pw:
        return "password", env_pw

    # 4. Password file exists → use it
    file_pw = load_password()
    if file_pw:
        return "password", file_pw

    # 5. Auto-generate password
    pw = generate_and_save_password()
    print(f"\n  Auto-generated password: {pw}", file=sys.stderr)
    print(f"  Saved to: {get_password_path()}\n", file=sys.stderr)
    return "password", pw


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

# Paths that bypass auth (login page itself, auth endpoints)
_AUTH_EXEMPT_PATHS = {"/login", "/auth/login", "/auth/mode", "/auth/logout"}

# File extensions that are always served without auth — the login page needs
# its own CSS, JS, images, and fonts before the user has a session cookie.
_STATIC_EXTENSIONS = {
    ".css",
    ".js",
    ".svg",
    ".png",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
    ".json",
}

# Socket-level localhost addresses — cannot be forged via HTTP headers
_LOCALHOST_ADDRS = {"127.0.0.1", "::1"}


class AuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that enforces authentication on non-localhost requests."""

    def __init__(
        self,
        app: object,
        auth_mode: str,
        secret: str,
        ttl_seconds: int,
        password: str = "",
    ):
        super().__init__(app)  # type: ignore[arg-type]
        self.auth_mode = auth_mode
        self.secret = secret
        self.ttl_seconds = ttl_seconds
        self.password = password

    async def dispatch(self, request: Request, call_next: object) -> Response:
        # 1. Localhost bypass — client.host is the socket-level IP
        client_host = request.client.host if request.client else ""
        if client_host in _LOCALHOST_ADDRS:
            return await call_next(request)  # type: ignore[misc]

        # 2. Exempt paths (login page, auth endpoints)
        if request.url.path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)  # type: ignore[misc]

        # 3. Paths starting with /auth/
        if request.url.path.startswith("/auth/"):
            return await call_next(request)  # type: ignore[misc]

        # 4. Static assets — login page needs its CSS/JS/images before auth
        path = request.url.path
        if any(path.endswith(ext) for ext in _STATIC_EXTENSIONS):
            return await call_next(request)  # type: ignore[misc]

        # 5. Valid session cookie
        cookie = request.cookies.get("dashboard_session")
        if cookie and verify_session_cookie(self.secret, cookie, self.ttl_seconds):
            return await call_next(request)  # type: ignore[misc]

        # 6. Authorization: Basic header
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                username, _, pw = decoded.partition(":")
                if self._check_credentials(username, pw):
                    return await call_next(request)  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse({"detail": "Invalid credentials"}, status_code=401)

        # 7. No auth — redirect browsers, 401 for API clients
        accept = request.headers.get("accept", "")
        if "application/json" in accept:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return RedirectResponse(url="/login", status_code=307)

    def _check_credentials(self, username: str, password: str) -> bool:
        """Validate credentials against the configured auth mode."""
        if self.auth_mode == "pam":
            return authenticate_pam(username, password)
        return password == self.password
