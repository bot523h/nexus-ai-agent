"""Command bus: validate -> authorize -> atomically apply.

The bus is the *only* write path into the central in-memory project state.
Every dispatch runs the full pipeline inside a re-entrant lock:

1. **Envelope validation** -- the payload is validated into a
   ``TypedCommand`` (protocol ``nagar.command.v1``); malformed envelopes are
   rejected before anything else happens.
2. **Idempotency replay** -- a repeated ``idempotency_key`` returns the
   cached result of the first application without re-executing.
3. **Registry lookup** -- unknown operations raise
   ``UnknownOperationError``; the agent can never reach unregistered code.
3.5 **Capability lifecycle gate** -- the spec's ``required_packs`` must
   resolve to ``AVAILABLE`` (or ``EXPERIMENTAL`` with the bus-level opt-in);
   unknown, ``STUB``, and ``RETIRED`` packs refuse before any work starts.
4. **Permission gate** -- the ``CapabilityRegistry`` decides per operation
   (A/B proceed, C only with explicit confirmation, D always denied).
5. **Reference pinning at receipt** -- every ``reference_field`` of the
   operation is resolved via the ``ReferenceResolver*`` against the state
   snapshot *now*, so expressions like "اینجا" or "۵ ثانیه قبل" freeze to an
   exact ``timecode_us`` with ``captured_at_command=True``.
6. **Typed input validation** -- the (pinned) input is validated against the
   operation's Pydantic input model.
7. **Precondition check** -- an optimistic-concurrency gate on
   ``state_revision`` and ``state_hash``; stale commands are rejected with
   the state left untouched.
8. **Atomic apply** -- the pure handler runs to completion, and only then
   are the new project, the bumped revision, the recomputed state hash and
   the ``EditTransaction`` committed in one step.  Any handler failure
   leaves the central state exactly as it was.

Undo is revision+snapshot based: every ``EditTransaction`` stores the full
previous in-memory snapshot plus ``previous_state_hash`` /
``new_state_hash``.  ``system.undo`` rewinds the most recent *editable*
transaction (classic NLE semantics) and records the rewind as a transaction
for the audit trail; undo records are not themselves undo targets.
"""

from __future__ import annotations

import threading
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.lifecycle import check_required_packs
from nexus_ai_agent.creative.studio.models import (
    PROTOCOL_VERSION,
    CommandExecutionError,
    CommandResult,
    CommandValidationError,
    EditTransaction,
    NagarError,
    PermissionDeniedError,
    Playhead,
    PreconditionError,
    Project,
    TypedCommand,
    compute_state_hash,
)
from nexus_ai_agent.creative.studio.references import ReferenceResolver


class CommandBus:
    """Validates typed commands and applies them atomically to central state."""

    def __init__(
        self,
        state: Project,
        registry: CapabilityRegistry | None = None,
        resolver: ReferenceResolver | None = None,
        *,
        allow_experimental: bool = False,
    ) -> None:
        self._registry = registry if registry is not None else build_wave1_registry()
        self._resolver = resolver if resolver is not None else ReferenceResolver()
        self._allow_experimental = allow_experimental
        self._lock = threading.RLock()
        self._project = state
        self._history: list[EditTransaction] = []
        self._idempotency: dict[str, CommandResult] = {}

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

    def dispatch(self, command: TypedCommand | dict[str, Any]) -> CommandResult:
        """Run the full validate -> authorize -> apply pipeline."""
        # 1. envelope validation (typed command, protocol v1)
        if isinstance(command, dict):
            try:
                command = TypedCommand.model_validate(command)
            except ValidationError as exc:
                raise CommandValidationError(f"invalid command envelope: {exc}") from exc
        with self._lock:
            return self._dispatch_locked(command)

    def _dispatch_locked(self, command: TypedCommand) -> CommandResult:
        if command.protocol_version != PROTOCOL_VERSION:
            raise CommandValidationError(
                f"unsupported protocol_version: {command.protocol_version!r}"
            )

        # 2. idempotency replay ------------------------------------------
        if command.idempotency_key is not None and command.idempotency_key in self._idempotency:
            return self._idempotency[command.idempotency_key]

        # 3. registry lookup ----------------------------------------------
        spec = self._registry.get_spec(command.operation)

        # 3.5 capability lifecycle gate -------------------------------------
        # Session 3: the spec's required_packs finally get checked.  Unknown
        # pack ids, STUB, and RETIRED refuse; EXPERIMENTAL needs the bus-level
        # opt-in.  Runs before permission/input work so a refused pack does
        # no work at all (fail-closed).
        if spec.required_packs:
            check_required_packs(spec.required_packs, allow_experimental=self._allow_experimental)

        # 4. permission gate -----------------------------------------------
        decision = self._registry.check_permission(command.operation, confirmed=command.confirmed)
        if not decision.allowed:
            raise PermissionDeniedError(f"{command.operation}: {decision.reason}")

        # 5. typed input validation ------------------------------------------
        # Validates against the operation's Pydantic input model; this also
        # coerces raw JSON references into typed ReferenceExpr/Playhead values
        # and materializes input defaults (e.g. ``at = "اینجا"``).
        try:
            validated = spec.input_model(**dict(command.input))
        except ValidationError as exc:
            raise CommandValidationError(f"{command.operation}: invalid input: {exc}") from exc

        # 6. pin references at command receipt --------------------------------
        # Every reference field of the (validated) input is resolved against
        # the state snapshot *now*: expressions like "اینجا" or "۵ ثانیه قبل"
        # freeze to an exact timecode_us with captured_at_command=True.
        input_data = validated.model_dump()
        for field_name in spec.reference_fields:
            raw_value: Any = getattr(validated, field_name)
            if raw_value is None:
                continue
            if isinstance(raw_value, Playhead):
                pinned = raw_value.model_copy(update={"captured_at_command": True})
            else:
                pinned = self._resolver.resolve(raw_value, self._project)
            input_data[field_name] = pinned.model_dump()

        # 7. precondition check ----------------------------------------------
        preconditions = command.preconditions
        current_revision = self._project.state_revision
        current_hash = self._project.state_hash
        expected_revision = preconditions.state_revision
        expected_hash = preconditions.state_hash
        if expected_revision is not None and expected_revision != current_revision:
            raise PreconditionError(
                f"stale state_revision: command expects {expected_revision}, "
                f"current is {current_revision}"
            )
        if expected_hash is not None and expected_hash != current_hash:
            raise PreconditionError(
                "stale state_hash: command preconditions do not match the current state"
            )

        # 8. atomic apply -------------------------------------------------------
        context = OperationContext(
            command=command, input_data=input_data, history=tuple(self._history)
        )
        try:
            outcome = spec.handler(self._project, context)
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
        new_project = outcome.project
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
        self._project = new_project
        self._history = [*outcome.history, transaction]

        result = CommandResult(
            transaction_id=transaction.transaction_id,
            state_revision=new_project.state_revision,
            state_hash=new_project.state_hash,
            output=outcome.output,
            diagnostics={
                "executor": "in-memory-reducer",
                "permission_level": spec.permission_level.value,
                "protocol_version": PROTOCOL_VERSION,
            },
            undo_available=True,
        )
        if command.idempotency_key is not None:
            self._idempotency[command.idempotency_key] = result
        return result
