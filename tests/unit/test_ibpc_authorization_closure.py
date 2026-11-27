"""Wave 3-R7 — the authorization gaps in the post-#365 IBPC service.

1. **An unconfigured role gate used to be a no-op.** ``_authorize_studio_action``
   returned early whenever ``deps.authorizer is None`` — the *default* — so a
   service composed without a Studio authorizer still minted (``approve``) and
   burned (``execute``) approval tokens, granted and revoked privacy consent,
   disclosed the audit log and drove session lifecycle transitions with no role
   proof at all.  Identity verification (``_resolve_authority``) fails closed when
   its port is missing; the role gate now does the same, with an explicit
   plan-only **allowlist** (``SUBMIT_INTENT``, ``READ_SESSION``) instead of a
   partial denylist: anything else — including an action added to
   ``StudioAction`` later — is refused by default.
2. **The authorizer's answer was trusted unchecked.**  When a Studio authorizer
   *was* configured, its return value was discarded: an ``allowed=False`` decision
   returned without raising, or a decision minted for a different principal,
   project, action or channel binding, was accepted as a grant.  Every decision is
   now verified field-by-field before any state changes.
3. **Canonical facts were trusted unchecked.** ``_resolve_authority`` returned
   whatever ``resolve_principal`` produced; callers copied
   ``facts.channel_binding_id`` straight into the queue payload and only compared
   it against the context at consume time.  A ``None``/``""``/whitespace binding,
   or a different ``principal_id``/``project_id``, was accepted.

Every refusal keeps the canonical public reason (``AUTHORIZATION_REFUSAL_REASON``)
and leaves the world untouched: no approval minted or consumed, no consent granted
or revoked, no audit row disclosed, no session transition, no queued job.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import types
from collections.abc import Callable, Generator
from typing import Any

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.adapters.studio_principal_auth import (
    ROLE_ACTIONS,
    ProjectMembershipAuthorizer,
    StudioPrincipalAuthStore,
)
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import (
    AuthorizationError,
    CanonicalAuthorityError,
    StudioAction,
    StudioAuthorizationDecision,
    StudioRole,
)
from nexus_ai_agent.nagar.ibpc.intent import Refusal
from nexus_ai_agent.nagar.ibpc.models import (
    ApprovalState,
    DataCategory,
    PrivacyMode,
    ProcessingScope,
)
from nexus_ai_agent.nagar.ibpc.service import (
    _AUTHORIZER_REQUIRED_ACTIONS,
    _PLAN_ONLY_ACTIONS_WITHOUT_AUTHORIZER,
    AUTHORIZATION_REFUSAL_REASON,
    IBPCService,
    IBPCServiceDeps,
)

PRINCIPAL = "user-1"
PROJECT = "project-r7"
BINDING = "cbn_" + "b" * 64
OTHER_BINDING = "cbn_" + "c" * 64


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #
class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict[str, Any]) -> str:
        self.enqueued.append(payload)
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str) -> Any:
        from nexus_ai_agent.application.ports.job_queue import JobStatus

        return JobStatus.FAILED_TERMINAL

    async def get_result(self, job_id: str) -> dict[str, Any]:
        return {"success": False, "error": "not executed in this test"}


class _AllowAll:
    """``StudioAuthorizerPort`` double: returns a consistent ``allowed`` decision."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def authorize_action(
        self,
        *,
        principal_id: str,
        project_id: str,
        action: Any,
        binding_ref: str = "",
    ) -> StudioAuthorizationDecision:
        self.calls.append(
            {
                "principal_id": principal_id,
                "project_id": project_id,
                "action": action,
                "binding_ref": binding_ref,
            }
        )
        return StudioAuthorizationDecision(
            allowed=True,
            principal_id=principal_id,
            project_id=project_id,
            role="OWNER",
            action=getattr(action, "value", str(action)),
            reason="test_double",
            binding_ref=binding_ref,
        )


class _Scripted:
    """``StudioAuthorizerPort`` double returning whatever the script says."""

    def __init__(self, make: Callable[..., Any]) -> None:
        self._make = make

    def authorize_action(
        self,
        *,
        principal_id: str,
        project_id: str,
        action: Any,
        binding_ref: str = "",
    ) -> Any:
        return self._make(principal_id, project_id, action, binding_ref)


def _decision(**overrides: Any) -> StudioAuthorizationDecision:
    fields: dict[str, Any] = {
        "allowed": True,
        "principal_id": PRINCIPAL,
        "project_id": PROJECT,
        "role": "OWNER",
        "action": StudioAction.APPROVE_PLAN.value,
        "reason": "authorized",
        "binding_ref": BINDING,
    }
    fields.update(overrides)
    return StudioAuthorizationDecision(**fields)


def _facts(**overrides: Any) -> Any:
    base = {"principal_id": PRINCIPAL, "project_id": PROJECT, "channel_binding_id": BINDING}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _Port:
    """``CanonicalAuthorityPort`` double with a per-call script."""

    def __init__(self, *answers: Any) -> None:
        self._answers = list(answers) or [_facts()]
        self.calls: list[tuple[str, str, str]] = []

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str) -> Any:
        self.calls.append((principal_id, project_id, binding_ref))
        answer = self._answers[min(len(self.calls) - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture()
def media(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        service_mod,
        "probe_media",
        lambda path, *, binary=None: (types.SimpleNamespace(duration_us=2_000_000), 30.0),
    )

    def fake_shadow(path: Any, scope: Any, plan_digest: str, **kwargs: Any) -> Any:
        payload = {
            "plan_digest": plan_digest,
            "scope_digest": scope.scope_digest,
            "source_duration_us": 2_000_000,
            "scope_start_us": scope.start_us,
            "scope_end_us": scope.end_us,
            "keyframes": [],
            "composite_digest": "sha256:" + "0" * 64,
        }
        return types.SimpleNamespace(
            plan_digest=plan_digest,
            scope_digest=scope.scope_digest,
            keyframes=(),
            model_dump=lambda **_kw: payload,
        )

    def fake_signature(path: Any, scope: Any, **kwargs: Any) -> Any:
        payload = {
            "scope_digest": scope.scope_digest,
            "asset_id": scope.asset_id,
            "start_us": scope.start_us,
            "end_us": scope.end_us,
            "color_histogram": [0.0] * 64,
            "motion_magnitude": 0.0,
            "sample_frames": 1,
            "computed_at": "2026-10-31T00:00:00Z",
            "signature_digest": "sha256:" + "0" * 64,
        }
        return types.SimpleNamespace(
            signature_digest=payload["signature_digest"],
            scope_digest=scope.scope_digest,
            model_dump=lambda **_kw: payload,
        )

    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)
    clip = tmp_path / "src.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    return types.SimpleNamespace(clip=clip, root=tmp_path)


_UNSET = object()


def _service(
    *,
    authority: Any = _UNSET,
    authorizer: Any = _UNSET,
    binding: str | None = BINDING,
    workspace: pathlib.Path | None = None,
    durable: bool = False,
) -> tuple[IBPCService, _Queue]:
    queue = _Queue()
    deps = IBPCServiceDeps(
        queue=queue,
        registry=build_runtime_registry(),
        workspace_root=pathlib.Path(workspace or "/tmp/ibpc-r7-closure"),
        revision_provider=lambda: 7,
        authority=_Port() if authority is _UNSET else authority,
        authorizer=None if authorizer is _UNSET else authorizer,
        channel_binding_ref=binding,
        durable=durable,
        approval_key_provider=StaticKeyRing({"unit": b"r" * 32}, active_key_id="unit"),
    )
    return IBPCService(deps), queue


def _open(service: IBPCService) -> str:
    return service.open_session(
        tenant_id="ten",
        project_id=PROJECT,
        principal_id=PRINCIPAL,
        user_id=1,
        chat_id=1,
        current_revision=7,
    )


def _submit(service: IBPCService, clip: pathlib.Path, session_id: str) -> Any:
    return service.submit_text_intent(
        session_id,
        "turn-1",
        sequence_number=1,
        text="trim from 0.2 to 0.6 seconds",
        asset_id="asset-r7",
        source_path=str(clip),
    )


# --------------------------------------------------------------------------- #
# 1) an unconfigured role gate is a refusal, not a no-op
# --------------------------------------------------------------------------- #
class TestTheUnconfiguredRoleGate:
    def test_the_policy_is_an_explicit_disjoint_allowlist(self) -> None:
        assert _PLAN_ONLY_ACTIONS_WITHOUT_AUTHORIZER == frozenset(
            {StudioAction.SUBMIT_INTENT, StudioAction.READ_SESSION}
        )
        assert _AUTHORIZER_REQUIRED_ACTIONS == frozenset(
            {
                StudioAction.APPROVE_PLAN,
                StudioAction.EXECUTE_PLAN,
                StudioAction.MANAGE_MEMBERSHIP,
                StudioAction.MANAGE_CONSENT,
                StudioAction.READ_AUDIT,
                StudioAction.CLOSE_SESSION,
            }
        )
        assert not (_PLAN_ONLY_ACTIONS_WITHOUT_AUTHORIZER & _AUTHORIZER_REQUIRED_ACTIONS)
        # READ_JOB is in neither set: the allowlist still refuses it (default deny)
        assert StudioAction.READ_JOB not in _PLAN_ONLY_ACTIONS_WITHOUT_AUTHORIZER
        assert StudioAction.READ_JOB not in _AUTHORIZER_REQUIRED_ACTIONS

    @pytest.mark.parametrize(
        "action",
        [
            StudioAction.APPROVE_PLAN,
            StudioAction.EXECUTE_PLAN,
            StudioAction.MANAGE_MEMBERSHIP,
            StudioAction.MANAGE_CONSENT,
            StudioAction.READ_AUDIT,
            StudioAction.CLOSE_SESSION,
        ],
    )
    def test_each_role_gated_action_refuses_without_an_authorizer(
        self, action: StudioAction
    ) -> None:
        service, queue = _service()
        with pytest.raises(AuthorizationError) as exc:
            service._authorize_studio_action(
                principal_id=PRINCIPAL, project_id=PROJECT, action=action
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert action.value in str(exc.value)
        assert queue.enqueued == []

    @pytest.mark.parametrize("action", [StudioAction.SUBMIT_INTENT, StudioAction.READ_SESSION])
    def test_planning_actions_keep_working_without_one(self, action: StudioAction) -> None:
        service, _queue = _service()
        service._authorize_studio_action(principal_id=PRINCIPAL, project_id=PROJECT, action=action)

    def test_an_action_the_allowlist_does_not_name_is_refused(self) -> None:
        service, _queue = _service()
        with pytest.raises(AuthorizationError) as exc:
            service._authorize_studio_action(
                principal_id=PRINCIPAL, project_id=PROJECT, action=StudioAction.READ_JOB
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON

    def test_a_configured_authorizer_is_consulted(self) -> None:
        authorizer = _AllowAll()
        service, _queue = _service(authorizer=authorizer)
        service._authorize_studio_action(
            principal_id=PRINCIPAL, project_id=PROJECT, action=StudioAction.APPROVE_PLAN
        )
        assert [call["action"] for call in authorizer.calls] == [StudioAction.APPROVE_PLAN]
        assert authorizer.calls[0]["binding_ref"] == BINDING

    def test_an_authorizer_less_service_cannot_approve(self, media: Any) -> None:
        service, queue = _service()
        session_id = _open(service)
        proposal = _submit(service, media.clip, session_id)
        assert type(proposal).__name__ == "PipelineResult", proposal
        with pytest.raises(AuthorizationError) as exc:
            service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        # the proposal was never minted into an approval, nothing is queued
        assert service.approvals.get(proposal.approval_id).state is ApprovalState.PENDING
        assert queue.enqueued == []

    def test_an_authorizer_less_service_cannot_consume(self, media: Any) -> None:
        # a valid, approved context exists only because an authorized composition
        # created it; the authorizer-less composition must not be able to burn it
        service, queue = _service(authorizer=_AllowAll())
        session_id = _open(service)
        proposal = _submit(service, media.clip, session_id)
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        service._deps.authorizer = None  # downgrade the composition
        # ``execute`` raises on a role denial (main's existing contract), and it
        # raises *before* the consume, so the approval is never burned
        with pytest.raises(AuthorizationError) as exc:
            asyncio.run(
                service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert service.approvals.get(proposal.approval_id).state is ApprovalState.APPROVED
        assert queue.enqueued == []


class TestAuthorizerLessConsentAuditAndLifecycle:
    """The three surfaces the old denylist let through untouched."""

    @pytest.fixture()
    def durable(self, tmp_path: pathlib.Path) -> Generator[Any, None, None]:
        service, queue = _service(workspace=tmp_path, durable=True)
        try:
            yield service, queue, tmp_path
        finally:
            service.close()

    def test_grant_privacy_consent_fails_closed(self, durable: Any) -> None:
        service, _queue, _tmp = durable
        with pytest.raises(AuthorizationError) as exc:
            service.grant_privacy_consent(
                project_id=PROJECT,
                principal_id=PRINCIPAL,
                mode=PrivacyMode.LOCAL_VAULT,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category=DataCategory.VIDEO,
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        store = service._privacy_store
        assert store is not None
        assert store.list_consents(project_id=PROJECT) == ()
        assert store.list_audit(project_id=PROJECT) == ()

    def test_revoke_privacy_consent_fails_closed(self, durable: Any) -> None:
        service, _queue, _tmp = durable
        grant = service._privacy.grant_durable_consent(
            project_id=PROJECT,
            principal_id=PRINCIPAL,
            mode=PrivacyMode.LOCAL_VAULT,
            processing_scope=ProcessingScope.LOCAL_PROCESSING,
            data_category=DataCategory.VIDEO,
        )
        with pytest.raises(AuthorizationError) as exc:
            service.revoke_privacy_consent(grant.consent_id, project_id=PROJECT, actor_id=PRINCIPAL)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        store = service._privacy_store
        assert store is not None
        still = store.get_consent(grant.consent_id)
        assert still is not None and still.revoked_at_utc == ""

    def test_list_privacy_audit_discloses_nothing(self, durable: Any) -> None:
        service, _queue, _tmp = durable
        grant = service._privacy.grant_durable_consent(
            project_id=PROJECT,
            principal_id=PRINCIPAL,
            mode=PrivacyMode.LOCAL_VAULT,
            processing_scope=ProcessingScope.LOCAL_PROCESSING,
            data_category=DataCategory.VIDEO,
        )
        store = service._privacy_store
        assert store is not None
        assert store.list_audit(project_id=PROJECT)  # the row the gate must not leak
        with pytest.raises(AuthorizationError) as exc:
            service.list_privacy_audit(project_id=PROJECT, principal_id=PRINCIPAL)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert grant.consent_id  # seeded, still present

    @pytest.mark.parametrize("operation", ["close", "pause", "abort", "resume"])
    def test_session_lifecycle_fails_closed(self, operation: str) -> None:
        service, _queue = _service()
        session_id = _open(service)
        session = service.sessions.get_session(session_id)
        before = session.status
        method = getattr(service, f"{operation}_session")
        with pytest.raises(AuthorizationError) as exc:
            method(session_id, principal_id=PRINCIPAL)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert service.sessions.get_session(session_id).status == before


# --------------------------------------------------------------------------- #
# 2) the authorizer's decision is verified, never trusted
# --------------------------------------------------------------------------- #
class TestTheStudioDecisionContract:
    def test_a_denied_decision_without_an_exception_is_refused(self) -> None:
        service, _queue = _service(
            authorizer=_Scripted(lambda p, pr, a, b: _decision(allowed=False, binding_ref=b))
        )
        with pytest.raises(AuthorizationError) as exc:
            service._authorize_studio_action(
                principal_id=PRINCIPAL, project_id=PROJECT, action=StudioAction.APPROVE_PLAN
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "did not allow" in exc.value.detail

    def test_a_non_boolean_allow_flag_is_refused(self) -> None:
        forged = StudioAuthorizationDecision.model_construct(
            allowed="yes",
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            role="OWNER",
            action=StudioAction.APPROVE_PLAN.value,
            reason="authorized",
            binding_ref=BINDING,
        )
        service, _queue = _service(authorizer=_Scripted(lambda p, pr, a, b: forged))
        with pytest.raises(AuthorizationError) as exc:
            service._authorize_studio_action(
                principal_id=PRINCIPAL, project_id=PROJECT, action=StudioAction.APPROVE_PLAN
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON

    def test_anything_that_is_not_a_decision_is_refused(self) -> None:
        service, _queue = _service(
            authorizer=_Scripted(lambda p, pr, a, b: types.SimpleNamespace(allowed=True))
        )
        with pytest.raises(AuthorizationError) as exc:
            service._authorize_studio_action(
                principal_id=PRINCIPAL, project_id=PROJECT, action=StudioAction.APPROVE_PLAN
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "StudioAuthorizationDecision" in exc.value.detail

    @pytest.mark.parametrize(
        ("field", "value", "expected_detail"),
        [
            ("principal_id", "someone-else", "principal"),
            ("project_id", "another-project", "project"),
            ("action", StudioAction.EXECUTE_PLAN.value, "action"),
            ("binding_ref", OTHER_BINDING, "channel binding"),
        ],
    )
    def test_a_decision_minted_for_another_scope_is_refused(
        self, field: str, value: str, expected_detail: str
    ) -> None:
        service, _queue = _service(
            authorizer=_Scripted(
                lambda p, pr, a, b: _decision(**{field: value}),  # noqa: B023
            )
        )
        with pytest.raises(AuthorizationError) as exc:
            service._authorize_studio_action(
                principal_id=PRINCIPAL, project_id=PROJECT, action=StudioAction.APPROVE_PLAN
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert expected_detail in exc.value.detail

    def test_a_consistent_decision_is_accepted_and_whitespace_is_normalised(self) -> None:
        service, _queue = _service(
            authorizer=_Scripted(
                lambda p, pr, a, b: _decision(principal_id=f"  {p}  ", project_id=f"{pr} ")
            )
        )
        service._authorize_studio_action(
            principal_id=PRINCIPAL, project_id=PROJECT, action=StudioAction.APPROVE_PLAN
        )

    def test_a_denied_decision_blocks_a_real_consent_grant(self, tmp_path: pathlib.Path) -> None:
        # end-to-end: the contract is enforced before the privacy store is written
        service, _queue = _service(
            workspace=tmp_path,
            durable=True,
            authorizer=_Scripted(lambda p, pr, a, b: _decision(allowed=False, binding_ref=b)),
        )
        try:
            with pytest.raises(AuthorizationError):
                service.grant_privacy_consent(
                    project_id=PROJECT,
                    principal_id=PRINCIPAL,
                    mode=PrivacyMode.LOCAL_VAULT,
                    processing_scope=ProcessingScope.LOCAL_PROCESSING,
                    data_category=DataCategory.VIDEO,
                )
            store = service._privacy_store
            assert store is not None
            assert store.list_consents(project_id=PROJECT) == ()
        finally:
            service.close()


class TestAuthorizedSurfaceBranches:
    """The gate is not the only thing on these surfaces: the authorized paths
    must still behave (tenant resolution, refusal of an unknown consent, a
    store-less audit read, a stale base revision, a drain deadline)."""

    def test_open_session_refuses_a_stale_base_revision(self) -> None:
        from nexus_ai_agent.nagar.ibpc.session import StaleRevisionError

        service, _queue = _service()
        with pytest.raises(StaleRevisionError) as exc:
            service.open_session(
                tenant_id="ten",
                project_id=PROJECT,
                principal_id=PRINCIPAL,
                user_id=1,
                chat_id=1,
                current_revision=7,
                base_revision_id="6",
            )
        assert exc.value.reason == "revision_mismatch"

    def test_grant_consent_resolves_the_tenant_from_the_authorizer(
        self, tmp_path: pathlib.Path
    ) -> None:
        class _TenantAware(_AllowAll):
            def get_project_tenant(self, project_id: str) -> str:
                return "ten-resolved"

        service, _queue = _service(workspace=tmp_path, durable=True, authorizer=_TenantAware())
        try:
            grant = service.grant_privacy_consent(
                project_id=PROJECT,
                principal_id=PRINCIPAL,
                mode=PrivacyMode.LOCAL_VAULT,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category=DataCategory.VIDEO,
            )
            assert grant.tenant_id == "ten-resolved"
        finally:
            service.close()

    def test_grant_consent_without_tenant_resolution_stays_explicit(
        self, tmp_path: pathlib.Path
    ) -> None:
        # `_AllowAll` has no `get_project_tenant`: the tenant stays as asked for
        service, _queue = _service(workspace=tmp_path, durable=True, authorizer=_AllowAll())
        try:
            grant = service.grant_privacy_consent(
                project_id=PROJECT,
                principal_id=PRINCIPAL,
                mode=PrivacyMode.LOCAL_VAULT,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category=DataCategory.VIDEO,
            )
            assert grant.tenant_id == ""
        finally:
            service.close()

    def test_revoking_an_unknown_consent_is_refused(self, tmp_path: pathlib.Path) -> None:
        from nexus_ai_agent.nagar.ibpc.privacy import PrivacyViolation

        service, _queue = _service(workspace=tmp_path, durable=True, authorizer=_AllowAll())
        try:
            with pytest.raises(PrivacyViolation) as exc:
                service.revoke_privacy_consent(
                    "cns_missing", project_id=PROJECT, actor_id=PRINCIPAL
                )
            assert exc.value.reason == "consent_not_found"
        finally:
            service.close()

    def test_an_audit_read_without_a_store_discloses_nothing(self) -> None:
        service, _queue = _service(authorizer=_AllowAll())
        assert service._privacy_store is None
        assert service.list_privacy_audit(project_id=PROJECT, principal_id=PRINCIPAL) == ()

    def test_the_drain_deadline_returns_the_last_observed_status(self) -> None:
        service, _queue = _service(authorizer=_AllowAll())
        status = asyncio.run(service._drain("job_not_started", timeout=0.0))
        assert status is not None


# --------------------------------------------------------------------------- #
# 3) the real RBAC authority: role x action matrix
# --------------------------------------------------------------------------- #
def _real_store(
    tmp_path: pathlib.Path, role_map: dict[StudioRole, str]
) -> StudioPrincipalAuthStore:
    store = StudioPrincipalAuthStore(tmp_path / "studio_auth.sqlite3")
    store.ensure_project(PROJECT, tenant_id="ten-1", active_revision_id="0")
    for role, principal in role_map.items():
        store.register_principal(
            principal,
            tenant_id="ten-1",
            raw_token=f"tok-{principal}-secret",
            display_name=principal,
            allow_system_bootstrap=True,
        )
        store.bind_channel(
            principal,
            channel="studio",
            external_subject=f"sub-{principal}",
            allow_system_bootstrap=True,
        )
        store.grant_membership(
            PROJECT, principal, role=role, granted_by="system", allow_system_bootstrap=True
        )
    return store


@pytest.fixture(scope="module")
def rbac_store(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[StudioPrincipalAuthStore, None, None]:
    """One real durable RBAC store for the whole role x action matrix."""
    path = tmp_path_factory.mktemp("rbac")
    store = _real_store(path, TestRealProjectMembershipAuthorizerMatrix.ROLE_PRINCIPALS)
    try:
        yield store
    finally:
        store.close()


class TestRealProjectMembershipAuthorizerMatrix:
    """`ProjectMembershipAuthorizer` + `StudioPrincipalAuthStore`, every role."""

    ROLE_PRINCIPALS = {
        StudioRole.OWNER: "prn_" + "a" * 32,
        StudioRole.EDITOR: "prn_" + "b" * 32,
        StudioRole.VIEWER: "prn_" + "c" * 32,
        StudioRole.AUDITOR: "prn_" + "d" * 32,
    }

    @pytest.fixture()
    def store(self, rbac_store: StudioPrincipalAuthStore) -> StudioPrincipalAuthStore:
        return rbac_store

    @pytest.mark.parametrize("role", list(StudioRole))
    @pytest.mark.parametrize("action", list(StudioAction))
    def test_role_action_matrix(
        self, store: StudioPrincipalAuthStore, role: StudioRole, action: StudioAction
    ) -> None:
        principal = self.ROLE_PRINCIPALS[role]
        service, _queue = _service(authorizer=ProjectMembershipAuthorizer(store), binding=None)
        expected_allowed = action in ROLE_ACTIONS[role]
        if expected_allowed:
            service._authorize_studio_action(
                principal_id=principal, project_id=PROJECT, action=action
            )
        else:
            with pytest.raises(AuthorizationError) as exc:
                service._authorize_studio_action(
                    principal_id=principal, project_id=PROJECT, action=action
                )
            # the deny comes from the RBAC store's own typed refusal
            assert exc.value.reason in {
                "insufficient_role",
                "not_a_project_member",
                "principal_not_found",
            }

    def test_a_viewer_cannot_approve_or_grant_consent(
        self, store: StudioPrincipalAuthStore
    ) -> None:
        service, _queue = _service(authorizer=ProjectMembershipAuthorizer(store), binding=None)
        for action in (
            StudioAction.APPROVE_PLAN,
            StudioAction.EXECUTE_PLAN,
            StudioAction.MANAGE_CONSENT,
            StudioAction.READ_AUDIT,
            StudioAction.CLOSE_SESSION,
        ):
            with pytest.raises(AuthorizationError):
                service._authorize_studio_action(
                    principal_id=self.ROLE_PRINCIPALS[StudioRole.VIEWER],
                    project_id=PROJECT,
                    action=action,
                )

    def test_an_auditor_reads_the_audit_but_cannot_approve(
        self, store: StudioPrincipalAuthStore
    ) -> None:
        service, _queue = _service(authorizer=ProjectMembershipAuthorizer(store), binding=None)
        auditor = self.ROLE_PRINCIPALS[StudioRole.AUDITOR]
        service._authorize_studio_action(
            principal_id=auditor, project_id=PROJECT, action=StudioAction.READ_AUDIT
        )
        with pytest.raises(AuthorizationError):
            service._authorize_studio_action(
                principal_id=auditor, project_id=PROJECT, action=StudioAction.APPROVE_PLAN
            )


# --------------------------------------------------------------------------- #
# 4) the canonical authority's answer is validated, never trusted
# --------------------------------------------------------------------------- #
class TestTheCanonicalFactsAreValidated:
    @pytest.mark.parametrize("binding", [None, "", "   ", 12345, object()])
    def test_a_missing_or_malformed_binding_is_refused(self, binding: Any) -> None:
        service, _queue = _service(authority=_Port(_facts(channel_binding_id=binding)))
        with pytest.raises(CanonicalAuthorityError) as exc:
            service._resolve_authority(PRINCIPAL, PROJECT)
        assert exc.value.reason == "binding_inconsistent"

    def test_a_binding_that_is_not_the_configured_one_is_refused(self) -> None:
        service, _queue = _service(authority=_Port(_facts(channel_binding_id=OTHER_BINDING)))
        with pytest.raises(CanonicalAuthorityError) as exc:
            service._resolve_authority(PRINCIPAL, PROJECT)
        assert exc.value.reason == "binding_inconsistent"

    @pytest.mark.parametrize("field", ["principal_id", "project_id"])
    @pytest.mark.parametrize("value", [None, "", "   ", 42, "someone-else", "another-project"])
    def test_a_mismatched_or_malformed_identity_is_refused(self, field: str, value: Any) -> None:
        service, _queue = _service(authority=_Port(_facts(**{field: value})))
        with pytest.raises(CanonicalAuthorityError) as exc:
            service._resolve_authority(PRINCIPAL, PROJECT)
        assert exc.value.reason == "identity_mismatch"

    def test_a_consistent_answer_is_accepted(self) -> None:
        service, _queue = _service()
        facts = service._resolve_authority(PRINCIPAL, PROJECT)
        assert facts.principal_id == PRINCIPAL
        assert facts.project_id == PROJECT
        assert facts.channel_binding_id == BINDING

    def test_the_public_boundary_refuses_and_queues_nothing(self, media: Any) -> None:
        # the port answers without a binding: the turn is refused before the fence
        service, queue = _service(authority=_Port(_facts(channel_binding_id=None)))
        session_id = _open(service)
        result = _submit(service, media.clip, session_id)
        assert isinstance(result, Refusal), result
        assert result.reason == AUTHORIZATION_REFUSAL_REASON
        assert "binding_inconsistent" in result.detail
        assert queue.enqueued == []
        assert service._contexts == {}

    def test_the_public_boundary_refuses_a_drifting_principal(self, media: Any) -> None:
        service, queue = _service(authority=_Port(_facts(principal_id="someone-else")))
        session_id = _open(service)
        result = _submit(service, media.clip, session_id)
        assert isinstance(result, Refusal), result
        assert result.reason == AUTHORIZATION_REFUSAL_REASON
        assert "identity_mismatch" in result.detail
        assert queue.enqueued == []


# --------------------------------------------------------------------------- #
# 5) one canonical reason literal, no concatenated spellings
# --------------------------------------------------------------------------- #
def test_the_reason_literal_exists_exactly_once_in_the_module() -> None:
    tree = ast.parse(pathlib.Path(service_mod.__file__).read_text(encoding="utf-8"))
    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "unauthorized_principal" in node.value
    ]
    assert len(literals) == 1, [node.value for node in literals]
    (only,) = literals
    assert only.value == AUTHORIZATION_REFUSAL_REASON
    annotated = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "AUTHORIZATION_REFUSAL_REASON"
    ]
    assert len(annotated) == 1
    assert annotated[0].value is only
    assert ast.unparse(annotated[0].annotation) == "Final[str]"

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name not in {"Refusal", "ApprovalError"}:
            continue
        first = node.args[0]
        if isinstance(first, ast.JoinedStr):
            if any(
                isinstance(inner, ast.Name) and inner.id == "AUTHORIZATION_REFUSAL_REASON"
                for inner in ast.walk(first)
            ):
                offenders.append(f"line {node.lineno}: interpolated reason")
            continue
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value.startswith("unauthorized_principal:"):
                offenders.append(f"line {node.lineno}: {first.value!r}")
    assert offenders == []
