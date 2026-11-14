"""Isolated same-origin UI/API with fail-closed authentication and bounded input.

Security contract:
* Unless explicitly in PUBLIC CATALOG PREVIEW, the API needs the dedicated
  NEXUS_COCKPIT_TOKEN. Missing configuration is 503, missing/wrong auth is 401.
* Preview exposes only builtin catalog metadata and schema validation. It does
  not expose settings, paths, users, jobs, databases or runtime telemetry.
* No CORS, cookies, token query parameters, third-party scripts, or request-body
  logging. Client credentials live in memory only. TLS belongs at the proxy.
* Streaming size and JSON complexity bounds precede Pydantic; at most four
  concurrent validation requests per process, including body reads. This is
  not a distributed rate limiter. Apply ingress limits on a public deployment.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from nexus_ai_agent.cockpit.catalog import (
    MAX_BODY_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    Catalog,
    UnknownOperation,
)

STATIC_ROOT = Path(__file__).with_name("static")
BODY_TIMEOUT_SECONDS = 10.0
CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    )
)


@dataclass(frozen=True)
class CockpitConfig:
    public_preview: bool = False
    token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.public_preview and self.token:
            raise ValueError("Choose protected mode OR public preview, not both.")
        if self.token and (len(self.token) < 32 or len(self.token) > 1_024):
            raise ValueError("NEXUS_COCKPIT_TOKEN must contain 32 to 1024 characters.")
        if self.token and any(not 33 <= ord(char) <= 126 for char in self.token):
            raise ValueError("NEXUS_COCKPIT_TOKEN must be printable ASCII without whitespace.")


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.]*$")
    input: dict[str, Any]


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _check_complexity(value: Any) -> None:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise _error(413, "input_too_complex")
        if isinstance(item, float) and not math.isfinite(item):
            raise _error(400, "invalid_json")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


async def _read_payload(request: Request) -> ValidationRequest:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise _error(415, "json_required")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise _error(415, "encoding_not_supported")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError:
            raise _error(400, "invalid_content_length") from None
        if size < 0:
            raise _error(400, "invalid_content_length")
        if size > MAX_BODY_BYTES:
            raise _error(413, "input_too_large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise _error(413, "input_too_large")
        body.extend(chunk)
    try:
        data = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise _error(400, "invalid_json") from None
    _check_complexity(data)
    try:
        return ValidationRequest.model_validate(data)
    except ValidationError:
        raise _error(422, "invalid_envelope") from None


def create_app(*, config: CockpitConfig | None = None, catalog: Catalog | None = None) -> FastAPI:
    """Create the opt-in cockpit, without importing the bot or the legacy API.

    Omitting config reads ONLY the dedicated token environment variable; it
    never loads .env or other project Settings. For ASGI servers, use
    ``uvicorn nexus_ai_agent.cockpit.app:create_app --factory --host 0.0.0.0``.
    """
    selected = config or CockpitConfig(token=os.environ.get("NEXUS_COCKPIT_TOKEN"))
    view = catalog or Catalog()
    slots = asyncio.Semaphore(4)
    app = FastAPI(title="NEXUS Cockpit", docs_url=None, redoc_url=None, openapi_url=None)

    async def authorize(request: Request) -> None:
        if selected.public_preview:
            return
        if not selected.token:
            raise _error(503, "authentication_not_configured")
        header = request.headers.get("authorization", "")
        scheme, _, credential = header.partition(" ")
        if (
            scheme.lower() != "bearer"
            or len(credential) > 1_024
            or not hmac.compare_digest(credential.encode("utf-8"), selected.token.encode("ascii"))
        ):
            raise HTTPException(
                status_code=401,
                detail={"code": "authentication_required"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.middleware("http")
    async def safety_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        # No frame-ancestors / X-Frame-Options: this effect-free surface is
        # intentionally embeddable in Arena's HTTPS preview. No broad CORS.
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html", media_type="text/html")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "scope": "cockpit_process", "bot_connected": False}

    @app.get("/api/cockpit/session")
    async def session() -> dict[str, Any]:
        return {
            "mode": "public_preview" if selected.public_preview else "protected",
            "requires_token": not selected.public_preview,
            "configured": selected.public_preview or bool(selected.token),
            "execution_enabled": False,
        }

    @app.get("/api/cockpit/catalog", dependencies=[Depends(authorize)])
    async def get_catalog() -> dict[str, Any]:
        return view.snapshot()

    @app.post("/api/cockpit/validate", dependencies=[Depends(authorize)])
    async def validate(request: Request) -> JSONResponse:
        if slots.locked():
            raise _error(503, "validation_busy")
        async with slots:
            # Timeout includes a slow streamed body. Explicit asyncio exception
            # keeps the supported Python 3.10 floor (not TaskGroup/timeout()).
            try:
                payload = await asyncio.wait_for(
                    _read_payload(request), timeout=BODY_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                raise _error(408, "input_timeout") from None
            try:
                result = await run_in_threadpool(
                    view.validate_input, payload.operation, payload.input
                )
            except UnknownOperation:
                raise _error(404, "unknown_operation") from None
            return JSONResponse(result)

    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="cockpit-static")
    return app
