"""Wave 3-R7 — cross-authority consistency and the identity-only execution ban.

Two defects survived R6:

1. **3-way consistency (Wave 1).** When both a durable role authority and the
   canonical identity port answered, R6 compared only
   ``resolved.channel_binding_id != facts.channel_binding_id`` — and even that
   only when the canonical binding was not ``None``.  A canonical port returning
   a different ``principal_id`` / ``project_id``, or a ``None`` / ``""`` /
   whitespace binding, was silently accepted, so a cross-principal or
   cross-binding approval could be minted.
2. **Identity-only execution (Wave 2).** An identity-only composition
   (``principal_authority is None``, ``require_role_authority=False``) could
   still ``approve()`` and ``execute()``.  ``CanonicalAuthorityPort`` proves
   identity and membership but carries no role — it cannot tell a ``VIEWER``
   from an ``EDITOR`` — so approving, consuming, restoring or enqueueing in that
   mode was a fail-open execution boundary.

Every refusal here must keep the public contract (``reason`` is exactly
``AUTHORIZATION_REFUSAL_REASON``, the diagnostic lives in ``detail``) and must
leave the world untouched: no turn fence, no session context, no approval
record, no queued job.
"""

from __future__ import annotations

import pathlib
import types

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import CanonicalAuthorityError
from nexus_ai_agent.nagar.ibpc.intent import Refusal
from nexus_ai_agent.nagar.ibpc.models import (
    ApprovalState,
    CapabilityIdentity,
    CompiledPlan,
    PlannedStep,
)
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    ACTION_ROLES,
    MembershipRole,
)
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    PrincipalFacts as RoleFacts,
)
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    StudioAction as Action,
)
from nexus_ai_agent.nagar.ibpc.service import (
    _EXECUTION_AND_APPROVAL_ACTIONS,
    AUTHORIZATION_REFUSAL_REASON,
    IBPCService,
    IBPCServiceDeps,
)

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
PRINCIPAL = "prn_" + "7" * 32
OTHER_PRINCIPAL = "prn_" + "8" * 32
PROJECT = "project-r7"
OTHER_PROJECT = "project-r7-other"
BINDING = "cbn_" + "b" * 64
OTHER_BINDING = "cbn_" + "c" * 64
_UNSET = object()


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #
class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueued.append(payload)
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str):
        from nexus_ai_agent.application.ports.job_queue import JobStatus

        return JobStatus.FAILED_TERMINAL

    async def get_result(self, job_id: str):
        return {"success": False, "error": "not executed in this test"}


def _identity(**overrides):
    """One canonical-port answer (a ``types.SimpleNamespace``, not a model)."""
    base = {
        "principal_id": PRINCIPAL,
        "project_id": PROJECT,
        "channel_binding_id": BINDING,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _CanonicalPort:
    """Canonical identity port double with a per-call script.

    The first answer serves the first resolution (session start), the second
    serves the next one (turn creation), and the last answer repeats forever —
    that is how "the authority changes its story between two gates" is modelled.
    """

    def __init__(self, *answers) -> None:
        self._answers = list(answers) or [_identity()]
        self.calls: list[tuple[str, str, str]] = []

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        self.calls.append((principal_id, project_id, binding_ref))
        answer = self._answers[min(len(self.calls) - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _RolePort:
    """Durable role-authority double."""

    def __init__(self, role: MembershipRole = MembershipRole.OWNER, facts=None) -> None:
        self.role = role
        self._facts = facts
        self.calls: list[tuple[str, str, str, Action]] = []

    def authorize_action(self, *, principal_id: str, project_id: str, binding_ref: str, action):
        self.calls.append((principal_id, project_id, binding_ref, action))
        if self._facts is not None:
            return self._facts
        return RoleFacts(
            principal_id=principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
            role=self.role,
        )


def _stub_compile(ast, *, registry):
    """Pack-free stand-in for the registry-validated compiler (see R2a)."""
    return CompiledPlan(
        ast_digest=ast.ast_digest,
        capability=CapabilityIdentity(
            operation_id=ast.operation,
            schema_version=1,
            permission_level="read",
            execution_modes=("local",),
            required_packs=(),
            required_permissions=(),
            schema_digest="sha256:" + "e" * 64,
        ),
        steps=(
            PlannedStep(
                operation=ast.operation,
                input=dict(ast.params),
                target={"project_id": ast.project_id, "asset_id": ast.asset_id},
            ),
        ),
    )


@pytest.fixture()
def media(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        service_mod,
        "probe_media",
        lambda path, *, binary=None: (types.SimpleNamespace(duration_us=3_000_000), 30.0),
    )

    def fake_shadow(path, scope, plan_digest, **kwargs):
        payload = {
            "plan_digest": plan_digest,
            "scope_digest": scope.scope_digest,
            "source_duration_us": 3_000_000,
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

    def fake_signature(path, scope, **kwargs):
        payload = {
            "scope_digest": scope.scope_digest,
            "asset_id": scope.asset_id,
            "start_us": scope.start_us,
            "end_us": scope.end_us,
            "color_histogram": [0.0] * 64,
            "motion_magnitude": 0.0,
            "sample_frames": 1,
            "computed_at": NOW(),
            "signature_digest": "sha256:" + "1" * 64,
        }
        return types.SimpleNamespace(
            signature_digest=payload["signature_digest"],
            scope_digest=scope.scope_digest,
            model_dump=lambda **_kw: payload,
        )

    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)
    monkeypatch.setattr(service_mod, "compile_ast", _stub_compile)
    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    return types.SimpleNamespace(clip=clip, root=tmp_path)


def _service(*, authority=_UNSET, role=_UNSET, binding: str | None = BINDING, **overrides):
    queue = _Queue()
    deps = dict(
        queue=queue,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]  # stubbed compiler, no pack composition
        workspace_root=pathlib.Path("/tmp/ibpc-r7"),
        revision_provider=lambda: 7,
        now=NOW,
        channel_binding_ref=binding,
        authority=_CanonicalPort() if authority is _UNSET else authority,
        principal_authority=_RolePort() if role is _UNSET else role,
        approval_key_provider=StaticKeyRing({"unit": b"r" * 32}, active_key_id="unit"),
    )
    deps.update(overrides)
    return IBPCService(IBPCServiceDeps(**deps)), queue  # type: ignore[arg-type]


def _open(service: IBPCService) -> str:
    return service.open_session(
        tenant_id="ten",
        project_id=PROJECT,
        principal_id=PRINCIPAL,
        user_id=1,
        chat_id=1,
        current_revision=7,
    )


def _submit(service: IBPCService, clip: pathlib.Path, session_id: str):
    return service.submit_text_intent(
        session_id,
        "turn-1",
        sequence_number=1,
        text="trim from 0.0 to 2.0 seconds",
        asset_id="asset-r7",
        source_path=str(clip),
    )


def _assert_untouched(service: IBPCService, queue: _Queue) -> None:
    """No session, no fence, no context, no approval record, no queued job."""
    assert queue.enqueued == []
    assert service._contexts == {}
    assert service._sessions._sessions == {}
    assert service.approvals._all() == ()


# --------------------------------------------------------------------------- #
# Wave 1 — 3-way identity / project / binding consistency
# --------------------------------------------------------------------------- #
class TestThreeWayConsistency:
    def _refusal(self, *, authority, role) -> ApprovalError:
        service, _queue = _service(authority=authority, role=role)
        with pytest.raises(ApprovalError) as exc:
            service._authorize_principal(
                principal_id=PRINCIPAL, project_id=PROJECT, action=Action.TURN_CREATE
            )
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        return exc.value

    def test_a_different_principal_from_the_canonical_port_is_refused(self) -> None:
        error = self._refusal(
            authority=_CanonicalPort(_identity(principal_id=OTHER_PRINCIPAL)), role=_RolePort()
        )
        assert "identity_mismatch" in error.detail

    def test_a_different_project_from_the_canonical_port_is_refused(self) -> None:
        error = self._refusal(
            authority=_CanonicalPort(_identity(project_id=OTHER_PROJECT)), role=_RolePort()
        )
        assert "identity_mismatch" in error.detail

    @pytest.mark.parametrize("binding", [None, "", "   ", 12345, object()])
    def test_a_missing_or_empty_canonical_binding_is_never_skipped(self, binding) -> None:
        """The R6 defect: ``None`` meant "the authority has no opinion"."""
        error = self._refusal(
            authority=_CanonicalPort(_identity(channel_binding_id=binding)), role=_RolePort()
        )
        assert "binding_inconsistent" in error.detail

    @pytest.mark.parametrize("field", ["principal_id", "project_id"])
    def test_a_malformed_canonical_identity_field_is_refused(self, field) -> None:
        for malformed in (None, "", "   ", 42):
            error = self._refusal(
                authority=_CanonicalPort(_identity(**{field: malformed})), role=_RolePort()
            )
            assert "identity_mismatch" in error.detail

    def test_a_canonical_binding_that_is_not_the_configured_binding_is_refused(self) -> None:
        error = self._refusal(
            authority=_CanonicalPort(_identity(channel_binding_id=OTHER_BINDING)),
            role=_RolePort(),
        )
        assert "binding_inconsistent" in error.detail

    def test_the_two_authorities_must_agree_on_the_binding(self) -> None:
        error = self._refusal(
            authority=_CanonicalPort(_identity()),
            role=_RolePort(facts=_identity(channel_binding_id=OTHER_BINDING)),
        )
        assert "binding_inconsistent" in error.detail

    @pytest.mark.parametrize("binding", [None, "", "   "])
    def test_the_role_authority_binding_must_be_present_too(self, binding) -> None:
        error = self._refusal(
            authority=_CanonicalPort(_identity()),
            role=_RolePort(facts=_identity(channel_binding_id=binding)),
        )
        assert "binding_inconsistent" in error.detail

    def test_the_two_authorities_must_agree_on_the_principal(self) -> None:
        error = self._refusal(
            authority=_CanonicalPort(_identity()),
            role=_RolePort(facts=_identity(principal_id=OTHER_PRINCIPAL)),
        )
        assert "identity_mismatch" in error.detail

    def test_the_two_authorities_must_agree_on_the_project(self) -> None:
        error = self._refusal(
            authority=_CanonicalPort(_identity()),
            role=_RolePort(facts=_identity(project_id=OTHER_PROJECT)),
        )
        assert "identity_mismatch" in error.detail

    def test_a_valid_role_response_with_a_malformed_canonical_response_is_refused(self) -> None:
        error = self._refusal(
            authority=_CanonicalPort(
                types.SimpleNamespace(principal_id=PRINCIPAL, channel_binding_id=None)
            ),
            role=_RolePort(),
        )
        assert "identity_mismatch" in error.detail

    # -- and the same defects at the public boundary ----------------------- #
    def test_a_drifting_canonical_port_never_opens_a_session(self) -> None:
        service, queue = _service(
            authority=_CanonicalPort(_identity(principal_id=OTHER_PRINCIPAL)), role=_RolePort()
        )
        with pytest.raises(ApprovalError) as exc:
            _open(service)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "identity_mismatch" in exc.value.detail
        _assert_untouched(service, queue)

    def test_a_missing_binding_never_opens_a_turn_fence(self, media) -> None:
        # session start succeeds; the canonical port then loses its binding
        service, queue = _service(
            authority=_CanonicalPort(_identity(), _identity(channel_binding_id=None)),
            role=_RolePort(),
        )
        session_id = _open(service)
        result = _submit(service, media.clip, session_id)
        assert isinstance(result, Refusal)
        assert result.reason == AUTHORIZATION_REFUSAL_REASON
        assert "binding_inconsistent" in result.detail
        assert queue.enqueued == []
        assert service._contexts == {}
        # refused before the fence: the turn never entered the map
        assert service._sessions.get_session(session_id).turns == {}

    def test_a_role_authority_that_loses_the_binding_never_opens_a_turn_fence(self, media) -> None:
        role = _RolePort()
        service, queue = _service(authority=_CanonicalPort(_identity()), role=role)
        session_id = _open(service)
        role._facts = _identity(channel_binding_id=None)
        result = _submit(service, media.clip, session_id)
        assert isinstance(result, Refusal)
        assert result.reason == AUTHORIZATION_REFUSAL_REASON
        assert "binding_inconsistent" in result.detail
        assert queue.enqueued == []
        assert service._contexts == {}
        assert service._sessions.get_session(session_id).turns == {}

    def test_a_canonical_refusal_is_still_typed_and_canonical(self) -> None:
        service, queue = _service(
            authority=_CanonicalPort(CanonicalAuthorityError("not_a_member", "no row")),
            role=_RolePort(),
        )
        with pytest.raises(ApprovalError) as exc:
            _open(service)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "not_a_member" in exc.value.detail
        _assert_untouched(service, queue)


# --------------------------------------------------------------------------- #
# Wave 2 — identity-only compositions can plan, never approve / consume / execute
# --------------------------------------------------------------------------- #
class TestTheIdentityOnlyExecutionBan:
    def test_the_ban_covers_approval_consumption_and_restoration(self) -> None:
        assert _EXECUTION_AND_APPROVAL_ACTIONS == {
            Action.PREVIEW_GENERATE,
            Action.APPROVAL_CONSUME,
            Action.REVISION_RESTORE,
        }
        for action in _EXECUTION_AND_APPROVAL_ACTIONS:
            assert MembershipRole.OWNER in ACTION_ROLES[action]

    @pytest.mark.parametrize(
        "action",
        [Action.PREVIEW_GENERATE, Action.APPROVAL_CONSUME, Action.REVISION_RESTORE],
    )
    def test_identity_only_refuses_every_execution_action(self, action) -> None:
        service, _queue = _service(role=None)
        with pytest.raises(ApprovalError) as exc:
            service._authorize_principal(principal_id=PRINCIPAL, project_id=PROJECT, action=action)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "role_verification_required" in exc.value.detail

    @pytest.mark.parametrize("action", [Action.SESSION_START, Action.TURN_CREATE])
    def test_identity_only_still_plans(self, action) -> None:
        service, _queue = _service(role=None)
        assert (
            service._authorize_principal(principal_id=PRINCIPAL, project_id=PROJECT, action=action)
            is None
        )

    def test_identity_only_cannot_approve(self, media) -> None:
        service, queue = _service(role=None)
        session_id = _open(service)
        proposal = _submit(service, media.clip, session_id)
        assert type(proposal).__name__ == "PipelineResult", proposal
        with pytest.raises(ApprovalError) as exc:
            service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "role_verification_required" in exc.value.detail
        # the proposal stays PENDING (never approved), nothing is queued
        binding = service.approvals.get(proposal.approval_id)
        assert binding.state is ApprovalState.PENDING
        assert binding.decided_at == ""
        assert queue.enqueued == []

    def test_identity_only_cannot_consume_or_enqueue(self, media) -> None:
        import asyncio

        # an approved context exists only because a *role* composition made it;
        # the identity-only composition must still refuse to consume it
        service, queue = _service()
        session_id = _open(service)
        proposal = _submit(service, media.clip, session_id)
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        service._deps.principal_authority = None  # downgrade to identity-only
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "role_verification_required" in outcome.detail
        # the approval was NOT burned and no job was admitted
        assert service.approvals.get(proposal.approval_id).state is ApprovalState.APPROVED
        assert queue.enqueued == []

    def test_identity_only_cannot_restore_a_revision(self) -> None:
        service, queue = _service(role=None)
        with pytest.raises(ApprovalError) as exc:
            service._authorize_principal(
                principal_id=PRINCIPAL, project_id=PROJECT, action=Action.REVISION_RESTORE
            )
        assert "role_verification_required" in exc.value.detail
        assert queue.enqueued == []


@pytest.mark.parametrize("role", [MembershipRole.VIEWER, MembershipRole.AUDITOR])
class TestReadOnlyRolesCannotMutate:
    @pytest.mark.parametrize(
        "action",
        [
            Action.SESSION_START,
            Action.TURN_CREATE,
            Action.PREVIEW_GENERATE,
            Action.APPROVAL_CONSUME,
            Action.REVISION_RESTORE,
        ],
    )
    def test_every_mutating_action_is_refused(self, role, action) -> None:
        service, _queue = _service(role=_RolePort(role=role))
        with pytest.raises(ApprovalError) as exc:
            service._authorize_principal(principal_id=PRINCIPAL, project_id=PROJECT, action=action)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "insufficient_role" in exc.value.detail

    def test_no_session_is_opened(self, role) -> None:
        service, queue = _service(role=_RolePort(role=role))
        with pytest.raises(ApprovalError) as exc:
            _open(service)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "insufficient_role" in exc.value.detail
        _assert_untouched(service, queue)

    def test_no_approval_is_minted_or_consumed(self, role, media) -> None:
        import asyncio

        service, queue = _service()  # OWNER creates the proposal …
        session_id = _open(service)
        proposal = _submit(service, media.clip, session_id)
        service._deps.principal_authority = _RolePort(role=role)  # … then downgrade
        with pytest.raises(ApprovalError) as exc:
            service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        assert exc.value.reason == AUTHORIZATION_REFUSAL_REASON
        assert "insufficient_role" in exc.value.detail
        assert service.approvals.get(proposal.approval_id).state is ApprovalState.PENDING
        assert service._contexts[proposal.approval_id].scope_signature is None

        service._deps.principal_authority = _RolePort()  # approve as OWNER
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        service._deps.principal_authority = _RolePort(role=role)  # consume as read-only
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "insufficient_role" in outcome.detail
        assert service.approvals.get(proposal.approval_id).state is ApprovalState.APPROVED
        assert queue.enqueued == []
