"""Wave 3-R7 — the two authorization gaps in the post-#365 IBPC service.

1. **An unconfigured role gate used to be a no-op.** ``_authorize_studio_action``
   returns early whenever ``deps.authorizer is None`` — the *default* — so a
   service composed without a Studio authorizer still minted (``approve``) and
   burned (``execute``) approval tokens with no role proof at all.  Identity
   verification (``_resolve_authority``) fails closed when its port is missing;
   the role gate now does the same for the actions that approve, execute or
   administer.
2. **Canonical facts were trusted unchecked.** ``_resolve_authority`` returned
   whatever ``resolve_principal`` produced; callers copied
   ``facts.channel_binding_id`` straight into the queue payload and only compared
   it against the context at consume time.  A ``None``/``""``/whitespace binding,
   or a different ``principal_id``/``project_id``, was accepted.

Both refusals keep the canonical public reason (``AUTHORIZATION_REFUSAL_REASON``)
and leave the world untouched: no approval minted or consumed, no queued job.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import types

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import (
    AuthorizationError,
    CanonicalAuthorityError,
    StudioAction,
    StudioAuthorizationDecision,
)
from nexus_ai_agent.nagar.ibpc.intent import Refusal
from nexus_ai_agent.nagar.ibpc.models import ApprovalState
from nexus_ai_agent.nagar.ibpc.service import (
    _EXECUTION_AND_APPROVAL_ACTIONS,
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
        self.enqueued: list[dict] = []

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueued.append(payload)
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str):
        from nexus_ai_agent.application.ports.job_queue import JobStatus

        return JobStatus.FAILED_TERMINAL

    async def get_result(self, job_id: str):
        return {"success": False, "error": "not executed in this test"}


class _AllowAll:
    """``StudioAuthorizerPort`` double: allows every action."""

    def authorize_action(self, *, principal_id, project_id, action, binding_ref=""):
        return StudioAuthorizationDecision(
            allowed=True,
            principal_id=principal_id,
            project_id=project_id,
            role="OWNER",
            action=getattr(action, "value", str(action)),
            reason="test_double",
            binding_ref=binding_ref,
        )


def _facts(**overrides):
    base = {"principal_id": PRINCIPAL, "project_id": PROJECT, "channel_binding_id": BINDING}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _Port:
    """``CanonicalAuthorityPort`` double with a per-call script."""

    def __init__(self, *answers) -> None:
        self._answers = list(answers) or [_facts()]
        self.calls: list[tuple[str, str, str]] = []

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        self.calls.append((principal_id, project_id, binding_ref))
        answer = self._answers[min(len(self.calls) - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture()
def media(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        service_mod,
        "probe_media",
        lambda path, *, binary=None: (types.SimpleNamespace(duration_us=2_000_000), 30.0),
    )

    def fake_shadow(path, scope, plan_digest, **kwargs):
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

    def fake_signature(path, scope, **kwargs):
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


def _service(*, authority=_UNSET, authorizer=_UNSET, binding: str | None = BINDING):
    queue = _Queue()
    deps = IBPCServiceDeps(
        queue=queue,  # type: ignore[arg-type]
        registry=build_runtime_registry(),
        workspace_root=pathlib.Path("/tmp/ibpc-r7-closure"),
        revision_provider=lambda: 7,
        authority=_Port() if authority is _UNSET else authority,
        authorizer=None if authorizer is _UNSET else authorizer,
        channel_binding_ref=binding,
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


def _submit(service: IBPCService, clip: pathlib.Path, session_id: str):
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
    def test_the_execution_actions_are_exactly_the_privileged_ones(self) -> None:
        assert _EXECUTION_AND_APPROVAL_ACTIONS == {
            StudioAction.APPROVE_PLAN,
            StudioAction.EXECUTE_PLAN,
            StudioAction.MANAGE_MEMBERSHIP,
        }

    @pytest.mark.parametrize(
        "action",
        [StudioAction.APPROVE_PLAN, StudioAction.EXECUTE_PLAN, StudioAction.MANAGE_MEMBERSHIP],
    )
    def test_each_privileged_action_refuses_without_an_authorizer(self, action) -> None:
        service, queue = _service()
        with pytest.raises(AuthorizationError) as exc:
            service._authorize_studio_action(
                principal_id=PRINCIPAL, project_id=PROJECT, action=action
            )
        assert exc.value.reason == "role_verification_required"
        assert action.value in str(exc.value)
        assert queue.enqueued == []

    @pytest.mark.parametrize(
        "action",
        [StudioAction.READ_SESSION, StudioAction.SUBMIT_INTENT, StudioAction.CLOSE_SESSION],
    )
    def test_planning_actions_keep_working_without_one(self, action) -> None:
        service, _queue = _service()
        assert (
            service._authorize_studio_action(
                principal_id=PRINCIPAL, project_id=PROJECT, action=action
            )
            is None
        )

    def test_a_configured_authorizer_is_consulted(self) -> None:
        seen: list[StudioAction] = []

        class _Recording(_AllowAll):
            def authorize_action(self, *, principal_id, project_id, action, binding_ref=""):
                seen.append(action)
                return super().authorize_action(
                    principal_id=principal_id,
                    project_id=project_id,
                    action=action,
                    binding_ref=binding_ref,
                )

        service, _queue = _service(authorizer=_Recording())
        service._authorize_studio_action(
            principal_id=PRINCIPAL, project_id=PROJECT, action=StudioAction.APPROVE_PLAN
        )
        assert seen == [StudioAction.APPROVE_PLAN]

    def test_an_authorizer_less_service_cannot_approve(self, media) -> None:
        service, queue = _service()
        session_id = _open(service)
        proposal = _submit(service, media.clip, session_id)
        assert type(proposal).__name__ == "PipelineResult", proposal
        with pytest.raises(AuthorizationError) as exc:
            service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        assert exc.value.reason == "role_verification_required"
        # the proposal was never minted into an approval, nothing is queued
        assert service.approvals.get(proposal.approval_id).state is ApprovalState.PENDING
        assert queue.enqueued == []

    def test_an_authorizer_less_service_cannot_consume(self, media) -> None:
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
        assert exc.value.reason == "role_verification_required"
        assert service.approvals.get(proposal.approval_id).state is ApprovalState.APPROVED
        assert queue.enqueued == []


# --------------------------------------------------------------------------- #
# 2) the canonical authority's answer is validated, never trusted
# --------------------------------------------------------------------------- #
class TestTheCanonicalFactsAreValidated:
    @pytest.mark.parametrize("binding", [None, "", "   ", 12345, object()])
    def test_a_missing_or_malformed_binding_is_refused(self, binding) -> None:
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
    def test_a_mismatched_or_malformed_identity_is_refused(self, field, value) -> None:
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

    def test_the_public_boundary_refuses_and_queues_nothing(self, media) -> None:
        # the port answers without a binding: the turn is refused before the fence
        service, queue = _service(authority=_Port(_facts(channel_binding_id=None)))
        session_id = _open(service)
        result = _submit(service, media.clip, session_id)
        assert isinstance(result, Refusal), result
        assert result.reason == AUTHORIZATION_REFUSAL_REASON
        assert "binding_inconsistent" in result.detail
        assert queue.enqueued == []
        assert service._contexts == {}

    def test_the_public_boundary_refuses_a_drifting_principal(self, media) -> None:
        service, queue = _service(authority=_Port(_facts(principal_id="someone-else")))
        session_id = _open(service)
        result = _submit(service, media.clip, session_id)
        assert isinstance(result, Refusal), result
        assert result.reason == AUTHORIZATION_REFUSAL_REASON
        assert "identity_mismatch" in result.detail
        assert queue.enqueued == []


# --------------------------------------------------------------------------- #
# 3) one canonical reason literal, no concatenated spellings
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
