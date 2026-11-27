"""The worker boundary refuses an actor whose authority changed after approval.

An approval says "this actor may execute this plan".  It is granted at one
instant and consumed at another, and between the two an owner can revoke a
membership, suspend a principal, or revoke the channel binding.  Before Wave 4
the conversational path re-checked only the *role* (which the authorizer reads
live) and never re-verified the full four-way snapshot at execution time, so a
revoked actor's already-approved turn could still execute.

These tests drive the real ``StudioUnifiedAuthorityBridge`` over a real
``StudioPrincipalAuthStore``: no fake authority, because the thing under test IS
the durable read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.studio_directory_bridge import StudioUnifiedAuthorityBridge
from nexus_ai_agent.adapters.studio_principal_auth import (
    StudioPrincipalAuthStore,
    StudioRole,
)
from nexus_ai_agent.nagar.ibpc.authority import StudioAction
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps

TENANT = "ten-auth"
PROJECT = "proj-auth"
OWNER = "prn_" + "1" * 32
OWNER_TOKEN = "owner-token-0123456789"


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    auth = StudioPrincipalAuthStore(tmp_path / "auth.sqlite3")
    auth.ensure_project(PROJECT, tenant_id=TENANT)
    auth.register_principal(
        OWNER,
        tenant_id=TENANT,
        raw_token=OWNER_TOKEN,
        allow_system_bootstrap=True,
    )
    auth.grant_membership(
        PROJECT, OWNER, role=StudioRole.OWNER, granted_by="system", allow_system_bootstrap=True
    )
    auth.bind_channel(
        OWNER,
        channel="studio",
        external_subject="subj-owner",
        user_id=1,
        chat_id=2,
        allow_system_bootstrap=True,
    )
    yield auth
    auth.close()


def _ctx(binding_ref: str) -> Any:
    class _Identity:
        principal_id = OWNER
        channel_binding_id = binding_ref

    class _Ctx:
        verified_identity = _Identity()

    return _Ctx()


def _service(root: Path, store: Any, bridge: Any) -> IBPCService:
    return IBPCService(
        IBPCServiceDeps(
            queue=None,
            registry=None,
            workspace_root=root,
            revision_provider=lambda: 0,
            authority=store,
            channel_binding_ref=(store.default_binding_for(OWNER) or "") if store else "",
            authority_bridge=bridge,
        )
    )


def test_an_active_owner_passes_the_worker_boundary(tmp_path: Path, store: Any) -> None:
    bridge = StudioUnifiedAuthorityBridge(store)
    service = _service(tmp_path, store, bridge)
    binding_ref = store.default_binding_for(OWNER) or ""
    refusal = service._verify_execution_authority(
        principal_id=OWNER, project_id=PROJECT, tenant_id=TENANT, ctx=_ctx(binding_ref)
    )
    assert refusal is None


def test_a_membership_revoked_after_approval_refuses_at_the_boundary(
    tmp_path: Path, store: Any
) -> None:
    """The exact gap: valid at approve time, revoked before execution."""
    bridge = StudioUnifiedAuthorityBridge(store)
    service = _service(tmp_path, store, bridge)
    binding_ref = store.default_binding_for(OWNER) or ""
    assert (
        service._verify_execution_authority(
            principal_id=OWNER, project_id=PROJECT, tenant_id=TENANT, ctx=_ctx(binding_ref)
        )
        is None
    )

    store.revoke_membership(
        PROJECT, OWNER, revoked_by="system", tenant_id=TENANT, allow_system_bootstrap=True
    )

    refusal = service._verify_execution_authority(
        principal_id=OWNER, project_id=PROJECT, tenant_id=TENANT, ctx=_ctx(binding_ref)
    )
    assert refusal is not None
    assert refusal.startswith("authority_refused:")
    # Not just "some refusal": the revoked *membership* is the named cause, so a
    # refusal for an unrelated reason cannot satisfy this test.
    assert "membership" in refusal, refusal


def test_a_tenant_mismatch_is_refused_not_widened(tmp_path: Path, store: Any) -> None:
    """The bridge never widens a decision: a wrong tenant is a refusal."""
    bridge = StudioUnifiedAuthorityBridge(store)
    service = _service(tmp_path, store, bridge)
    binding_ref = store.default_binding_for(OWNER) or ""
    refusal = service._verify_execution_authority(
        principal_id=OWNER, project_id=PROJECT, tenant_id="ten-someone-else", ctx=_ctx(binding_ref)
    )
    assert refusal is not None
    assert refusal.startswith("authority_refused:")
    assert "ten-someone-else" in refusal or "tenant" in refusal, refusal


def test_a_missing_binding_is_refused(tmp_path: Path, store: Any) -> None:
    """An execution with no binding reference cannot be verified, so it fails."""
    bridge = StudioUnifiedAuthorityBridge(store)
    service = IBPCService(
        IBPCServiceDeps(
            queue=None,
            registry=None,
            workspace_root=tmp_path,
            revision_provider=lambda: 0,
            authority=store,
            channel_binding_ref="",
            authority_bridge=bridge,
        )
    )
    refusal = service._verify_execution_authority(
        principal_id=OWNER, project_id=PROJECT, tenant_id=TENANT, ctx=_ctx("")
    )
    assert refusal is not None
    assert refusal.startswith("authority_refused:")
    assert "binding" in refusal, refusal


def test_a_raising_bridge_is_a_refusal_never_a_pass(tmp_path: Path) -> None:
    """A durable-store outage must not become an authorization bypass."""

    class _Exploding:
        def verify_worker_execution_authority(self, **_kwargs: Any) -> Any:
            raise RuntimeError("the authority store is unreachable")

    service = _service(tmp_path, None, _Exploding())
    refusal = service._verify_execution_authority(
        principal_id=OWNER, project_id=PROJECT, tenant_id=TENANT, ctx=_ctx("bind-1")
    )
    assert refusal is not None
    assert refusal.startswith("authority_refused:")
    # The exception TYPE is the reason, so an unrelated refusal cannot pass.
    assert "RuntimeError" in refusal, refusal
    assert "unreachable" in refusal, refusal


def test_an_unwired_bridge_keeps_the_legacy_path(tmp_path: Path) -> None:
    """A deployment with no bridge is not refused for a seam it never had."""
    service = _service(tmp_path, None, None)
    refusal = service._verify_execution_authority(
        principal_id=OWNER, project_id=PROJECT, tenant_id=TENANT, ctx=_ctx("bind-1")
    )
    assert refusal is None


def test_the_bridge_defaults_to_the_execute_action(store: Any) -> None:
    """The worker boundary asks about execution, not about reading."""
    bridge = StudioUnifiedAuthorityBridge(store)
    binding_ref = store.default_binding_for(OWNER) or ""
    proof = bridge.verify_worker_execution_authority(
        principal_id=OWNER,
        project_id=PROJECT,
        tenant_id=TENANT,
        binding_ref=binding_ref,
    )
    assert proof.principal_id == OWNER
    assert proof.project_id == PROJECT
    assert str(proof.role).lower().endswith("owner")
    assert StudioAction.EXECUTE_PLAN in proof.permitted_actions
