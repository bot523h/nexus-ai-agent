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
4b. **Capability lifecycle / pack gate** (task-183 seam) -- every pack the
   *registry* declares for the operation must resolve to ``AVAILABLE``, or to
   ``EXPERIMENTAL`` with the bus-level ``allow_experimental`` opt-in. Unknown
   pack ids, ``STUB`` and ``RETIRED`` refuse. The gate runs after the actor
   grant (so lifecycle can never grant what authorization denied) and before
   policy, reference pinning, idempotency reservation and the handler (so a
   refused pack performs zero work and leaves state and reservations
   untouched). ``allow_experimental`` is composition-root state, never an
   envelope field -- a client cannot opt itself into experimental packs.
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
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from nexus_ai_agent.creative.studio.authorization import ProjectAccess, ProjectAuthorizer
from nexus_ai_agent.creative.studio.capabilities import (
    ENGINE_PORT_ATTRIBUTES,
    CapabilityRegistry,
    OperationContext,
    OperationSpec,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.lifecycle import check_required_packs
from nexus_ai_agent.creative.studio.models import (
    PROTOCOL_VERSION,
    AuthorizationError,
    CapabilityError,
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


# --------------------------------------------------------------------------- #
# PlanTransaction: atomic multi-step execution with LIFO compensating rollback
# --------------------------------------------------------------------------- #
#
# A ``PlanTransaction`` is an ordered saga of steps. Each step either dispatches
# a typed command through the bus or runs a caller-supplied ``execute`` closure,
# and may carry a ``compensate`` closure that undoes its effect. Execution is
# fail-closed: the first failing step stops forward progress immediately, every
# previously succeeded step is compensated in exact reverse (LIFO) order, and a
# compensation failure never abandons the remaining compensations -- all
# rollback errors are collected and the transaction lands in ``ROLLBACK_FAILED``.
# A finished transaction (``COMMITTED``, ``ROLLED_BACK`` or ``ROLLBACK_FAILED``)
# can never be re-committed or re-rolled-back: the idempotency guard raises.


MAX_COMMITTED_PLANS = 256


class PlanTransactionState(Enum):
    """Explicit state machine for a :class:`PlanTransaction`."""

    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


_FINISHED_PLAN_STATES = frozenset(
    {
        PlanTransactionState.COMMITTED,
        PlanTransactionState.ROLLED_BACK,
        PlanTransactionState.ROLLBACK_FAILED,
    }
)


class PlanTransactionError(CommandExecutionError):
    """A plan step failed; the transaction records the rollback outcome."""

    def __init__(
        self,
        message: str,
        *,
        plan_id: str = "",
        failed_step_index: int | None = None,
        original_error: BaseException | None = None,
        rollback_errors: tuple[BaseException, ...] = (),
    ) -> None:
        super().__init__(message)
        self.plan_id = plan_id
        self.failed_step_index = failed_step_index
        self.original_error = original_error
        self.rollback_errors = rollback_errors


class PlanRollbackError(PlanTransactionError):
    """A plan step failed *and* at least one compensation also failed."""


def _canonical_hash(payload: Any) -> str:
    """SHA-256 hex digest of the canonical JSON form of ``payload``.

    Non-JSON-native values fall back to ``repr`` so hashing never raises; the
    digest is still deterministic for a given payload within a process.
    """
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError):
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _code_evidence(code: Any) -> Any:
    """Hash bytecode/constants without importing a serialization runtime."""
    if not hasattr(code, "co_code"):
        return code
    return {
        "bytecode": code.co_code.hex(),
        "names": code.co_names,
        "constants": [_code_evidence(value) for value in code.co_consts],
    }


def _clone_plan_snapshot(source: Mapping[str, Any]) -> dict[str, Any]:
    """Copy the three typed state containers, including cached result models."""
    return {
        "_project": source["_project"].model_copy(deep=True),
        "_history": [record.model_copy(deep=True) for record in source["_history"]],
        "_idempotency": {
            key: _Reservation(
                value.fingerprint, value.result.model_copy(deep=True) if value.result else None
            )
            for key, value in source["_idempotency"].items()
        },
    }


@dataclass(frozen=True)
class PlanStepRecord:
    """Immutable audit record for one executed plan step."""

    step_index: int
    capability_name: str
    status: str  # "succeeded" | "failed"
    input_hash: str
    output_hash: str


@dataclass(frozen=True)
class PlanRollbackRecord:
    """Immutable audit record for one compensated plan step."""

    step_index: int
    capability_name: str
    status: str  # "succeeded" | "failed"
    input_hash: str
    output_hash: str


@dataclass(frozen=True)
class PlanStep:
    """One step of a :class:`PlanTransaction`.

    Exactly one of ``command`` (dispatched through the bus) or ``execute``
    (a caller closure) must be supplied. ``compensate`` is the rollback
    closure invoked with the step's result when a later step fails; it is
    optional for internal bus-only commands. Custom closures require one by
    default. ``rollback_on_self_error`` opts into emergency compensation of a
    partially executed failing step; it receives None when no result exists.
    Internal bus state is always restored from the pre-plan snapshot on failure.
    """

    operation: str
    command: TypedCommand | dict[str, Any] | str | bytes | None = None
    execute: Callable[[], Any] | None = None
    compensate: Callable[[Any], Any] | None = None
    rollback_on_self_error: bool = False

    def __post_init__(self) -> None:
        if not self.operation:
            raise PlanTransactionError("plan step requires a non-empty operation name")
        if (self.command is None) == (self.execute is None):
            raise PlanTransactionError(
                f"plan step {self.operation!r} must supply exactly one of command or execute"
            )
        if self.command is not None and _parse(self.command).operation != self.operation:
            raise ValueError("step operation does not match command.operation")


@dataclass
class PlanTransaction:
    """An ordered multi-step transaction with compensating rollback.

    ``commit`` runs every step in order. The first step that raises or returns
    an unsuccessful result (``False`` from an ``execute`` closure) stops forward
    execution immediately; all previously succeeded steps are then compensated
    in exact reverse (LIFO) order. Compensation failures are collected without
    abandoning the remaining compensations and move the transaction to
    ``ROLLBACK_FAILED``; a clean rollback lands in ``ROLLED_BACK``. Full success
    lands in ``COMMITTED``. Re-committing or re-rolling-back a finished
    transaction raises :class:`PlanTransactionError`.
    """

    plan_id: str = field(default_factory=lambda: uuid4().hex)
    steps: tuple[PlanStep, ...] = ()
    require_compensation: bool = True
    _snapshot: dict[str, Any] | None = field(default=None, repr=False)
    _bus: CommandBus | None = field(default=None, repr=False)
    state: PlanTransactionState = PlanTransactionState.PENDING
    step_records: list[PlanStepRecord] = field(default_factory=list)
    rollback_records: list[PlanRollbackRecord] = field(default_factory=list)
    _committed_hash: str = field(default="", repr=False)
    _committed_revision: int = field(default=-1, repr=False)
    _committed_history_len: int = field(default=0, repr=False)
    _committed_last_id: str | None = field(default=None, repr=False)
    _step_results: dict[int, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise ValueError("plan_id must be a non-empty string")
        if self.require_compensation and any(
            step.execute is not None and step.compensate is None for step in self.steps
        ):
            raise ValueError("custom steps require compensation")
        if not self.steps:
            raise PlanTransactionError("plan transaction requires at least one step")

    # -- execution ------------------------------------------------------- #

    def commit(self, bus: CommandBus | None = None) -> PlanTransaction:
        """Execute under the bus lock, retaining its complete pre-plan snapshot."""
        if bus is None:
            return self._commit_steps(None)
        with bus._lock:
            if bus._executing:
                raise CommandExecutionError("Re-entrant execution is forbidden")
            if self.state is not PlanTransactionState.PENDING:
                raise PlanTransactionError("transaction already finished")
            fingerprint = _canonical_hash(
                [self._step_input_hash(i, step) for i, step in enumerate(self.steps)]
            )
            prior = bus._plan_cache.get(self.plan_id) if self.plan_id else None
            if prior is not None:
                if prior[0] != fingerprint:
                    raise ValueError("plan_id input conflict")
                bus._plan_cache.move_to_end(self.plan_id)
                return prior[1]
            self._snapshot = _clone_plan_snapshot(vars(bus))
            self._bus = bus
            bus._executing = True
            try:
                result = self._commit_steps(bus)
                self._committed_hash = bus.state_hash
                self._committed_revision = bus.state_revision
                self._committed_history_len = len(bus._history)
                self._committed_last_id = bus._history[-1].transaction_id if bus._history else None
                if self.plan_id:
                    bus._plan_cache[self.plan_id] = (fingerprint, self)
                    while len(bus._plan_cache) > MAX_COMMITTED_PLANS:
                        bus._plan_cache.popitem(last=False)
                return result
            except BaseException:
                self._restore_snapshot()
                raise
            finally:
                bus._executing = False

    def _restore_snapshot(self) -> None:
        if self._bus is not None and self._snapshot is not None:
            for name, value in _clone_plan_snapshot(self._snapshot).items():
                setattr(self._bus, name, value)

    def _commit_steps(self, bus: CommandBus | None) -> PlanTransaction:
        """Forward execution and compensation while the outer lock is held."""
        if self.state in _FINISHED_PLAN_STATES:
            raise PlanTransactionError(
                f"plan {self.plan_id!r} already finished in state {self.state.value}; "
                "re-commit is forbidden",
                plan_id=self.plan_id,
            )
        if self.state in (PlanTransactionState.EXECUTING, PlanTransactionState.ROLLING_BACK):
            raise PlanTransactionError(
                f"plan {self.plan_id!r} is already {self.state.value}; "
                "re-entrant commit is forbidden",
                plan_id=self.plan_id,
            )
        self.state = PlanTransactionState.EXECUTING
        failed_index: int | None = None
        failed_error: BaseException | None = None
        for index, step in enumerate(self.steps):
            input_hash = self._step_input_hash(index, step)
            try:
                result = self._execute_step(step, bus)
                if result is False:
                    raise PlanTransactionError(
                        f"plan step {index} ({step.operation!r}) returned an unsuccessful result",
                        plan_id=self.plan_id,
                        failed_step_index=index,
                    )
            except BaseException as exc:  # noqa: BLE001 - any step failure rolls back
                failed_index = index
                failed_error = exc
                self.step_records.append(
                    PlanStepRecord(
                        step_index=index,
                        capability_name=step.operation,
                        status="failed",
                        input_hash=input_hash,
                        output_hash=_canonical_hash(f"{type(exc).__name__}: {exc}"),
                    )
                )
                break
            self._step_results[index] = result
            self.step_records.append(
                PlanStepRecord(
                    step_index=index,
                    capability_name=step.operation,
                    status="succeeded",
                    input_hash=input_hash,
                    output_hash=_canonical_hash(result),
                )
            )
        if failed_index is None:
            self.state = PlanTransactionState.COMMITTED
            return self
        # Fail-closed: stop forward progress and compensate in exact LIFO order.
        self.state = PlanTransactionState.ROLLING_BACK
        rollback_errors = self._run_compensations()
        if rollback_errors:
            self.state = PlanTransactionState.ROLLBACK_FAILED
            raise PlanRollbackError(
                f"plan {self.plan_id!r} step {failed_index} failed and "
                f"{len(rollback_errors)} compensation(s) also failed; "
                "transaction is ROLLBACK_FAILED",
                plan_id=self.plan_id,
                failed_step_index=failed_index,
                original_error=failed_error,
                rollback_errors=tuple(rollback_errors),
            ) from failed_error
        self.state = PlanTransactionState.ROLLED_BACK
        raise PlanTransactionError(
            f"plan {self.plan_id!r} step {failed_index} failed; "
            "all prior steps were rolled back (ROLLED_BACK)",
            plan_id=self.plan_id,
            failed_step_index=failed_index,
            original_error=failed_error,
        ) from failed_error

    def rollback(self) -> PlanTransaction:
        """Compensate every succeeded step in LIFO order (manual rollback)."""
        if self.state in _FINISHED_PLAN_STATES:
            raise PlanTransactionError(
                f"plan {self.plan_id!r} already finished in state {self.state.value}; "
                "re-rollback is forbidden",
                plan_id=self.plan_id,
            )
        if self.state == PlanTransactionState.ROLLING_BACK:
            raise PlanTransactionError(
                f"plan {self.plan_id!r} is already ROLLING_BACK; re-entrant rollback is forbidden",
                plan_id=self.plan_id,
            )
        self.state = PlanTransactionState.ROLLING_BACK
        rollback_errors = self._run_compensations()
        if rollback_errors:
            self.state = PlanTransactionState.ROLLBACK_FAILED
            raise PlanRollbackError(
                f"plan {self.plan_id!r} rollback failed with "
                f"{len(rollback_errors)} compensation error(s); transaction is ROLLBACK_FAILED",
                plan_id=self.plan_id,
                rollback_errors=tuple(rollback_errors),
            )
        self.state = PlanTransactionState.ROLLED_BACK
        return self

    def rollback_committed_plan(self) -> PlanTransaction:
        """Undo a committed plan only if no subsequent bus write intervened."""
        bus = self._bus
        if bus is None:
            raise PlanTransactionError("committed undo requires a bus")
        with bus._lock:
            if bus._executing:
                raise CommandExecutionError("Re-entrant execution is forbidden")
            if self.state is not PlanTransactionState.COMMITTED:
                raise PlanTransactionError("plan is not committed")
            if (
                bus.state_hash != self._committed_hash
                or bus.state_revision != self._committed_revision
                or len(bus._history) != self._committed_history_len
                or (bus._history[-1].transaction_id if bus._history else None)
                != self._committed_last_id
            ):
                raise PlanTransactionError(
                    "bus changed after plan commit (ABA or revision drift detected)"
                )
            bus._executing = True
            try:
                self.state = PlanTransactionState.ROLLING_BACK
                errors = self._run_compensations()
                self._restore_snapshot()
                bus._plan_cache.pop(self.plan_id, None)
                self.state = (
                    PlanTransactionState.ROLLBACK_FAILED
                    if errors
                    else PlanTransactionState.ROLLED_BACK
                )
                if errors:
                    raise PlanRollbackError("committed undo failed", rollback_errors=tuple(errors))
                return self
            finally:
                bus._executing = False

    # -- internals ------------------------------------------------------- #

    def _step_input_hash(self, index: int, step: PlanStep) -> str:
        if step.command is not None:
            return _fingerprint(_parse(step.command))
        fn = step.execute
        code = getattr(fn, "__code__", None)
        cells = getattr(fn, "__closure__", None) or ()
        return _canonical_hash(
            {
                "operation": step.operation,
                "step_index": index,
                "code": _code_evidence(code) if code else str(type(fn)),
                "defaults": getattr(fn, "__defaults__", None),
                "closure": [cell.cell_contents for cell in cells],
            }
        )

    def _execute_step(self, step: PlanStep, bus: CommandBus | None) -> Any:
        if step.execute is not None:
            return step.execute()
        if bus is None:
            raise PlanTransactionError(
                f"plan step {step.operation!r} carries a command but no bus was supplied",
                plan_id=self.plan_id,
            )
        parsed = _parse(step.command)  # type: ignore[arg-type]
        return bus._dispatch_locked(parsed)

    def _run_compensations(self) -> list[BaseException]:
        """Compensate succeeded steps in exact LIFO order; collect all errors."""
        errors: list[BaseException] = []
        # The failing step may have external partial effects. Opt in explicitly
        # to emergency compensation (its result is None if execution raised).
        succeeded = [
            r
            for r in self.step_records
            if r.status == "succeeded"
            or (r.status == "failed" and self.steps[r.step_index].rollback_on_self_error)
        ]
        for record in reversed(succeeded):
            step = self.steps[record.step_index]
            if step.compensate is None:
                if step.execute is not None:
                    errors.append(ValueError(f"uncompensated step {record.step_index}"))
                continue
            try:
                compensation_result = step.compensate(self._step_results.get(record.step_index))
            except BaseException as exc:  # noqa: BLE001 - collect, never abandon
                errors.append(exc)
                self.rollback_records.append(
                    PlanRollbackRecord(
                        step_index=record.step_index,
                        capability_name=step.operation,
                        status="failed",
                        input_hash=record.input_hash,
                        output_hash=_canonical_hash(f"{type(exc).__name__}: {exc}"),
                    )
                )
            else:
                self.rollback_records.append(
                    PlanRollbackRecord(
                        step_index=record.step_index,
                        capability_name=step.operation,
                        status="succeeded",
                        input_hash=record.input_hash,
                        output_hash=_canonical_hash(compensation_result),
                    )
                )
        return errors


class CommandBus:
    """Validates typed commands and applies them atomically to central state."""

    def __init__(
        self,
        state: Project,
        registry: CapabilityRegistry | None = None,
        resolver: ReferenceResolver | None = None,
        *,
        authorizer: ProjectAuthorizer | None = None,
        allow_experimental: bool = False,
        audio_engine: Any = None,
        engine_ports: Mapping[str, Any] | None = None,
    ) -> None:
        self._registry = registry if registry is not None else build_wave1_registry()
        self._resolver = resolver if resolver is not None else ReferenceResolver()
        self._authorizer = authorizer
        # Composition-root execution seam for media operations (typed ``Any``
        # for the same reason as ``OperationContext.audio_engine``).  The bus
        # only carries it to the handler; it never inspects it, so a command can
        # neither supply nor replace an engine.
        self._audio_engine = audio_engine
        # Wave 5: the five NAGAR engine seams, keyed by the *operation context
        # attribute* they populate. Unknown keys are a composition error, not a
        # silently ignored hint — a misspelt port would otherwise present as an
        # engine that is permanently "not composed".
        unknown = set(engine_ports or {}) - set(ENGINE_PORT_ATTRIBUTES.values())
        if unknown:
            raise CapabilityError(f"unknown engine port(s): {', '.join(sorted(unknown))}")
        self._engine_ports: dict[str, Any] = dict(engine_ports or {})
        # Trusted composition-root opt-in for EXPERIMENTAL packs. Deliberately
        # not an envelope field: a command must never widen its own lifecycle.
        self._allow_experimental = allow_experimental
        self._lock = threading.RLock()
        # model_copy(update=...) bypasses Pydantic's derived state_hash and
        # validation (the worker uses it when registering its staged asset).
        # Never let a stale/fabricated hash become an optimistic precondition.
        self._project = Project.model_validate(state.model_dump(mode="json"))
        self._history: list[EditTransaction] = []
        self._idempotency: dict[tuple[str, str, str], _Reservation] = {}
        self._executing = False
        self._plan_cache: OrderedDict[str, tuple[str, PlanTransaction]] = OrderedDict()

    @property
    def project(self) -> Project:
        """A deep copy of the central state (safe to read without the lock)."""
        with self._lock:
            return self._project.model_copy(deep=True)

    @property
    def registry(self) -> CapabilityRegistry:
        """The authoritative operation allow-list this bus enforces.

        Exposed read-only so an upstream planner (e.g. the creative spine) can
        compile against the *same* registry the bus validates, instead of a
        second copy that could drift.
        """
        return self._registry

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

    def dispatch_plan(self, plan: PlanTransaction) -> PlanTransaction:
        """Execute a :class:`PlanTransaction` atomically under the bus lock.

        Steps run in order; the first failure stops forward progress and every
        previously succeeded step is compensated in exact LIFO order. The
        transaction's state machine (``COMMITTED`` / ``ROLLED_BACK`` /
        ``ROLLBACK_FAILED``) and its step/rollback records carry the outcome.
        """
        if not isinstance(plan, PlanTransaction):
            raise CommandValidationError("dispatch_plan requires a PlanTransaction")
        with self._lock:
            if self._executing:
                raise CommandExecutionError("nested plan dispatch during execution is forbidden")
            return self._dispatch_plan_locked(plan)

    def _dispatch_plan_locked(self, plan: PlanTransaction) -> PlanTransaction:
        """Transactional plan execution (see :meth:`dispatch_plan`)."""
        return plan.commit(self)

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

        # 4b. Capability lifecycle / pack gate (PR#67 implementation, task-183
        # seam). The registry -- never the client -- names the required packs.
        # Runs after the actor grant and before policy/refs/reservation/handler.
        if descriptor.required_packs:
            check_required_packs(
                descriptor.required_packs, allow_experimental=self._allow_experimental
            )

        # 4c. Opt-in-only capabilities that no pack speaks for (the NAGAR engine
        # verifications): the same composition-root policy, stated explicitly so
        # "registered" never quietly means "reachable".
        if descriptor.requires_opt_in and not self._allow_experimental:
            raise ExecutionPolicyError(
                f"{command.operation}: requires the composition-root experimental opt-in"
            )

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
        previous = self._executing
        self._executing = True
        try:
            return self._apply(command, spec, input_data)
        finally:
            self._executing = previous

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
        # ``audio_engine`` is both the OMEGA port and the legacy constructor
        # argument, so an explicitly supplied engine wins over any port entry
        # (a caller cannot pass both and have one silently shadow the other).
        engine_kwargs: dict[str, Any] = {"audio_engine": self._audio_engine, **self._engine_ports}
        context = OperationContext(
            command=command,
            input_data=input_data,
            history=tuple(self._history),
            **engine_kwargs,
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
                # Preview/master boundary stamp (contract doc §13): every result
                # carries the mode it was executed under and whether that
                # execution is master-authoritative for this operation. A
                # non-authoritative result can never be master evidence.
                "execution_mode": command.execution_policy.mode,
                "authoritative": spec.authoritative_for(command.execution_policy.mode),
            },
            undo_available=True,
        )
        # All potentially failing validation/construction is complete before
        # either central state or the reservation's result is committed.
        self._project = new_project
        self._history = [*outcome.history, transaction]
        return result
