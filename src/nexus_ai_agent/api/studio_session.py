"""Browser session wrapper over the existing dashboard gate (NAGAR Studio web app).

There is exactly one credential in the system for the Studio HTTP surface: the
deployment's ``NEXUS_DASHBOARD_TOKEN`` (see :mod:`nexus_ai_agent.api.dashboard`).
A browser must never hold that token in script-readable storage, so this module
lets an operator exchange it **once** for a short-lived, ``HttpOnly`` cookie.

Design (web/README.md):

* The cookie is a stateless, tamper-evident capability ``v1.<exp>.<nonce>.<mac>``.
  The MAC key is *derived* from the dashboard token with a domain-separation label,
  so there is no second secret to provision and rotating the dashboard token
  invalidates every outstanding session at once.
* There is no server-side session table. That keeps "one store, one authority":
  the price is that a single stolen cookie cannot be revoked before ``exp``
  (``SESSION_TTL_SECONDS``); rotate the dashboard token to revoke all of them.
* The session proves *the same thing* the bearer header proves (knowledge of the
  deployment token). It grants no extra authority and no per-user identity. Project
  scope stays the server-side ``NEXUS_STUDIO_PROJECT_IDS`` policy.
* State-changing session routes (login/logout) require a custom request header, so
  a cross-site form post cannot drive them; the cookie is also ``SameSite=Strict``.
* Everything fails closed: no configured token -> ``503``; malformed, expired,
  future-dated or forged cookie -> treated as absent.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from nexus_ai_agent.api.dashboard import require_dashboard_token
from nexus_ai_agent.config.settings import get_settings

#: ``__Host-`` pins the cookie to this exact origin: it requires ``Secure``,
#: ``Path=/`` and forbids a ``Domain`` attribute.
SESSION_COOKIE = "__Host-nagar_studio"
SESSION_TTL_SECONDS = 4 * 60 * 60
#: Custom header a login/logout request must carry (forces a CORS preflight).
CSRF_HEADER = "x-nagar-studio"
CSRF_VALUE = "1"

_KEY_LABEL = b"nagar.studio.session.v1"
_VERSION = "v1"

#: Global failed-login throttle. Behind a proxy the peer address is not a reliable
#: client identity, so the budget is deliberately global rather than per-client: it
#: bounds online guessing against the one shared credential and only ever blocks
#: *new logins*, never an existing session or the bearer header.
FAILED_LOGIN_LIMIT = 10
FAILED_LOGIN_WINDOW_SECONDS = 60.0

_failures: deque[float] = deque()
_failures_lock = threading.Lock()


def reset_login_throttle() -> None:
    """Forget recorded failures (used by tests)."""
    with _failures_lock:
        _failures.clear()


def _throttled(now: float) -> float | None:
    """Seconds until another login attempt is allowed, or ``None``."""
    with _failures_lock:
        while _failures and now - _failures[0] > FAILED_LOGIN_WINDOW_SECONDS:
            _failures.popleft()
        if len(_failures) >= FAILED_LOGIN_LIMIT:
            return max(1.0, FAILED_LOGIN_WINDOW_SECONDS - (now - _failures[0]))
    return None


def _record_failure(now: float) -> None:
    with _failures_lock:
        _failures.append(now)


def _session_key(dashboard_token: str) -> bytes:
    return hmac.new(dashboard_token.encode(), _KEY_LABEL, hashlib.sha256).digest()


def _mac(key: bytes, body: str) -> str:
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def issue_session_value(dashboard_token: str, *, now: float | None = None) -> tuple[str, int]:
    """Return ``(cookie_value, expires_at_unix)`` for a freshly authenticated caller."""
    issued = int(time.time() if now is None else now)
    expires = issued + SESSION_TTL_SECONDS
    body = f"{_VERSION}.{expires}.{secrets.token_hex(16)}"
    return f"{body}.{_mac(_session_key(dashboard_token), body)}", expires


def verify_session_value(
    value: str | None, dashboard_token: str, *, now: float | None = None
) -> int | None:
    """Return the session expiry (unix seconds) if ``value`` is valid, else ``None``."""
    if not value or len(value) > 256:
        return None
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != _VERSION:
        return None
    version, expires_text, nonce, mac = parts
    if not expires_text.isdigit() or not (16 <= len(nonce) <= 64):
        return None
    expires = int(expires_text)
    current = time.time() if now is None else now
    # Reject expired cookies and cookies dated further ahead than we could have issued.
    if expires <= current or expires - current > SESSION_TTL_SECONDS + 60:
        return None
    expected = _mac(_session_key(dashboard_token), f"{version}.{expires_text}.{nonce}")
    if not hmac.compare_digest(mac, expected):
        return None
    return expires


def _configured_token() -> str:
    token = get_settings().api_dashboard_token
    if not token:
        raise HTTPException(status_code=503, detail="Security configuration incomplete")
    return token


def authenticate_studio_caller(request: Request, authorization: str | None) -> None:
    """Authenticate a Studio HTTP caller: bearer header **or** valid session cookie.

    An ``Authorization`` header, when present, is authoritative and is judged by the
    existing dashboard gate (no fallback to the cookie), so adding browser sessions
    cannot weaken or change the bearer behaviour.
    """
    token = _configured_token()
    if authorization is not None:
        require_dashboard_token(authorization)
        return
    if verify_session_value(request.cookies.get(SESSION_COOKIE), token) is None:
        raise HTTPException(status_code=401, detail="authentication required")


def require_studio_caller(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """FastAPI dependency form of :func:`authenticate_studio_caller`."""
    authenticate_studio_caller(request, authorization)


def _require_csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail="missing request marker")


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


router = APIRouter(prefix="/api/studio", tags=["studio-session"])


@router.get("/session")
async def get_session_state(request: Request) -> dict[str, object]:
    """Report whether the caller holds a valid session (never an error for 'no')."""
    token = get_settings().api_dashboard_token
    if not token:
        return {"configured": False, "authenticated": False, "expires_at": None}
    expires = verify_session_value(request.cookies.get(SESSION_COOKIE), token)
    return {"configured": True, "authenticated": expires is not None, "expires_at": expires}


@router.post("/session")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    """Exchange the deployment token for a short-lived HttpOnly session cookie."""
    expected = _configured_token()
    _require_csrf_header(request)
    now = time.time()
    wait = _throttled(now)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail="too many failed attempts",
            headers={"Retry-After": str(int(wait))},
        )
    if not hmac.compare_digest(body.token.encode(), expected.encode()):
        _record_failure(now)
        raise HTTPException(status_code=401, detail="invalid credentials")
    value, expires = issue_session_value(expected, now=now)
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return {"authenticated": True, "expires_at": expires}


@router.delete("/session")
async def logout(request: Request, response: Response) -> dict[str, object]:
    """Clear the session cookie in this browser (stateless: see module docstring)."""
    _require_csrf_header(request)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return {"authenticated": False}


__all__ = [
    "CSRF_HEADER",
    "CSRF_VALUE",
    "SESSION_COOKIE",
    "SESSION_TTL_SECONDS",
    "authenticate_studio_caller",
    "issue_session_value",
    "require_studio_caller",
    "reset_login_throttle",
    "router",
    "verify_session_value",
]
