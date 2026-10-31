"""Single-use, digest-bound approval ledger for the IBPC layer.

Rules enforced here (mission §5/§6):

* an approval binds principal + session + turn + sequence + **source revision**
  + ast/plan/preview/asset digests — any mismatch is a typed refusal;
* APPROVED → CONSUMED happens exactly once at execution start; a replay is
  refused;
* revocation (interrupt/cancel/stale turn) moves the binding to REVOKED and no
  later call can resurrect it;
* the binding is **not** a permission grant — it is evidence of a human
  decision; the canonical Policy/CommandBus/queue still authorize.

The ledger is in-memory session state, not a durable evidence store: durable
evidence lives in the existing provenance journal/passport (see
:mod:`nagar.ibpc.service`). No parallel ledger is created.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding, ApprovalState


class ApprovalError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class ApprovalLedger:
    """Thread-safe, single-use approval bindings."""

    def __init__(self, *, now: Callable[[], str]) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._bindings: dict[str, ApprovalBinding] = {}

    def create(self, binding: ApprovalBinding) -> ApprovalBinding:
        """Register a PENDING binding. Re-creating the same id is refused."""
        if binding.state is not ApprovalState.PENDING:
            raise ApprovalError("invalid_state", "only PENDING bindings can be registered")
        with self._lock:
            if binding.approval_id in self._bindings:
                raise ApprovalError("duplicate_approval", binding.approval_id)
            stored = binding.model_copy(update={"created_at": self._now()})
            self._bindings[stored.approval_id] = stored
            return stored

    def get(self, approval_id: str) -> ApprovalBinding:
        with self._lock:
            try:
                return self._bindings[approval_id]
            except KeyError:
                raise ApprovalError("unknown_approval", approval_id) from None

    def _check_binding_fields(
        self,
        binding: ApprovalBinding,
        *,
        principal_id: str,
        source_revision: int,
        ast_digest: str,
        plan_digest: str,
        preview_digest: str,
        source_asset_digest: str,
    ) -> None:
        if binding.principal_id != principal_id:
            raise ApprovalError("wrong_principal", "approval belongs to another principal")
        if binding.source_revision != source_revision:
            raise ApprovalError(
                "stale_revision",
                f"approval bound to revision {binding.source_revision}, current {source_revision}",
            )
        if binding.ast_digest != ast_digest:
            raise ApprovalError("ast_mismatch", "AST changed after the approval was created")
        if binding.plan_digest != plan_digest:
            raise ApprovalError("plan_mismatch", "plan changed after the approval was created")
        if binding.preview_digest != preview_digest:
            raise ApprovalError(
                "preview_tampered", "preview digest does not match the approved preview"
            )
        if binding.source_asset_digest != source_asset_digest:
            raise ApprovalError("asset_changed", "source asset changed after the preview")

    def approve(
        self,
        approval_id: str,
        *,
        principal_id: str,
        source_revision: int,
        ast_digest: str,
        plan_digest: str,
        preview_digest: str,
        source_asset_digest: str,
        turn_is_active: Callable[[], bool],
    ) -> ApprovalBinding:
        """PENDING → APPROVED, only when every binding field still matches."""
        with self._lock:
            binding = self.get(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "approval already consumed")
            if binding.state is ApprovalState.REVOKED:
                raise ApprovalError("revoked", "approval was revoked (interrupt/cancel/stale)")
            if binding.state is not ApprovalState.PENDING:
                raise ApprovalError("invalid_state", f"cannot approve from {binding.state.value}")
            if not turn_is_active():
                raise ApprovalError("turn_inactive", "the turn is no longer active")
            self._check_binding_fields(
                binding,
                principal_id=principal_id,
                source_revision=source_revision,
                ast_digest=ast_digest,
                plan_digest=plan_digest,
                preview_digest=preview_digest,
                source_asset_digest=source_asset_digest,
            )
            approved = binding.model_copy(
                update={"state": ApprovalState.APPROVED, "decided_at": self._now()}
            )
            self._bindings[approval_id] = approved
            return approved

    def consume(
        self,
        approval_id: str,
        *,
        principal_id: str,
        source_revision: int,
        ast_digest: str,
        plan_digest: str,
        preview_digest: str,
        source_asset_digest: str,
    ) -> ApprovalBinding:
        """APPROVED → CONSUMED, exactly once. Any later use is a replay refusal."""
        with self._lock:
            binding = self.get(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "approval already consumed — execution is single-use")
            if binding.state is ApprovalState.REVOKED:
                raise ApprovalError("revoked", "approval was revoked")
            if binding.state is not ApprovalState.APPROVED:
                raise ApprovalError("not_approved", f"cannot consume from {binding.state.value}")
            self._check_binding_fields(
                binding,
                principal_id=principal_id,
                source_revision=source_revision,
                ast_digest=ast_digest,
                plan_digest=plan_digest,
                preview_digest=preview_digest,
                source_asset_digest=source_asset_digest,
            )
            consumed = binding.model_copy(update={"state": ApprovalState.CONSUMED})
            self._bindings[approval_id] = consumed
            return consumed

    def revoke(self, approval_id: str) -> ApprovalBinding:
        """REVOKED — terminal. Used on interrupt/cancel/stale turn."""
        with self._lock:
            binding = self.get(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "a consumed approval cannot be revoked")
            revoked = binding.model_copy(update={"state": ApprovalState.REVOKED})
            self._bindings[approval_id] = revoked
            return revoked

    def revoke_turn(self, session_id: str, turn_id: str) -> tuple[ApprovalBinding, ...]:
        """Revoke every non-terminal binding of one turn (interrupt/cancel)."""
        with self._lock:
            out: list[ApprovalBinding] = []
            for approval_id, binding in list(self._bindings.items()):
                if (
                    binding.session_id == session_id
                    and binding.turn_id == turn_id
                    and binding.state in (ApprovalState.PENDING, ApprovalState.APPROVED)
                ):
                    out.append(self.revoke(approval_id))
            return tuple(out)


__all__ = ["ApprovalError", "ApprovalLedger"]
