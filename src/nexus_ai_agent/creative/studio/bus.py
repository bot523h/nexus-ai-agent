"""The one Nagar write path: validate -> authorize -> atomically apply.

The bus is the *only* write path into the central in-memory project state.
Every dispatch runs the full pipeline inside a re-entrant lock, in the
canonical Gate 2 order (D-0013), each step failing closed:

1. **Parse** -- JSON text, a mapping or an already-typed command go through
   the *same* gates; typed objects are revalidated, never trusted blindly.
2. **Envelope + operation schema validation** -- protocol ``nagar.command.v1``
   with envelope schema 1|2, a registered operation, a matching operation
   schema version, and input validated against the operation's Pydantic model.
3. **Actor / project authorization** -- ``target.project_id`` is only a
   *claim*. An injected trusted authorizer must independently bind that
   project and the real actor. A command that carries an actor claim without
   an authorizer is refused; a claim-less legacy (schema 1) command without
   an authorizer dispatches under implicit local trust (deprecated
   compatibility path for in-process runtime call sites).
4. **Capability authorization** -- the operation's capability must exist, be
   available, and version-match; a client capability snapshot is an advisory
   hint that is verified against the authoritative registry, never trusted.
   The grant's permissions must cover the operation's required permissions.
5. **Execution policy** -- the requested mode must be advertised by the spec
   (``local`` only today) and the A/B/C/D ladder must allow the command.
6. **Reference validation** -- every declared ``input_refs`` entry is checked
   against the authorized current project, and semantic time references are
   pinned at receipt (``captured_at_command=True``).
7. **Idempotency reservation** -- a keyed request reserves
   ``(project_id, operation, idempotency_key)`` before any handler runs. A
   redelivery with the same logical payload returns the exact original
   result; the same key with a different payload is a deterministic
   ``IdempotencyConflictError``. Reservations are per-bus in-memory: they do
   not claim cross-process or cross-restart durability.
8. **Revision / precondition check** -- an optimistic-concurrency gate on
   ``state_revision`` and ``state_hash`` evaluated for new work only; a stale
   command is rejected with the state left untouched, and a failed command
   releases its reservation so a corrected retry can proceed.
9. **Atomic apply** -- the pure handler runs to completion against an
   isolated copy, and only then are the new project, the bumped revision,
   the recomputed state hash, the ``EditTransaction`` and the reservation's
   result committed together. Any handler failure leaves the central state
   exactly as it was.

Undo is revision+snapshot based: every ``EditTransaction`` stores the full
previous in-memory snapshot plus ``previous_state_hash`` /
``new_state_hash``.  ``system.undo`` rewinds the most recent *editable*
transaction (classic NLE semantics) and records the rewind as a transaction
for the audit trail; undo records are not themselves undo targets.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from nexus_ai_agent.creative.studio.authorization import ProjectAccess, ProjectAuthorizer
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationSpec,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.models import (
    PROTOCOL_VERSION,
    AuthorizationError,
    CommandExecutionError,
    CommandResult,
    CommandValidationError,
    EditTransaction,
    ExecutionPolicyError,
    IdempotencyConflictError,
    NagarError,
    Playhead,
    PreconditionError,
    Preconditions,
    Project,
    TypedCommand,
    compute_state_hash,
)
from nexus_ai_agent.creative.studio.references import ReferenceResolver


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate command JSON field: {key!r}")
        data[key] = value
    return data


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


def _parse(command: TypedCommand | dict[str, Any] | str | bytes) -> TypedCommand:
    """Parse and revalidate even an already-typed object (model_copy is unsafe)."""
    try:
        if isinstance(command, TypedCommand):
            raw: Any = command.model_dump(mode="json")
        elif isinstance(command, (str, bytes)):
            raw = json.loads(
                command, object_pairs_hook=_unique_json_pairs, parse_constant=_reject_constant
            )
        else:
            raw = command
        if not isinstance(raw, dict):
            raise ValueError("command must be a JSON object")
        return TypedCommand.model_validate(raw)
    except (ValidationError, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise CommandValidationError(f"invalid command envelope: {exc}") from exc


def _fingerprint(command: TypedCommand) -> str:
    # A redelivery may have a new transport command_id / trace_id. Every field
    # that can change authority, policy, input or meaning is still fingerprinted.
    # Snapshots are checked against the registry on EVERY attempt, but are not
    # themselves execution authority or part of the logical payload.
    payload = command.model_dump(
        mode="json", exclude={"command_id", "trace_id", "capability_snapshot"}
    )
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Reservation:
    fingerprint: str
    result: CommandResult | None = None  # None means execution is in flight


class CommandBus:
    """Validates typed commands and applies them atomically to central state."""

    def __init__(
        self,
        state: Project,
        registry: CapabilityRegistry | None = None,
        resolver: ReferenceResolver | None = None,
        *,
        authorizer: ProjectAuthorizer | None = None,
    ) -> None:
        self._registry = registry if registry is not None else build_wave1_registry()
        self._resolver = resolver if resolver is not None else ReferenceResolver()
        self._authorizer = authorizer
        self._lock = threading.RLock()
        # model_copy(update=...) bypasses Pydantic's derived state_hash and
        # validation (the worker uses it when registering its staged asset).
        # Never let a stale/fabricated hash become an optimistic precondition.
        self._project = Project.model_validate(state.model_dump(mode="json"))
        self._history: list[EditTransaction] = []
        self._idempotency: dict[tuple[str, str, str], _Reservation] = {}
        self._executing = False

    @property
    def project(self) -> Project:
        """A deep copy of the central state (safe to read without the lock)."""
        with self._lock:
            return self._project.model_copy(deep=True)

    @property
    def history(self) -> tuple[EditTransaction, ...]:
        with self._lock:
            return tuple(self._history)

    @property
    def state_revision(self) -> int:
        with self._lock:
            return self._project.state_revision

    @property
    def state_hash(self) -> str:
        with self._lock:
            return self._project.state_hash

    def dispatch(self, command: TypedCommand | dict[str, Any] | str | bytes) -> CommandResult:
        """Run the full validate -> authorize -> apply pipeline."""
        parsed = _parse(command)  # 1. Parse + 2a. versioned envelope schema
        with self._lock:
            if self._executing:
                raise CommandExecutionError("nested command dispatch during execution is forbidden")
            return self._dispatch_locked(parsed)

    def _dispatch_locked(self, command: TypedCommand) -> CommandResult:
        if command.protocol_version != PROTOCOL_VERSION:
            raise CommandValidationError(
                f"unsupported protocol_version: {command.protocol_version!r}"
            )

        # 2b. Operation schema: look up the input model but do not execute it.
        # Unknown operations have no schema and therefore fail here. Schema
        # checks are pure/bounded (command input <= 512 KiB).
        spec = self._registry.get_spec(command.operation)
        if command.operation_schema_version != spec.schema_version:
            raise CommandValidationError(
                f"unsupported operation schema_version for {command.operation!r}: "
                f"{command.operation_schema_version}"
            )
        try:
            validated = spec.input_model.model_validate(command.input)
        except ValidationError as exc:
            raise CommandValidationError(f"{command.operation}: invalid input: {exc}") from exc

        # 3. Actor / project: target.project_id is only a *claim*. The injected
        # authorizer must independently bind that project and the real actor.
        # Claim-less legacy commands without an authorizer dispatch under
        # implicit local trust (deprecated); an actor claim without an
        # authorizer is refused -- identity must never be self-asserted.
        project_id = self._project.project_id
        if command.target.project_id is not None and command.target.project_id != project_id:
            raise AuthorizationError("command targets a different project")
        access: ProjectAccess | None = None
        if self._authorizer is None:
            if command.actor is not None:
                raise AuthorizationError(
                    "command carries an actor claim but no trusted project authorizer is configured"
                )
        else:
            if command.actor is None:
                raise AuthorizationError("command carries no actor claim for authorization")
            access = self._authorizer.authorize(command.actor, project_id)
            if access.actor != command.actor or access.project_id != project_id:
                raise AuthorizationError("authorizer returned a grant for another actor or project")

        # 4. Registry/capability, version, installed availability and the
        # operation's *trusted* permissions (not client-supplied permissions).
        descriptor = self._registry.check_capability(command)
        if access is not None:
            access.require_permissions(descriptor.required_permissions)

        # 5. Execution policy: only an advertised mode; A/B/C/D ladder remains
        # authoritative. The envelope cannot opt into egress or a shell.
        if command.execution_policy.mode not in descriptor.execution_modes:
            raise ExecutionPolicyError(
                f"{command.operation}: unavailable execution mode {command.execution_policy.mode!r}"
            )
        decision = self._registry.check_permission(command.operation, confirmed=command.confirmed)
        if not decision.allowed:
            raise ExecutionPolicyError(f"{command.operation}: {decision.reason}")

        # 6. All declared input_refs and semantic time references are validated
        # against the authorized current project before any reservation/handler.
        self._resolver.validate_input_refs(command.input_refs, self._project)
        input_data = validated.model_dump(mode="json")
        for field_name in spec.reference_fields:
            raw_value: Any = getattr(validated, field_name)
            if raw_value is None:
                continue
            if isinstance(raw_value, Playhead):
                pinned = raw_value.model_copy(update={"captured_at_command": True})
            else:
                pinned = self._resolver.resolve(raw_value, self._project)
            input_data[field_name] = pinned.model_dump(mode="json")

        # 7. Reserve (project, operation, key) under the project lock. Without
        # an explicit key there is no reservation (legacy semantic). A
        # re-delivery returns the exact original result, never a second
        # handler invocation.
        if command.idempotency_key is None:
            self._check_preconditions(command.preconditions)
            return self._apply_guarded(command, spec, input_data)

        key = (project_id, command.operation, command.idempotency_key)
        fingerprint = _fingerprint(command)
        prior = self._idempotency.get(key)
        if prior is not None:
            if prior.fingerprint != fingerprint:
                raise IdempotencyConflictError("idempotency key reused with different payload")
            if prior.result is None:
                raise CommandExecutionError("idempotent command is already executing")
            return prior.result.model_copy(deep=True)

        self._idempotency[key] = _Reservation(fingerprint=fingerprint)
        try:
            self._check_preconditions(command.preconditions)
            result = self._apply_guarded(command, spec, input_data)
            self._idempotency[key] = _Reservation(fingerprint, result.model_copy(deep=True))
            return result
        finally:
            if self._idempotency.get(key) == _Reservation(fingerprint):
                # Any failed precondition/handler released its reservation.
                self._idempotency.pop(key)

    def _apply_guarded(
        self, command: TypedCommand, spec: OperationSpec, input_data: dict[str, Any]
    ) -> CommandResult:
        self._executing = True
        try:
            return self._apply(command, spec, input_data)
        finally:
            self._executing = False

    def _check_preconditions(self, preconditions: Preconditions) -> None:
        expected_revision = preconditions.state_revision
        expected_hash = preconditions.state_hash
        if expected_revision is not None and expected_revision != self._project.state_revision:
            raise PreconditionError(
                f"stale state_revision: command expects {expected_revision}, "
                f"current is {self._project.state_revision}"
            )
        if expected_hash is not None and expected_hash != self._project.state_hash:
            raise PreconditionError(
                "stale state_hash: command preconditions do not match the current state"
            )

    def _apply(
        self, command: TypedCommand, spec: OperationSpec, input_data: dict[str, Any]
    ) -> CommandResult:
        """9. The only execution request into a registered pure handler."""
        context = OperationContext(
            command=command, input_data=input_data, history=tuple(self._history)
        )
        try:
            # A buggy handler cannot mutate central state even if it then raises.
            outcome = spec.handler(self._project.model_copy(deep=True), context)
        except NagarError:
            raise
        except Exception as exc:  # handlers must not leak untyped errors
            raise CommandExecutionError(f"{command.operation} failed: {exc}") from exc

        # Commit step: the handler ran to completion, so the new project, the
        # bumped revision, the recomputed hash and the transaction are
        # installed together.  ``state_revision`` is monotonic by design;
        # content identity is carried by ``state_hash`` (undo restores the
        # exact previous hash, keeping precondition gates sound).
        previous_hash = self._project.state_hash
        previous_revision = self._project.state_revision
        new_project = outcome.project.model_copy(deep=True)
        if new_project.project_id != self._project.project_id:
            raise CommandExecutionError("handler changed the authorized project identity")
        new_project.state_revision = previous_revision + 1
        new_project.state_hash = compute_state_hash(new_project)
        transaction = EditTransaction(
            transaction_id=f"tx_{uuid4().hex}",
            command_id=command.command_id,
            operation=command.operation,
            permission_level=spec.permission_level,
            parent_revision=previous_revision,
            previous_state_hash=previous_hash,
            new_state_hash=new_project.state_hash,
            state_before=self._project.model_dump(mode="json"),
        )
        result = CommandResult(
            transaction_id=transaction.transaction_id,
            state_revision=new_project.state_revision,
            state_hash=new_project.state_hash,
            output=outcome.output,
            diagnostics={
                "executor": "in-memory-reducer",
                "permission_level": spec.permission_level.value,
                "protocol_version": PROTOCOL_VERSION,
                "trace_id": command.trace_id,
            },
            undo_available=True,
        )
        # All potentially failing validation/construction is complete before
        # either central state or the reservation's result is committed.
        self._project = new_project
        self._history = [*outcome.history, transaction]
        return result
