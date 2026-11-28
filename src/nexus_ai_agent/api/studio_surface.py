"""Serve the standalone Studio product surface at ``/studio/app``.

The product frame says a capability must be reachable without Telegram. The
Studio API (``/api/studio/...``) has existed for a while; this module adds the
human surface that exercises it, as a self-contained static bundle with no build
step and no framework.

It is deliberately separate from ``web/static`` (the PWA shell): that directory
is owned by the in-flight Studio client work, and a second writer there would
produce a merge conflict rather than a product. When the PWA shell grows the
same panels, delete this module and its directory.

Delivery follows the shell's rule: one literal route per allow-listed asset, no
directory serving and no request-derived path, so the API-gate inventory can
enumerate every operation this module exposes.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

SURFACE_DIR = Path(__file__).resolve().parents[1] / "web" / "studio_surface"
SURFACE_MOUNT = "/studio/app"
API_PREFIX = f"{SURFACE_MOUNT}/api"

#: The browser holds only this opaque handle; the principal token stays server-side.
PRINCIPAL_COOKIE = "nexus_studio_principal"
SESSION_TTL_SECONDS = 3600.0
_MAX_SESSIONS = 512
#: One principal may hold at most this many live sessions. Without it a single
#: caller refreshing in a loop could evict every other principal's session.
_MAX_SESSIONS_PER_PRINCIPAL = 8
#: Failed logins allowed per client before the door shuts, and for how long.
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_MAX_TRACKED_KEYS = 1024

#: Files the surface needs; a missing one is a packaging bug, not a 404.
REQUIRED_FILES = ("index.html", "studio.css", "studio.js")

_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "studio.css": "text/css; charset=utf-8",
    "studio.js": "text/javascript; charset=utf-8",
}

_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}

router = APIRouter(tags=["studio-surface"])


def _asset_response(name: str) -> Response:
    return FileResponse(
        SURFACE_DIR / name,
        media_type=_CONTENT_TYPES[name],
        headers=dict(_HEADERS),
    )


@router.get(SURFACE_MOUNT)
async def studio_surface_redirect() -> Response:
    # Relative asset URLs only resolve under the trailing slash.
    return RedirectResponse(f"{SURFACE_MOUNT}/", status_code=307, headers=dict(_HEADERS))


@router.get(f"{SURFACE_MOUNT}/")
async def studio_surface_index(request: Request) -> Response:
    return _asset_response("index.html")


def _register_asset_route(name: str) -> None:
    async def _serve(request: Request) -> Response:
        return _asset_response(name)

    router.add_api_route(
        f"{SURFACE_MOUNT}/{name}", _serve, methods=["GET"], name=f"studio_surface_asset_{name}"
    )


for _asset in REQUIRED_FILES:
    if _asset != "index.html":
        _register_asset_route(_asset)


# ── the authenticated lane ──────────────────────────────────────────────────
#
# The bundle is public bytes; the data it shows is not. A principal signs in
# once with their token, and the server keeps that token: the browser receives
# only an opaque handle in an HttpOnly, SameSite=Strict, Secure cookie. The
# token therefore never lands in localStorage, never appears in a URL, and is
# never echoed by a response — the three places a token leaks in practice.
#
# The lane is a thin, explicit proxy over ``api/studio_ibpc.py``: each route
# re-uses the real handler, so authentication, membership authorization and
# every transition rule stay in one place. Nothing here re-implements them.


@dataclass(frozen=True)
class PrincipalSession:
    """An authenticated browser session; ``token`` never leaves the process."""

    session_id: str
    principal_id: str
    tenant_id: str
    token: str
    expires_at: float


class PrincipalSessionRegistry:
    """Bounded, expiring, in-process store of authenticated browser sessions.

    Sessions die with the process, and that is the intended trade for a
    single-process deployment: a restart forces a re-login instead of
    resurrecting a credential nobody can revoke. The store is bounded twice so
    a flood of logins cannot grow memory without limit *or* starve another
    principal: a principal keeps at most ``max_sessions_per_principal`` live
    sessions (its own oldest are evicted first), and the store as a whole keeps
    at most ``max_sessions``.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = SESSION_TTL_SECONDS,
        capacity: int = _MAX_SESSIONS,
        max_sessions_per_principal: int = _MAX_SESSIONS_PER_PRINCIPAL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_sessions_per_principal < 1:
            raise ValueError("max_sessions_per_principal must be positive")
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._per_principal = max_sessions_per_principal
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, PrincipalSession] = {}

    def issue(self, *, principal_id: str, tenant_id: str, token: str) -> PrincipalSession:
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            # Evict only *this* principal's oldest session first: one principal
            # refreshing its own page must never evict another principal's live
            # session — that would turn a login storm into a targeted DoS.
            while self._count_for(principal_id) >= self._per_principal:
                self._evict_oldest_locked(principal_id=principal_id)
            while len(self._sessions) >= self._capacity:
                self._evict_oldest_locked()
            record = PrincipalSession(
                session_id=secrets.token_urlsafe(32),
                principal_id=principal_id,
                tenant_id=tenant_id,
                token=token,
                expires_at=now + self._ttl,
            )
            self._sessions[record.session_id] = record
            return record

    def _count_for(self, principal_id: str) -> int:
        return sum(1 for item in self._sessions.values() if item.principal_id == principal_id)

    def _evict_oldest_locked(self, *, principal_id: str | None = None) -> None:
        candidates = [
            item
            for item in self._sessions.values()
            if principal_id is None or item.principal_id == principal_id
        ]
        if not candidates:  # pragma: no cover - the caller counts this principal first
            return
        oldest = min(candidates, key=lambda item: item.expires_at)
        del self._sessions[oldest.session_id]

    def resolve(self, session_id: str | None) -> PrincipalSession | None:
        if not session_id:
            return None
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            return self._sessions.get(session_id)

    def revoke(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _expire_locked(self, now: float) -> None:
        for key in [key for key, item in self._sessions.items() if item.expires_at <= now]:
            del self._sessions[key]


session_registry = PrincipalSessionRegistry()


class LoginAttemptLimiter:
    """Sliding-window limit on failed logins, keyed by client.

    A brute-force attempt against ``/login`` costs one principal-token
    comparison; without a limiter an attacker with a fast connection has
    unlimited tries. The clock is injected and must be **monotonic**, so a
    system time change cannot open or close the window, and tests can advance it
    without sleeping.

    Memory is bounded: expired buckets are pruned on every write, and if that is
    not enough the oldest bucket is evicted, so ``max_tracked_keys`` is a hard
    ceiling rather than a target.
    """

    def __init__(
        self,
        *,
        max_failures: int = _LOGIN_MAX_FAILURES,
        window_seconds: float = _LOGIN_WINDOW_SECONDS,
        max_tracked_keys: int = _LOGIN_MAX_TRACKED_KEYS,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_tracked_keys < 1:
            raise ValueError("max_tracked_keys must be positive")
        self._max_failures = max_failures
        self._window = float(window_seconds)
        self._max_keys = max_tracked_keys
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _prune_locked(self, key: str, now: float) -> list[float]:
        cutoff = now - self._window
        kept = [stamp for stamp in self._failures.get(key, []) if stamp > cutoff]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)
        return kept

    def _evict_oldest_locked(self) -> None:
        if not self._failures:  # pragma: no cover - pruning always leaves the caller above 0
            return
        oldest_key = min(self._failures, key=lambda key: max(self._failures[key]))
        del self._failures[oldest_key]

    def check(self, key: str, *, now: float) -> float | None:
        """Return seconds to wait when the key is blocked, else ``None``.

        The wait is the time until the oldest failure of the window expires —
        never a fixed value, so the client is told the truth.
        """
        with self._lock:
            recent = self._prune_locked(key, now)
            if len(recent) < self._max_failures:
                return None
            return max(self._window - (now - min(recent)), 0.0)

    def record_failure(self, key: str, *, now: float) -> None:
        with self._lock:
            recent = self._prune_locked(key, now)
            recent.append(now)
            self._failures[key] = recent
            if len(self._failures) >= self._max_keys:
                self._prune_all_locked(now)
                while len(self._failures) >= self._max_keys:
                    self._evict_oldest_locked()

    def record_success(self, key: str) -> None:
        """A successful login clears the key; a legitimate user is never penalised."""
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        """Forget every bucket (test isolation, or an operator-driven reset)."""
        with self._lock:
            self._failures.clear()

    def _prune_all_locked(self, now: float) -> None:
        for key in list(self._failures):
            self._prune_locked(key, now)

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._failures)


login_limiter = LoginAttemptLimiter()


def _client_key(request: Request) -> str:
    """Rate-limit key: the connecting address, or one bucket when unknown.

    ``request.client`` is the transport peer, never a client-supplied header —
    ``X-Forwarded-For`` is trivially spoofed and would let an attacker rotate
    keys for free.
    """
    client = request.client
    if client is not None and client.host:
        return client.host
    return "local"


def _too_many_attempts(wait_seconds: float) -> JSONResponse:
    """The 429 returned when a client has exhausted its failed-login budget.

    A response rather than an ``HTTPException`` because the body must be flat
    (``{"detail": ..., "retry_after_seconds": ...}``); FastAPI nests a dict
    ``detail`` under another ``detail`` key, which is not the documented shape.
    """
    retry_after = max(1, int(wait_seconds + 0.999))
    return JSONResponse(
        status_code=429,
        content={"detail": "rate_limit_exceeded", "retry_after_seconds": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


def _record_login_failure(client_key: str) -> None:
    login_limiter.record_failure(client_key, now=time.monotonic())


def _require_marker(request: Request) -> None:
    """A state-changing call must carry the CSRF marker the bundle always sends."""
    from nexus_ai_agent.api.studio_session import CSRF_HEADER, CSRF_VALUE

    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail="missing request marker")


def _current_session(request: Request) -> PrincipalSession:
    record = session_registry.resolve(request.cookies.get(PRINCIPAL_COOKIE))
    if record is None:
        raise HTTPException(status_code=401, detail="studio app session required")
    return record


SessionDep = Annotated[PrincipalSession, Depends(_current_session)]
BodyDep = Annotated[Any, Body()]


def _validated(model: type[BaseModel], body: Any) -> Any:
    """Validate a proxy body with the owning module's own model.

    The lane forwards to the real handlers, so the schema must be *their*
    schema: a copy here would drift. A bad body is a 422, never a 500.
    """
    from pydantic import ValidationError

    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"invalid request body: {exc}") from exc


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_token: str = Field(min_length=8, max_length=512)


login_router = APIRouter(prefix=API_PREFIX, tags=["studio-surface"])
api_router = APIRouter(prefix=API_PREFIX, tags=["studio-surface"])


def _auth_store_path() -> Path:
    """Where the principal store lives, resolved exactly like the IBPC lane does."""
    import os

    from nexus_ai_agent.adapters.studio_principal_auth import durable_studio_auth_path

    configured = os.getenv("NEXUS_STUDIO_AUTH_DB", "").strip()
    if configured:
        return Path(configured)
    root = os.getenv("NEXUS_STUDIO_WORKSPACE_ROOT", "").strip()
    return durable_studio_auth_path(Path(root) if root else Path("workspace"))


@login_router.post("/login", dependencies=[Depends(_require_marker)])
async def studio_surface_login(body: LoginRequest, request: Request, response: Response) -> Any:
    """Exchange a principal token for an opaque server-side session."""
    from nexus_ai_agent.adapters.studio_principal_auth import PrincipalAuthenticationError
    from nexus_ai_agent.api.studio_ibpc import get_studio_auth_store

    client_key = _client_key(request)
    wait = login_limiter.check(client_key, now=time.monotonic())
    if wait is not None:
        # Refused before the store is touched: a throttled client learns nothing
        # about whether its token would have been valid.
        return _too_many_attempts(wait)

    # Opening the store *creates* it. A login probe must never mint durable
    # state as a side effect: an unregistered deployment fails closed here, and
    # a later real registration still finds an honest "no principals" answer.
    if not _auth_store_path().is_file():
        _record_login_failure(client_key)
        raise HTTPException(status_code=401, detail="principal_authentication_unavailable")
    store = get_studio_auth_store()
    try:
        try:
            principal_id, tenant_id = store.authenticate_principal_context(body.principal_token)
        except PrincipalAuthenticationError as exc:
            _record_login_failure(client_key)
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        store.close()
    login_limiter.record_success(client_key)
    record = session_registry.issue(
        principal_id=principal_id, tenant_id=tenant_id, token=body.principal_token
    )
    response.set_cookie(
        PRINCIPAL_COOKIE,
        record.session_id,
        max_age=int(SESSION_TTL_SECONDS),
        path=API_PREFIX,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return {
        "principal_id": record.principal_id,
        "tenant_id": record.tenant_id,
        "expires_at": int(record.expires_at),
    }


@api_router.post("/logout", dependencies=[Depends(_require_marker)])
async def studio_surface_logout(session: SessionDep, response: Response) -> dict[str, Any]:
    """Revoke this browser's session; unauthenticated callers are refused."""
    session_registry.revoke(session.session_id)
    response.delete_cookie(
        PRINCIPAL_COOKIE, path=API_PREFIX, secure=True, httponly=True, samesite="strict"
    )
    return {"authenticated": False}


@api_router.get("/me")
async def studio_surface_me(session: SessionDep) -> dict[str, Any]:
    return {
        "principal_id": session.principal_id,
        "tenant_id": session.tenant_id,
        "expires_at": int(session.expires_at),
    }


@api_router.get("/sessions")
async def studio_surface_sessions(
    session: SessionDep,
    project_id: Annotated[str, Query(min_length=1, max_length=128)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """List the durable IBPC sessions of a project the caller may read."""
    from nexus_ai_agent.api import studio_ibpc
    from nexus_ai_agent.nagar.ibpc.authority import StudioAction

    store = studio_ibpc.get_studio_auth_store()
    try:
        binding_ref = store.default_binding_for(session.principal_id) or ""
        studio_ibpc._authorize(
            store,
            principal_id=session.principal_id,
            project_id=project_id,
            action=StudioAction.READ_SESSION,
            binding_ref=binding_ref,
        )
        # Defence in depth: the authorizer already refuses a cross-tenant project
        # (``cross_tenant_denied``) before this line runs, so a session's tenant and
        # its project's tenant cannot disagree while that check holds. This second
        # line stays for the day the mapping is edited directly in the database and
        # the authorizer's join is changed with it; it is unreachable today, which
        # is exactly what the uncovered line records.
        if store.get_project_tenant(project_id) != session.tenant_id:  # pragma: no cover
            raise HTTPException(status_code=403, detail="tenant_mismatch")
        service = studio_ibpc._build_service(store, binding_ref=binding_ref)
        try:
            rows = service.sessions.list_sessions(
                tenant_id=session.tenant_id, project_id=project_id, limit=limit
            )
        finally:
            service.close()
    finally:
        store.close()
    return {"project_id": project_id, "sessions": list(rows)}


@api_router.post(
    "/projects/{project_id}/ibpc/sessions",
    status_code=201,
    dependencies=[Depends(_require_marker)],
)
async def studio_surface_open_session(project_id: str, body: BodyDep, session: SessionDep) -> Any:
    from nexus_ai_agent.api import studio_ibpc

    payload = _validated(studio_ibpc.OpenSessionRequest, body)
    return await studio_ibpc.open_ibpc_session(
        project_id=project_id, body=payload, x_nexus_principal_token=session.token
    )


@api_router.get("/projects/{project_id}/ibpc/sessions/{session_id}")
async def studio_surface_get_session(project_id: str, session_id: str, session: SessionDep) -> Any:
    from nexus_ai_agent.api import studio_ibpc

    return await studio_ibpc.get_ibpc_session(
        project_id=project_id, session_id=session_id, x_nexus_principal_token=session.token
    )


@api_router.post(
    "/projects/{project_id}/ibpc/sessions/{session_id}/close",
    dependencies=[Depends(_require_marker)],
)
async def studio_surface_close_session(
    project_id: str, session_id: str, session: SessionDep
) -> Any:
    from nexus_ai_agent.api import studio_ibpc

    return await studio_ibpc.close_ibpc_session(
        project_id=project_id, session_id=session_id, x_nexus_principal_token=session.token
    )


@api_router.post(
    "/projects/{project_id}/ibpc/sessions/{session_id}/turns",
    status_code=201,
    dependencies=[Depends(_require_marker)],
)
async def studio_surface_submit_turn(
    project_id: str, session_id: str, body: BodyDep, session: SessionDep
) -> Any:
    from nexus_ai_agent.api import studio_ibpc

    payload = _validated(studio_ibpc.SubmitTurnRequest, body)
    return await studio_ibpc.submit_ibpc_turn(
        project_id=project_id,
        session_id=session_id,
        body=payload,
        x_nexus_principal_token=session.token,
    )


@api_router.post(
    "/projects/{project_id}/ibpc/sessions/{session_id}/turns/{turn_id}/approve",
    dependencies=[Depends(_require_marker)],
)
async def studio_surface_approve_turn(
    project_id: str, session_id: str, turn_id: str, body: BodyDep, session: SessionDep
) -> Any:
    from nexus_ai_agent.api import studio_ibpc

    payload = _validated(studio_ibpc.ApproveTurnRequest, body)
    return await studio_ibpc.approve_ibpc_turn(
        project_id=project_id,
        session_id=session_id,
        turn_id=turn_id,
        body=payload,
        x_nexus_principal_token=session.token,
    )


@api_router.post(
    "/projects/{project_id}/ibpc/sessions/{session_id}/turns/{turn_id}/execute",
    dependencies=[Depends(_require_marker)],
)
async def studio_surface_execute_turn(
    project_id: str, session_id: str, turn_id: str, body: BodyDep, session: SessionDep
) -> Any:
    from nexus_ai_agent.api import studio_ibpc

    payload = _validated(studio_ibpc.ApproveTurnRequest, body)
    return await studio_ibpc.execute_ibpc_turn(
        project_id=project_id,
        session_id=session_id,
        turn_id=turn_id,
        body=payload,
        x_nexus_principal_token=session.token,
    )


#: Every authenticated operation of this lane, with the handler that serves it,
#: for the API-gate inventory and for a reviewer who wants the surface in one
#: glance. ``tests/unit/test_studio_surface_security.py`` asserts that this table
#: and the installed router agree, so a route cannot be added without appearing
#: here — and every non-GET entry must declare the CSRF marker dependency.
@dataclass(frozen=True)
class AuthenticatedRoute:
    method: str
    path: str
    handler: str


def authenticated_surface_routes() -> tuple[AuthenticatedRoute, ...]:
    """The authenticated surface, in one place, keyed by handler name."""
    base = API_PREFIX
    sessions = f"{base}/projects/{{project_id}}/ibpc/sessions"
    turns = f"{sessions}/{{session_id}}/turns"
    return (
        AuthenticatedRoute("POST", f"{base}/login", "studio_surface_login"),
        AuthenticatedRoute("POST", f"{base}/logout", "studio_surface_logout"),
        AuthenticatedRoute("GET", f"{base}/me", "studio_surface_me"),
        AuthenticatedRoute("GET", f"{base}/sessions", "studio_surface_sessions"),
        AuthenticatedRoute("POST", sessions, "studio_surface_open_session"),
        AuthenticatedRoute("GET", f"{sessions}/{{session_id}}", "studio_surface_get_session"),
        AuthenticatedRoute(
            "POST", f"{sessions}/{{session_id}}/close", "studio_surface_close_session"
        ),
        AuthenticatedRoute("POST", turns, "studio_surface_submit_turn"),
        AuthenticatedRoute("POST", f"{turns}/{{turn_id}}/approve", "studio_surface_approve_turn"),
        AuthenticatedRoute("POST", f"{turns}/{{turn_id}}/execute", "studio_surface_execute_turn"),
    )


#: Every authenticated operation of this lane. Derived from the table above so
#: the two can never drift.
AUTHENTICATED_SURFACE_PATHS: tuple[str, ...] = tuple(
    dict.fromkeys(route.path for route in authenticated_surface_routes())
)


#: Every operation this bundle serves; the allow-list *is* the route table.
PUBLIC_SURFACE_PATHS: tuple[str, ...] = (
    SURFACE_MOUNT,
    f"{SURFACE_MOUNT}/",
    *(f"{SURFACE_MOUNT}/{name}" for name in REQUIRED_FILES if name != "index.html"),
)


def install_studio_surface(app: FastAPI) -> None:
    """Register the surface routes; fail loudly when the wheel omitted a file."""
    missing = [name for name in REQUIRED_FILES if not (SURFACE_DIR / name).is_file()]
    if missing:
        raise RuntimeError(f"studio surface bundle is incomplete: {', '.join(missing)}")
    app.include_router(router)
    app.include_router(login_router)
    app.include_router(api_router)


__all__ = [
    "API_PREFIX",
    "AUTHENTICATED_SURFACE_PATHS",
    "AuthenticatedRoute",
    "LoginAttemptLimiter",
    "PRINCIPAL_COOKIE",
    "PUBLIC_SURFACE_PATHS",
    "REQUIRED_FILES",
    "SURFACE_DIR",
    "SURFACE_MOUNT",
    "PrincipalSession",
    "PrincipalSessionRegistry",
    "authenticated_surface_routes",
    "install_studio_surface",
    "login_limiter",
    "session_registry",
]
