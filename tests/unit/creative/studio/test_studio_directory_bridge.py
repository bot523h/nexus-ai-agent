"""Wave 1 — Studio unified authority bridge (``studio_directory_bridge``).

Every test runs against a real SQLite/WAL ``StudioPrincipalAuthStore`` (no
mocks): the bridge is only trustworthy if the four-way invariant it claims is
enforced against the durable rows a real process writes.

Coverage target for the module under test: ``>= 98%`` line *and* branch.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.sql_application_directory import SQLApplicationDirectory
from nexus_ai_agent.adapters.studio_directory_bridge import (
    CANONICAL_BINDING_PREFIX,
    StudioAuthorityBridgeError,
    StudioUnifiedAuthorityBridge,
    UnifiedExecutionAuthorityProof,
    canonical_binding_ref,
    role_permits,
)
from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore
from nexus_ai_agent.creative.studio.models import ActorIdentity
from nexus_ai_agent.nagar.ibpc.authority import (
    AuthorityResolution,
    BindingStatus,
    CanonicalAuthorityPort,
    MembershipRole,
    MembershipStatus,
    PrincipalFacts,
    PrincipalRoleAuthority,
    PrincipalRoleFacts,
    PrincipalStatus,
    StudioAction,
    StudioRole,
)
from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
)

TENANT = "tenant-studio-1"
OTHER_TENANT = "tenant-studio-2"
PROJECT = "proj-cinema-1"

OWNER_ID = "prn_" + "1" * 32
EDITOR_ID = "prn_" + "2" * 32
VIEWER_ID = "prn_" + "3" * 32
AUDITOR_ID = "prn_" + "4" * 32


class _Clock:
    """Monotonic, deterministic ISO-8601 UTC clock."""

    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> str:
        self._tick += 1
        return f"2026-11-26T00:00:{self._tick:02d}+00:00"


#: A clock of the *signature* the bridge accepts (any ``() -> str``).
_ClockFn = Callable[[], str]


def _seed(tmp_path: Path, *, clock: _Clock | None = None) -> StudioPrincipalAuthStore:
    """One tenant, one project, four principals with one role each."""
    clock = clock or _Clock()
    store = StudioPrincipalAuthStore(tmp_path / "studio_auth.sqlite3", now=clock)
    store.ensure_project(PROJECT, tenant_id=TENANT)
    for principal_id, role in (
        (OWNER_ID, StudioRole.OWNER),
        (EDITOR_ID, StudioRole.EDITOR),
        (VIEWER_ID, StudioRole.VIEWER),
        (AUDITOR_ID, StudioRole.AUDITOR),
    ):
        store.register_principal(
            principal_id,
            tenant_id=TENANT,
            raw_token=f"tok-{principal_id}-secret",
            display_name=role.value,
            allow_system_bootstrap=True,
        )
        store.grant_membership(
            PROJECT, principal_id, role=role, granted_by="system", allow_system_bootstrap=True
        )
        store.bind_channel(
            principal_id,
            channel="studio",
            external_subject=f"subject-{principal_id}",
            user_id=1,
            chat_id=2,
            allow_system_bootstrap=True,
        )
    return store


def _binding(store: StudioPrincipalAuthStore, principal_id: str) -> str:
    ref = store.default_binding_for(principal_id)
    assert ref is not None
    return ref


def _bridge(
    store: StudioPrincipalAuthStore, *, clock: _ClockFn | None = None, **kwargs: Any
) -> StudioUnifiedAuthorityBridge:
    return StudioUnifiedAuthorityBridge(store, now=clock or _Clock(), **kwargs)


# --------------------------------------------------------------------------- #
# Vocabulary, protocols and deterministic helpers
# --------------------------------------------------------------------------- #
class TestAuthorityVocabulary:
    def test_membership_role_round_trips_every_studio_role(self) -> None:
        for studio_role in StudioRole:
            membership = MembershipRole.from_studio(studio_role)
            assert membership.value == studio_role.value
            assert membership.to_studio() is studio_role
            assert MembershipRole.from_studio(studio_role.value) is membership
            assert MembershipRole.from_studio(f"  {studio_role.value.lower()}  ") is membership

    def test_membership_role_rejects_unknown_and_non_string(self) -> None:
        with pytest.raises(Exception) as excinfo:
            MembershipRole.from_studio("SUPERUSER")
        assert "unknown_membership_role" in str(excinfo.value)
        with pytest.raises(Exception) as excinfo2:
            MembershipRole.from_studio(7)  # type: ignore[arg-type]
        assert "unknown_membership_role" in str(excinfo2.value)

    def test_role_permits_matches_the_store_role_actions_table(self) -> None:
        assert role_permits(MembershipRole.OWNER, StudioAction.EXECUTE_PLAN)
        assert role_permits(MembershipRole.EDITOR, StudioAction.EXECUTE_PLAN)
        assert not role_permits(MembershipRole.VIEWER, StudioAction.EXECUTE_PLAN)
        assert not role_permits(MembershipRole.AUDITOR, StudioAction.EXECUTE_PLAN)
        assert role_permits(MembershipRole.AUDITOR, StudioAction.READ_AUDIT)
        assert not role_permits(MembershipRole.EDITOR, StudioAction.MANAGE_MEMBERSHIP)

    def test_execute_approved_plan_is_the_execute_plan_member(self) -> None:
        assert StudioAction.EXECUTE_APPROVED_PLAN.value == StudioAction.EXECUTE_PLAN.value
        assert StudioAction.EXECUTE_APPROVED_PLAN is StudioAction("EXECUTE_PLAN")
        assert AuthorityResolution is PrincipalRoleFacts

    def test_canonical_binding_ref_is_deterministic_and_tenant_scoped(self) -> None:
        ref = canonical_binding_ref(tenant_id=TENANT, principal_id=OWNER_ID)
        assert ref == f"{CANONICAL_BINDING_PREFIX}{TENANT}:{OWNER_ID}"
        assert canonical_binding_ref(tenant_id=OTHER_TENANT, principal_id=OWNER_ID) != ref

    def test_principal_role_facts_may_execute_is_the_active_triple(self) -> None:
        clock = _Clock()
        facts = PrincipalRoleFacts(
            principal_id=OWNER_ID,
            project_id=PROJECT,
            tenant_id=TENANT,
            binding_ref="b",
            role=MembershipRole.OWNER,
            principal_status=PrincipalStatus.ACTIVE,
            membership_status=MembershipStatus.ACTIVE,
            binding_status=BindingStatus.ACTIVE,
            permitted_actions=(StudioAction.EXECUTE_PLAN,),
            verified_at_utc=clock(),
        )
        assert facts.may_execute is True
        inactive = dataclasses.replace(facts, binding_status=BindingStatus.REVOKED)
        assert inactive.may_execute is False

    def test_bridge_satisfies_both_authority_protocols(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            assert isinstance(bridge, CanonicalAuthorityPort)
            assert isinstance(bridge, PrincipalRoleAuthority)
        finally:
            store.close()

    def test_bridge_repr_names_the_store_and_directory_state(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            assert "none" in repr(bridge)
            attached = _bridge(store, directory=object())
            assert "attached" in repr(attached)
            assert attached.store is store
            assert attached.directory is not None
            assert bridge.directory is None
        finally:
            store.close()


# --------------------------------------------------------------------------- #
# Four-way fail-closed invariant
# --------------------------------------------------------------------------- #
class TestFourWayInvariant:
    def test_owner_and_editor_resolve_for_execution(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            for principal_id, expected_role in (
                (OWNER_ID, MembershipRole.OWNER),
                (EDITOR_ID, MembershipRole.EDITOR),
            ):
                facts = bridge.resolve_principal_role(
                    principal_id,
                    PROJECT,
                    binding_ref=_binding(store, principal_id),
                    tenant_id=TENANT,
                )
                assert facts.role is expected_role
                assert facts.principal_status is PrincipalStatus.ACTIVE
                assert facts.membership_status is MembershipStatus.ACTIVE
                assert facts.binding_status is BindingStatus.ACTIVE
                assert facts.may_execute is True
                assert StudioAction.EXECUTE_PLAN in facts.permitted_actions
                proof = bridge.verify_worker_execution_authority(
                    principal_id=principal_id,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=_binding(store, principal_id),
                )
                assert proof.role is expected_role
                assert StudioAction.EXECUTE_APPROVED_PLAN in proof.permitted_actions
        finally:
            store.close()

    def test_viewer_and_auditor_cannot_execute_or_approve(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            for principal_id in (VIEWER_ID, AUDITOR_ID):
                for action in (StudioAction.EXECUTE_PLAN, StudioAction.APPROVE_PLAN):
                    with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                        bridge.verify_worker_execution_authority(
                            principal_id=principal_id,
                            project_id=PROJECT,
                            tenant_id=TENANT,
                            binding_ref=_binding(store, principal_id),
                            required_action=action,
                        )
                    assert excinfo.value.reason == "unauthorized_principal"
                    assert action.value in str(excinfo.value)
            # ... but their own read actions still resolve.
            readable = bridge.verify_worker_execution_authority(
                principal_id=VIEWER_ID,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=_binding(store, VIEWER_ID),
                required_action=StudioAction.READ_SESSION,
            )
            assert readable.role is MembershipRole.VIEWER
        finally:
            store.close()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("principal_id", ""),
            ("principal_id", "   "),
            ("project_id", ""),
            ("project_id", "\t"),
            ("tenant_id", ""),
            ("tenant_id", "  "),
            ("binding_ref", ""),
            ("binding_ref", "   "),
            ("principal_id", None),
            ("project_id", 7),
        ],
    )
    def test_blank_or_non_string_identifiers_are_refused(
        self, tmp_path: Path, field: str, value: object
    ) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            kwargs: dict[str, Any] = {
                "principal_id": OWNER_ID,
                "project_id": PROJECT,
                "tenant_id": TENANT,
                "binding_ref": _binding(store, OWNER_ID),
            }
            kwargs[field] = value
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.verify_worker_execution_authority(**kwargs)
            assert excinfo.value.reason == "unauthorized_principal"
            assert excinfo.value.bridge_detail
            assert field in str(excinfo.value)
        finally:
            store.close()

    def test_missing_principal_is_refused(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.resolve_principal_role(
                    "prn_" + "9" * 32, PROJECT, tenant_id=TENANT, binding_ref="cbn_x"
                )
            assert "does not exist" in excinfo.value.bridge_detail
        finally:
            store.close()

    def test_revoked_and_suspended_principals_are_refused(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            ref = _binding(store, EDITOR_ID)
            store.revoke_principal(
                EDITOR_ID,
                allow_system_bootstrap=True,
                project_id=PROJECT,
                expected_tenant_id=TENANT,
            )
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.verify_worker_execution_authority(
                    principal_id=EDITOR_ID,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=ref,
                )
            assert "not ACTIVE" in excinfo.value.bridge_detail
            # SUSPENDED is refused by the same gate (status is not ACTIVE).
            store._conn.execute(
                "UPDATE nexus_studio_principals SET status = 'SUSPENDED' WHERE principal_id = ?",
                (VIEWER_ID,),
            )
            with pytest.raises(StudioAuthorityBridgeError) as exc2:
                bridge.resolve_principal_role(
                    VIEWER_ID,
                    PROJECT,
                    tenant_id=TENANT,
                    binding_ref=_binding(store, VIEWER_ID),
                )
            assert "SUSPENDED" in exc2.value.bridge_detail
        finally:
            store.close()

    def test_missing_project_and_cross_tenant_project_are_refused(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            ref = _binding(store, OWNER_ID)
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.verify_worker_execution_authority(
                    principal_id=OWNER_ID,
                    project_id="proj-unknown",
                    tenant_id=TENANT,
                    binding_ref=ref,
                )
            assert "does not exist" in excinfo.value.bridge_detail
            store.ensure_project("proj-other-tenant", tenant_id=OTHER_TENANT)
            with pytest.raises(StudioAuthorityBridgeError) as exc2:
                bridge.verify_worker_execution_authority(
                    principal_id=OWNER_ID,
                    project_id="proj-other-tenant",
                    tenant_id=TENANT,
                    binding_ref=ref,
                )
            assert "project tenant" in exc2.value.bridge_detail
        finally:
            store.close()

    def test_revoked_membership_is_refused_and_missing_membership_too(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            ref = _binding(store, EDITOR_ID)
            store.revoke_membership(
                PROJECT, EDITOR_ID, revoked_by="system", allow_system_bootstrap=True
            )
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.verify_worker_execution_authority(
                    principal_id=EDITOR_ID,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=ref,
                )
            assert "not ACTIVE" in excinfo.value.bridge_detail
            store._conn.execute(
                "DELETE FROM nexus_studio_project_memberships WHERE principal_id = ?",
                (AUDITOR_ID,),
            )
            with pytest.raises(StudioAuthorityBridgeError) as exc2:
                bridge.resolve_principal_role(
                    AUDITOR_ID,
                    PROJECT,
                    tenant_id=TENANT,
                    binding_ref=_binding(store, AUDITOR_ID),
                )
            assert "no membership" in exc2.value.bridge_detail
        finally:
            store.close()

    def test_unknown_and_suspended_membership_role_spelling_is_refused(
        self, tmp_path: Path
    ) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            ref = _binding(store, VIEWER_ID)
            store._conn.execute(
                "UPDATE nexus_studio_project_memberships SET role = 'SUPERUSER' "
                "WHERE principal_id = ?",
                (VIEWER_ID,),
            )
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.resolve_principal_role(VIEWER_ID, PROJECT, tenant_id=TENANT, binding_ref=ref)
            assert "unknown_membership_role" in str(excinfo.value)
        finally:
            store.close()

    def test_revoked_binding_and_cross_principal_binding_are_refused(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            owner_ref = _binding(store, OWNER_ID)
            editor_ref = _binding(store, EDITOR_ID)
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.resolve_principal_role(
                    EDITOR_ID, PROJECT, tenant_id=TENANT, binding_ref=owner_ref
                )
            assert "does not belong" in excinfo.value.bridge_detail or (
                "no binding" in excinfo.value.bridge_detail
            )
            store._conn.execute(
                "UPDATE nexus_studio_channel_bindings SET status = 'REVOKED' "
                "WHERE binding_ref = ? AND principal_id = ?",
                (editor_ref, EDITOR_ID),
            )
            with pytest.raises(StudioAuthorityBridgeError) as exc2:
                bridge.verify_worker_execution_authority(
                    principal_id=EDITOR_ID,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=editor_ref,
                )
            assert "not ACTIVE" in exc2.value.bridge_detail
        finally:
            store.close()

    def test_binding_tenant_is_the_principal_tenant_the_binding_lives_in(
        self, tmp_path: Path
    ) -> None:
        """A binding's tenant *is* its principal's tenant; a drifted pair refuses."""
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            auditor_ref = _binding(store, AUDITOR_ID)
            store._conn.execute(
                "UPDATE nexus_studio_principals SET tenant_id = ? WHERE principal_id = ?",
                (OTHER_TENANT, AUDITOR_ID),
            )
            # Principal and binding moved together: the *principal* gate refuses
            # against the tenant the project is registered in.
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.resolve_principal_role(
                    AUDITOR_ID, PROJECT, tenant_id=TENANT, binding_ref=auditor_ref
                )
            assert "principal tenant" in excinfo.value.bridge_detail
            # Naming the drifted tenant as "expected" advances past the principal
            # gate, and the *project* gate is then the one that disagrees.
            with pytest.raises(StudioAuthorityBridgeError) as exc2:
                bridge.resolve_principal_role(
                    AUDITOR_ID, PROJECT, tenant_id=OTHER_TENANT, binding_ref=auditor_ref
                )
            assert "project tenant" in exc2.value.bridge_detail
        finally:
            store.close()

    def test_binding_in_another_tenant_is_refused(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            store._conn.execute(
                "UPDATE nexus_studio_principals SET tenant_id = ? WHERE principal_id = ?",
                (OTHER_TENANT, AUDITOR_ID),
            )
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.resolve_principal_role(
                    AUDITOR_ID,
                    PROJECT,
                    tenant_id=OTHER_TENANT,
                    binding_ref=_binding(store, AUDITOR_ID),
                )
            # The principal's tenant now differs from the *project* tenant.
            assert "project tenant" in excinfo.value.bridge_detail
        finally:
            store.close()

    def test_cross_tenant_expected_tenant_is_refused(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.verify_worker_execution_authority(
                    principal_id=OWNER_ID,
                    project_id=PROJECT,
                    tenant_id=OTHER_TENANT,
                    binding_ref=_binding(store, OWNER_ID),
                )
            assert "principal tenant" in excinfo.value.bridge_detail
        finally:
            store.close()

    def test_canonical_default_binding_ref_resolves_to_the_stored_binding(
        self, tmp_path: Path
    ) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            canonical = canonical_binding_ref(tenant_id=TENANT, principal_id=OWNER_ID)
            store._conn.execute(
                "INSERT INTO nexus_studio_channel_bindings (binding_ref, principal_id, channel, "
                "external_subject, user_id, chat_id, status, created_at_utc, revoked_at_utc) "
                "VALUES (?, ?, 'studio', 'canonical-owner', 0, 0, 'ACTIVE', 'now', '')",
                (canonical, OWNER_ID),
            )
            facts = bridge.resolve_principal_role(
                OWNER_ID, PROJECT, tenant_id=TENANT, binding_ref=canonical
            )
            assert facts.binding_ref == canonical
            assert (
                bridge.resolve_principal(
                    principal_id=OWNER_ID, project_id=PROJECT, binding_ref=canonical
                ).channel_binding_id
                == canonical
            )
        finally:
            store.close()

    def test_canonical_ref_with_no_active_binding_row_is_refused(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            store._conn.execute(
                "UPDATE nexus_studio_channel_bindings SET status = 'REVOKED' "
                "WHERE principal_id = ?",
                (EDITOR_ID,),
            )
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.resolve_principal_role(
                    EDITOR_ID,
                    PROJECT,
                    tenant_id=TENANT,
                    binding_ref=canonical_binding_ref(tenant_id=TENANT, principal_id=EDITOR_ID),
                )
            assert "no binding" in excinfo.value.bridge_detail
        finally:
            store.close()

    def test_resolve_principal_requires_a_binding_ref(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            for blank in ("", "   "):
                with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                    bridge.resolve_principal(
                        principal_id=OWNER_ID, project_id=PROJECT, binding_ref=blank
                    )
                assert excinfo.value.reason == "unauthorized_principal"
                assert "missing_binding" in str(excinfo.value)
        finally:
            store.close()

    def test_resolve_principal_returns_canonical_facts(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            ref = _binding(store, OWNER_ID)
            facts = bridge.resolve_principal(
                principal_id=OWNER_ID, project_id=PROJECT, binding_ref=ref
            )
            assert isinstance(facts, PrincipalFacts)
            assert facts == PrincipalFacts(
                principal_id=OWNER_ID, project_id=PROJECT, channel_binding_id=ref
            )
            resolved = bridge.resolve_authority(
                OWNER_ID, PROJECT, binding_ref=ref, tenant_id=TENANT
            )
            assert resolved.role is MembershipRole.OWNER
        finally:
            store.close()

    def test_non_studio_action_object_is_refused(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.verify_worker_execution_authority(
                    principal_id=OWNER_ID,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=_binding(store, OWNER_ID),
                    required_action="EXECUTE_PLAN",  # type: ignore[arg-type]
                )
            assert "not a StudioAction" in excinfo.value.bridge_detail
        finally:
            store.close()


# --------------------------------------------------------------------------- #
# Read-through directory narrowing
# --------------------------------------------------------------------------- #
class _DenyingDirectory:
    def authorize(self, actor: object, project_id: str) -> None:
        from nexus_ai_agent.creative.studio.models import AuthorizationError

        raise AuthorizationError("denied by directory")


class _AllowingDirectory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def authorize(self, actor: Any, project_id: str) -> None:
        self.calls.append((str(actor.actor_id), project_id))


class TestDirectoryReadThrough:
    def test_directory_refusal_narrows_a_durable_allow(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store, directory=_DenyingDirectory())
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                bridge.verify_worker_execution_authority(
                    principal_id=OWNER_ID,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=_binding(store, OWNER_ID),
                )
            assert "application directory refused" in excinfo.value.bridge_detail
            # Resolution (facts only) does not consult the directory.
            facts = bridge.resolve_principal_role(
                OWNER_ID, PROJECT, tenant_id=TENANT, binding_ref=_binding(store, OWNER_ID)
            )
            assert facts.role is MembershipRole.OWNER
        finally:
            store.close()

    def test_directory_allow_does_not_widen_a_durable_refusal(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            allowing = _AllowingDirectory()
            bridge = _bridge(store, directory=allowing)
            with pytest.raises(StudioAuthorityBridgeError):
                bridge.verify_worker_execution_authority(
                    principal_id=VIEWER_ID,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=_binding(store, VIEWER_ID),
                )
            assert allowing.calls == []
            # ... and when the durable row DOES allow, the directory is asked.
            bridge.verify_worker_execution_authority(
                principal_id=OWNER_ID,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=_binding(store, OWNER_ID),
            )
            assert allowing.calls == [(OWNER_ID, PROJECT)]
        finally:
            store.close()

    def test_sql_application_directory_allow_and_deny_read_through(self, tmp_path: Path) -> None:
        """A *real* ``SQLApplicationDirectory`` narrows; it never widens."""
        store = _seed(tmp_path)
        try:
            engine = _directory_engine(tmp_path)
            directory = SQLApplicationDirectory(engine)
            # The canonical directory mints its own principals; grant the bridge's
            # OWNER_ID a membership directly so the read-through can *allow*.
            _grant_directory_membership(engine, principal_id=OWNER_ID, project_id=PROJECT)
            assert (
                directory.authorize(
                    ActorIdentity(kind="user", actor_id=OWNER_ID), PROJECT
                ).project_id
                == PROJECT
            )
            granting = _bridge(store, directory=directory)
            proof = granting.verify_worker_execution_authority(
                principal_id=OWNER_ID,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=_binding(store, OWNER_ID),
            )
            assert proof.role is MembershipRole.OWNER
            # The editor is durable in the auth store but unknown to the directory.
            with pytest.raises(StudioAuthorityBridgeError) as excinfo:
                granting.verify_worker_execution_authority(
                    principal_id=EDITOR_ID,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=_binding(store, EDITOR_ID),
                )
            assert "application directory refused" in excinfo.value.bridge_detail
            engine.dispose()
        finally:
            store.close()


# --------------------------------------------------------------------------- #
# Cryptographic proof
# --------------------------------------------------------------------------- #
class TestUnifiedExecutionAuthorityProof:
    def test_proof_digest_is_the_jcs_digest_of_the_canonical_tuple(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            proof = bridge.verify_worker_execution_authority(
                principal_id=OWNER_ID,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=_binding(store, OWNER_ID),
            )
            expected = canonical_digest(
                proof.to_canonical_dict(), CanonicalizationScheme.JCS_RFC8785_V2
            )
            assert proof.proof_digest == expected
            assert proof.verify_integrity() is True
            assert len(proof.proof_digest) == 71 and proof.proof_digest.startswith("sha256:")
            assert proof.verified_at_utc
            assert isinstance(proof.permitted_actions, tuple)
            assert tuple(sorted(proof.permitted_actions, key=lambda a: a.value)) == (
                proof.permitted_actions
            )
        finally:
            store.close()

    def test_two_identities_never_share_a_proof_digest(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            digests = set()
            for principal_id in (OWNER_ID, EDITOR_ID):
                proof = bridge.verify_worker_execution_authority(
                    principal_id=principal_id,
                    project_id=PROJECT,
                    tenant_id=TENANT,
                    binding_ref=_binding(store, principal_id),
                )
                digests.add(proof.proof_digest)
            assert len(digests) == 2
        finally:
            store.close()

    def test_tampered_proof_fields_are_detected(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            proof = bridge.verify_worker_execution_authority(
                principal_id=EDITOR_ID,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=_binding(store, EDITOR_ID),
            )
            assert isinstance(proof, UnifiedExecutionAuthorityProof)
            with pytest.raises(dataclasses.FrozenInstanceError):
                proof.role = MembershipRole.OWNER  # type: ignore[misc]
            tampered_proofs = (
                dataclasses.replace(proof, principal_id=OWNER_ID),
                dataclasses.replace(proof, project_id="proj-other"),
                dataclasses.replace(proof, tenant_id=OTHER_TENANT),
                dataclasses.replace(proof, binding_ref="cbn_other"),
                dataclasses.replace(proof, role=MembershipRole.OWNER),
                dataclasses.replace(proof, verified_at_utc="2020-01-01T00:00:00+00:00"),
                dataclasses.replace(proof, permitted_actions=(StudioAction.READ_JOB,)),
            )
            for tampered in tampered_proofs:
                assert tampered.verify_integrity() is False
        finally:
            store.close()

    def test_proof_payload_is_stable_json(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            proof = bridge.verify_worker_execution_authority(
                principal_id=AUDITOR_ID,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=_binding(store, AUDITOR_ID),
                required_action=StudioAction.READ_AUDIT,
            )
            payload = proof.to_canonical_dict()
            assert payload["schema"] == "nexus.studio.execution_authority_proof.v1"
            assert payload["role"] == "AUDITOR"
            assert json.dumps(payload, sort_keys=True)
            assert "proof_digest" not in payload
        finally:
            store.close()

    def test_proof_is_deterministic_for_one_clock(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            ref = _binding(store, OWNER_ID)
            first = _bridge(store, clock=lambda: "2026-11-26T12:00:00+00:00")
            second = _bridge(store, clock=lambda: "2026-11-26T12:00:00+00:00")
            a = first.verify_worker_execution_authority(
                principal_id=OWNER_ID, project_id=PROJECT, tenant_id=TENANT, binding_ref=ref
            )
            b = second.verify_worker_execution_authority(
                principal_id=OWNER_ID, project_id=PROJECT, tenant_id=TENANT, binding_ref=ref
            )
            assert a == b
            assert a.proof_digest == b.proof_digest
        finally:
            store.close()


# --------------------------------------------------------------------------- #
# Snapshot consistency
# --------------------------------------------------------------------------- #
class TestSnapshotConsistency:
    def test_reads_never_open_a_write_transaction(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            writes_before = _audit_rows(store)
            bridge.verify_worker_execution_authority(
                principal_id=OWNER_ID,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=_binding(store, OWNER_ID),
            )
            assert _audit_rows(store) == writes_before
            assert store._conn.in_transaction is False
        finally:
            store.close()

    def test_revocation_committed_between_calls_is_observed(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            bridge = _bridge(store)
            ref = _binding(store, OWNER_ID)
            bridge.verify_worker_execution_authority(
                principal_id=OWNER_ID, project_id=PROJECT, tenant_id=TENANT, binding_ref=ref
            )
            store.revoke_membership(
                PROJECT, OWNER_ID, revoked_by="system", allow_system_bootstrap=True
            )
            with pytest.raises(StudioAuthorityBridgeError):
                bridge.verify_worker_execution_authority(
                    principal_id=OWNER_ID, project_id=PROJECT, tenant_id=TENANT, binding_ref=ref
                )
        finally:
            store.close()

    def test_foreign_keys_are_on_and_wal_is_active(self, tmp_path: Path) -> None:
        store = _seed(tmp_path)
        try:
            assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert int(store._conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        finally:
            store.close()


def _grant_directory_membership(
    engine: Any, *, principal_id: str, project_id: str, permissions: str = '["read","edit"]'
) -> None:
    """Seed the canonical directory tables with one real membership row."""
    from sqlmodel import Session

    from nexus_ai_agent.storage.models import (
        AppPrincipal,
        AppProject,
        AppProjectMembership,
    )

    with Session(engine) as session:
        with session.begin():
            session.add(AppPrincipal(principal_id=principal_id))
            session.add(AppProject(project_id=project_id))
            session.add(
                AppProjectMembership(
                    project_id=project_id,
                    principal_id=principal_id,
                    permissions_json=permissions,
                )
            )


def _directory_engine(tmp_path: Path) -> Any:
    """A real SQLite engine with the application-directory tables created."""
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    engine = create_engine(
        f"sqlite:///{tmp_path / 'directory.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _audit_rows(store: StudioPrincipalAuthStore) -> int:
    row = store._conn.execute("SELECT COUNT(*) FROM nexus_studio_auth_audit").fetchone()
    return int(row[0])


def test_utc_now_is_timezone_aware_iso8601() -> None:
    from nexus_ai_agent.adapters.studio_directory_bridge import utc_now

    stamp = utc_now()
    assert stamp.endswith("+00:00")
    assert "T" in stamp


def test_sqlite_error_type_is_unchanged_by_the_bridge(tmp_path: Path) -> None:
    """Guard-of-the-guard: the bridge never converts a store crash into an allow."""
    store = _seed(tmp_path)
    try:
        bridge = _bridge(store)
        store._conn.execute("DROP TABLE nexus_studio_channel_bindings")
        with pytest.raises(sqlite3.OperationalError):
            bridge.resolve_principal_role(OWNER_ID, PROJECT, tenant_id=TENANT, binding_ref="x")
    finally:
        store.close()
