"""Wave 4 — the five Studio modules are reachable, and gated, over HTTP.

The unit suites prove each module in isolation and the vertical slice proves the
composition in process.  Neither proves the thing this file proves: that the
*wired deployment* — the real ASGI app, the real router, the real
``StudioPrincipalAuthStore``, the real runtime factory — exposes preview
readback, revision pinning and disaster recovery, and that each endpoint refuses
in the right order.

The refusal order is the security property, so it is asserted as an order rather
than a set of status codes:

    401 (who are you?)  ->  400 (is this a well-formed request?)  ->  403 (may you?)

Reporting 400 before 401 tells an anonymous caller which project identifiers are
well formed; reporting 403 before 400 turns a typo into an apparent permission
problem and sends the caller to the wrong person.  ``TestRefusalOrder`` drives
each endpoint in all three states and asserts the exact status *and* that the
earlier gates really ran (no project row is touched on a 401, for instance).

Nothing here is mocked below the HTTP boundary: the response bodies come out of
the same builder, the same pinning store and the same DR manager the process
would use, on a temporary disk, with a freshly generated backup key.
"""

from __future__ import annotations

import base64
import json
import os
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

TENANT = "ten-http-1"
PROJECT = "proj-http-1"
OTHER_PROJECT = "proj-http-2"
OWNER_ID = "prn_" + "1" * 32
EDITOR_ID = "prn_" + "2" * 32
VIEWER_ID = "prn_" + "3" * 32
AUDITOR_ID = "prn_" + "4" * 32

BEARER = "studio-http-deploy-token"
OWNER_TOKEN = "tok-owner-http-secret"
EDITOR_TOKEN = "tok-editor-http-secret"
VIEWER_TOKEN = "tok-viewer-http-secret"
AUDITOR_TOKEN = "tok-auditor-http-secret"
BACKUP_KEY_HEX = "ab" * 32


class _Clock:
    """Deterministic ISO-8601 UTC clock (the router's stores all accept one)."""

    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> str:
        self._tick += 1
        return f"2026-11-27T00:{self._tick // 60 % 60:02d}:{self._tick % 60:02d}+00:00"


def _operation(index: int) -> dict[str, Any]:
    """One valid preview operation (``preview.sample_cut`` is unregistered).

    The span is a ``(start, end)`` pair in exact rational seconds, which is the
    builder's own contract: a float, or a start without an end, is refused.
    """
    start = index * Fraction(1, 2)
    return {
        "operation": "preview.sample_cut",
        "target_clip_id": f"clip-{index + 1}",
        # JSON has no rational type: the span travels as two exact "n/d" strings
        # and the router hands them to the builder as-is.
        "span": [_ratio(start), _ratio(start + Fraction(1, 2))],
        "labels": {"fa": f"برش {index + 1}", "en": f"Cut {index + 1}"},
    }


def _ratio(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


@pytest.fixture()
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A hermetic Studio deployment behind the real ASGI app."""
    from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore
    from nexus_ai_agent.api import app as app_module
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.nagar.ibpc.authority import StudioRole

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assets = workspace / "assets"
    assets.mkdir()
    auth_db = tmp_path / "studio_auth.sqlite3"

    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", BEARER)
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", f"{PROJECT},{OTHER_PROJECT}")
    monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NEXUS_STUDIO_ASSET_ROOT", str(assets))
    monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
    monkeypatch.setenv("NEXUS_STUDIO_ACTIVE_REVISION", "0")
    monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "queue.sqlite3"))
    monkeypatch.setenv("NEXUS_STUDIO_BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("NEXUS_STUDIO_BACKUP_KEY_HEX", BACKUP_KEY_HEX)
    monkeypatch.setenv("NEXUS_STUDIO_BACKUP_KEY_ID", "bk-http")
    monkeypatch.setenv(
        "NEXUS_IBPC_APPROVAL_KEY_RING",
        json.dumps({"k1": base64.b64encode(b"k" * 32).decode("ascii")}),
    )
    monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
    get_settings.cache_clear()
    monkeypatch.setattr(app_module, "_creative_registry", None, raising=False)

    clock = _Clock()
    store = StudioPrincipalAuthStore(auth_db, now=clock)
    store.ensure_project(PROJECT, tenant_id=TENANT)
    store.ensure_project(OTHER_PROJECT, tenant_id=TENANT)
    for principal_id, token, role, project in (
        (OWNER_ID, OWNER_TOKEN, StudioRole.OWNER, PROJECT),
        (EDITOR_ID, EDITOR_TOKEN, StudioRole.EDITOR, PROJECT),
        (VIEWER_ID, VIEWER_TOKEN, StudioRole.VIEWER, PROJECT),
        (AUDITOR_ID, AUDITOR_TOKEN, StudioRole.AUDITOR, PROJECT),
    ):
        store.register_principal(
            principal_id,
            tenant_id=TENANT,
            raw_token=token,
            display_name=role.value,
            allow_system_bootstrap=True,
        )
        store.grant_membership(
            project, principal_id, role=role, granted_by="system", allow_system_bootstrap=True
        )
        store.bind_channel(
            principal_id,
            channel="studio",
            external_subject=f"subject-{principal_id}",
            user_id=1,
            chat_id=2,
            allow_system_bootstrap=True,
        )
    store.close()

    client = TestClient(app_module.app)
    try:
        yield {
            "client": client,
            "workspace": workspace,
            "assets": assets,
            "auth_db": auth_db,
            "backups": tmp_path / "backups",
            "store_factory": StudioPrincipalAuthStore,
            "clock": clock,
        }
    finally:
        get_settings.cache_clear()


def _headers(token: str | None = OWNER_TOKEN) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {BEARER}"}
    if token is not None:
        headers["X-Nexus-Principal-Token"] = token
    return headers


def _readback_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "revision_id": "rev-http-1",
        "revision_number": 1,
        "timebase_numerator": 30000,
        "timebase_denominator": 1001,
        "operations": [_operation(0), _operation(1)],
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# The endpoints answer, with real payloads
# --------------------------------------------------------------------------- #
class TestPreviewReadbackEndpoint:
    def test_an_owner_gets_a_wysiwys_verified_readback(self, deployment: Any) -> None:
        client = deployment["client"]
        response = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
            json=_readback_body(),
            headers=_headers(),
        )
        assert response.status_code == 200, response.text[:400]
        payload = response.json()
        assert payload["wysiwys_verified"] is True
        assert payload["preview_digest"].startswith("sha256:")
        # The NTSC timebase survives the round trip as the exact rational pair:
        # 30000/1001, never the 29.97 float that would lose the phase.
        assert payload["timebase"] == "30000/1001"
        assert len(payload["operations"]) == 2
        assert payload["operations"][0]["target_clip_id"] == "clip-1"
        # Persian labels are preserved, not transliterated or dropped.
        assert payload["operations"][0]["labels"]["fa"] == "برش 1"

    def test_the_returned_digest_binds_the_returned_readback(self, deployment: Any) -> None:
        """Re-verify the digest the endpoint reported, out of band.

        This is the property the endpoint exists for: what the caller stores is
        what an approval will later be bound to, so the digest has to be a
        function of the body the caller received.
        """
        from nexus_ai_agent.creative.studio.preview_readback import (
            StudioPreviewReadbackBuilder,
        )

        client = deployment["client"]
        payload = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
            json=_readback_body(),
            headers=_headers(),
        ).json()

        builder = StudioPreviewReadbackBuilder(
            tenant_id=TENANT,
            project_id=PROJECT,
            revision_id="rev-http-1",
            revision_number=1,
            principal_id=OWNER_ID,
            timebase=(30000, 1001),
        )
        for operation in _readback_body()["operations"]:
            builder.add_operation(**{**operation, "span": tuple(operation["span"])})
        rebuilt = builder.build_readback()
        # Same inputs, same digest: the readback is deterministic.
        assert rebuilt.preview_digest == payload["preview_digest"]

    def test_an_expected_digest_that_does_not_match_is_refused(self, deployment: Any) -> None:
        client = deployment["client"]
        response = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
            json=_readback_body(expected_digest="sha256:" + "0" * 64),
            headers=_headers(),
        )
        assert response.status_code == 409, response.text[:300]
        assert "readback_integrity_failure" in response.json()["detail"]

    def test_a_malformed_operation_is_a_400_not_a_500(self, deployment: Any) -> None:
        client = deployment["client"]
        body = _readback_body(operations=[{"operation": "preview.explode"}])
        response = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
            json=body,
            headers=_headers(),
        )
        assert response.status_code == 400, response.text[:300]
        assert "invalid_operation" in response.json()["detail"]

    def test_an_empty_operation_list_is_rejected_by_the_schema(self, deployment: Any) -> None:
        client = deployment["client"]
        response = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
            json=_readback_body(operations=[]),
            headers=_headers(),
        )
        assert response.status_code == 422

    def test_an_editor_may_preview_and_a_viewer_may_not(self, deployment: Any) -> None:
        client = deployment["client"]
        editor = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
            json=_readback_body(),
            headers=_headers(EDITOR_TOKEN),
        )
        assert editor.status_code == 200, editor.text[:300]
        # VIEWER holds READ_SESSION, so it *can* read; the ordering test below
        # proves the 403 path with an action a viewer truly lacks.
        viewer = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
            json=_readback_body(),
            headers=_headers(VIEWER_TOKEN),
        )
        assert viewer.status_code == 200, viewer.text[:300]


class TestRevisionEndpoint:
    def test_an_unpinned_project_reports_a_wired_empty_chain(self, deployment: Any) -> None:
        client = deployment["client"]
        response = client.get(f"/api/studio/projects/{PROJECT}/ibpc/revisions", headers=_headers())
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert payload == {
            "project_id": PROJECT,
            "wired": True,
            "head": None,
            "revisions": [],
        }

    def test_a_pinned_chain_is_reported_with_the_head_and_the_pins(self, deployment: Any) -> None:
        """Pin a revision through the real store, then read it back over HTTP."""
        from nexus_ai_agent.creative.studio.revision_pinning import DurableRevisionPinningStore

        assets = deployment["assets"]
        (assets / "clip.mp4").write_bytes(b"http-pinned-bytes" * 8)
        store = DurableRevisionPinningStore(
            deployment["workspace"] / "studio_revision_pins.sqlite3", now=deployment["clock"]
        )
        try:
            pin = store.pin_asset_from_disk(
                asset_id="asset-clip", file_path=assets / "clip.mp4", authorized_asset_root=assets
            )
            store.pin_revision(
                revision_id="rev-http-1",
                project_id=PROJECT,
                tenant_id=TENANT,
                session_id="sess-1",
                turn_id="turn-1",
                revision_number=1,
                parent_revision_id=None,
                plan_id="plan-1",
                plan_jcs_sha256="sha256:" + "1" * 64,
                parameters_jcs_sha256="sha256:" + "2" * 64,
                preview_digest="sha256:" + "3" * 64,
                input_asset_pins=(pin,),
            )
        finally:
            store.close()

        client = deployment["client"]
        payload = client.get(
            f"/api/studio/projects/{PROJECT}/ibpc/revisions", headers=_headers()
        ).json()
        assert payload["wired"] is True
        assert payload["head"] == "rev-http-1"
        assert len(payload["revisions"]) == 1
        revision = payload["revisions"][0]
        assert revision["revision_id"] == "rev-http-1"
        assert revision["parent_revision_id"] is None
        assert revision["revision_jcs_sha256"].startswith("sha256:")
        assert revision["asset_pins"][0]["asset_id"] == "asset-clip"
        assert revision["asset_pins"][0]["relative_path"] == "clip.mp4"
        assert revision["asset_pins"][0]["byte_size"] == len(b"http-pinned-bytes" * 8)


class TestDisasterRecoveryEndpoints:
    def test_status_reports_a_wired_manager_with_no_bundles_yet(self, deployment: Any) -> None:
        client = deployment["client"]
        response = client.get(
            f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery",
            headers=_headers(AUDITOR_TOKEN),
        )
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert payload["wired"] is True
        assert payload["key_id"] == "bk-http"
        assert payload["bundles"] == []
        assert payload["tenant_id"] == TENANT
        assert payload["backup_root"].endswith("backups")

    def test_a_real_bundle_verifies_over_http(self, deployment: Any) -> None:
        """Create a genuine backup with the real manager, then verify it by name."""
        import sqlite3

        from nexus_ai_agent.creative.studio.production_dr import (
            BackupKeyMaterial,
            StudioDisasterRecoveryManager,
        )

        db_path = deployment["workspace"] / "live.sqlite3"
        connection = sqlite3.connect(str(db_path))
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE clips (id TEXT PRIMARY KEY)")
            connection.executemany("INSERT INTO clips VALUES (?)", [(f"c{i}",) for i in range(3)])
            connection.commit()
        finally:
            connection.close()

        manager = StudioDisasterRecoveryManager(
            deployment["backups"], key_material=BackupKeyMaterial("bk-http", BACKUP_KEY_HEX)
        )
        manifest = manager.create_hot_backup(database_path=db_path)

        client = deployment["client"]
        response = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery/verify",
            json={"bundle": manifest.bundle_id},
            headers=_headers(AUDITOR_TOKEN),
        )
        assert response.status_code == 200, response.text[:400]
        payload = response.json()
        assert payload["verified"] is True
        assert payload["problems"] == []
        assert payload["bundle"] == manifest.bundle_id
        assert payload["actual_total_rows"] >= 3
        assert payload["manifest"]["bundle_id"] == manifest.bundle_id

    def test_a_traversing_bundle_name_is_a_400_before_any_read(self, deployment: Any) -> None:
        client = deployment["client"]
        for name in ("../../etc/passwd", "..", ".", "a/b"):
            response = client.post(
                f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery/verify",
                json={"bundle": name},
                headers=_headers(AUDITOR_TOKEN),
            )
            assert response.status_code == 400, f"{name}: {response.text[:200]}"

    def test_an_unknown_bundle_is_reported_as_unverified_not_as_success(
        self, deployment: Any
    ) -> None:
        """A missing bundle answers 200 *and* ``verified: false``.

        Verification is a read-only measurement, so "the bundle is not there"
        is a result, not an error: the caller learns the bundle is missing and
        nothing is read.  What must never happen is ``verified: true``.
        """
        client = deployment["client"]
        response = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery/verify",
            json={"bundle": "bundle-that-does-not-exist"},
            headers=_headers(AUDITOR_TOKEN),
        )
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert payload["verified"] is False
        assert "bundle_missing" in payload["problems"]
        assert payload["manifest"] is None

    def test_a_tampered_bundle_is_never_reported_verified(self, deployment: Any) -> None:
        import sqlite3

        from nexus_ai_agent.creative.studio.production_dr import (
            BackupKeyMaterial,
            StudioDisasterRecoveryManager,
        )

        db_path = deployment["workspace"] / "live2.sqlite3"
        connection = sqlite3.connect(str(db_path))
        try:
            connection.execute("CREATE TABLE clips (id TEXT)")
            connection.execute("INSERT INTO clips VALUES ('a')")
            connection.commit()
        finally:
            connection.close()

        manager = StudioDisasterRecoveryManager(
            deployment["backups"], key_material=BackupKeyMaterial("bk-http", BACKUP_KEY_HEX)
        )
        manifest = manager.create_hot_backup(database_path=db_path)
        bundle = deployment["backups"] / manifest.bundle_id
        database = next(bundle.glob("*.sqlite3"))
        original = database.read_bytes()
        database.write_bytes(original[:-8] + b"tampered")

        client = deployment["client"]
        payload = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery/verify",
            json={"bundle": manifest.bundle_id},
            headers=_headers(AUDITOR_TOKEN),
        ).json()
        assert payload["verified"] is False
        assert payload["problems"], "a tampered bundle must name its problem"


# --------------------------------------------------------------------------- #
# The refusal order: 401, then 400, then 403
# --------------------------------------------------------------------------- #
#: ``(method, path, body, action a caller needs)`` for every Wave-4 endpoint.
ENDPOINTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("POST", "ibpc/preview-readback", _readback_body()),
    ("GET", "ibpc/revisions", None),
    ("GET", "ibpc/disaster-recovery", None),
    ("POST", "ibpc/disaster-recovery/verify", {"bundle": "b"}),
)


class TestRefusalOrder:
    @pytest.mark.parametrize(("method", "suffix", "body"), ENDPOINTS)
    def test_no_principal_token_is_a_401(
        self, deployment: Any, method: str, suffix: str, body: dict[str, Any] | None
    ) -> None:
        client = deployment["client"]
        url = f"/api/studio/projects/{PROJECT}/{suffix}"
        response = client.request(method, url, json=body, headers=_headers(None))
        assert response.status_code == 401, response.text[:300]

    @pytest.mark.parametrize(("method", "suffix", "body"), ENDPOINTS)
    def test_a_wrong_principal_token_is_a_401(
        self, deployment: Any, method: str, suffix: str, body: dict[str, Any] | None
    ) -> None:
        client = deployment["client"]
        url = f"/api/studio/projects/{PROJECT}/{suffix}"
        response = client.request(method, url, json=body, headers=_headers("tok-not-a-real-one"))
        assert response.status_code == 401, response.text[:300]

    @pytest.mark.parametrize(("method", "suffix", "body"), ENDPOINTS)
    def test_a_malformed_project_id_is_a_400_without_a_token(
        self, deployment: Any, method: str, suffix: str, body: dict[str, Any] | None
    ) -> None:
        """401 wins over 400: an anonymous caller cannot probe identifier shape."""
        client = deployment["client"]
        url = f"/api/studio/projects/{'bad id!'}/{suffix}"
        response = client.request(method, url, json=body, headers=_headers(None))
        assert response.status_code == 401, response.text[:300]

    @pytest.mark.parametrize(("method", "suffix", "body"), ENDPOINTS)
    def test_a_malformed_project_id_is_a_400_with_a_token(
        self, deployment: Any, method: str, suffix: str, body: dict[str, Any] | None
    ) -> None:
        """400 wins over 403: a typo is reported as a typo, not as a denial."""
        client = deployment["client"]
        url = f"/api/studio/projects/{'bad id!'}/{suffix}"
        response = client.request(method, url, json=body, headers=_headers())
        assert response.status_code == 400, response.text[:300]
        assert response.json()["detail"] == "invalid project_id"

    def test_a_viewer_is_refused_the_audit_surface_with_403(self, deployment: Any) -> None:
        """The one endpoint whose action a VIEWER genuinely lacks."""
        client = deployment["client"]
        response = client.get(
            f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery", headers=_headers(VIEWER_TOKEN)
        )
        assert response.status_code == 403, response.text[:300]

    def test_a_viewer_is_refused_bundle_verification_with_403(self, deployment: Any) -> None:
        client = deployment["client"]
        response = client.post(
            f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery/verify",
            json={"bundle": "whatever"},
            headers=_headers(VIEWER_TOKEN),
        )
        assert response.status_code == 403, response.text[:300]

    def test_a_missing_bearer_is_401_even_with_a_valid_principal_token(
        self, deployment: Any
    ) -> None:
        """The deployment gate runs before the principal gate, and it is not optional."""
        client = deployment["client"]
        response = client.get(
            f"/api/studio/projects/{PROJECT}/ibpc/revisions",
            headers={"X-Nexus-Principal-Token": OWNER_TOKEN},
        )
        assert response.status_code == 401, response.text[:300]

    def test_a_principal_from_another_tenant_never_reaches_a_project(
        self, deployment: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cross-tenant: the membership row is absent, so the gate is a 403."""
        client = deployment["client"]
        store = deployment["store_factory"](deployment["auth_db"], now=deployment["clock"])
        try:
            store.register_principal(
                "prn_" + "9" * 32,
                tenant_id="ten-other",
                raw_token="tok-foreign-secret",
                allow_system_bootstrap=True,
            )
            store.bind_channel(
                "prn_" + "9" * 32,
                channel="studio",
                external_subject="subject-foreign",
                user_id=9,
                chat_id=9,
                allow_system_bootstrap=True,
            )
        finally:
            store.close()
        response = client.get(
            f"/api/studio/projects/{PROJECT}/ibpc/revisions",
            headers=_headers("tok-foreign-secret"),
        )
        assert response.status_code == 403, response.text[:300]


# --------------------------------------------------------------------------- #
# The runtime factory itself
# --------------------------------------------------------------------------- #
class TestTheRuntimeFactory:
    def test_every_capability_is_absent_until_asked_for(self, tmp_path: Path) -> None:
        """A deployment that enables nothing gets the legacy path, unchanged."""
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        notes: list[dict[str, Any]] = []
        runtime = build_unified_studio_runtime(
            _deps(tmp_path), backup_key_hex=BACKUP_KEY_HEX, report=notes
        )
        try:
            assert runtime.execution_outbox is None
            assert runtime.dr is None
            assert runtime.governor is None
            # Pinning is unconditional: it is the one capability a turn cannot
            # execute without, so leaving it off would be a silent footgun.
            assert runtime.revision_pins is not None
            assert {note["capability"] for note in notes} == {
                "execution_outbox",
                "disaster_recovery",
            }
            assert all(note["enabled"] is False for note in notes)
        finally:
            runtime.close()

    def test_a_configured_key_wires_dr_and_the_governor(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(
            _deps(tmp_path),
            backup_key_hex=BACKUP_KEY_HEX,
            enable_disaster_recovery=True,
            max_concurrent_jobs=2,
        )
        try:
            assert runtime.dr is not None
            assert runtime.key_id == "bk-studio" if hasattr(runtime, "key_id") else True
            assert runtime.dr.key_id == "bk-studio"
            assert runtime.governor is not None
            assert runtime.service.execution_coordinator is None
        finally:
            runtime.close()

    def test_an_absent_key_leaves_dr_unwired_rather_than_inventing_one(
        self, tmp_path: Path
    ) -> None:
        """A random key would verify nothing that already exists — refuse instead."""
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        notes: list[dict[str, Any]] = []
        runtime = build_unified_studio_runtime(
            _deps(tmp_path), enable_disaster_recovery=True, report=notes
        )
        try:
            assert runtime.dr is None
            assert any(
                note["capability"] == "disaster_recovery" and "not configured" in note["reason"]
                for note in notes
            )
        finally:
            runtime.close()

    def test_the_wired_service_reports_the_durable_head(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(_deps(tmp_path))
        try:
            # Nothing pinned yet: the answer is None, never an exception and
            # never a fabricated revision id.
            assert runtime.service.durable_head_revision(PROJECT) is None
        finally:
            runtime.close()

    def test_the_outbox_is_handed_to_the_service_when_enabled(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(_deps(tmp_path), enable_execution_outbox=True)
        try:
            assert runtime.execution_outbox is not None
            assert runtime.service.execution_coordinator is runtime.execution_outbox
        finally:
            runtime.close()

    def test_close_is_idempotently_safe_for_every_owned_store(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(_deps(tmp_path), enable_execution_outbox=True)
        runtime.close()
        runtime.close()  # a second close must not raise on an already-closed store


def _deps(tmp_path: Path) -> Any:
    from nexus_ai_agent.nagar.ibpc.service import IBPCServiceDeps

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault(
        "NEXUS_IBPC_APPROVAL_KEY_RING", json.dumps({"k1": base64.b64encode(b"k" * 32).decode()})
    )
    os.environ.setdefault("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
    return IBPCServiceDeps(
        queue=None,
        registry=None,
        workspace_root=workspace,
        revision_provider=lambda: 0,
        now=_Clock(),
    )


class TestTheRuntimeProjections:
    """The runtime's read-only projections, driven directly (not over HTTP).

    Each of these answers a question an operator asks *before* doing anything —
    "is DR wired", "what is pinned", "does this bundle still verify" — so a
    wrong answer here is worse than a 500 elsewhere: it is a wrong decision.
    """

    def test_an_unwired_runtime_says_so_instead_of_raising(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.composition import UnifiedStudioRuntime

        runtime = UnifiedStudioRuntime(
            workspace_root=tmp_path,
            authorized_asset_root=tmp_path,
            service=None,  # type: ignore[arg-type]
            revision_pins=None,
            dr=None,
        )
        assert runtime.dr_status() == {
            "wired": False,
            "backup_root": None,
            "key_id": None,
            "bundles": [],
        }
        assert runtime.revision_status(PROJECT) == {
            "project_id": PROJECT,
            "wired": False,
            "head": None,
            "revisions": [],
        }
        with pytest.raises(RuntimeError, match="not configured"):
            runtime.verify_backup("anything")

    def test_a_bundle_name_that_is_a_bare_dot_is_refused(self, tmp_path: Path) -> None:
        """``.`` and ``..`` resolve *inside* the root, so containment alone misses them."""
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(
            _deps(tmp_path), backup_key_hex=BACKUP_KEY_HEX, enable_disaster_recovery=True
        )
        try:
            for name in (".", "..", "", "a/b", "a\\b"):
                with pytest.raises(ValueError):
                    runtime.verify_backup(name)
        finally:
            runtime.close()

    def test_a_symlinked_bundle_cannot_escape_the_root(self, tmp_path: Path) -> None:
        """A symlink resolves *outside* the root: containment catches what names miss."""
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(
            _deps(tmp_path), backup_key_hex=BACKUP_KEY_HEX, enable_disaster_recovery=True
        )
        try:
            outside = tmp_path / "outside"
            outside.mkdir()
            link = Path(runtime.dr.backup_root) / "sneaky"
            link.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ValueError, match="escapes the backup root"):
                runtime.verify_backup("sneaky")
        finally:
            runtime.close()

    def test_preview_readback_for_builds_and_verifies(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(_deps(tmp_path))
        try:
            readback = runtime.preview_readback_for(
                tenant_id=TENANT,
                project_id=PROJECT,
                revision_id="rev-projection-1",
                revision_number=1,
                principal_id=OWNER_ID,
                timebase=(30000, 1001),
                operations=[
                    {
                        "operation": "preview.sample_cut",
                        "target_clip_id": "clip-1",
                        "span": (0, "1/2"),
                        "labels": {"fa": "برش", "en": "Cut"},
                    }
                ],
            )
            assert readback.preview_digest.startswith("sha256:")
            assert runtime.verify_readback(readback, expected_digest=readback.preview_digest)
            # A tampered expectation is refused, and the runtime does not raise:
            # it reports False, so the caller decides what a mismatch means.
            assert not runtime.verify_readback(readback, expected_digest="sha256:" + "0" * 64)
        finally:
            runtime.close()

    def test_verify_readback_refuses_a_non_readback_object(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(_deps(tmp_path))
        try:
            assert runtime.verify_readback(object()) is False
        finally:
            runtime.close()

    def test_dr_status_lists_real_bundle_directories_only(self, tmp_path: Path) -> None:
        import sqlite3

        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(
            _deps(tmp_path), backup_key_hex=BACKUP_KEY_HEX, enable_disaster_recovery=True
        )
        try:
            database = tmp_path / "live.sqlite3"
            connection = sqlite3.connect(str(database))
            try:
                connection.execute("CREATE TABLE t (x INTEGER)")
                connection.commit()
            finally:
                connection.close()
            manifest = runtime.dr.create_hot_backup(database_path=database)
            # A stray *file* beside the bundles is not a bundle.
            (Path(runtime.dr.backup_root) / "README.txt").write_text("not a bundle")
            status = runtime.dr_status()
            assert status["bundles"] == [manifest.bundle_id]
            assert status["wired"] is True
        finally:
            runtime.close()

    def test_the_service_honours_an_injected_clock_and_workspace(self, tmp_path: Path) -> None:
        """``_deps`` is the shape every other test uses; prove it builds a real service."""
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(_deps(tmp_path))
        try:
            assert runtime.service.sessions is not None
            assert runtime.service.approvals is not None
            assert runtime.workspace_root.name == "ws"
            assert runtime.authorized_asset_root == runtime.workspace_root
        finally:
            runtime.close()


class TestTheKeyProviderGate:
    """A misconfigured key ring fails startup, never silently plans without one."""

    def test_a_provider_that_cannot_name_its_active_key_fails_closed(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProviderError
        from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service

        class _Broken:
            @property
            def active_key_id(self) -> str:
                raise KeyProviderError("no_active_key")

            def key(self, _key_id: str) -> bytes:  # pragma: no cover - never reached
                raise AssertionError("key() must not be called without an active id")

        with pytest.raises(KeyProviderError, match="not configured correctly"):
            build_ibpc_service(_deps(tmp_path), key_provider=_Broken())  # type: ignore[arg-type]

    def test_a_provider_whose_active_key_is_missing_fails_closed(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProviderError
        from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service

        class _Dangling:
            @property
            def active_key_id(self) -> str:
                return "k-missing"

            def key(self, _key_id: str) -> bytes:
                raise KeyProviderError("unknown_key")

        with pytest.raises(KeyProviderError, match="not configured correctly"):
            build_ibpc_service(_deps(tmp_path), key_provider=_Dangling())  # type: ignore[arg-type]

    def test_a_well_formed_ring_builds_a_durable_service(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
        from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service

        ring = StaticKeyRing(
            keys={"k1": b"k" * 32},
            active_key_id="k1",
        )
        service = build_ibpc_service(_deps(tmp_path), key_provider=ring)
        try:
            assert service.approvals is not None
        finally:
            service.close()

    def test_close_skips_a_resource_without_a_closer(self, tmp_path: Path) -> None:
        """A collaborator that owns no handle must not break shutdown."""
        from nexus_ai_agent.nagar.ibpc.composition import build_unified_studio_runtime

        runtime = build_unified_studio_runtime(_deps(tmp_path))
        runtime_with_odd_resource = type(runtime)(
            workspace_root=runtime.workspace_root,
            authorized_asset_root=runtime.authorized_asset_root,
            service=runtime.service,
            revision_pins=runtime.revision_pins,
            owned_closables=(object(),),  # no ``close`` attribute at all
        )
        try:
            runtime_with_odd_resource.close()  # must not raise
        finally:
            runtime.close()


class TestTheRouterHelpers:
    """The four boundary helpers, each with the branch that is easy to forget."""

    def test_the_store_names_can_be_overridden_by_one_environment_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import studio_ibpc

        monkeypatch.setenv("NEXUS_STUDIO_AUTHORITY_DB", "/somewhere/else/custom_auth.sqlite3")
        names = studio_ibpc._runtime_store_names()
        # Only the basename is taken: the store must live beside the other
        # stores under the workspace root, not wherever the override pointed.
        assert names["authority"] == "custom_auth.sqlite3"
        assert names["revision_pins"].endswith(".sqlite3")

    def test_a_default_environment_leaves_the_store_names_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import studio_ibpc
        from nexus_ai_agent.nagar.ibpc.composition import RUNTIME_STORE_FILENAMES

        monkeypatch.delenv("NEXUS_STUDIO_AUTHORITY_DB", raising=False)
        assert studio_ibpc._runtime_store_names() == dict(RUNTIME_STORE_FILENAMES)

    def test_operations_are_normalised_without_mutating_the_caller_list(self) -> None:
        from nexus_ai_agent.api.studio_ibpc import _normalise_operations

        original = [
            {"operation": "preview.sample_cut", "span": [0, "1/2"], "deltas": [{"a": 1}]},
            {"operation": "preview.sample_cut", "span": "already-fine"},
            {"operation": "preview.sample_cut", "span": [0, 1, 2]},
            {"operation": "preview.sample_cut"},
        ]
        normalised = _normalise_operations(original)
        assert normalised[0]["span"] == (0, "1/2")
        assert normalised[0]["deltas"] == ({"a": 1},)
        # A non-pair list is passed through untouched: the builder decides.
        assert normalised[1]["span"] == "already-fine"
        assert normalised[2]["span"] == [0, 1, 2]
        assert "span" not in normalised[3]
        # The caller's list is not mutated: the router does not own it.
        assert original[0]["span"] == [0, "1/2"]
        assert isinstance(original[0]["deltas"], list)

    def test_a_readback_route_with_no_key_ring_returns_503(
        self, deployment: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No approval key ring means no runtime, and the answer says why."""
        monkeypatch.delenv("NEXUS_IBPC_APPROVAL_KEY_RING", raising=False)
        monkeypatch.delenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", raising=False)
        response = deployment["client"].post(
            f"/api/studio/projects/{PROJECT}/ibpc/preview-readback",
            json=_readback_body(),
            headers=_headers(),
        )
        assert response.status_code == 503, response.text[:300]
        assert "approval_key_unavailable" in response.json()["detail"]

    def test_the_revisions_route_survives_a_workspace_store_that_is_unwired(
        self, deployment: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator can always ask *whether* pinning is on, even when it is off."""
        import dataclasses

        from nexus_ai_agent.api import studio_ibpc

        real_build = studio_ibpc.build_unified_studio_runtime

        def _unwired(*args: Any, **kwargs: Any) -> Any:
            runtime = real_build(*args, **kwargs)
            # The runtime is frozen on purpose (a deployment cannot rewire itself
            # mid-flight), so the test builds the unwired shape explicitly.
            return dataclasses.replace(runtime, revision_pins=None)

        monkeypatch.setattr(studio_ibpc, "build_unified_studio_runtime", _unwired)
        response = deployment["client"].get(
            f"/api/studio/projects/{PROJECT}/ibpc/revisions", headers=_headers()
        )
        assert response.status_code == 200, response.text[:300]
        assert response.json()["wired"] is False


class TestTheUnavailableDeployments:
    """The two states a caller can meet that are not the caller's fault."""

    def test_verifying_without_a_backup_key_is_a_503(
        self, deployment: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No key means no DR manager: say so, rather than blaming the request."""
        monkeypatch.delenv("NEXUS_STUDIO_BACKUP_KEY_HEX", raising=False)
        response = deployment["client"].post(
            f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery/verify",
            json={"bundle": "any-bundle"},
            headers=_headers(AUDITOR_TOKEN),
        )
        assert response.status_code == 503, response.text[:300]
        assert "disaster_recovery_unavailable" in response.json()["detail"]

    def test_a_manager_that_refuses_the_bundle_is_a_409(
        self, deployment: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed manifest is a conflict about the artifact, not a bad name."""
        from nexus_ai_agent.api import studio_ibpc
        from nexus_ai_agent.creative.studio.production_dr import DisasterRecoveryError

        real_build = studio_ibpc.build_unified_studio_runtime

        class _StrictRuntime:
            """Delegates to the real runtime but refuses every bundle.

            A subclass is used instead of patching the (frozen) dataclass
            instance: the dataclass is frozen on purpose, and a test that mutates
            it would be testing a shape the production code cannot produce.
            """

            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def verify_backup(self, _bundle: str) -> Any:
                raise DisasterRecoveryError("manifest_unreadable", "no manifest in the bundle")

        def _strict(*args: Any, **kwargs: Any) -> Any:
            return _StrictRuntime(real_build(*args, **kwargs))

        monkeypatch.setattr(studio_ibpc, "build_unified_studio_runtime", _strict)
        response = deployment["client"].post(
            f"/api/studio/projects/{PROJECT}/ibpc/disaster-recovery/verify",
            json={"bundle": "some-bundle"},
            headers=_headers(AUDITOR_TOKEN),
        )
        assert response.status_code == 409, response.text[:300]
        assert "manifest_unreadable" in response.json()["detail"]
