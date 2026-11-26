"""Wave 3-R6 — one canonical refusal contract for authorization failures.

``reason`` is a caller contract, not a log line.  The R2b rewrite briefly spelled
authorization refusals as ``reason="unauthorized_principal:<diagnostic>"``, which
silently broke every caller that switches on the exact reason and pushed the test
suite into prefix-matching.  The rule this module pins:

* ``ApprovalError.reason == "unauthorized_principal"`` — always, exactly;
* the diagnostic (``insufficient_role``, ``role_verification_required``,
  ``binding_inconsistent``, ``authority_unconfigured``, …) travels in ``detail``,
  and therefore in ``str(exc)`` so human-facing logs keep the specificity;
* every public method that *contractually* returns ``Refusal`` keeps returning
  ``Refusal(reason="unauthorized_principal", detail=...)`` — never a leaked
  ``ApprovalError``.
"""

from __future__ import annotations

import ast
import pathlib
import types

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError
from nexus_ai_agent.nagar.ibpc.authority import CanonicalAuthorityError, PrincipalFacts
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    AuthorizationRefusalError,
    MembershipRole,
)
from nexus_ai_agent.nagar.ibpc.principal_authority import StudioAction as Action
from nexus_ai_agent.nagar.ibpc.service import (
    AUTHORIZATION_REFUSAL_REASON,
    IBPCService,
    IBPCServiceDeps,
    _authorization_error,
    _authorization_refusal,
)

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
PRINCIPAL = "prn_" + "4" * 32
PROJECT = "project-r6"
BINDING_REF = "cbn_" + "f" * 64


class _IdentityPort:
    """Canonical authority double: identity only, optional verified role."""

    def __init__(self, *, role=None, error: Exception | None = None) -> None:
        self.role = role
        self.error = error

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
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


class _RolePort:
    """Durable principal-authority double (the role source)."""

    def __init__(self, *, principal_id: str | None = None, binding: str = BINDING_REF, error=None):
        self.principal_id = principal_id
        self.binding = binding
        self.error = error

    def authorize_action(self, *, principal_id: str, project_id: str, binding_ref: str, action):
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(
            principal_id=self.principal_id or principal_id,
            project_id=project_id,
            channel_binding_id=self.binding,
            role=MembershipRole.OWNER,
        )


def _service(**overrides) -> IBPCService:
    deps = dict(
        queue=types.SimpleNamespace(enqueued=[]),  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]  # no pack composition in a refusal test
        workspace_root=pathlib.Path("/tmp/ibpc-r6"),
        revision_provider=lambda: 7,
        now=NOW,
        channel_binding_ref=BINDING_REF,
        authority=_IdentityPort(),
    )
    deps.update(overrides)
    return IBPCService(IBPCServiceDeps(**deps))  # type: ignore[arg-type]


def _authorize(service: IBPCService, action=Action.TURN_CREATE) -> ApprovalError:
    with pytest.raises(ApprovalError) as exc:
        service._authorize_principal(principal_id=PRINCIPAL, project_id=PROJECT, action=action)
    return exc.value


#: (case, deps overrides, action, expected diagnostic in ``detail``)
REFUSAL_CASES = (
    (
        "authority_unconfigured",
        {"authority": None},
        Action.TURN_CREATE,
        "authority_unconfigured",
    ),
    (
        "not_a_member",
        {"authority": _IdentityPort(error=CanonicalAuthorityError("not_a_member", "no row"))},
        Action.TURN_CREATE,
        "not_a_member",
    ),
    (
        "insufficient_role",
        {"authority": _IdentityPort(role=MembershipRole.VIEWER)},
        Action.TURN_CREATE,
        "insufficient_role",
    ),
    (
        "role_verification_required",
        {"authority": _IdentityPort()},
        Action.REVISION_RESTORE,
        "role_verification_required",
    ),
    (
        "binding_inconsistent",
        {"authority": _IdentityPort(), "principal_authority": _RolePort(binding="cbn_" + "0" * 64)},
        Action.TURN_CREATE,
        "binding_inconsistent",
    ),
    (
        "identity_mismatch",
        {"authority": _IdentityPort(), "principal_authority": _RolePort(principal_id="prn_x")},
        Action.TURN_CREATE,
        "identity_mismatch",
    ),
    (
        "authority_refused",
        {
            "authority": _IdentityPort(),
            "principal_authority": _RolePort(error=RuntimeError("nope")),
        },
        Action.TURN_CREATE,
        "authority_refused",
    ),
)


class TestTheApprovalErrorSpelling:
    @pytest.mark.parametrize(
        "deps,action,diagnostic",
        [case[1:] for case in REFUSAL_CASES],
        ids=[c[0] for c in REFUSAL_CASES],
    )
    def test_the_reason_is_always_the_canonical_constant(self, deps, action, diagnostic) -> None:
        error = _authorize(_service(**deps), action)
        assert error.reason == AUTHORIZATION_REFUSAL_REASON
        assert error.reason == "unauthorized_principal"
        # the specificity is not lost — it lives in ``detail`` (and in ``str``)
        assert diagnostic in error.detail
        assert f"unauthorized_principal: {diagnostic}" in str(error)

    def test_the_reason_literal_exists_exactly_once_in_the_module(self) -> None:
        """Wave 3-R7 AST fence: one constant, no concatenated spellings.

        The bug was a *spelling*, so the spelling is guarded structurally: the
        literal may appear exactly once (the ``AUTHORIZATION_REFUSAL_REASON``
        assignment, annotated ``Final[str]``), and no ``Refusal(...)`` /
        ``ApprovalError(...)`` call may build a reason by interpolation or by a
        ``"unauthorized_principal:..."`` literal.
        """
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
        # … it is the declared module constant (``AUTHORIZATION_REFUSAL_REASON:
        # Final[str] = "unauthorized_principal"``), nothing else
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
                # ``f"{AUTHORIZATION_REFUSAL_REASON}:{diagnostic}"`` is the exact
                # spelling this contract forbids
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

    def test_a_legacy_spelling_is_normalised_on_the_way_out(self) -> None:
        legacy = ApprovalError(
            "unauthorized_principal:insufficient_role", "timeline.trim needs EDITOR"
        )
        refusal = _authorization_refusal(legacy)
        assert type(refusal).__name__ == "Refusal"
        assert refusal.reason == "unauthorized_principal"
        assert refusal.detail.startswith("insufficient_role")
        assert "timeline.trim needs EDITOR" in refusal.detail

    def test_an_unrelated_approval_failure_keeps_its_diagnostic_as_detail(self) -> None:
        refusal = _authorization_refusal(ApprovalError("something_else", "detail"))
        assert refusal.reason == "unauthorized_principal"
        assert refusal.detail == "something_else: detail"

    def test_the_helper_never_invents_a_reason(self) -> None:
        error = _authorization_error("insufficient_role", "")
        assert error.reason == "unauthorized_principal"
        assert error.detail == "insufficient_role"
        assert str(error) == "unauthorized_principal: insufficient_role"


class TestThePublicRefusalContract:
    def test_turn_creation_returns_a_refusal_not_an_exception(self) -> None:
        service = _service(authority=_IdentityPort(role=MembershipRole.VIEWER))
        session = types.SimpleNamespace(
            session_id="sess_r6", principal_id=PRINCIPAL, project_id=PROJECT
        )
        refusal = service._authorize_turn_creation(session)
        assert refusal is not None
        assert refusal.reason == "unauthorized_principal"
        assert "insufficient_role" in refusal.detail

    def test_an_authorized_call_returns_no_refusal(self) -> None:
        service = _service(authority=_IdentityPort(role=MembershipRole.EDITOR))
        session = types.SimpleNamespace(
            session_id="sess_r6", principal_id=PRINCIPAL, project_id=PROJECT
        )
        assert service._authorize_turn_creation(session) is None

    def test_the_session_boundary_stays_typed(self) -> None:
        """``open_session`` returns a session id, so its refusal is an exception —
        but the same canonical reason, never a concatenated one."""
        # a configured role authority makes the session gate active; without a
        # canonical identity port that gate is the typed session-boundary refusal
        service = _service(authority=None, principal_authority=_RolePort())
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
        assert "authority_unconfigured" in exc.value.detail

    def test_a_role_authority_refusal_reason_is_never_mangled(self) -> None:
        service = _service(
            authority=_IdentityPort(),
            principal_authority=_RolePort(
                error=AuthorizationRefusalError("insufficient_role", "VIEWER cannot edit")
            ),
        )
        error = _authorize(service)
        assert error.reason == "unauthorized_principal"
        assert error.detail.startswith("insufficient_role")
        assert "VIEWER cannot edit" in error.detail


class TestTheRoleEnforcementPredicate:
    def test_a_configured_role_authority_enforces_roles_without_the_flag(self) -> None:
        deps = IBPCServiceDeps(
            queue=None,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            workspace_root=pathlib.Path("/tmp/ibpc-r6"),
            revision_provider=lambda: 7,
            now=NOW,
            principal_authority=_RolePort(),
        )
        assert deps.require_role_authority is False
        assert deps.role_authority_enforced is True

    def test_an_identity_only_composition_does_not_enforce_roles_but_still_blocks_restore(
        self,
    ) -> None:
        deps = IBPCServiceDeps(
            queue=None,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            workspace_root=pathlib.Path("/tmp/ibpc-r6"),
            revision_provider=lambda: 7,
            now=NOW,
        )
        assert deps.role_authority_enforced is False
        # …and the destructive action is refused all the same, because the
        # OWNER-only rule is keyed on the ACTION, not on this predicate.
        service = _service(authority=_IdentityPort())
        error = _authorize(service, Action.REVISION_RESTORE)
        assert error.reason == "unauthorized_principal"
        assert "role_verification_required" in error.detail

    def test_the_flag_alone_enforces_roles(self) -> None:
        deps = IBPCServiceDeps(
            queue=None,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            workspace_root=pathlib.Path("/tmp/ibpc-r6"),
            revision_provider=lambda: 7,
            now=NOW,
            require_role_authority=True,
        )
        assert deps.role_authority_enforced is True
