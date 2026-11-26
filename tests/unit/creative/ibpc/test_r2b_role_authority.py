"""Wave 3-R2b — no synthesized ``OWNER``, and the strict role matrix.

``_authorize_principal`` used to fall back to ``MembershipRole.OWNER`` whenever
no durable role authority was configured and the canonical identity port had
verified the principal. ``CanonicalAuthorityPort`` proves *identity*
(principal → project membership → channel binding ownership); it says nothing
about a **role**, so the fallback silently granted the OWNER-only
``REVISION_RESTORE`` to any member.

The gate now:

1. always calls ``_resolve_authority`` first (so the legacy canonical identity
   check — and its 23/23 mutation coverage — keeps running);
2. never fabricates a role: a verified ``role`` on the resolved facts is enforced
   strictly against the same ``ACTION_ROLES`` matrix the durable authority uses;
3. fails closed with ``role_verification_required`` when a role is needed and no
   role authority exists (``require_role_authority=True``, or an OWNER-only
   action such as ``REVISION_RESTORE``);
4. returns ``None`` — never a role-bearing fact — for the remaining legacy
   composition, so no caller can mistake identity for a grant;
5. in ``build_durable_ibpc_service`` (``require_role_authority=True`` by default)
   refuses to compose at all without a principal authority.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError, DurableCanonicalApprovalLedger
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import CanonicalAuthorityError, PrincipalFacts
from nexus_ai_agent.nagar.ibpc.composition import build_durable_ibpc_service
from nexus_ai_agent.nagar.ibpc.live_session import DurableLiveSessionStore
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    ACTION_ROLES,
    AuthorizationRefusalError,
    DurableStudioAuthorizationService,
    MembershipRole,
    StudioAuthorizationError,
)
from nexus_ai_agent.nagar.ibpc.principal_authority import StudioAction as Action
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
KEY = b"o" * 32
PRINCIPAL = "prn_" + "6" * 32
PROJECT = "project-r2b"
BINDING_REF = "cbn_" + "d" * 64

MUTATING_ACTIONS = (
    Action.SESSION_START,
    Action.TURN_CREATE,
    Action.PREVIEW_GENERATE,
    Action.APPROVAL_CONSUME,
)


class _IdentityAuthority:
    """Canonical identity port: proves identity, optionally carries a role."""

    def __init__(self, *, role: MembershipRole | None = None, error: Exception | None = None):
        self.role = role
        self.error = error
        self.calls: list[str] = []

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        self.calls.append(principal_id)
        if self.error is not None:
            raise self.error
        facts = {
            "principal_id": principal_id,
            "project_id": project_id,
            "channel_binding_id": binding_ref,
        }
        if self.role is None:
            return PrincipalFacts(**facts)
        return types.SimpleNamespace(**facts, role=self.role)


def _service(tmp_path: Path, **overrides) -> IBPCService:
    deps = dict(
        queue=object(),  # type: ignore[arg-type]
        registry=build_runtime_registry(),
        workspace_root=tmp_path,
        revision_provider=lambda: 7,
        now=NOW,
        channel_binding_ref=BINDING_REF,
        authority=_IdentityAuthority(),
        approval_key_provider=StaticKeyRing({"unit": KEY}, active_key_id="unit"),
        approval_ledger=DurableCanonicalApprovalLedger(
            tmp_path / "approvals.sqlite3",
            key_provider=StaticKeyRing({"unit": KEY}, active_key_id="unit"),
            now=NOW,
        ),
        session_store=DurableLiveSessionStore(tmp_path / "sessions.sqlite3", now=NOW),
    )
    deps.update(overrides)
    return IBPCService(IBPCServiceDeps(**deps))  # type: ignore[arg-type]


class TestNoSynthesizedOwner:
    def test_the_legacy_identity_fallback_returns_no_role_grant(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        facts = service._authorize_principal(
            principal_id=PRINCIPAL, project_id=PROJECT, action=Action.TURN_CREATE
        )
        # identity was verified, but no role is invented for the caller
        assert facts is None
        # and the canonical authority was actually consulted
        assert service._deps.authority.calls == [PRINCIPAL]

    def test_the_owner_only_action_is_refused_without_a_role(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        with pytest.raises(ApprovalError) as exc:
            service._authorize_principal(
                principal_id=PRINCIPAL, project_id=PROJECT, action=Action.REVISION_RESTORE
            )
        assert exc.value.reason == "unauthorized_principal"
        assert "role_verification_required" in exc.value.detail
        assert "REVISION_RESTORE" in exc.value.detail

    def test_require_role_authority_refuses_every_action_without_one(self, tmp_path: Path) -> None:
        service = _service(tmp_path, require_role_authority=True)
        for action in (*MUTATING_ACTIONS, Action.REVISION_RESTORE):
            with pytest.raises(ApprovalError) as exc:
                service._authorize_principal(
                    principal_id=PRINCIPAL, project_id=PROJECT, action=action
                )
            assert exc.value.reason == "unauthorized_principal"
            assert "role_verification_required" in exc.value.detail

    def test_the_only_owner_only_action_is_derived_from_the_matrix(self) -> None:
        owner_only = {a for a, roles in ACTION_ROLES.items() if roles == {MembershipRole.OWNER}}
        assert owner_only == {Action.REVISION_RESTORE}
        assert service_mod._OWNER_ONLY_ACTIONS == owner_only


class TestAVerifiedRoleOnTheIdentityFacts:
    @pytest.mark.parametrize("role", [MembershipRole.VIEWER, MembershipRole.AUDITOR])
    def test_read_only_roles_are_denied_every_mutating_action(self, tmp_path: Path, role) -> None:
        for action in MUTATING_ACTIONS:
            service = _service(
                tmp_path / f"{role.value}-{action.value}", authority=_IdentityAuthority(role=role)
            )
            with pytest.raises(ApprovalError) as exc:
                service._authorize_principal(
                    principal_id=PRINCIPAL, project_id=PROJECT, action=action
                )
            assert exc.value.reason == "unauthorized_principal"
            assert "insufficient_role" in exc.value.detail
        # ... and the destruction action too
        service = _service(
            tmp_path / f"{role.value}-restore", authority=_IdentityAuthority(role=role)
        )
        with pytest.raises(ApprovalError):
            service._authorize_principal(
                principal_id=PRINCIPAL, project_id=PROJECT, action=Action.REVISION_RESTORE
            )

    def test_an_editor_may_edit_but_never_restore(self, tmp_path: Path) -> None:
        editor = _IdentityAuthority(role=MembershipRole.EDITOR)
        service = _service(tmp_path, authority=editor)
        for action in MUTATING_ACTIONS:
            facts = service._authorize_principal(
                principal_id=PRINCIPAL, project_id=PROJECT, action=action
            )
            assert facts is not None
            assert facts.role is MembershipRole.EDITOR
        with pytest.raises(ApprovalError) as exc:
            service._authorize_principal(
                principal_id=PRINCIPAL, project_id=PROJECT, action=Action.REVISION_RESTORE
            )
        assert exc.value.reason == "unauthorized_principal"
        assert "insufficient_role" in exc.value.detail

    def test_an_owner_may_restore(self, tmp_path: Path) -> None:
        service = _service(tmp_path, authority=_IdentityAuthority(role=MembershipRole.OWNER))
        facts = service._authorize_principal(
            principal_id=PRINCIPAL, project_id=PROJECT, action=Action.REVISION_RESTORE
        )
        assert facts is not None and facts.role is MembershipRole.OWNER


class TestTheDurableRoleAuthority:
    def _studio(self, tmp_path: Path, role: MembershipRole):
        authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
        authz.register_principal(PRINCIPAL)
        binding = authz.bind_channel(
            principal_id=PRINCIPAL, channel="telegram", external_id="tg-r2b"
        )
        authz.grant_membership(project_id=PROJECT, principal_id=PRINCIPAL, role=role)
        return authz, binding

    @pytest.mark.parametrize(
        "role,allowed",
        [
            (MembershipRole.VIEWER, set()),
            (MembershipRole.AUDITOR, set()),
            (MembershipRole.EDITOR, set(MUTATING_ACTIONS)),
            (MembershipRole.OWNER, set(MUTATING_ACTIONS) | {Action.REVISION_RESTORE}),
        ],
    )
    def test_the_matrix_is_enforced_by_the_authority(self, tmp_path: Path, role, allowed) -> None:
        authz, binding = self._studio(tmp_path, role)
        try:
            service = _service(
                tmp_path,
                principal_authority=authz,
                channel_binding_ref=binding,
                require_role_authority=True,
            )
            for action in (*MUTATING_ACTIONS, Action.REVISION_RESTORE):
                if action in allowed:
                    facts = service._authorize_principal(
                        principal_id=PRINCIPAL, project_id=PROJECT, action=action
                    )
                    assert facts is not None and facts.role is role
                else:
                    with pytest.raises(ApprovalError):
                        service._authorize_principal(
                            principal_id=PRINCIPAL, project_id=PROJECT, action=action
                        )
        finally:
            authz.close()

    def test_a_disagreeing_binding_is_refused(self, tmp_path: Path) -> None:
        authz, binding = self._studio(tmp_path, MembershipRole.OWNER)
        try:
            service = _service(
                tmp_path,
                principal_authority=authz,
                channel_binding_ref=binding,
                authority=_IdentityAuthority(),
            )
            # the identity port reports a *different* binding than the one the
            # role was verified under
            service._deps.authority = types.SimpleNamespace(
                resolve_principal=lambda **kw: PrincipalFacts(
                    principal_id=kw["principal_id"],
                    project_id=kw["project_id"],
                    channel_binding_id="cbn_" + "9" * 64,
                )
            )
            with pytest.raises(ApprovalError) as exc:
                service._authorize_principal(
                    principal_id=PRINCIPAL, project_id=PROJECT, action=Action.TURN_CREATE
                )
            assert exc.value.reason == "unauthorized_principal"
            assert "binding_inconsistent" in exc.value.detail
        finally:
            authz.close()

    def test_a_role_authority_without_a_role_is_refused(self, tmp_path: Path) -> None:
        service = _service(
            tmp_path,
            principal_authority=types.SimpleNamespace(
                authorize_action=lambda **_kw: types.SimpleNamespace(
                    principal_id=PRINCIPAL,
                    project_id=PROJECT,
                    channel_binding_id=BINDING_REF,
                    role=None,
                )
            ),
        )
        with pytest.raises(ApprovalError) as exc:
            service._authorize_principal(
                principal_id=PRINCIPAL, project_id=PROJECT, action=Action.TURN_CREATE
            )
        assert exc.value.reason == "unauthorized_principal"
        assert "role_verification_required" in exc.value.detail


class TestProductionCompositionRequiresRoleAuthority:
    def test_the_factory_refuses_without_a_principal_authority(self, tmp_path: Path) -> None:
        deps = IBPCServiceDeps(
            queue=object(),  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 7,
            now=NOW,
        )
        with pytest.raises(AuthorizationRefusalError) as exc:
            build_durable_ibpc_service(tmp_path / "w3", KEY, deps=deps)
        assert exc.value.reason == "role_authority_required"
        # it is a Studio authorization refusal, so existing handlers still catch it
        assert isinstance(exc.value, StudioAuthorizationError)

    def test_the_opt_out_composes_a_service_without_role_authority(self, tmp_path: Path) -> None:
        deps = IBPCServiceDeps(
            queue=object(),  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 7,
            now=NOW,
        )
        service = build_durable_ibpc_service(
            tmp_path / "w3", KEY, deps=deps, require_role_authority=False
        )
        assert service._deps.principal_authority is None
        assert service._deps.require_role_authority is False
        # and such a service still refuses the OWNER-only action
        service._deps.authority = _IdentityAuthority()
        with pytest.raises(ApprovalError):
            service._authorize_principal(
                principal_id=PRINCIPAL, project_id=PROJECT, action=Action.REVISION_RESTORE
            )

    def test_the_factory_threads_the_flag_into_the_service(self, tmp_path: Path) -> None:
        authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
        try:
            deps = IBPCServiceDeps(
                queue=object(),  # type: ignore[arg-type]
                registry=build_runtime_registry(),
                workspace_root=tmp_path,
                revision_provider=lambda: 7,
                now=NOW,
            )
            service = build_durable_ibpc_service(
                tmp_path / "w3", KEY, deps=deps, principal_authority=authz
            )
            assert service._deps.require_role_authority is True
            assert service._deps.principal_authority is authz
        finally:
            authz.close()


class TestTheServiceBoundaryStillFailsClosed:
    def test_a_session_cannot_open_when_identity_verification_fails(self, tmp_path: Path) -> None:
        service = _service(
            tmp_path,
            authority=_IdentityAuthority(
                error=CanonicalAuthorityError("authorization_denied", "revoked")
            ),
            require_role_authority=True,
        )
        with pytest.raises(ApprovalError) as exc:
            service.open_session(
                tenant_id="ten",
                project_id=PROJECT,
                principal_id=PRINCIPAL,
                user_id=1,
                chat_id=1,
                current_revision=7,
            )
        assert exc.value.reason == "unauthorized_principal"
        assert "authorization_denied" in exc.value.detail

    def test_a_legacy_composition_still_executes_the_edit_lane(self, tmp_path: Path) -> None:
        """The fallback is narrow on purpose: identity-only composition keeps the
        non-role actions working, and only a role proof is withheld."""
        service = _service(tmp_path, require_role_authority=False)
        sid = service.open_session(
            tenant_id="ten",
            project_id=PROJECT,
            principal_id=PRINCIPAL,
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        assert isinstance(sid, str)
        outcome = asyncio.run(service.execute(sid, "turn-1", "ap-unknown", principal_id=PRINCIPAL))
        assert outcome.status == "refused"
