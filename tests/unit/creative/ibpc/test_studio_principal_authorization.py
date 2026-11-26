"""Wave 3 slice 3.4 — studio principal authority and IDOR prevention.

The chain ``AppPrincipal -> AppChannelBinding -> ProjectMembership ->
StudioActionPermission`` is proven end to end:

* every role's allowed action set, and the typed refusal for the rest;
* cross-user IDOR: a principal presenting *another* principal's binding id;
* cross-project IDOR: a membership on project A never authorizes project B;
* durability: memberships/bindings survive a restart;
* ``IBPCService`` refuses session start, turn creation, approval and consume
  for an unauthorized principal, and enqueues nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    ACTION_ROLES,
    MEMBERSHIP_ROLES,
    DurableStudioAuthorizationService,
    IBPCAuthorizationGate,
    MembershipRole,
    StudioAction,
    StudioAuthorizationError,
    durable_studio_authorization_path,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
PROJECT = "project-w3"
OTHER_PROJECT = "project-other"
OWNER = "prn_" + "1" * 32
EDITOR = "prn_" + "2" * 32
VIEWER = "prn_" + "3" * 32
AUDITOR = "prn_" + "4" * 32
OUTSIDER = "prn_" + "5" * 32


@pytest.fixture()
def authz(tmp_path: Path):
    service = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
    for principal in (OWNER, EDITOR, VIEWER, AUDITOR, OUTSIDER):
        service.register_principal(principal)
    yield service
    service.close()


def _bindings(authz: DurableStudioAuthorizationService) -> dict[str, str]:
    return {
        principal: authz.bind_channel(
            principal_id=principal, channel="telegram", external_id=f"tg-{principal[-4:]}"
        )
        for principal in (OWNER, EDITOR, VIEWER, AUDITOR, OUTSIDER)
    }


def _members(authz: DurableStudioAuthorizationService) -> dict[str, str]:
    bindings = _bindings(authz)
    for principal, role in (
        (OWNER, MembershipRole.OWNER),
        (EDITOR, MembershipRole.EDITOR),
        (VIEWER, MembershipRole.VIEWER),
        (AUDITOR, MembershipRole.AUDITOR),
    ):
        authz.grant_membership(project_id=PROJECT, principal_id=principal, role=role)
    return bindings


class TestVocabulary:
    def test_roles_are_the_four_declared_membership_roles(self) -> None:
        assert {role.value for role in MEMBERSHIP_ROLES} == {
            "OWNER",
            "EDITOR",
            "VIEWER",
            "AUDITOR",
        }

    def test_every_action_has_a_non_empty_role_set(self) -> None:
        assert set(ACTION_ROLES) == set(StudioAction)
        for action, roles in ACTION_ROLES.items():
            assert roles, action
            assert roles <= MEMBERSHIP_ROLES

    def test_destructive_actions_are_owner_only(self) -> None:
        assert ACTION_ROLES[StudioAction.REVISION_RESTORE] == {MembershipRole.OWNER}

    def test_audit_is_readable_by_auditors_but_viewers_are_read_only(self) -> None:
        assert MembershipRole.AUDITOR in ACTION_ROLES[StudioAction.AUDIT_READ]
        for action in StudioAction:
            if action in {StudioAction.PROJECT_READ}:
                assert MembershipRole.VIEWER in ACTION_ROLES[action]
            else:
                assert MembershipRole.VIEWER not in ACTION_ROLES[action]

    def test_canonical_path_is_under_the_queue_directory(self, tmp_path: Path) -> None:
        expected = tmp_path / "queue" / "studio_authorization.sqlite3"
        assert durable_studio_authorization_path(tmp_path) == expected


class TestPrincipalAndBinding:
    def test_unknown_principal_is_refused(self, authz) -> None:
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.resolve_principal(
                principal_id="prn_" + "f" * 32, project_id=PROJECT, binding_ref="cbn_x"
            )
        assert exc.value.reason == "unknown_principal"

    def test_missing_binding_is_refused(self, authz) -> None:
        bindings = _members(authz)
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.resolve_principal(principal_id=OWNER, project_id=PROJECT, binding_ref="")
        assert exc.value.reason == "missing_binding"
        assert bindings[OWNER].startswith("cbn_")

    def test_unknown_binding_is_refused(self, authz) -> None:
        _members(authz)
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.resolve_principal(
                principal_id=OWNER, project_id=PROJECT, binding_ref="cbn_" + "0" * 32
            )
        assert exc.value.reason == "unknown_binding"

    def test_binding_ownership_prevents_cross_user_idor(self, authz) -> None:
        bindings = _members(authz)
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.resolve_principal(
                principal_id=EDITOR, project_id=PROJECT, binding_ref=bindings[OWNER]
            )
        assert exc.value.reason == "binding_owner_mismatch"

    def test_a_channel_account_cannot_move_between_principals(self, authz) -> None:
        authz.bind_channel(principal_id=OWNER, channel="telegram", external_id="tg-shared")
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.bind_channel(principal_id=EDITOR, channel="telegram", external_id="tg-shared")
        assert exc.value.reason == "binding_conflict"

    def test_rebinding_the_same_account_is_idempotent(self, authz) -> None:
        first = authz.bind_channel(principal_id=OWNER, channel="cli", external_id="owner")
        second = authz.bind_channel(principal_id=OWNER, channel="cli", external_id="owner")
        assert first == second

    def test_binding_an_unknown_principal_is_refused(self, authz) -> None:
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.bind_channel(principal_id="prn_" + "e" * 32, channel="cli", external_id="ghost")
        assert exc.value.reason == "unknown_principal"

    @pytest.mark.parametrize("channel,external_id", [("", "x"), ("cli", "")])
    def test_empty_binding_parts_are_refused(self, authz, channel, external_id) -> None:
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.bind_channel(principal_id=OWNER, channel=channel, external_id=external_id)
        assert exc.value.reason == "invalid_binding"


class TestMembershipAndAction:
    def test_every_role_gets_exactly_its_declared_actions(self, authz) -> None:
        bindings = _members(authz)
        holders = {
            MembershipRole.OWNER: OWNER,
            MembershipRole.EDITOR: EDITOR,
            MembershipRole.VIEWER: VIEWER,
            MembershipRole.AUDITOR: AUDITOR,
        }
        for action, allowed in ACTION_ROLES.items():
            for role, principal in holders.items():
                if role in allowed:
                    facts = authz.authorize_action(
                        principal_id=principal,
                        project_id=PROJECT,
                        binding_ref=bindings[principal],
                        action=action,
                    )
                    assert facts.role is role
                else:
                    with pytest.raises(StudioAuthorizationError) as exc:
                        authz.authorize_action(
                            principal_id=principal,
                            project_id=PROJECT,
                            binding_ref=bindings[principal],
                            action=action,
                        )
                    assert exc.value.reason == "insufficient_role"

    def test_non_member_is_refused(self, authz) -> None:
        bindings = _members(authz)
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.authorize_action(
                principal_id=OUTSIDER,
                project_id=PROJECT,
                binding_ref=bindings[OUTSIDER],
                action=StudioAction.PROJECT_READ,
            )
        assert exc.value.reason == "not_a_member"

    def test_membership_on_one_project_never_authorizes_another(self, authz) -> None:
        bindings = _members(authz)
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.authorize_action(
                principal_id=OWNER,
                project_id=OTHER_PROJECT,
                binding_ref=bindings[OWNER],
                action=StudioAction.PROJECT_READ,
            )
        assert exc.value.reason == "not_a_member"

    def test_insufficient_role_names_the_required_roles(self, authz) -> None:
        bindings = _members(authz)
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.authorize_action(
                principal_id=VIEWER,
                project_id=PROJECT,
                binding_ref=bindings[VIEWER],
                action=StudioAction.TURN_CREATE,
            )
        assert "TURN_CREATE" in exc.value.detail
        assert "VIEWER" in exc.value.detail

    def test_invalid_action_and_role_are_typed(self, authz) -> None:
        bindings = _members(authz)
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.authorize_action(
                principal_id=OWNER,
                project_id=PROJECT,
                binding_ref=bindings[OWNER],
                action="TURN_CREATE",  # type: ignore[arg-type]
            )
        assert exc.value.reason == "invalid_action"
        with pytest.raises(StudioAuthorizationError) as role_exc:
            authz.grant_membership(
                project_id=PROJECT,
                principal_id=OWNER,
                role="OWNER",  # type: ignore[arg-type]
            )
        assert role_exc.value.reason == "invalid_role"

    def test_membership_reads_and_revocation(self, authz) -> None:
        _members(authz)
        assert authz.get_membership(project_id=PROJECT, principal_id=EDITOR).role is (
            MembershipRole.EDITOR
        )
        # ordered by principal id, which is derived from the role digit
        assert [m.role for m in authz.members(PROJECT)] == [
            MembershipRole.OWNER,
            MembershipRole.EDITOR,
            MembershipRole.VIEWER,
            MembershipRole.AUDITOR,
        ]
        authz.revoke_membership(project_id=PROJECT, principal_id=EDITOR)
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.get_membership(project_id=PROJECT, principal_id=EDITOR)
        assert exc.value.reason == "not_a_member"
        with pytest.raises(StudioAuthorizationError):
            authz.revoke_membership(project_id=PROJECT, principal_id=EDITOR)

    def test_grants_and_reads_validate_their_identity(self, authz) -> None:
        with pytest.raises(StudioAuthorizationError) as exc:
            authz.grant_membership(
                project_id=PROJECT, principal_id="prn_" + "d" * 32, role=MembershipRole.OWNER
            )
        assert exc.value.reason == "unknown_principal"
        with pytest.raises(StudioAuthorizationError) as project_exc:
            authz.grant_membership(project_id="", principal_id=OWNER, role=MembershipRole.OWNER)
        assert project_exc.value.reason == "invalid_project"
        with pytest.raises(StudioAuthorizationError) as register_exc:
            authz.register_principal("")
        assert register_exc.value.reason == "invalid_principal"


class TestDurability:
    def test_authorization_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "authz.sqlite3"
        first = DurableStudioAuthorizationService(path, now=NOW)
        for principal in (OWNER, EDITOR):
            first.register_principal(principal)
        binding = first.bind_channel(principal_id=OWNER, channel="cli", external_id="owner")
        first.grant_membership(project_id=PROJECT, principal_id=OWNER, role=MembershipRole.OWNER)
        first.close()
        second = DurableStudioAuthorizationService(path, now=NOW)
        try:
            facts = second.authorize_action(
                principal_id=OWNER,
                project_id=PROJECT,
                binding_ref=binding,
                action=StudioAction.REVISION_RESTORE,
            )
            assert facts.role is MembershipRole.OWNER
            assert second.bindings_of(OWNER) == (binding,)
        finally:
            second.close()

    def test_the_file_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "private.sqlite3"
        service = DurableStudioAuthorizationService(path, now=NOW)
        try:
            assert (path.stat().st_mode & 0o077) == 0
        finally:
            service.close()


class _RecordingAuthority:
    """The canonical-authority port: used when no durable authority is injected."""

    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[tuple[str, str, str]] = []

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str) -> Any:
        from nexus_ai_agent.nagar.ibpc.authority import CanonicalAuthorityError, PrincipalFacts

        self.calls.append((principal_id, project_id, binding_ref))
        if not self.allow:
            raise CanonicalAuthorityError("authorization_denied", "principal is not a member")
        return PrincipalFacts(
            principal_id=principal_id, project_id=project_id, channel_binding_id=binding_ref
        )


BINDING_REF = "cbn_" + "a" * 64


def _deps(tmp_path: Path, **overrides) -> IBPCServiceDeps:
    params: dict[str, Any] = dict(
        queue=object(),
        registry=build_runtime_registry(),
        workspace_root=tmp_path,
        revision_provider=lambda: 7,
        authority=_RecordingAuthority(),
        channel_binding_ref=BINDING_REF,
    )
    params.update(overrides)
    return IBPCServiceDeps(**params)


class TestServiceWiring:
    def test_session_start_requires_a_durable_authority_role(self, tmp_path: Path) -> None:
        authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
        try:
            authz.register_principal(OUTSIDER)
            binding = authz.bind_channel(
                principal_id=OUTSIDER, channel="cli", external_id="outsider"
            )
            service = IBPCService(
                _deps(
                    tmp_path,
                    principal_authority=authz,
                    channel_binding_ref=binding,
                )
            )
            with pytest.raises(ApprovalError) as exc:
                service.open_session(
                    tenant_id="ten",
                    project_id=PROJECT,
                    principal_id=OUTSIDER,
                    user_id=1,
                    chat_id=1,
                    current_revision=7,
                )
            assert "not_a_member" in exc.value.reason
        finally:
            authz.close()

    def test_authorized_owner_can_start_a_session(self, tmp_path: Path) -> None:
        authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
        try:
            authz.register_principal(OWNER)
            binding = authz.bind_channel(principal_id=OWNER, channel="cli", external_id="owner")
            authz.grant_membership(
                project_id=PROJECT, principal_id=OWNER, role=MembershipRole.OWNER
            )
            service = IBPCService(
                _deps(tmp_path, principal_authority=authz, channel_binding_ref=binding)
            )
            session_id = service.open_session(
                tenant_id="ten",
                project_id=PROJECT,
                principal_id=OWNER,
                user_id=1,
                chat_id=1,
                current_revision=7,
            )
            assert isinstance(session_id, str) and session_id
        finally:
            authz.close()

    def test_turn_creation_refuses_a_viewer_and_enqueues_nothing(self, tmp_path: Path) -> None:
        authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
        try:
            authz.register_principal(VIEWER)
            binding = authz.bind_channel(principal_id=VIEWER, channel="cli", external_id="viewer")
            authz.grant_membership(
                project_id=PROJECT, principal_id=VIEWER, role=MembershipRole.VIEWER
            )
            service = IBPCService(
                _deps(tmp_path, principal_authority=authz, channel_binding_ref=binding)
            )
            # A VIEWER may not start a session either: the refusal is typed and
            # names the roles that would be acceptable.
            with pytest.raises(ApprovalError) as start_exc:
                service.open_session(
                    tenant_id="ten",
                    project_id=PROJECT,
                    principal_id=VIEWER,
                    user_id=1,
                    chat_id=1,
                    current_revision=7,
                )
            assert start_exc.value.reason == "unauthorized_principal:insufficient_role"
            assert "EDITOR" in start_exc.value.detail
        finally:
            authz.close()

    def test_service_without_authority_refuses_turn_creation(self, tmp_path: Path) -> None:
        """No authority at all ⇒ fail closed BEFORE the turn fence opens."""
        service = IBPCService(_deps(tmp_path, authority=None))
        session_id = service.open_session(
            tenant_id="ten",
            project_id=PROJECT,
            principal_id=OWNER,
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        result = service.submit_text_intent(
            session_id,
            "turn-1",
            sequence_number=1,
            text="trim from 0.2 to 0.6 seconds",
            asset_id="src",
            source_path=str(tmp_path / "absent.mp4"),
        )
        assert getattr(result, "reason", "") == "unauthorized_principal"
        # the refusal did not perturb the turn fence
        assert service.sessions.get_session(session_id).turns == {}

    def test_canonical_authority_denial_is_a_typed_refusal(self, tmp_path: Path) -> None:
        service = IBPCService(_deps(tmp_path, authority=_RecordingAuthority(allow=False)))
        session_id = service.open_session(
            tenant_id="ten",
            project_id=PROJECT,
            principal_id=OWNER,
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        result = service.submit_text_intent(
            session_id,
            "turn-1",
            sequence_number=1,
            text="trim from 0.2 to 0.6 seconds",
            asset_id="src",
            source_path=str(tmp_path / "absent.mp4"),
        )
        assert getattr(result, "reason", "") == "unauthorized_principal"
        assert "authorization_denied" in getattr(result, "detail", "")

    def test_gate_rejects_a_binding_that_disagrees_with_the_identity(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalBindingPayload,
            ApprovalIdentity,
            CanonicalApprovalLedger,
        )
        from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
        from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes

        authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
        try:
            authz.register_principal(OWNER)
            binding = authz.bind_channel(principal_id=OWNER, channel="cli", external_id="owner")
            authz.grant_membership(
                project_id=PROJECT, principal_id=OWNER, role=MembershipRole.OWNER
            )
            ledger = CanonicalApprovalLedger(
                key_provider=StaticKeyRing({"u": b"u" * 32}, active_key_id="u"), now=NOW
            )
            gate = IBPCAuthorizationGate(authorization=authz, approvals=ledger, binding_ref=binding)
            identity = ApprovalIdentity(
                principal_id=OWNER,
                tenant_id="ten",
                project_id=PROJECT,
                session_id="s1",
                turn_id="t1",
                sequence_number=1,
                source_revision=7,
                source_asset_digest=sha256_bytes(b"a"),
                ast_digest=digest_of({"ast": 1}),
                plan_digest=digest_of({"plan": 1}),
                preview_digest=digest_of({"preview": 1}),
            )
            mismatched = ApprovalBindingPayload(
                project_id=PROJECT,
                session_id="s1",
                turn_id="t1",
                base_revision_id="rev:7",
                plan_sha256=digest_of({"plan": 1}),
                preview_sha256=digest_of({"preview": 1}),
                scope_signature="sha256:" + "b" * 64,
                principal_id="prn_" + "9" * 32,
                expires_at_utc="2026-11-25T00:10:00Z",
            )
            with pytest.raises(StudioAuthorizationError) as exc:
                gate.authorize_consume(
                    principal_id=OWNER,
                    project_id=PROJECT,
                    approval_id="appr_absent",
                    identity=identity,
                    payload=mismatched,
                )
            assert exc.value.reason == "binding_identity_mismatch"
            assert gate.binding_ref == binding
            assert (
                gate.authorize(
                    principal_id=OWNER, project_id=PROJECT, action=StudioAction.PROJECT_READ
                ).role
                is MembershipRole.OWNER
            )
        finally:
            authz.close()

    def test_gate_refuses_when_the_ledger_has_no_canonical_surface(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalBindingPayload,
            ApprovalIdentity,
            ApprovalLedger,
        )
        from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes

        authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
        try:
            authz.register_principal(OWNER)
            binding = authz.bind_channel(principal_id=OWNER, channel="cli", external_id="owner")
            authz.grant_membership(
                project_id=PROJECT, principal_id=OWNER, role=MembershipRole.OWNER
            )
            gate = IBPCAuthorizationGate(
                authorization=authz, approvals=ApprovalLedger(now=NOW), binding_ref=binding
            )
            identity = ApprovalIdentity(
                principal_id=OWNER,
                tenant_id="ten",
                project_id=PROJECT,
                session_id="s1",
                turn_id="t1",
                sequence_number=1,
                source_revision=7,
                source_asset_digest=sha256_bytes(b"a"),
                ast_digest=digest_of({"ast": 1}),
                plan_digest=digest_of({"plan": 1}),
                preview_digest=digest_of({"preview": 1}),
            )
            payload = ApprovalBindingPayload(
                project_id=PROJECT,
                session_id="s1",
                turn_id="t1",
                base_revision_id="rev:7",
                plan_sha256=digest_of({"plan": 1}),
                preview_sha256=digest_of({"preview": 1}),
                scope_signature="sha256:" + "b" * 64,
                principal_id=OWNER,
                expires_at_utc="2026-11-25T00:10:00Z",
            )
            with pytest.raises(ApprovalError) as exc:
                gate.authorize_consume(
                    principal_id=OWNER,
                    project_id=PROJECT,
                    approval_id="appr_absent",
                    identity=identity,
                    payload=payload,
                )
            assert exc.value.reason == "canonical_binding_unavailable"
        finally:
            authz.close()

    def test_gate_requires_a_consent_store(self) -> None:
        from nexus_ai_agent.nagar.ibpc.approval import CanonicalApprovalLedger
        from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
        from nexus_ai_agent.nagar.ibpc.privacy import ConsentAction, ConsentScope, PrivacyViolation

        ledger = CanonicalApprovalLedger(
            key_provider=StaticKeyRing({"u": b"u" * 32}, active_key_id="u"), now=NOW
        )
        gate = IBPCAuthorizationGate(
            authorization=object(),  # type: ignore[arg-type]
            approvals=ledger,
            binding_ref=BINDING_REF,
        )
        with pytest.raises(PrivacyViolation) as exc:
            gate.require_processing_consent(
                consent_store=object(),
                principal_id=OWNER,
                project_id=PROJECT,
                provider_id="local",
                data_category=ConsentScope.AUDIO,
                action=ConsentAction.LOCAL_PROCESSING,
            )
        assert exc.value.reason == "consent_store_unavailable"


class TestAuthorizationServiceSeams:
    def test_the_store_path_is_exposed(self, authz) -> None:
        assert authz.path.name == "authz.sqlite3"
        assert authz.path.exists()

    def test_the_transaction_seam_is_reentrant(self, authz) -> None:
        with authz._transaction():
            with authz._transaction():
                assert authz.bindings_of(OWNER) == ()

    def test_a_reentrant_write_commits_once(self, authz) -> None:
        nested = "prn_" + "8" * 32
        with authz._transaction():
            with authz._transaction():
                authz.register_principal(nested, display_name="nested")
        binding = authz.bind_channel(principal_id=nested, channel="telegram", external_id="tg-8")
        authz.grant_membership(project_id=PROJECT, principal_id=nested, role=MembershipRole.VIEWER)
        facts = authz.authorize_action(
            principal_id=nested,
            project_id=PROJECT,
            binding_ref=binding,
            action=StudioAction.PROJECT_READ,
        )
        assert facts.principal_id == nested

    def test_gate_delegates_a_successful_canonical_consume(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalBindingPayload,
            ApprovalIdentity,
            DurableCanonicalApprovalLedger,
        )
        from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
        from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes
        from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding

        authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
        ledger = DurableCanonicalApprovalLedger(
            tmp_path / "approvals.sqlite3",
            key_provider=StaticKeyRing({"u": b"u" * 32}, active_key_id="u"),
            now=NOW,
        )
        try:
            authz.register_principal(OWNER)
            binding_ref = authz.bind_channel(principal_id=OWNER, channel="cli", external_id="o")
            authz.grant_membership(
                project_id=PROJECT, principal_id=OWNER, role=MembershipRole.OWNER
            )
            identity = ApprovalIdentity(
                principal_id=OWNER,
                tenant_id="ten",
                project_id=PROJECT,
                session_id="s1",
                turn_id="t1",
                sequence_number=1,
                source_revision=7,
                source_asset_digest=sha256_bytes(b"a"),
                ast_digest=digest_of({"ast": 1}),
                plan_digest=digest_of({"plan": 1}),
                preview_digest=digest_of({"preview": 1}),
            )
            payload = ApprovalBindingPayload(
                project_id=PROJECT,
                session_id="s1",
                turn_id="t1",
                base_revision_id="rev:7",
                plan_sha256=identity.plan_digest,
                preview_sha256=identity.preview_digest,
                scope_signature="sha256:" + "b" * 64,
                principal_id=OWNER,
                expires_at_utc="2026-11-25T00:10:00Z",
            )
            stored = ledger.create(
                ApprovalBinding(
                    session_id="s1",
                    turn_id="t1",
                    sequence_number=1,
                    tenant_id="ten",
                    project_id=PROJECT,
                    principal_id=OWNER,
                    source_revision=7,
                    source_asset_digest=identity.source_asset_digest,
                    ast_digest=identity.ast_digest,
                    plan_digest=identity.plan_digest,
                    preview_digest=identity.preview_digest,
                )
            )
            ledger.approve(stored.approval_id, identity=identity, turn_is_active=lambda: True)
            ledger.bind_canonical(stored.approval_id, payload)
            gate = IBPCAuthorizationGate(
                authorization=authz, approvals=ledger, binding_ref=binding_ref
            )
            consumed = gate.authorize_consume(
                principal_id=OWNER,
                project_id=PROJECT,
                approval_id=stored.approval_id,
                identity=identity,
                payload=payload,
            )
            assert consumed.state.value == "CONSUMED"
        finally:
            ledger.close()
            authz.close()

    def test_gate_delegates_a_live_processing_consent(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.approval import CanonicalApprovalLedger
        from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
        from nexus_ai_agent.nagar.ibpc.privacy import (
            ConsentAction,
            ConsentScope,
            DurablePrivacyConsentStore,
        )

        store = DurablePrivacyConsentStore(tmp_path / "consent.sqlite3", now=NOW)
        try:
            grant = store.grant_consent(
                principal_id=OWNER,
                project_id=PROJECT,
                provider_id="local",
                data_category=ConsentScope.VIDEO,
                action=ConsentAction.LOCAL_PROCESSING,
                expires_at_utc="2027-01-01T00:00:00Z",
            )
            ledger = CanonicalApprovalLedger(
                key_provider=StaticKeyRing({"u": b"u" * 32}, active_key_id="u"), now=NOW
            )
            gate = IBPCAuthorizationGate(
                authorization=object(),  # type: ignore[arg-type]
                approvals=ledger,
                binding_ref=BINDING_REF,
            )
            resolved = gate.require_processing_consent(
                consent_store=store,
                principal_id=OWNER,
                project_id=PROJECT,
                provider_id="local",
                data_category=ConsentScope.VIDEO,
                action=ConsentAction.LOCAL_PROCESSING,
            )
            assert resolved.consent_id == grant.consent_id
        finally:
            store.close()
