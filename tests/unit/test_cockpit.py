"""Cockpit evidence: catalog truth, effect-free validation and HTTP boundaries.

These tests run offline with the real builtin registry and its real Pydantic
models. No Telegram/LLM/DB runtime is created. Browser behavior has a separate
optional, executable smoke harness: scripts/test_cockpit_browser.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, model_validator

from nexus_ai_agent.cockpit import app as app_module
from nexus_ai_agent.cockpit.app import CockpitConfig, create_app
from nexus_ai_agent.cockpit.catalog import (
    MAX_BODY_BYTES,
    MAX_ERRORS,
    NOT_CHECKED,
    Catalog,
    CatalogUnavailable,
    UnknownOperation,
)
from nexus_ai_agent.creative.packs.registry import PackRegistry
from nexus_ai_agent.creative.packs.runtime import build_pack_runtime
from nexus_ai_agent.creative.studio.capabilities import OperationSpec
from nexus_ai_agent.creative.studio.models import PermissionLevel

ROOT = Path(__file__).parents[2]
TOKEN = "unit-test-only-not-a-real-credential-123456"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
VALID_TRIM = {"clip_asset_id": "not-a-real-asset", "in_point_us": 0, "out_point_us": 5_000_000}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog()


@pytest.fixture()
def preview(catalog: Catalog):
    with TestClient(
        create_app(config=CockpitConfig(public_preview=True), catalog=catalog)
    ) as client:
        yield client


@pytest.fixture()
def protected(catalog: Catalog):
    with TestClient(create_app(config=CockpitConfig(token=TOKEN), catalog=catalog)) as client:
        yield client


def test_catalog_uses_real_registry_counts_schemas_and_trust(catalog: Catalog) -> None:
    runtime = build_pack_runtime()
    snapshot = catalog.snapshot()
    assert snapshot["scope"] == "installed_catalog"
    assert snapshot["bot_connected"] is False
    assert snapshot["execution_enabled"] is False
    assert snapshot["summary"]["operations"] == len(runtime.operation_ids())
    assert snapshot["summary"]["packs"] == len(runtime.status())
    assert snapshot["summary"]["domains"] == len(runtime.registry.list_domains())
    assert snapshot["summary"]["confirmation_required"] == sum(
        runtime.registry.get_spec(op).permission_level == PermissionLevel.CONFIRMATION
        for op in runtime.operation_ids()
    )
    assert {op["id"] for op in snapshot["operations"]} == set(runtime.operation_ids())
    for op in snapshot["operations"]:
        description = runtime.registry.describe(op["id"])
        assert op["input_schema"] == description.operation_schema
        assert op["required_permissions"] == list(description.required_permissions)
        assert op["reference_fields"] == list(runtime.registry.get_spec(op["id"]).reference_fields)
    for pack in snapshot["packs"]:
        registered = runtime.packs.get(pack["id"])
        assert pack["signature_state"] == registered.report.signature_state
        assert pack["signature_verified"] == registered.report.trusted
        assert pack["anchor"] == "builtin"
        assert pack["active_in_catalog"] is False
        assert "source" not in pack
    # Crucial current truth: builtin placeholders are not Ed25519 verification.
    placeholders = [p for p in snapshot["packs"] if p["signature_state"] == "placeholder"]
    assert placeholders
    assert all(p["signature_verified"] is False for p in placeholders)


def test_fingerprint_is_stable_but_snapshot_is_detached(catalog: Catalog) -> None:
    a, b = catalog.snapshot(), catalog.snapshot()
    assert a["fingerprint"] == b["fingerprint"] == Catalog().fingerprint
    assert len(a["fingerprint"]) == 64
    assert "observed_at" in a
    a["operations"].clear()
    a["summary"]["operations"] = -1
    assert catalog.snapshot()["operations"] == b["operations"]
    assert catalog.snapshot()["summary"] == b["summary"]


def test_fingerprint_changes_with_the_real_contract() -> None:
    runtime = build_pack_runtime()
    before = Catalog(runtime).fingerprint

    class ProbeInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        amount: int

    def never_execute(*_args: Any) -> Any:
        raise AssertionError("handler must not execute")

    runtime.registry.register_operation(
        "probe",
        "test",
        OperationSpec(
            "probe.test", "test-only spec", PermissionLevel.IMMEDIATE, ProbeInput, never_execute
        ),
    )
    assert Catalog(runtime).fingerprint != before


def test_catalog_never_activates_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("inspection must not activate a pack")

    monkeypatch.setattr(PackRegistry, "activate", forbidden)
    assert Catalog().snapshot()["packs"]


def test_missing_pack_data_is_not_an_empty_green_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PackRegistry, "builtin_manifest_paths", staticmethod(lambda root=None: []))
    with pytest.raises(CatalogUnavailable):
        Catalog()
    with pytest.raises(CatalogUnavailable):
        Catalog(build_pack_runtime())


def test_canonical_validation_accepts_and_rejects_real_models(catalog: Catalog) -> None:
    good = catalog.validate_input("timeline.trim", VALID_TRIM)
    assert good["valid"] is True
    assert good["scope"] == "input_schema_only"
    assert good["executed"] is False and good["execution_authorized"] is False
    assert good["not_checked"] == list(NOT_CHECKED)
    assert good["defaulted_fields"] == ["output_asset_id"]
    assert good["catalog_fingerprint"] == catalog.fingerprint
    assert "input" not in good and "normalized_input" not in good
    bad = catalog.validate_input("timeline.trim", {**VALID_TRIM, "out_point_us": 0})
    assert bad["valid"] is False
    assert bad["errors"][0]["code"] == "value_error"
    missing = catalog.validate_input("timeline.trim", {})
    assert missing["error_count"] == 3
    assert {e["path"][0] for e in missing["errors"]} == set(VALID_TRIM)


def test_coercion_matches_the_existing_input_model(catalog: Catalog) -> None:
    payload = {**VALID_TRIM, "out_point_us": "5000000"}
    assert catalog.validate_input("timeline.trim", payload)["valid"] is True


def test_validation_never_invokes_the_operation_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = build_pack_runtime()
    original = runtime.registry.get_spec

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("validation must never invoke a handler")

    monkeypatch.setattr(
        runtime.registry,
        "get_spec",
        lambda operation: replace(original(operation), handler=forbidden),
    )
    catalog = Catalog(runtime)
    assert catalog.validate_input("timeline.trim", VALID_TRIM)["valid"]
    assert catalog.validate_input("media.pause", {})["valid"]
    # Even a C-level operation can have schema-valid input, without authorization,
    # filesystem I/O, a renderer, or evidence of execution.
    result = catalog.validate_input(
        "slideshow.render",
        {
            "output_path": "/not-a-real-file.mp4",
            "output_sha256": "not-real-evidence",
            "duration_us": 1,
            "parent_asset_ids": ["synthetic"],
        },
    )
    assert result["valid"] is True
    assert result["executed"] is False and result["execution_authorized"] is False


def test_input_validation_cannot_probe_files_or_network(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins
    import socket

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("input-only validation attempted an external effect")

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(Path, "open", forbidden)
        guard.setattr(Path, "stat", forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(subprocess, "run", forbidden)
        result = catalog.validate_input(
            "slideshow.render",
            {
                "output_path": "/never-read.mp4",
                "output_sha256": "not-evidence",
                "duration_us": 1,
                "parent_asset_ids": ["not-a-real-asset"],
            },
        )
    assert result["valid"] is True and result["executed"] is False


def test_errors_do_not_echo_custom_validator_messages_context_or_input() -> None:
    runtime = build_pack_runtime()

    class SecretInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        content: str

        @model_validator(mode="after")
        def reject(self) -> SecretInput:
            raise ValueError(f"should never leave server: {self.content}")

    def never_execute(*_args: Any) -> Any:
        raise AssertionError("handler invoked")

    runtime.registry.register_operation(
        "probe",
        "secret",
        OperationSpec(
            "probe.secret", "test-only", PermissionLevel.IMMEDIATE, SecretInput, never_execute
        ),
    )
    result = Catalog(runtime).validate_input("probe.secret", {"content": "SECRET_SENTINEL"})
    assert result["valid"] is False
    serialized = json.dumps(result)
    assert "SECRET_SENTINEL" not in serialized
    assert "should never leave server" not in serialized
    assert "ctx" not in serialized and "input" not in result["errors"][0]


def test_unknown_field_names_are_redacted_and_errors_are_capped(catalog: Catalog) -> None:
    result = catalog.validate_input("media.pause", {f"SECRET_KEY_{i}": "SECRET" for i in range(40)})
    assert result["valid"] is False
    assert result["error_count"] == 40
    assert len(result["errors"]) == MAX_ERRORS
    assert all(error["path"] == ["?"] for error in result["errors"])
    assert "SECRET" not in json.dumps(result)


def test_unregistered_operation_is_not_a_schema_or_handler_lookup(catalog: Catalog) -> None:
    with pytest.raises(UnknownOperation):
        catalog.validate_input("system.shell", {"command": "whatever"})


@pytest.mark.parametrize("path", ["/api/cockpit/catalog", "/api/cockpit/validate"])
def test_default_api_is_fail_closed_without_token(catalog: Catalog, path: str) -> None:
    with TestClient(create_app(config=CockpitConfig(), catalog=catalog)) as client:
        response = (
            client.post(path, content=b"not json")
            if path.endswith("validate")
            else client.get(path)
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "authentication_not_configured"
    assert response.headers["Cache-Control"] == "no-store"


def test_factory_only_reads_the_dedicated_token(
    monkeypatch: pytest.MonkeyPatch, catalog: Catalog
) -> None:
    monkeypatch.delenv("NEXUS_COCKPIT_TOKEN", raising=False)
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", TOKEN)
    with TestClient(create_app(catalog=catalog)) as client:
        assert client.get("/api/cockpit/catalog", headers=AUTH).status_code == 503
    monkeypatch.setenv("NEXUS_COCKPIT_TOKEN", TOKEN)
    with TestClient(create_app(catalog=catalog)) as client:
        assert client.get("/api/cockpit/catalog", headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic bad"},
        {"Authorization": "Bearer bad"},
        {"Authorization": "Bearer " + "x" * 1_025},
    ],
)
def test_bad_or_missing_auth_cannot_read_or_validate(
    protected: TestClient, headers: dict[str, str]
) -> None:
    for response in (
        protected.get("/api/cockpit/catalog", headers=headers),
        protected.post("/api/cockpit/validate", headers=headers, content=b"broken"),
    ):
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert TOKEN not in response.text


def test_query_parameter_and_cookie_are_not_auth(protected: TestClient) -> None:
    protected.cookies.set("token", TOKEN)
    response = protected.get(f"/api/cockpit/catalog?token={TOKEN}")
    assert response.status_code == 401
    assert TOKEN not in response.text


def test_authorized_requests_work_without_cookies_or_token_reflection(
    protected: TestClient,
) -> None:
    response = protected.get("/api/cockpit/catalog", headers={"Authorization": f"bEaReR {TOKEN}"})
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert "set-cookie" not in response.headers
    response = protected.post(
        "/api/cockpit/validate",
        headers=AUTH,
        json={"operation": "timeline.trim", "input": VALID_TRIM},
    )
    assert response.status_code == 200 and response.json()["valid"]
    assert TOKEN not in response.text


@pytest.mark.parametrize(
    "token", ["short", " " * 32, "x" * 1_025, "x" * 31 + "é", "x" * 16 + "\t" + "y" * 16]
)
def test_bad_secret_configuration_is_rejected_without_reflecting_secret(token: str) -> None:
    with pytest.raises(ValueError) as exc:
        CockpitConfig(token=token)
    assert token not in str(exc.value)
    assert TOKEN not in repr(CockpitConfig(token=TOKEN))


def test_preview_cannot_accidentally_override_a_configured_token() -> None:
    with pytest.raises(ValueError):
        CockpitConfig(public_preview=True, token=TOKEN)


def test_session_and_liveness_do_not_claim_bot_readiness(
    protected: TestClient, preview: TestClient
) -> None:
    assert protected.get("/api/cockpit/session").json() == {
        "mode": "protected",
        "requires_token": True,
        "configured": True,
        "execution_enabled": False,
    }
    assert preview.get("/api/cockpit/session").json()["mode"] == "public_preview"
    assert preview.get("/healthz").json() == {
        "status": "ok",
        "scope": "cockpit_process",
        "bot_connected": False,
    }


def test_public_preview_exposes_only_catalog_metadata(
    preview: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "APIKEY_SENTINEL")
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://user:PW_SENTINEL@internal/db")
    response = preview.get("/api/cockpit/catalog")
    assert response.status_code == 200
    assert "SENTINEL" not in response.text
    assert str(ROOT) not in response.text
    assert not {"settings", "users", "jobs", "database_url", "token"} & set(response.json())


def test_unknown_operation_is_404_not_execution(preview: TestClient) -> None:
    response = preview.post(
        "/api/cockpit/validate", json={"operation": "system.shell", "input": {}}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_operation"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        1,
        "secret",
        {},
        {"operation": "media.pause"},
        {"operation": 42, "input": {}},
        {"operation": "media.pause", "input": []},
        {"operation": "media.pause", "input": None},
        {"operation": "media.pause", "input": {}, "confirmed": True},
        {"operation": "../private", "input": {}},
        {"operation": "x" * 129, "input": {}},
    ],
)
def test_invalid_envelope_is_generic_and_not_reflected(preview: TestClient, payload: Any) -> None:
    response = preview.post(
        "/api/cockpit/validate",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_envelope"}}


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"\xff",
        b'{"operation":"media.pause","input":{"x":NaN}}',
        b'{"operation":"media.pause","input":{"x":Infinity}}',
        b'{"operation":"media.pause","input":{"x":1e999}}',
        b'{"operation":"media.pause","operation":"media.play","input":{}}',
        b'{"operation":"media.pause","input":{"x":1,"x":2}}',
    ],
)
def test_ambiguous_or_invalid_json_is_rejected(preview: TestClient, body: bytes) -> None:
    response = preview.post(
        "/api/cockpit/validate", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "invalid_json"}}


def test_large_body_rejected_both_by_header_and_by_stream(preview: TestClient) -> None:
    body = b" " * (MAX_BODY_BYTES + 1)
    for content, headers in [
        (body, {"Content-Type": "application/json"}),
        (iter([body[:20_000], body[20_000:]]), {"Content-Type": "application/json"}),
        (body, {"Content-Type": "application/json", "Content-Length": "0"}),
    ]:
        response = preview.post("/api/cockpit/validate", content=content, headers=headers)
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "input_too_large"


@pytest.mark.parametrize("length", ["-1", "not-a-number"])
def test_invalid_length_is_rejected(preview: TestClient, length: str) -> None:
    response = preview.post(
        "/api/cockpit/validate",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": length},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ],
)
def test_non_json_and_encoded_bodies_are_not_accepted(
    preview: TestClient, headers: dict[str, str]
) -> None:
    assert preview.post("/api/cockpit/validate", content=b"{}", headers=headers).status_code == 415


def test_json_depth_and_node_count_are_bounded(preview: TestClient) -> None:
    deep: Any = 0
    for _ in range(25):
        deep = [deep]
    for value in (deep, list(range(2_049))):
        response = preview.post(
            "/api/cockpit/validate", json={"operation": "media.pause", "input": {"x": value}}
        )
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "input_too_complex"


async def test_four_validation_slots_do_not_queue_unbounded_readers(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read = app_module._read_payload
    entered, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def blocked_read(request: Request) -> app_module.ValidationRequest:
        nonlocal reads
        reads += 1
        if reads == 4:
            entered.set()
        await release.wait()
        return await real_read(request)

    monkeypatch.setattr(app_module, "_read_payload", blocked_read)
    app = create_app(config=CockpitConfig(public_preview=True), catalog=catalog)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = [
            asyncio.create_task(
                client.post("/api/cockpit/validate", json={"operation": "media.pause", "input": {}})
            )
            for _ in range(4)
        ]
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            busy = await client.post("/api/cockpit/validate", json={})
            assert busy.status_code == 503
            assert busy.json()["detail"]["code"] == "validation_busy"
            assert reads == 4  # rejected request never began reading its body
            assert (await client.get("/healthz")).status_code == 200
        finally:
            release.set()
            responses = await asyncio.gather(*pending)
        assert all(response.status_code == 200 for response in responses)
        assert (
            await client.post(
                "/api/cockpit/validate", json={"operation": "media.pause", "input": {}}
            )
        ).status_code == 200


async def test_slow_body_has_a_deadline_and_releases_capacity(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read = app_module._read_payload

    async def never_read(_request: Request) -> app_module.ValidationRequest:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(app_module, "_read_payload", never_read)
    monkeypatch.setattr(app_module, "BODY_TIMEOUT_SECONDS", 0.01)
    app = create_app(config=CockpitConfig(public_preview=True), catalog=catalog)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        timed_out = await client.post("/api/cockpit/validate", json={})
        assert timed_out.status_code == 408
        monkeypatch.setattr(app_module, "_read_payload", real_read)
        assert (
            await client.post(
                "/api/cockpit/validate", json={"operation": "media.pause", "input": {}}
            )
        ).status_code == 200


def test_json_content_type_with_charset_is_accepted(preview: TestClient) -> None:
    result = preview.post(
        "/api/cockpit/validate",
        content='{"operation":"media.pause","input":{}}',
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert result.status_code == 200 and result.json()["valid"]


@pytest.mark.parametrize(
    "path",
    [
        "/docs",
        "/openapi.json",
        "/api/dashboard/stats",
        "/api/cockpit/execute",
        "/creative/video-edit",
        "/webhook/telegram",
        "/static/%2e%2e/app.py",
    ],
)
def test_no_legacy_mutating_or_arbitrary_file_surface(preview: TestClient, path: str) -> None:
    assert preview.get(path).status_code == 404
    assert preview.post(path, json={}).status_code in (404, 405)


def test_same_origin_security_headers_and_preview_host(preview: TestClient) -> None:
    response = preview.get(
        "/", headers={"Host": "3000-sandbox.e2b.app", "Origin": "https://untrusted.example"}
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]
    assert "'unsafe-eval'" not in response.headers["content-security-policy"]
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert "object-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "x-frame-options" not in response.headers  # Arena preview can embed this read-only app
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "name,mime",
    [
        ("cockpit.js", "javascript"),
        ("cockpit.css", "text/css"),
        ("icons.svg", "image/svg+xml"),
        ("mark.svg", "image/svg+xml"),
        ("orbit.svg", "image/svg+xml"),
        ("vazirmatn.woff2", "font/woff2"),
        ("OFL.txt", "text/plain"),
    ],
)
def test_assets_are_local_and_available_without_runtime_or_auth(
    protected: TestClient, name: str, mime: str
) -> None:
    response = protected.get(f"/static/{name}")
    assert response.status_code == 200
    assert mime in response.headers["content-type"]
    assert len(response.content) > 20


def test_font_is_licensed_and_pack_data_is_in_the_wheel_manifest() -> None:
    assets = ROOT / "src/nexus_ai_agent/cockpit/static"
    assert (assets / "vazirmatn.woff2").read_bytes().startswith(b"wOF2")
    assert "SIL OPEN FONT LICENSE" in (assets / "OFL.txt").read_text()
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "recursive-include src/nexus_ai_agent/cockpit/static" in manifest
    assert "recursive-include src/nexus_ai_agent/creative/packs *.json" in manifest


def test_cli_help_and_fail_closed_startup() -> None:
    env = {key: value for key, value in os.environ.items() if key != "NEXUS_COCKPIT_TOKEN"}
    command = [sys.executable, "-m", "nexus_ai_agent.cockpit"]
    help_result = subprocess.run(
        command + ["--help"], env=env, text=True, capture_output=True, timeout=15
    )
    assert help_result.returncode == 0
    assert "INPUT SCHEMA ONLY" in help_result.stdout
    missing = subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
    assert missing.returncode == 2
    assert "NEXUS_COCKPIT_TOKEN" in missing.stderr and "--preview" in missing.stderr
    bad_port = subprocess.run(
        command + ["--preview", "--port", "0"], env=env, text=True, capture_output=True, timeout=15
    )
    assert bad_port.returncode == 2


def test_standalone_import_does_not_boot_database_bot_or_provider() -> None:
    probe = """
import sys
from nexus_ai_agent.cockpit.app import create_app, CockpitConfig
from fastapi.testclient import TestClient
with TestClient(create_app(config=CockpitConfig(public_preview=True))) as client:
    assert client.get('/api/cockpit/catalog').status_code == 200
    response = client.post('/api/cockpit/validate',
                           json={'operation':'media.pause','input':{}})
    assert response.json()['valid']
for prefix in ('nexus_ai_agent.bot', 'nexus_ai_agent.api', 'nexus_ai_agent.storage',
               'nexus_ai_agent.features', 'nexus_ai_agent.llm',
               'telegram', 'langgraph', 'sqlmodel', 'llama_cpp', 'litellm'):
    assert not any(m == prefix or m.startswith(prefix + '.') for m in sys.modules), prefix
# The existing creative/__init__ imports video_director -> settings definitions.
# That harmless import is not Settings construction or a .env/provider read.
from nexus_ai_agent.config.settings import get_settings
assert get_settings.cache_info().misses == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
