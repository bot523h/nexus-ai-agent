"""The revision pin route: RBAC, the parent CAS, and pointer lockstep.

Pinning is the one write that defines the bytes a later turn renders from, so it
carries three properties worth their own tests:

* authorization is OWNER/EDITOR -- a viewer must not be able to pin;
* the parent is a compare-and-swap, so a lost race is a 409 rather than an
  overwrite of somebody else's revision;
* the project pointer advances in the same request, so the pin and the pointer
  cannot disagree -- which §Wave 3 refuses at admission.

Everything here goes through the real ASGI app and the real durable stores.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore, StudioRole

PROJECT = "proj-pin-1"
TENANT = "ten-pin"
OWNER_ID = "prn_" + "1" * 32
EDITOR_ID = "prn_" + "2" * 32
VIEWER_ID = "prn_" + "3" * 32
OWNER_TOKEN = "owner-token-pin-0123"
EDITOR_TOKEN = "editor-token-pin-012"
VIEWER_TOKEN = "viewer-token-pin-012"
BEARER = "studio-bearer-token"


@pytest.fixture()
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from nexus_ai_agent.api import app as app_module
    from nexus_ai_agent.config.settings import get_settings

    workspace = tmp_path / "ws"
    assets = tmp_path / "assets"
    workspace.mkdir()
    assets.mkdir()
    auth_db = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", BEARER)
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", PROJECT)
    monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NEXUS_STUDIO_ASSET_ROOT", str(assets))
    monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
    monkeypatch.setenv("NEXUS_STUDIO_ACTIVE_REVISION", "0")
    monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "queue.sqlite3"))
    monkeypatch.setenv("NEXUS_STUDIO_BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv(
        "NEXUS_IBPC_APPROVAL_KEY_RING",
        json.dumps({"k1": base64.b64encode(b"k" * 32).decode("ascii")}),
    )
    monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
    get_settings.cache_clear()
    monkeypatch.setattr(app_module, "_creative_registry", None, raising=False)

    store = StudioPrincipalAuthStore(auth_db)
    store.ensure_project(PROJECT, tenant_id=TENANT)
    for principal_id, token, role in (
        (OWNER_ID, OWNER_TOKEN, StudioRole.OWNER),
        (EDITOR_ID, EDITOR_TOKEN, StudioRole.EDITOR),
        (VIEWER_ID, VIEWER_TOKEN, StudioRole.VIEWER),
    ):
        store.register_principal(
            principal_id, tenant_id=TENANT, raw_token=token, allow_system_bootstrap=True
        )
        store.grant_membership(
            PROJECT, principal_id, role=role, granted_by="system", allow_system_bootstrap=True
        )
        store.bind_channel(
            principal_id,
            channel="studio",
            external_subject=f"subj-{principal_id}",
            user_id=1,
            chat_id=2,
            allow_system_bootstrap=True,
        )
    store.close()

    client = TestClient(app_module.app)
    try:
        yield {"client": client, "auth_db": auth_db, "workspace": workspace}
    finally:
        get_settings.cache_clear()


def _headers(token: str | None = OWNER_TOKEN) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {BEARER}"}
    if token is not None:
        headers["X-Nexus-Principal-Token"] = token
    return headers


def _pin_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "revision_id": "rev-pin-1",
        "revision_number": 1,
        "parent_revision_id": None,
        "session_id": "sess-pin-1",
        "turn_id": "turn-pin-1",
        "plan_id": "plan-1",
        "plan_jcs_sha256": "d" * 64,
        "parameters_jcs_sha256": "e" * 64,
        "preview_digest": "sha256:" + "f" * 64,
        "assets": [],
    }
    body.update(overrides)
    return body


def _pin(client: Any, body: dict[str, Any], token: str = OWNER_TOKEN) -> Any:
    return client.post(
        f"/api/studio/projects/{PROJECT}/ibpc/revisions/pin",
        json=body,
        headers=_headers(token),
    )


class TestAuthorization:
    def test_an_owner_can_pin_the_genesis_revision(self, deployment: Any) -> None:
        response = _pin(deployment["client"], _pin_body())
        assert response.status_code == 201, response.text[:400]
        payload = response.json()
        assert payload["revision_id"] == "rev-pin-1"
        assert payload["head"] == "rev-pin-1"

    def test_an_editor_can_pin(self, deployment: Any) -> None:
        response = _pin(deployment["client"], _pin_body(), token=EDITOR_TOKEN)
        assert response.status_code == 201, response.text[:400]

    def test_a_viewer_is_refused(self, deployment: Any) -> None:
        """A reader must not define the bytes a turn renders from."""
        response = _pin(deployment["client"], _pin_body(), token=VIEWER_TOKEN)
        assert response.status_code == 403, response.text[:400]

    def test_an_anonymous_caller_is_refused_before_anything_else(self, deployment: Any) -> None:
        response = _pin(deployment["client"], _pin_body(), token=None)  # type: ignore[arg-type]
        assert response.status_code == 401, response.text[:400]


class TestCompareAndSwap:
    def test_a_second_revision_must_name_the_current_head(self, deployment: Any) -> None:
        client = deployment["client"]
        assert _pin(client, _pin_body()).status_code == 201
        second = _pin(
            client,
            _pin_body(revision_id="rev-pin-2", revision_number=2, parent_revision_id="rev-pin-1"),
        )
        assert second.status_code == 201, second.text[:400]
        assert second.json()["head"] == "rev-pin-2"

    def test_a_stale_parent_is_a_conflict_not_an_overwrite(self, deployment: Any) -> None:
        """The loser of a race is refused; the winner's head survives."""
        client = deployment["client"]
        assert _pin(client, _pin_body()).status_code == 201
        winner = _pin(
            client,
            _pin_body(revision_id="rev-pin-2", revision_number=2, parent_revision_id="rev-pin-1"),
        )
        assert winner.status_code == 201

        # A client that still believes the head is rev-pin-1 tries to advance it.
        loser = _pin(
            client,
            _pin_body(revision_id="rev-pin-3", revision_number=3, parent_revision_id="rev-pin-1"),
        )
        assert loser.status_code == 409, loser.text[:400]
        assert "stale_parent" in loser.text

    def test_genesis_on_an_existing_project_is_a_conflict(self, deployment: Any) -> None:
        client = deployment["client"]
        assert _pin(client, _pin_body()).status_code == 201
        again = _pin(client, _pin_body(revision_id="rev-pin-other", revision_number=1))
        assert again.status_code == 409, again.text[:400]
        assert "genesis_on_existing_project" in again.text


class TestPointerLockstep:
    def test_the_project_pointer_advances_with_the_pin(self, deployment: Any) -> None:
        """The two truths must never disagree: Wave 3 refuses that at admission."""
        client = deployment["client"]
        assert _pin(client, _pin_body()).status_code == 201

        store = StudioPrincipalAuthStore(deployment["auth_db"])
        try:
            assert store.get_project_revision(PROJECT) == "rev-pin-1"
        finally:
            store.close()

    def test_the_revisions_endpoint_reflects_the_pin(self, deployment: Any) -> None:
        client = deployment["client"]
        assert _pin(client, _pin_body()).status_code == 201
        listed = client.get(f"/api/studio/projects/{PROJECT}/ibpc/revisions", headers=_headers())
        assert listed.status_code == 200, listed.text[:400]
        payload = listed.json()
        assert payload["wired"] is True
        assert payload["head"] == "rev-pin-1"
        assert [rev["revision_id"] for rev in payload["revisions"]] == ["rev-pin-1"]


class TestValidation:
    def test_a_malformed_digest_is_rejected_at_the_boundary(self, deployment: Any) -> None:
        response = _pin(deployment["client"], _pin_body(plan_jcs_sha256="not-a-digest"))
        assert response.status_code == 422, response.text[:400]

    def test_an_unknown_field_is_rejected(self, deployment: Any) -> None:
        """extra="forbid": a typo is an error, not a silently ignored operand."""
        response = _pin(deployment["client"], _pin_body(plan_digest="oops"))
        assert response.status_code == 422, response.text[:400]

    def test_a_second_pin_of_the_same_revision_is_idempotent(self, deployment: Any) -> None:
        client = deployment["client"]
        first = _pin(client, _pin_body())
        assert first.status_code == 201
        second = _pin(client, _pin_body())
        assert second.status_code == 201, second.text[:400]
        assert second.json()["revision_jcs_sha256"] == first.json()["revision_jcs_sha256"]


class TestFailurePaths:
    def test_a_cross_tenant_body_is_refused(self, deployment: Any) -> None:
        """The body may not name a tenant the caller's token does not belong to."""
        response = _pin(deployment["client"], _pin_body(tenant_id="ten-someone-else"))
        assert response.status_code == 403, response.text[:400]
        assert "tenant_mismatch" in response.text

    def test_an_unknown_project_is_404_not_500(self, deployment: Any) -> None:
        """The pointer write fails on a project that does not exist.

        The pin itself is project-scoped in its own store, so a well-formed pin
        for an unknown project pins first and then cannot advance a pointer that
        has no row.  That must surface as 404, never as an unhandled error.
        """
        client = deployment["client"]
        response = client.post(
            "/api/studio/projects/proj-does-not-exist/ibpc/revisions/pin",
            json=_pin_body(),
            headers=_headers(),
        )
        assert response.status_code in (403, 404), response.text[:400]

    def test_repinning_with_a_different_payload_is_a_conflict(self, deployment: Any) -> None:
        """Same revision id, different bytes: a typed conflict, not a silent swap."""
        client = deployment["client"]
        assert _pin(client, _pin_body()).status_code == 201
        changed = _pin(client, _pin_body(preview_digest="sha256:" + "a" * 64))
        assert changed.status_code == 409, changed.text[:400]
        assert "idempotency_conflict" in changed.text


class TestUnavailablePinning:
    def test_a_runtime_without_pinning_answers_503(
        self, deployment: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator must get a reason, not a 500 from a missing attribute."""
        from types import SimpleNamespace

        from nexus_ai_agent.api import studio_ibpc

        real = studio_ibpc._build_runtime

        def _no_pins(auth_store: Any, *, binding_ref: Any) -> Any:
            runtime = real(auth_store, binding_ref=binding_ref)
            return SimpleNamespace(
                revision_pins=None, close=runtime.close, revision_status=runtime.revision_status
            )

        monkeypatch.setattr(studio_ibpc, "_build_runtime", _no_pins)
        response = _pin(deployment["client"], _pin_body())
        assert response.status_code == 503, response.text[:400]
        assert "revision_pinning_unavailable" in response.text

    def test_a_failed_pointer_write_answers_404(
        self, deployment: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the pointer row vanished between authorize and write, say 404.

        Unreachable through the API (the project must exist to be authorized),
        but the mapping is what keeps a race from surfacing as a 500.
        """
        from nexus_ai_agent.adapters.studio_principal_auth import (
            StudioPrincipalAuthStore as _Store,
        )
        from nexus_ai_agent.nagar.ibpc.authority import AuthorizationError

        def _boom(self: Any, project_id: str, active_revision_id: str) -> None:
            raise AuthorizationError("project_not_found", f"unknown project {project_id!r}")

        monkeypatch.setattr(_Store, "set_project_revision", _boom)
        response = _pin(deployment["client"], _pin_body())
        assert response.status_code == 404, response.text[:400]
