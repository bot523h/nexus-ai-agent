"""A-06 — the OpenAPI fail-closed route inventory (task-271, P2).

The HTTP surface of this service is the widest attack surface in the
repository, and it grew one router at a time: a public landing page, a
liveness probe, a bearer-token dashboard, a project-scoped Studio read
surface, a Telegram webhook with its own shared secret, and the frozen
legacy creative lane behind HMAC request signatures.  Every one of those
boundaries has its own unit test — and that is exactly the gap.  Nothing
proved that the set of operations the application *actually exposes* is
covered by those unit tests, so a new router (or a new operation inside an
existing router) could reach production without ever meeting a gate.

This module closes that gap.  It never trusts a hand-written list of route
names:

* the inventory is derived from the application's **own OpenAPI document**
  and cross-checked against the routes FastAPI really registered, so a
  router that is mounted but not described (or described but not mounted)
  is a failure — and a route hidden with ``include_in_schema=False`` is a
  failure too, because such an operation would be invisible to an
  OpenAPI-based inventory;
* every operation must be classified: either it is on the **explicit
  public allowlist** below, or it belongs to one named security surface.
  An unclassified operation fails the test with the offending method and
  path;
* every gated operation is then driven through the real ASGI app in three
  hostile states — security unconfigured, security configured but no
  credentials, security configured with wrong credentials — and the *exact*
  expected status is asserted, so a 404/405/422 can never be mistaken for
  "the gate ran";
* the positive states are proved per surface, including that a valid
  credential for one surface never opens another one, and that the Studio
  project scope is enforced *after* authentication;
* a controlled mutation probe proves the guard has teeth: a newly
  registered, unprotected route is detected, and the application is
  restored afterwards.

The suite is hermetic: a temporary SQLite database, explicitly pinned
environment variables, no outbound network, no real jobs, no persisted
uploads, and no leaked global state.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module

# ── the policy ──────────────────────────────────────────────────────────────

#: The **explicit public allowlist**: the application operations that are
#: public by design.  The landing page renders a static HTML shell and the
#: liveness probe answers "the process is up" without touching the
#: database; neither carries user data.  Nothing else may be public — a new
#: operation either joins this list (with a reason) or it inherits the gate
#: of the surface that owns it.
PUBLIC_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/"),
        ("GET", "/healthz"),
    }
)

#: The HTTP methods an OpenAPI path item may declare.  A path item may also
#: carry non-method keys (``parameters``, ``summary``, ``$ref``, …), so an
#: inventory that iterates the raw path item would invent operations that do
#: not exist.
HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: Sample values for the path parameters of the audited operations.
#: ``project_id`` must be inside the configured Studio project allowlist,
#: otherwise the project-scope gate would reject it and the inventory could
#: not tell "scope denied" from "route missing".
PATH_PARAM_VALUES: dict[str, str] = {
    "project_id": "proj-alpha",
    "job_id": "job-1",
}

#: The credentials used by the positive (authorised) cases.
DASHBOARD_TOKEN = "p2-dashboard-token"
HMAC_KEY = "p2-hmac-key-0123456789abcdef"
WEBHOOK_SECRET = "p2-webhook-secret"
AUTHORIZED_PROJECT = "proj-alpha"
DENIED_PROJECT = "proj-beta"


@dataclass(frozen=True)
class Surface:
    """One security boundary and the exact statuses it must produce.

    ``unconfigured_status`` is what the surface answers when its security
    configuration is absent (fail-closed), ``unauthenticated_status`` is what
    it answers when it is configured but the caller presents no or wrong
    credentials.  The two are deliberately *not* collapsed into one number:
    the contract distinguishes "you are not allowed" from "this deployment
    is not configured", and a test that accepted both would hide a gate that
    stopped running.
    """

    name: str
    #: The path prefix that owns the surface (mutually exclusive with
    #: ``exact_paths``).
    path_prefix: str = ""
    #: Exact paths owned by the surface.
    exact_paths: frozenset[str] = frozenset()
    #: Status when the security configuration is absent.
    unconfigured_status: int = 503
    #: Status when the surface is configured but the credentials are wrong.
    unauthenticated_status: int = 401
    #: The configuration that arms the surface (documentation only).
    configuration: str = ""

    def owns(self, operation: tuple[str, str]) -> bool:
        _method, path = operation
        if path in self.exact_paths:
            return True
        return bool(self.path_prefix) and path.startswith(self.path_prefix)

    def expected_status(self, state: str) -> int:
        """The status this surface must answer in the given probe state."""
        if state == "unconfigured":
            return self.unconfigured_status
        return self.unauthenticated_status


#: Every security boundary of the HTTP surface, in the order they are
#: matched.  A path belongs to exactly one surface; the inventory test
#: fails when an operation belongs to none.
SURFACES: tuple[Surface, ...] = (
    Surface(
        name="dashboard-bearer",
        path_prefix="/api/dashboard/",
        unconfigured_status=503,
        unauthenticated_status=401,
        configuration="NEXUS_DASHBOARD_TOKEN",
    ),
    Surface(
        name="studio-scoped",
        path_prefix="/api/studio/",
        unconfigured_status=503,
        unauthenticated_status=401,
        configuration="NEXUS_DASHBOARD_TOKEN + NEXUS_STUDIO_PROJECT_IDS",
    ),
    Surface(
        name="legacy-creative-hmac",
        path_prefix="/creative/",
        unconfigured_status=503,
        unauthenticated_status=401,
        configuration="NEXUS_API_HMAC_KEY",
    ),
    Surface(
        # The webhook denies rather than reporting "not configured": Telegram
        # retries a 5xx, and an unconfigured deployment must not look like a
        # transient failure that will fix itself.
        name="telegram-webhook-secret",
        exact_paths=frozenset({"/webhook/telegram"}),
        unconfigured_status=403,
        unauthenticated_status=403,
        configuration="NEXUS_WEBHOOK_SECRET",
    ),
)


def surface_of(operation: tuple[str, str]) -> Surface | None:
    """The surface that owns *operation*, or ``None`` when none does."""
    for surface in SURFACES:
        if surface.owns(operation):
            return surface
    return None


# ── the inventory ───────────────────────────────────────────────────────────


def _iter_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Yield every route reachable from *routes*, nested routers included.

    FastAPI changed how ``include_router`` is represented: older versions
    copy the router's routes into ``app.routes`` (flat), newer versions keep
    the router behind an ``_IncludedRouter`` wrapper.  An inventory that only
    looked at the flat list would silently lose a whole surface (the
    dashboard and the Studio router are both included routers here), so the
    walk follows the nesting in both shapes.
    """
    seen: set[int] = set()
    pending: list[Any] = list(routes)
    while pending:
        route = pending.pop(0)
        if id(route) in seen:
            continue
        seen.add(id(route))
        yield route
        for attribute in ("original_router", "router"):
            nested = getattr(route, attribute, None)
            nested_routes = getattr(nested, "routes", None)
            if nested_routes:
                pending.extend(nested_routes)


def _http_routes(app: FastAPI) -> dict[str, set[str]]:
    """Every registered HTTP route of the application: path → methods."""
    routes: dict[str, set[str]] = {}
    for route in _iter_routes(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method.lower() in HTTP_METHODS:
                routes.setdefault(path, set()).add(method.upper())
    return routes


def _documentation_paths(app: FastAPI) -> set[str]:
    """FastAPI's own documentation endpoints, read from the app's config."""
    candidates = (
        app.openapi_url,
        app.docs_url,
        app.redoc_url,
        getattr(app, "swagger_ui_oauth2_redirect_url", None),
    )
    return {path for path in candidates if path}


def _fresh_schema(app: FastAPI) -> Mapping[str, Any]:
    """The application's OpenAPI document, regenerated from the live routes.

    ``app.openapi()`` memoises its result.  A memoised document is exactly
    the failure mode this suite must catch (a route that was just
    registered, or removed, is not in the cache), so the cache is dropped
    first and the document is rebuilt from the routes that exist right now.
    """
    app.openapi_schema = None
    return app.openapi()


def _openapi_operations(schema: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The operations the OpenAPI document declares (methods only)."""
    operations: set[tuple[str, str]] = set()
    paths = schema.get("paths")
    if not isinstance(paths, Mapping):
        return operations
    for path, item in paths.items():
        if not isinstance(item, Mapping):
            continue
        for method in item:
            if method.lower() in HTTP_METHODS:
                operations.add((method.upper(), path))
    return operations


def _path_parameter_names(schema: Mapping[str, Any], path: str) -> list[str]:
    """The path parameters of one operation, read from the schema."""
    paths = schema.get("paths")
    item = paths.get(path) if isinstance(paths, Mapping) else None
    if not isinstance(item, Mapping):
        return []
    names: list[str] = []

    def _collect(parameters: Any) -> None:
        if not isinstance(parameters, list):
            return
        for parameter in parameters:
            if not isinstance(parameter, Mapping):
                continue
            if parameter.get("in") != "path":
                continue
            name = parameter.get("name")
            if isinstance(name, str) and name not in names:
                names.append(name)

    _collect(item.get("parameters"))
    for key, operation in item.items():
        if key.lower() not in HTTP_METHODS or not isinstance(operation, Mapping):
            continue
        _collect(operation.get("parameters"))
    return names


def _fill_path(path: str, names: Iterable[str]) -> str:
    """Replace every path parameter with a sample value."""
    filled = path
    for name in names:
        filled = filled.replace("{" + name + "}", PATH_PARAM_VALUES.get(name, f"p2-{name}"))
    assert "{" not in filled, f"unfilled path parameter in {path!r} (parameters: {list(names)})"
    return filled


@dataclass(frozen=True)
class Inventory:
    """What the application really serves, derived from the live app."""

    app: FastAPI
    schema: Mapping[str, Any]
    #: Every operation the OpenAPI document declares.
    operations: frozenset[tuple[str, str]]
    #: Every operation the application has registered a route for.
    registered: frozenset[tuple[str, str]]
    #: FastAPI's own documentation endpoints (registered, but not described
    #: by the OpenAPI document).
    documentation: frozenset[tuple[str, str]]

    @property
    def public(self) -> frozenset[tuple[str, str]]:
        """The operations that are public by design."""
        return PUBLIC_OPERATIONS | self.documentation

    @property
    def gated(self) -> frozenset[tuple[str, str]]:
        """The operations that must sit behind a security surface."""
        return self.operations - self.public


def build_inventory(app: FastAPI) -> Inventory:
    """Derive the inventory from the live application."""
    schema = _fresh_schema(app)
    operations = _openapi_operations(schema)
    documentation_paths = _documentation_paths(app)
    registered: set[tuple[str, str]] = set()
    documentation: set[tuple[str, str]] = set()
    for path, methods in _http_routes(app).items():
        for method in methods:
            operation = (method, path)
            registered.add(operation)
            if path in documentation_paths:
                documentation.add(operation)
    return Inventory(
        app=app,
        schema=schema,
        operations=frozenset(operations),
        registered=frozenset(registered),
        documentation=frozenset(documentation),
    )


# ── the probe ───────────────────────────────────────────────────────────────


def _sweep(
    client: TestClient,
    inventory: Inventory,
    state: str,
    headers: Mapping[str, str] | None = None,
) -> dict[tuple[str, str], str]:
    """Probe every gated operation and report the ones that misbehave.

    ``state`` is one of ``unconfigured`` (no security configuration at all),
    ``unauthenticated`` (configured, but no credentials) or ``wrong``
    (configured, but wrong credentials).  The expected status is the one the
    owning surface demands — never a set of "acceptable" codes, because a
    set would let a 404/405/422 pass as a gate that ran.

    The probe sends no body and no query parameters on purpose.  FastAPI
    solves the route's dependencies *before* it validates the body, the
    query and the path parameters (verified for the installed FastAPI), so
    a missing parameter can never turn a security rejection into a 422.
    """
    deviations: dict[tuple[str, str], str] = {}
    for operation in sorted(inventory.gated):
        method, path = operation
        url = _fill_path(path, _path_parameter_names(inventory.schema, path))
        response = client.request(method, url, headers=dict(headers) if headers else None)
        surface = surface_of(operation)
        if surface is None:
            deviations[operation] = (
                f"{method} {path}: no security surface owns this operation "
                f"(observed status {response.status_code})"
            )
            continue
        expected = surface.expected_status(state)
        if response.status_code != expected:
            deviations[operation] = (
                f"{method} {path}: expected {expected} for surface {surface.name!r} "
                f"in state {state!r}, got {response.status_code}: {response.text[:160]!r}"
            )
    return deviations


def _hmac_headers(key: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    """Sign ``body`` the way the legacy creative lane expects."""
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    message = f"{stamp}:".encode() + body
    signature = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return {"X-NEXUS-Timestamp": stamp, "X-NEXUS-Signature": signature}


def _multipart_upload(field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    """A deterministic multipart body (and its content type)."""
    boundary = "p2-route-inventory-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _creative_job_ids(creative_dir: Path) -> list[str]:
    """The job ids the legacy creative registry holds (empty when unused)."""
    db_file = creative_dir / "creative_jobs.sqlite3"
    if not db_file.exists():
        return []
    with sqlite3.connect(db_file) as connection:
        try:
            rows = connection.execute("SELECT id FROM creative_jobs").fetchall()
        except sqlite3.OperationalError:
            return []
    return sorted(str(row[0]) for row in rows)


def _run_async(coro: Any) -> Any:
    """Run a coroutine on a private loop, without touching the current one.

    ``asyncio.run`` installs its own loop as *the* current loop and, on
    Python 3.10, clears it again when it returns — which would fight with
    the loop pytest-asyncio manages for the surrounding test.  A private
    loop keeps the suite independent of that machinery on every supported
    interpreter (3.10, 3.11, 3.12).
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubApplication:
    """Minimal stand-in for a PTB application (bot + update queue)."""

    def __init__(self) -> None:
        self.bot = None
        self.update_queue: asyncio.Queue[Any] = asyncio.Queue()


# ── the environment ─────────────────────────────────────────────────────────


@dataclass
class ApiEnv:
    """A hermetic application environment."""

    app: FastAPI
    client: TestClient
    root: Path
    db_path: Path
    creative_dir: Path


def _reset_storage_caches() -> None:
    """Drop the cached engine/session factory so the next path is used."""
    db_module._engine = None  # type: ignore[attr-defined]
    db_module._engine_path = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]
    db_module._initialized_paths.clear()


@pytest.fixture()
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiEnv]:
    """The application wired to a temporary database and no inherited secrets.

    Isolation rules this fixture enforces:

    * the database path is pinned to an absolute file *inside the temporary
      directory* — changing the working directory alone would not stop a
      developer with an absolute ``NEXUS_DB_PATH`` from being written to;
    * every path-backed setting is pinned, so ``get_settings()`` cannot
      create directories inside the repository;
    * the inherited ``NEXUS_DATABASE_URL`` is removed, so the suite can
      never reach a developer's PostgreSQL;
    * every security variable is removed, so a developer's real token can
      never make a "fails closed" assertion pass;
    * the settings cache, the storage engine cache, the creative registry
      global and the application's webhook state are restored afterwards.
    """
    from nexus_ai_agent.api import app as api_app_module

    creative_dir = tmp_path / "creative"
    creative_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "langgraph.sqlite"))
    monkeypatch.setenv("NEXUS_VECTOR_PATH", str(tmp_path / "vector.sqlite"))
    monkeypatch.setenv("NEXUS_MODEL_PATH", str(tmp_path / "model.gguf"))
    monkeypatch.setenv("NEXUS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_dir))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:p2-route-inventory")
    for name in (
        "NEXUS_DATABASE_URL",
        "DATABASE_URL",
        "NEXUS_DASHBOARD_TOKEN",
        "NEXUS_API_HMAC_KEY",
        "NEXUS_WEBHOOK_SECRET",
        "WEBHOOK_SECRET",
        "NEXUS_STUDIO_PROJECT_IDS",
        "NEXUS_API_CORS_ORIGINS",
        # The un-prefixed aliases of the path settings: a host that exports
        # DB_PATH must not redirect the suite's database either.
        "DB_PATH",
        "CHECKPOINT_PATH",
        "VECTOR_PATH",
        "MODEL_PATH",
        "CACHE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    settings_module.get_settings.cache_clear()
    _reset_storage_caches()

    app = api_app_module.app
    saved_state = {
        "webhook_application": getattr(app.state, "webhook_application", None),
        "webhook_secret": getattr(app.state, "webhook_secret", None),
    }
    saved_registry = api_app_module._creative_registry  # noqa: SLF001
    app.state.webhook_application = None
    app.state.webhook_secret = None
    api_app_module._creative_registry = None  # noqa: SLF001

    with TestClient(app) as client:
        yield ApiEnv(
            app=app,
            client=client,
            root=tmp_path,
            db_path=tmp_path / "app.sqlite",
            creative_dir=creative_dir,
        )

    api_app_module._creative_registry = saved_registry  # noqa: SLF001
    app.state.webhook_application = saved_state["webhook_application"]
    app.state.webhook_secret = saved_state["webhook_secret"]
    settings_module.get_settings.cache_clear()
    # The engine is deliberately *not* nulled here: the repository's autouse
    # fixture (``tests/conftest.py::_dispose_db_engine``) disposes it on a
    # live event loop, and dropping the reference first would leak the
    # aiosqlite connection instead of closing it.
    db_module._initialized_paths.clear()


def _arm_security(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure every security surface (the "configured" probe state)."""
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", DASHBOARD_TOKEN)
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", HMAC_KEY)
    monkeypatch.setenv("NEXUS_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", AUTHORIZED_PROJECT)
    settings_module.get_settings.cache_clear()


# ── the inventory itself ────────────────────────────────────────────────────


def test_the_inventory_covers_every_registered_operation(api_env: ApiEnv) -> None:
    """The inventory is the application's own document, cross-checked.

    Three things must hold at once: the OpenAPI document describes exactly
    the routes the application registered, the operations it does *not*
    describe are only FastAPI's own documentation endpoints, and no
    operation hides behind ``include_in_schema=False`` (such an operation
    would be invisible to an OpenAPI-based inventory — the hole this whole
    suite exists to close).
    """
    inventory = build_inventory(api_env.app)

    described_but_unregistered = sorted(inventory.operations - inventory.registered)
    assert not described_but_unregistered, (
        "the OpenAPI document describes operations the application does not "
        f"serve: {described_but_unregistered}"
    )

    registered_but_undescribed = sorted(inventory.registered - inventory.operations)
    assert registered_but_undescribed == sorted(inventory.documentation), (
        "operations that the OpenAPI document does not describe are invisible "
        "to the inventory; only FastAPI's documentation endpoints may be "
        f"hidden: {registered_but_undescribed}"
    )

    hidden = sorted(
        (method, route.path)
        for route in _iter_routes(api_env.app.routes)
        if isinstance(route, APIRoute) and not route.include_in_schema
        for method in (route.methods or set())
    )
    assert not hidden, (
        f"operations excluded from the OpenAPI document are invisible to the inventory: {hidden}"
    )

    # The audited surfaces are really present (the routers are mounted).
    paths = {path for _method, path in inventory.operations}
    assert {"/api/dashboard/stats", "/api/dashboard/recent_users"} <= paths
    assert any(path.startswith("/api/studio/") for path in paths)
    assert {"/creative/video-edit", "/creative/jobs/{job_id}"} <= paths
    assert "/webhook/telegram" in paths
    assert {"/", "/healthz"} <= paths


def test_every_registered_operation_is_classified(api_env: ApiEnv) -> None:
    """Every operation is public by design, or owned by a named surface."""
    inventory = build_inventory(api_env.app)
    unclassified = sorted(
        operation
        for operation in inventory.operations
        if operation not in inventory.public and surface_of(operation) is None
    )
    assert not unclassified, (
        "operations that are neither public by design nor owned by a security "
        f"surface (classify them, or add them to the public allowlist): {unclassified}"
    )

    # Every declared surface must really be exercised by a real operation —
    # a policy entry that covers nothing would rot silently.
    for surface in SURFACES:
        owned = sorted(op for op in inventory.operations if surface.owns(op))
        assert owned, f"security surface {surface.name!r} owns no registered operation"


def test_the_public_allowlist_is_reachable_without_any_credential(api_env: ApiEnv) -> None:
    """The public surface is exactly the allowlist, and it stays public."""
    inventory = build_inventory(api_env.app)

    landing = api_env.client.get("/")
    assert landing.status_code == 200
    assert "text/html" in landing.headers.get("content-type", "")

    health = api_env.client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    # FastAPI's own documentation endpoints are public as well.  They are
    # listed here (not silently ignored) because an unauthenticated reader
    # can fetch the shape of the API from them; see the residual risk in the
    # P2 report.
    assert inventory.documentation, "FastAPI's documentation endpoints disappeared"
    for method, path in sorted(inventory.documentation):
        response = api_env.client.request(method, path)
        assert response.status_code == 200, f"{method} {path} is not reachable"

    # The public surface is exactly the allowlist plus those endpoints.
    public_paths = {path for _method, path in inventory.public}
    assert {"/", "/healthz"} <= public_paths


# ── the negative matrix ─────────────────────────────────────────────────────


def test_every_gated_operation_fails_closed_without_security_configuration(api_env: ApiEnv) -> None:
    """Unconfigured security must close every gated operation."""
    inventory = build_inventory(api_env.app)
    deviations = _sweep(api_env.client, inventory, "unconfigured")
    assert deviations == {}, f"operations that are not fail-closed: {deviations}"


def test_every_gated_operation_rejects_missing_credentials(api_env: ApiEnv, monkeypatch) -> None:
    """Configured security must reject a caller that presents no credential."""
    _arm_security(monkeypatch)
    inventory = build_inventory(api_env.app)
    deviations = _sweep(api_env.client, inventory, "unauthenticated")
    assert deviations == {}, f"operations that accepted a missing credential: {deviations}"


def test_every_gated_operation_rejects_wrong_credentials(api_env: ApiEnv, monkeypatch) -> None:
    """Configured security must reject wrong credentials of every kind."""
    _arm_security(monkeypatch)
    inventory = build_inventory(api_env.app)
    wrong = {
        "Authorization": "Bearer not-the-dashboard-token",
        "X-NEXUS-Timestamp": str(int(time.time())),
        "X-NEXUS-Signature": "00" * 32,
        "X-Telegram-Bot-Api-Secret-Token": "not-the-webhook-secret",
    }
    deviations = _sweep(api_env.client, inventory, "wrong", headers=wrong)
    assert deviations == {}, f"operations that accepted a wrong credential: {deviations}"


# ── the positive matrix ─────────────────────────────────────────────────────


def test_the_dashboard_surface_opens_with_the_configured_bearer_token(
    api_env: ApiEnv, monkeypatch
) -> None:
    """The dashboard is reachable with its own token — and only with it."""
    _arm_security(monkeypatch)
    inventory = build_inventory(api_env.app)
    dashboard = sorted(op for op in inventory.gated if op[1].startswith("/api/dashboard/"))
    assert dashboard, "the dashboard router is not registered"

    authorized = {"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
    for method, path in dashboard:
        response = api_env.client.request(method, path, headers=authorized)
        assert response.status_code == 200, f"{method} {path}: {response.text[:200]}"

    for method, path in dashboard:
        response = api_env.client.request(method, path, headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401, f"{method} {path} accepted a wrong token"


def test_the_studio_surface_enforces_project_scope_after_authentication(
    api_env: ApiEnv, monkeypatch
) -> None:
    """Authentication is not authorization: the project scope still applies."""
    _arm_security(monkeypatch)
    inventory = build_inventory(api_env.app)
    studio = sorted(op for op in inventory.gated if op[1].startswith("/api/studio/"))
    assert studio, "the studio router is not registered"

    from nexus_ai_agent.api.app import get_creative_registry

    registry = get_creative_registry()
    job_id = _run_async(registry.create_job("video_edit", {"project_id": AUTHORIZED_PROJECT}))

    authorized = {"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
    allowed = api_env.client.get(
        f"/api/studio/projects/{AUTHORIZED_PROJECT}/jobs/{job_id}/state", headers=authorized
    )
    assert allowed.status_code == 200, allowed.text[:200]
    assert allowed.json()["project_id"] == AUTHORIZED_PROJECT

    # The same credential, a project that is not in the allowlist: the
    # project-scope gate denies it even though the caller is authenticated.
    denied = api_env.client.get(
        f"/api/studio/projects/{DENIED_PROJECT}/jobs/{job_id}/state", headers=authorized
    )
    assert denied.status_code == 404, denied.text[:200]
    assert denied.json()["detail"] == "studio project not found"

    # And the very same job under the authorised project is not readable
    # without the token.
    unauthenticated = api_env.client.get(
        f"/api/studio/projects/{AUTHORIZED_PROJECT}/jobs/{job_id}/state"
    )
    assert unauthenticated.status_code == 401, unauthenticated.text[:200]


def test_the_legacy_creative_surface_opens_only_with_a_valid_signature(
    api_env: ApiEnv, monkeypatch
) -> None:
    """The HMAC gate must run before the handler, and reject stale signatures."""
    _arm_security(monkeypatch)

    # Missing signature headers.
    missing = api_env.client.post("/creative/video-edit", content=b"")
    assert missing.status_code == 401, missing.text[:200]
    assert "missing signature headers" in missing.json()["detail"]

    # A stale timestamp is rejected on its own terms.
    stale = api_env.client.post(
        "/creative/video-edit",
        content=b"",
        headers=_hmac_headers(HMAC_KEY, b"", timestamp=int(time.time()) - 4000),
    )
    assert stale.status_code == 401, stale.text[:200]
    assert "stale signature timestamp" in stale.json()["detail"]

    # A fresh timestamp with the wrong signature is rejected on its own terms.
    invalid = api_env.client.post(
        "/creative/video-edit",
        content=b"",
        headers=_hmac_headers("another-key", b""),
    )
    assert invalid.status_code == 401, invalid.text[:200]
    assert "invalid signature" in invalid.json()["detail"]

    # A valid signature reaches the handler (which then rejects the empty
    # form) — proof that the gate ran and let the request through.
    valid = api_env.client.post(
        "/creative/video-edit", content=b"", headers=_hmac_headers(HMAC_KEY, b"")
    )
    assert valid.status_code == 400, valid.text[:200]
    assert "Provide either file or video_url" in valid.json()["detail"]

    # The read half of the same lane is gated by the same signature: a job
    # row is only readable with a valid signature.
    from nexus_ai_agent.api.app import get_creative_registry

    registry = get_creative_registry()
    job_id = _run_async(registry.create_job("video_edit", {"project_id": AUTHORIZED_PROJECT}))
    read_ok = api_env.client.get(f"/creative/jobs/{job_id}", headers=_hmac_headers(HMAC_KEY, b""))
    assert read_ok.status_code == 200, read_ok.text[:200]
    assert read_ok.json()["id"] == job_id
    read_denied = api_env.client.get(
        f"/creative/jobs/{job_id}", headers=_hmac_headers("another-key", b"")
    )
    assert read_denied.status_code == 401, read_denied.text[:200]


def test_the_webhook_surface_opens_only_with_the_configured_shared_secret(
    api_env: ApiEnv, monkeypatch, sample_telegram_update: dict[str, Any]
) -> None:
    """The webhook has its own secret boundary, independent of the API token."""
    _arm_security(monkeypatch)
    secret_header = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}

    # The right secret, but no application published yet: the secret gate ran
    # and passed, the next stage reports "not ready" (a distinct status, so
    # the two gates can never be confused).
    not_ready = api_env.client.post(
        "/webhook/telegram", json=sample_telegram_update, headers=secret_header
    )
    assert not_ready.status_code == 503, not_ready.text[:200]
    assert not_ready.json()["error"] == "webhook application not ready"

    # The stub application is published *before* the rejected requests, so
    # the "nothing was enqueued" assertions below have teeth: a rejected
    # delivery must not reach the update queue.
    stub = _StubApplication()
    api_env.app.state.webhook_application = stub

    # Wrong secret.
    wrong = api_env.client.post(
        "/webhook/telegram",
        json=sample_telegram_update,
        headers=secret_header | {"X-Telegram-Bot-Api-Secret-Token": "nope"},
    )
    assert wrong.status_code == 403, wrong.text[:200]
    assert wrong.json()["error"] == "invalid secret"
    assert stub.update_queue.empty(), "a rejected delivery was enqueued"

    # Missing secret.
    missing = api_env.client.post("/webhook/telegram", json=sample_telegram_update)
    assert missing.status_code == 403, missing.text[:200]
    assert stub.update_queue.empty(), "a rejected delivery was enqueued"

    # The right secret and a published application: the update is accepted.
    accepted = api_env.client.post(
        "/webhook/telegram", json=sample_telegram_update, headers=secret_header
    )
    assert accepted.status_code == 200, accepted.text[:200]
    assert accepted.json() == {"ok": True}
    assert not stub.update_queue.empty()


def test_a_valid_credential_never_opens_another_surface(api_env: ApiEnv, monkeypatch) -> None:
    """The gates are independent: one surface's credential opens nothing else."""
    _arm_security(monkeypatch)

    # The dashboard token opens the dashboard — and nothing else.
    dashboard_only = {"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
    assert api_env.client.get("/api/dashboard/stats", headers=dashboard_only).status_code == 200
    creative = api_env.client.post("/creative/video-edit", content=b"", headers=dashboard_only)
    assert creative.status_code == 401, creative.text[:200]
    webhook = api_env.client.post("/webhook/telegram", json={}, headers=dashboard_only)
    assert webhook.status_code == 403, webhook.text[:200]

    # A valid HMAC signature opens the legacy creative lane — and nothing else.
    signed = _hmac_headers(HMAC_KEY, b"")
    assert (
        api_env.client.post("/creative/video-edit", content=b"", headers=signed).status_code == 400
    )
    dashboard = api_env.client.get("/api/dashboard/stats", headers=signed)
    assert dashboard.status_code == 401, dashboard.text[:200]
    studio = api_env.client.get(
        f"/api/studio/projects/{AUTHORIZED_PROJECT}/jobs/job-1/state", headers=signed
    )
    assert studio.status_code == 401, studio.text[:200]

    # The webhook secret opens the webhook — and nothing else.
    secret_only = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
    assert api_env.client.post("/webhook/telegram", json={}, headers=secret_only).status_code == 503
    assert api_env.client.get("/api/dashboard/stats", headers=secret_only).status_code == 401
    assert (
        api_env.client.post("/creative/video-edit", content=b"", headers=secret_only).status_code
        == 401
    )


# ── side effects ────────────────────────────────────────────────────────────


def test_a_rejected_creative_request_creates_no_job_and_persists_no_upload(
    api_env: ApiEnv, monkeypatch
) -> None:
    """The gate must run before the body is parsed and before anything is written."""
    _arm_security(monkeypatch)

    from nexus_ai_agent.api.app import get_creative_registry

    registry = get_creative_registry()
    _run_async(registry.create_job("video_edit", {"project_id": AUTHORIZED_PROJECT}))
    before = _creative_job_ids(api_env.creative_dir)
    assert before, "the creative registry is empty — the assertion below has no teeth"

    body, content_type = _multipart_upload("file", "probe.mp4", b"not-a-real-video")
    response = api_env.client.post(
        "/creative/video-edit",
        content=body,
        headers={
            "Content-Type": content_type,
            "X-NEXUS-Timestamp": str(int(time.time())),
            "X-NEXUS-Signature": "00" * 32,
        },
    )
    assert response.status_code == 401, response.text[:200]
    assert "invalid signature" in response.json()["detail"]

    # No new job row, and no upload file left behind.
    assert _creative_job_ids(api_env.creative_dir) == before
    assert list(api_env.creative_dir.glob("creative-upload-*")) == []


# ── the guard has teeth ─────────────────────────────────────────────────────


def test_a_newly_registered_unprotected_route_is_detected(api_env: ApiEnv, monkeypatch) -> None:
    """A new, unprotected operation must turn the inventory red.

    The probe route is registered on the live application for the duration of
    this test only and is removed again in the ``finally`` path, together
    with the OpenAPI cache it invalidated.
    """
    _arm_security(monkeypatch)
    before_routes = list(api_env.app.router.routes)

    @api_env.app.get("/__p2_unprotected_probe__")
    async def _unprotected_probe() -> dict[str, str]:
        return {"ok": "unprotected"}

    added = [route for route in api_env.app.router.routes if route not in before_routes]
    try:
        assert len(added) == 1, "the probe route was not registered"
        inventory = build_inventory(api_env.app)
        probe_operation = ("GET", "/__p2_unprotected_probe__")

        # 1. the operation is part of the inventory …
        assert probe_operation in inventory.operations, inventory.operations
        # 2. … it is not owned by any security surface …
        assert surface_of(probe_operation) is None
        # 3. … and the sweep reports it, with the status it really answers.
        deviations = _sweep(api_env.client, inventory, "wrong")
        assert probe_operation in deviations, deviations
        assert "200" in deviations[probe_operation], deviations[probe_operation]
        # 4. the probe really is reachable and unprotected.
        response = api_env.client.get("/__p2_unprotected_probe__")
        assert response.status_code == 200
    finally:
        for route in added:
            api_env.app.router.routes.remove(route)
        api_env.app.openapi_schema = None

    # The application is restored: no probe route, and the inventory is green
    # again.
    assert list(api_env.app.router.routes) == before_routes
    restored = build_inventory(api_env.app)
    assert ("GET", "/__p2_unprotected_probe__") not in restored.operations
    assert _sweep(api_env.client, restored, "wrong") == {}
