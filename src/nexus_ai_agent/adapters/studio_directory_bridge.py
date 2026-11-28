"""Unified Studio identity bridge: one authority answer for API *and* Worker.

Wave 1 / Wave 4 closure.  Two code paths have historically answered "may this
principal do this?" from two different places:

* the HTTP surface registers principals, channel bindings and project
  memberships in :class:`~nexus_ai_agent.adapters.studio_principal_auth.
  StudioPrincipalAuthStore` (which itself satisfies
  :class:`~nexus_ai_agent.nagar.ibpc.authority.CanonicalAuthorityPort` and
  :class:`~nexus_ai_agent.nagar.ibpc.authority.StudioAuthorizerPort`);
* the canonical worker / ``IBPCService`` re-validate an *execution* against
  ``AuthorityResolution`` / ``PrincipalRoleFacts`` facts read independently.

When the two readers are two different objects, a binding that the API accepts
can be invisible to the worker (or vice versa) — an authority split-brain.  The
bridge removes the split: :class:`StudioUnifiedAuthorityBridge` is a single,
deterministic adapter over the durable store that satisfies *both*
:class:`~nexus_ai_agent.nagar.ibpc.authority.CanonicalAuthorityPort` and
:class:`~nexus_ai_agent.nagar.ibpc.authority.PrincipalRoleAuthority`, so the
same four-way invariant is enforced on both paths.

The four-way fail-closed invariant (every read inside ONE consistent snapshot):

a. the principal row exists and is ``ACTIVE``;
b. the project row exists and ``principal.tenant_id == project.tenant_id ==
   the tenant the caller expects``;
c. the (project, principal) membership row exists, is ``ACTIVE``, and its role
   (``OWNER`` / ``EDITOR`` / ``VIEWER`` / ``AUDITOR``) is permitted the requested
   ``StudioAction``;
d. the channel binding belongs to *this* principal, is ``ACTIVE``, lives in the
   same tenant, and — when the caller names one — is exactly the binding it
   names, or the canonical default ``studio:{tenant_id}:{principal_id}``.

Anything else raises :class:`StudioAuthorityBridgeError`, a typed refusal
carrying the canonical ``reason="unauthorized_principal"`` plus a *diagnostic*
``detail``.  Diagnostic only: the detail names which of a–d failed for operators,
and every value that crosses into the proof digest is re-derived from the store.

A successful worker verification returns an immutable
:class:`UnifiedExecutionAuthorityProof` whose ``proof_digest`` is the RFC 8785
JCS SHA-256 of its own canonical tuple — so a proof produced for one
(principal, project, tenant, binding, action) set can never be replayed for
another, and a tampered field is detectable by recomputation alone.
"""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nexus_ai_agent.adapters.studio_principal_auth import (
    ROLE_ACTIONS,
    StudioPrincipalAuthStore,
)
from nexus_ai_agent.nagar.ibpc.authority import (
    AuthorizationError,
    BindingStatus,
    CanonicalAuthorityError,
    MembershipRole,
    MembershipStatus,
    PrincipalFacts,
    PrincipalRoleFacts,
    PrincipalStatus,
    StudioAction,
)
from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
)

__all__ = [
    "CANONICAL_BINDING_PREFIX",
    "StudioAuthorityBridgeError",
    "StudioUnifiedAuthorityBridge",
    "UnifiedExecutionAuthorityProof",
    "canonical_binding_ref",
    "role_permits",
    "utc_now",
]

#: Prefix of the deterministic default binding a principal gets per tenant.
CANONICAL_BINDING_PREFIX = "studio:"

_PROOF_SCHEMA = "nexus.studio.execution_authority_proof.v1"


def utc_now() -> str:
    """One spelling of "now" for every durable timestamp in this module."""
    return datetime.now(timezone.utc).isoformat()


def canonical_binding_ref(*, tenant_id: str, principal_id: str) -> str:
    """The deterministic default binding ref: ``studio:{tenant}:{principal}``."""
    return f"{CANONICAL_BINDING_PREFIX}{tenant_id}:{principal_id}"


def role_permits(role: MembershipRole, action: StudioAction) -> bool:
    """True when ``role`` grants ``action`` (the store's one ROLE_ACTIONS table)."""
    return action in ROLE_ACTIONS[role.to_studio()]


class StudioAuthorityBridgeError(AuthorizationError):
    """Typed fail-closed refusal from the unified authority bridge.

    ``reason`` is the canonical refusal class (``unauthorized_principal`` for
    every identity refusal); ``detail`` is diagnostic and never a grant.
    """

    def __init__(self, detail: str, *, cause: Exception | None = None) -> None:
        super().__init__("unauthorized_principal", detail)
        self.bridge_detail = detail
        self.cause = cause


@dataclass(frozen=True)
class UnifiedExecutionAuthorityProof:
    """Immutable evidence that the worker may execute one approved action.

    Every field is a *fact read from the durable store* inside one snapshot; the
    dataclass is frozen so a verifier downstream cannot edit it, and
    ``proof_digest`` binds the whole tuple under RFC 8785 JCS.
    """

    principal_id: str
    project_id: str
    tenant_id: str
    binding_ref: str
    role: MembershipRole
    permitted_actions: tuple[StudioAction, ...]
    verified_at_utc: str
    proof_digest: str

    def to_canonical_dict(self) -> dict[str, Any]:
        """The payload the digest is computed over (no ``proof_digest``)."""
        return {
            "schema": _PROOF_SCHEMA,
            "principal_id": self.principal_id,
            "project_id": self.project_id,
            "tenant_id": self.tenant_id,
            "binding_ref": self.binding_ref,
            "role": self.role.value,
            "permitted_actions": [action.value for action in self.permitted_actions],
            "verified_at_utc": self.verified_at_utc,
        }

    def compute_digest(self) -> str:
        """RFC 8785 JCS SHA-256 over the canonical proof tuple."""
        return canonical_digest(self.to_canonical_dict(), CanonicalizationScheme.JCS_RFC8785_V2)

    def verify_integrity(self) -> bool:
        """True when ``proof_digest`` is exactly the digest of the fields."""
        return hmac.compare_digest(self.compute_digest(), self.proof_digest)


@dataclass(frozen=True)
class _AuthoritySnapshot:
    """One consistent read of the four rows the bridge decides on."""

    principal_tenant: str
    principal_status: str
    project_tenant: str
    membership_role: str
    membership_status: str
    binding_tenant: str
    binding_status: str
    binding_ref: str


class StudioUnifiedAuthorityBridge:
    """Durable bridge satisfying ``CanonicalAuthorityPort`` *and*
    ``PrincipalRoleAuthority`` over one :class:`StudioPrincipalAuthStore`.

    The store is the single durable authority: the bridge never caches a
    decision, never widens one, and re-reads every row inside a
    ``BEGIN`` snapshot for each call.  An optional
    :class:`~nexus_ai_agent.adapters.sql_application_directory.
    SQLApplicationDirectory` may be attached for read-through of the canonical
    project access (``authorize``) — it can only *narrow* the decision, never
    widen it: a directory refusal is a bridge refusal.

    Parameters
    ----------
    store:
        The durable principal/binding/membership store (required).
    directory:
        Optional canonical application directory consulted read-through after
        the four-way check passes.  Anything that raises
        ``creative.studio.models.AuthorizationError`` narrows to a refusal.
    now:
        Injected clock for deterministic ``verified_at_utc`` in tests.
    """

    def __init__(
        self,
        store: StudioPrincipalAuthStore,
        *,
        directory: object | None = None,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self._store = store
        self._directory = directory
        self._now = now

    # -- introspection ---------------------------------------------------- #

    @property
    def store(self) -> StudioPrincipalAuthStore:
        """The durable store this bridge reads (never mutates)."""
        return self._store

    @property
    def directory(self) -> object | None:
        """The optionally attached application directory (read-through only)."""
        return self._directory

    def __repr__(self) -> str:
        return (
            f"StudioUnifiedAuthorityBridge(store={self._store.path.name!r}, "
            f"directory={'attached' if self._directory is not None else 'none'})"
        )

    # -- public protocol methods ------------------------------------------ #

    def resolve_authority(
        self,
        principal_id: str,
        project_id: str,
        *,
        binding_ref: str = "",
        tenant_id: str = "",
    ) -> PrincipalRoleFacts:
        """Resolve the full role facts for one actor (the ``AuthorityResolution``)."""
        return self.resolve_principal_role(
            principal_id, project_id, binding_ref=binding_ref, tenant_id=tenant_id
        )

    def resolve_principal_role(
        self,
        principal_id: str,
        project_id: str,
        *,
        binding_ref: str = "",
        tenant_id: str = "",
    ) -> PrincipalRoleFacts:
        """Four-way fail-closed resolution; raises on any missing/inactive row."""
        snapshot = self._read_snapshot(
            principal_id=principal_id,
            project_id=project_id,
            tenant_id=tenant_id,
            binding_ref=binding_ref,
            action=None,
        )
        role = MembershipRole.from_studio(snapshot.membership_role)
        return PrincipalRoleFacts(
            principal_id=principal_id,
            project_id=project_id,
            tenant_id=tenant_id,
            binding_ref=snapshot.binding_ref,
            role=role,
            principal_status=PrincipalStatus(snapshot.principal_status),
            membership_status=MembershipStatus(snapshot.membership_status),
            binding_status=BindingStatus(snapshot.binding_status),
            permitted_actions=tuple(
                sorted(ROLE_ACTIONS[role.to_studio()], key=lambda action: action.value)
            ),
            verified_at_utc=self._now(),
        )

    def resolve_principal(
        self,
        *,
        principal_id: str,
        project_id: str,
        binding_ref: str,
    ) -> PrincipalFacts:
        """``CanonicalAuthorityPort`` shape: the verified identity for one binding."""
        if not binding_ref or not binding_ref.strip():
            raise StudioAuthorityBridgeError(
                "missing_binding",
                cause=CanonicalAuthorityError("missing_binding", "binding_ref is required"),
            )
        facts = self.resolve_principal_role(
            principal_id,
            project_id,
            binding_ref=binding_ref,
            tenant_id=self._store.get_principal_tenant(principal_id),
        )
        return PrincipalFacts(
            principal_id=facts.principal_id,
            project_id=facts.project_id,
            channel_binding_id=facts.binding_ref,
        )

    def verify_worker_execution_authority(
        self,
        *,
        principal_id: str,
        project_id: str,
        tenant_id: str,
        binding_ref: str,
        required_action: StudioAction = StudioAction.EXECUTE_APPROVED_PLAN,
    ) -> UnifiedExecutionAuthorityProof:
        """The worker's own gate: may this actor execute *this* action, now?

        Fails closed with :class:`StudioAuthorityBridgeError` when any of the
        four invariants does not hold, when the binding is unknown/revoked/belongs
        to another principal or tenant, or when the role does not grant
        ``required_action``.
        """
        if not isinstance(required_action, StudioAction):
            raise StudioAuthorityBridgeError(
                f"required_action {required_action!r} is not a StudioAction"
            )
        snapshot = self._read_snapshot(
            principal_id=principal_id,
            project_id=project_id,
            tenant_id=tenant_id,
            binding_ref=binding_ref,
            action=required_action,
        )
        # ``_read_snapshot`` already applied the role gate to ``required_action``
        # inside the read snapshot; the role is re-derived from the *snapshot*
        # row (never from a caller value) so the proof records the stored role.
        role = MembershipRole.from_studio(snapshot.membership_role)
        self._narrow_through_directory(principal_id=principal_id, project_id=project_id)
        permitted = tuple(sorted(ROLE_ACTIONS[role.to_studio()], key=lambda action: action.value))
        verified_at = self._now()
        proof = UnifiedExecutionAuthorityProof(
            principal_id=principal_id,
            project_id=project_id,
            tenant_id=tenant_id,
            binding_ref=snapshot.binding_ref,
            role=role,
            permitted_actions=permitted,
            verified_at_utc=verified_at,
            proof_digest="",
        )
        return UnifiedExecutionAuthorityProof(
            principal_id=proof.principal_id,
            project_id=proof.project_id,
            tenant_id=proof.tenant_id,
            binding_ref=proof.binding_ref,
            role=proof.role,
            permitted_actions=proof.permitted_actions,
            verified_at_utc=proof.verified_at_utc,
            proof_digest=proof.compute_digest(),
        )

    # -- internals -------------------------------------------------------- #

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        """One consistent read view; never a write transaction.

        ``BEGIN`` (deferred) makes every ``SELECT`` in the block see the same
        snapshot of the store even if another process commits a revocation
        mid-decision — the decision can only be made against one state.
        """
        conn = self._store._conn  # noqa: SLF001 - the durable store is the authority
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("COMMIT")

    def _read_snapshot(
        self,
        *,
        principal_id: str,
        project_id: str,
        tenant_id: str | None,
        binding_ref: str,
        action: StudioAction | None,
    ) -> _AuthoritySnapshot:
        for field_name, value in (
            ("principal_id", principal_id),
            ("project_id", project_id),
            ("binding_ref", binding_ref),
        ):
            if not isinstance(value, str) or not value.strip():
                raise StudioAuthorityBridgeError(
                    f"{field_name} must be a non-empty string (got {value!r})"
                )
        # ``None`` means "read the tenant from the durable principal row"
        # (the ``CanonicalAuthorityPort`` shape carries no tenant argument); a
        # caller that *does* name a tenant must name a non-empty string.
        if tenant_id is not None and (not isinstance(tenant_id, str) or not tenant_id.strip()):
            raise StudioAuthorityBridgeError(
                f"tenant_id must be a non-empty string (got {tenant_id!r})"
            )
        resolved_binding = binding_ref.strip()
        with self._snapshot() as conn:
            principal_row = conn.execute(
                "SELECT tenant_id, status FROM nexus_studio_principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
            if principal_row is None:
                raise StudioAuthorityBridgeError(f"principal {principal_id!r} does not exist")
            principal_tenant = str(principal_row[0])
            principal_status = str(principal_row[1])
            if principal_status != PrincipalStatus.ACTIVE.value:
                raise StudioAuthorityBridgeError(
                    f"principal {principal_id!r} is {principal_status}, not ACTIVE"
                )
            expected_tenant = principal_tenant if tenant_id is None else tenant_id
            if not hmac.compare_digest(principal_tenant, expected_tenant):
                raise StudioAuthorityBridgeError(
                    f"principal tenant {principal_tenant!r} != expected {expected_tenant!r}"
                )

            project_row = conn.execute(
                "SELECT tenant_id FROM nexus_studio_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project_row is None:
                raise StudioAuthorityBridgeError(f"project {project_id!r} does not exist")
            project_tenant = str(project_row[0])
            if not hmac.compare_digest(project_tenant, expected_tenant):
                raise StudioAuthorityBridgeError(
                    f"project tenant {project_tenant!r} != expected {expected_tenant!r}"
                )

            membership_row = conn.execute(
                """
                SELECT role, status
                FROM nexus_studio_project_memberships
                WHERE project_id = ? AND principal_id = ?
                """,
                (project_id, principal_id),
            ).fetchone()
            if membership_row is None:
                raise StudioAuthorityBridgeError(
                    f"principal {principal_id!r} has no membership in {project_id!r}"
                )
            membership_role = str(membership_row[0])
            membership_status = str(membership_row[1])
            if membership_status != MembershipStatus.ACTIVE.value:
                raise StudioAuthorityBridgeError(
                    f"membership for {principal_id!r} is {membership_status}, not ACTIVE"
                )
            try:
                role = MembershipRole.from_studio(membership_role)
            except CanonicalAuthorityError as exc:
                raise StudioAuthorityBridgeError(str(exc), cause=exc) from exc
            if action is not None and not role_permits(role, action):
                raise StudioAuthorityBridgeError(
                    f"role {role.value} does not permit {action.value}"
                )

            canonical = canonical_binding_ref(tenant_id=expected_tenant, principal_id=principal_id)
            binding_row = conn.execute(
                """
                SELECT binding_ref, status
                FROM nexus_studio_channel_bindings
                WHERE binding_ref = ? AND principal_id = ?
                """,
                (resolved_binding, principal_id),
            ).fetchone()
            if binding_row is None and resolved_binding == canonical:
                # The deterministic default ref ``studio:{tenant}:{principal}``
                # resolves to this principal's own oldest ACTIVE binding in the
                # tenant, whatever spelling that row was registered under.
                binding_row = conn.execute(
                    """
                    SELECT binding_ref, status
                    FROM nexus_studio_channel_bindings
                    WHERE principal_id = ? AND status = ?
                    ORDER BY rowid ASC
                    LIMIT 1
                    """,
                    (principal_id, BindingStatus.ACTIVE.value),
                ).fetchone()
            if binding_row is None:
                raise StudioAuthorityBridgeError(
                    f"no binding {resolved_binding!r} belongs to {principal_id!r} "
                    f"in tenant {expected_tenant!r}"
                )
            stored_binding_ref = str(binding_row[0])
            binding_status = str(binding_row[1])
            if binding_status != BindingStatus.ACTIVE.value:
                raise StudioAuthorityBridgeError(
                    f"binding {stored_binding_ref!r} is {binding_status}, not ACTIVE"
                )
            binding_tenant = principal_tenant

        return _AuthoritySnapshot(
            principal_tenant=principal_tenant,
            principal_status=principal_status,
            project_tenant=project_tenant,
            membership_role=membership_role,
            membership_status=membership_status,
            binding_tenant=binding_tenant,
            binding_status=binding_status,
            binding_ref=stored_binding_ref,
        )

    def _narrow_through_directory(self, *, principal_id: str, project_id: str) -> None:
        """Read-through the canonical application directory; it may only deny."""
        if self._directory is None:
            return
        from nexus_ai_agent.creative.studio.models import ActorIdentity
        from nexus_ai_agent.creative.studio.models import AuthorizationError as DirectoryRefusal

        actor = ActorIdentity(kind="user", actor_id=principal_id)
        try:
            self._directory.authorize(actor, project_id)  # type: ignore[attr-defined]
        except DirectoryRefusal as exc:
            raise StudioAuthorityBridgeError(
                f"application directory refused {principal_id!r} on {project_id!r}: {exc}",
                cause=exc,
            ) from exc
