"""Trusted canonical-authority seam for the IBPC slice (Phase 1).

The IBPC service must never treat a session's ``principal_id``/``project_id``
as an authorization grant, and it must never mint a channel binding id.  It
resolves the acting identity through an **injected** :class:`CanonicalAuthorityPort`
supplied by server-side composition (the same ``SQLApplicationDirectory`` the
canonical worker re-reads independently), and it fails closed with a typed
:class:`CanonicalAuthorityError` when the principal is unknown, is not a member
of the project, or the channel binding does not belong to the acting principal.

``DirectoryAuthority`` is a boundary-safe concrete adapter: it wraps any object
exposing the directory protocol (``authorize`` / ``require_binding``) that the
composition root passes in.  It deliberately imports neither
``nexus_ai_agent.adapters`` nor the studio bus/authorization modules, so
``tests/architecture/test_ibpc_boundary.py`` stays green.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nexus_ai_agent.application.principal import PrincipalId

__all__ = [
    "CanonicalAuthorityError",
    "CanonicalAuthorityPort",
    "DirectoryAuthority",
    "PrincipalFacts",
]


class CanonicalAuthorityError(ValueError):
    """Typed refusal: the acting identity could not be trusted. Never a crash."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PrincipalFacts:
    """Server-verified identity the queue payload may carry.

    These are *references* revalidated by the canonical worker, never grants:
    ``channel_binding_id`` is the canonical ``cbn_<64hex>`` reference for the
    binding the acting principal actually owns.
    """

    principal_id: str
    project_id: str
    channel_binding_id: str


@runtime_checkable
class CanonicalAuthorityPort(Protocol):
    """Resolve and verify the acting principal for one project + binding."""

    def resolve_principal(
        self,
        *,
        principal_id: str,
        project_id: str,
        binding_ref: str,
    ) -> PrincipalFacts: ...


class DirectoryAuthority:
    """Reference adapter over a durable application directory.

    ``directory`` is injected by the composition root (e.g. an
    ``SQLApplicationDirectory``); this class never imports it.  It performs the
    same two checks the canonical worker performs independently:
    project membership (``authorize``) and binding ownership
    (``require_binding``).
    """

    def __init__(self, directory: object) -> None:
        self._directory = directory

    def resolve_principal(
        self,
        *,
        principal_id: str,
        project_id: str,
        binding_ref: str,
    ) -> PrincipalFacts:
        from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError

        if not binding_ref:
            raise CanonicalAuthorityError(
                "missing_binding", "no channel binding was established for this session"
            )
        try:
            principal = PrincipalId(principal_id)
        except ValueError as exc:
            raise CanonicalAuthorityError(
                "malformed_principal", f"{principal_id!r} is not a canonical principal"
            ) from exc
        actor = ActorIdentity(kind="user", actor_id=str(principal))
        try:
            self._directory.authorize(actor, project_id)  # type: ignore[attr-defined]
            self._directory.require_binding(binding_ref, principal)  # type: ignore[attr-defined]
        except AuthorizationError as exc:
            raise CanonicalAuthorityError("authorization_denied", str(exc)) from exc
        except ValueError as exc:
            raise CanonicalAuthorityError("malformed_reference", str(exc)) from exc
        return PrincipalFacts(
            principal_id=str(principal),
            project_id=project_id,
            channel_binding_id=binding_ref,
        )
