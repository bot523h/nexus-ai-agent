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
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol, TypedDict, cast
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
from nexus_ai_agent.creative.studio.durable_dedupe import (
    ApprovalBinding,
    ApprovalBindingError,
    ApprovalVerifier,
    DurablePlanDedupePort,
    PlanApprovalRequirement,
    PlanInDoubtQuarantineError,
    PlanStoreStateError,
    RecoveryEvidence,
    verify_approval_binding,
)
from nexus_ai_agent.creative.studio.lifecycle import check_required_packs
from nexus_ai_agent.creative.studio.models import (
    PROTOCOL_VERSION,
    ActorIdentity,
    AuthorizationError,
    CapabilityError,
    CommandExecutionError,
    CommandResult,
    CommandValidationError,
    EditTransaction,
    ExecutionPolicyError,
    IdempotencyConflictError,
    NagarError,
    PermissionLevel,
    Playhead,
    PreconditionError,
    Preconditions,
    Project,
    TypedCommand,
    compute_state_hash,
)
from nexus_ai_agent.creative.studio.plan_identifiers import (
    derive_effect_key,
    validate_plan_identity,
)
from nexus_ai_agent.creative.studio.references import ReferenceResolver


class _DurableFenceArgs(TypedDict):
    project_id: str
    attempt: int
    owner_token: str


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
MAX_PLAN_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_PLAN_SUMMARIES = 4096


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
    """Total audit hash: ASCII escaping preserves lone surrogates safely.

    Audit evidence must never abort compensation, even for hostile result
    objects. Replay identity uses the stricter callable evidence below.
    """
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
    except Exception:
        canonical = json.dumps({"unserializable_type": type(payload).__qualname__})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _durable_hash(payload: Any) -> str:
    """Hash only actual, finite JSON; never turn malformed evidence into a receipt."""
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _raise_control_error(original: BaseException | None, errors: list[BaseException]) -> None:
    for error in [original, *errors]:
        if error is not None and not isinstance(error, Exception):
            raise error


def _failed_boolean(result: Any) -> bool:
    # Keep Studio dependency-free; NumPy bool scalars expose these stable type
    # identifiers in both NumPy 1.x (bool_) and 2.x (bool).
    boolean = isinstance(result, bool) or (
        type(result).__module__ == "numpy" and type(result).__name__ in ("bool", "bool_")
    )
    return boolean and not bool(result)


def _code_evidence(code: Any) -> Any:
    """Hash bytecode/constants without importing a serialization runtime."""
    if not hasattr(code, "co_code"):
        return code
    return {
        "bytecode": code.co_code.hex(),
        "names": code.co_names,
        "constants": [_code_evidence(value) for value in code.co_consts],
    }


def _stable_value(value: Any, seen: frozenset[int]) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        # JSON rejects non-finite floats: those callbacks cannot auto-replay.
        try:
            json.dumps(value, allow_nan=False)
            return value
        except ValueError:
            return {"opaque_replay_state": "nonfinite"}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if id(value) in seen:
        return {"opaque_replay_state": "cycle"}
    visited = seen | {id(value)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item, visited) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _stable_value(item, visited) for key, item in value.items()}
    if callable(value):
        return _callable_evidence(value, seen)
    return {"opaque_replay_state": type(value).__qualname__}


def _callable_evidence(fn: Callable[..., Any] | None, seen: frozenset[int] = frozenset()) -> Any:
    if fn is None:
        return None
    if id(fn) in seen:
        return {"opaque_replay_state": "recursive callable"}
    visited = seen | {id(fn)}
    if type(fn).__module__ == "functools" and type(fn).__name__ == "partial":
        return {
            "partial": _callable_evidence(cast(Any, fn).func, visited),
            "args": _stable_value(cast(Any, fn).args, visited),
            "keywords": _stable_value(cast(Any, fn).keywords, visited),
        }
    code = getattr(fn, "__code__", None)
    if code is None:
        if getattr(fn, "__module__", None) == "builtins":
            owner = getattr(fn, "__self__", None)
            return {
                "builtin": getattr(fn, "__qualname__", type(fn).__qualname__),
                "owner": _stable_value(owner, visited)
                if owner is not None and not isinstance(owner, type(json))
                else None,
            }
        if not isinstance(fn, type) and hasattr(fn, "__dict__"):
            return {
                "instance": type(fn).__qualname__,
                "state": _stable_value(vars(fn), visited),
                "call": _callable_evidence(type(fn).__call__, visited),
            }
        return {"opaque_replay_state": type(fn).__qualname__}
    cells = getattr(fn, "__closure__", None) or ()
    globals_ = getattr(fn, "__globals__", {})
    owner = getattr(fn, "__self__", None)
    return {
        "code": _code_evidence(code),
        "defaults": _stable_value(getattr(fn, "__defaults__", None), visited),
        "kwdefaults": _stable_value(getattr(fn, "__kwdefaults__", None), visited),
        "closure": [_stable_value(cell.cell_contents, visited) for cell in cells],
        "owner": (
            _stable_value(vars(owner), visited)
            if hasattr(owner, "__dict__")
            else _stable_value(owner, visited)
        ),
        "globals": {
            name: _stable_value(globals_[name], visited)
            for name in code.co_names
            if name in globals_
        },
    }


def _replay_safe(value: Any) -> bool:
    if isinstance(value, dict):
        return "opaque_replay_state" not in value and all(_replay_safe(v) for v in value.values())
    if isinstance(value, list):
        return all(_replay_safe(v) for v in value)
    return True


def _clone_plan_snapshot(source: Mapping[str, Any]) -> dict[str, Any]:
    """Share immutable serialized history across live snapshots, never live models."""
    intern = {
        raw: raw
        for _, plan in source.get("_plan_cache", {}).values()
        if plan._snapshot
        for raw in plan._snapshot["_history"]
    }
    history = []
    for record in source["_history"]:
        raw = record if isinstance(record, bytes) else record.model_dump_json().encode("utf-8")
        history.append(intern.get(raw, raw))
    return {
        "_project": source["_project"].model_copy(deep=True),
        "_history": tuple(history),
        "_idempotency": {
            key: _Reservation(
                value.fingerprint, value.result.model_copy(deep=True) if value.result else None
            )
            for key, value in source["_idempotency"].items()
        },
    }


def _snapshot_cache_bytes(cache: Mapping[str, tuple[str, Any]]) -> int:
    history: set[bytes] = set()
    total = 0
    for _, plan in cache.values():
        snapshot = plan._snapshot
        if snapshot is not None:
            history.update(snapshot["_history"])
            total += len(snapshot["_project"].model_dump_json().encode("utf-8"))
            total += 64 + 8 * len(snapshot["_history"])
            total += sum(
                128 + len(value.result.model_dump_json().encode("utf-8"))
                for value in snapshot["_idempotency"].values()
                if value.result
            )
    return total + sum(64 + len(raw) for raw in history)


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
    external_effects: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or not self.operation:
            raise PlanTransactionError("plan step requires a non-empty operation name")
        if (self.command is None) == (self.execute is None):
            raise PlanTransactionError(
                f"plan step {self.operation!r} must supply exactly one of command or execute"
            )
        if self.compensate is not None and not callable(self.compensate):
            raise ValueError("compensate must be callable")
        if self.external_effects and self.compensate is None:
            raise ValueError("external effects require compensation")
        if self.command is not None:
            parsed = _parse(self.command)
            if parsed.operation != self.operation:
                raise ValueError("step operation does not match command.operation")
            # Immutable transport owns a validated deep copy of nested payloads.
            object.__setattr__(self, "command", parsed.model_dump_json())


@dataclass(frozen=True)
class PlanResultSummary:
    """Lightweight redelivery tombstone: no snapshots, callbacks or bus references."""

    plan_id: str
    state: PlanTransactionState
    step_records: tuple[PlanStepRecord, ...]
    committed_revision: int
    replay_safe: bool = True


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
    admitted_ir_hash: str | None = None
    _snapshot: dict[str, Any] | None = field(default=None, repr=False)
    _bus: CommandBus | None = field(default=None, repr=False)
    state: PlanTransactionState = PlanTransactionState.PENDING
    _step_records: list[PlanStepRecord] = field(default_factory=list, init=False, repr=False)
    _rollback_records: list[PlanRollbackRecord] = field(
        default_factory=list, init=False, repr=False
    )
    _accepted_callbacks: tuple[str, ...] = field(default=(), init=False, repr=False)
    _external_step_indices: set[int] = field(default_factory=set, init=False, repr=False)
    _started_steps: set[int] = field(default_factory=set, init=False, repr=False)
    _replay_safe: bool = field(default=True, init=False, repr=False)
    _commit_epoch: int = field(default=-1, init=False, repr=False)
    _committed_hash: str = field(default="", repr=False)
    _committed_revision: int = field(default=-1, repr=False)
    _committed_history_len: int = field(default=0, repr=False)
    _committed_last_id: str | None = field(default=None, repr=False)
    _step_results: dict[int, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        validate_plan_identity(self.plan_id, name="plan_id")
        if self.require_compensation and any(
            step.execute is not None and step.compensate is None for step in self.steps
        ):
            raise ValueError("custom steps require compensation")
        if not self.steps:
            raise PlanTransactionError("plan transaction requires at least one step")

    @property
    def step_records(self) -> tuple[PlanStepRecord, ...]:
        return tuple(self._step_records)

    @property
    def rollback_records(self) -> tuple[PlanRollbackRecord, ...]:
        return tuple(self._rollback_records)

    @staticmethod
    def _callback_hash(step: PlanStep) -> str:
        return _canonical_hash(
            {
                "execute": _callable_evidence(step.execute),
                "compensate": _callable_evidence(step.compensate),
            }
        )

    def _preflight(self, bus: CommandBus | None) -> None:
        """Accept callback state and authoritative external-effect declarations.

        This runs before any forward effect and, for attached plans, under the
        same bus lock as execution. Opaque evidence cannot authorize replay.
        """
        self._external_step_indices.clear()
        for index, step in enumerate(self.steps):
            external = step.external_effects
            if bus is not None and step.command is not None:
                spec = bus.registry.get_spec(_parse(step.command).operation)
                external = (
                    external
                    or spec.external_effects
                    or bool(getattr(spec.handler, "external_effects", False))
                )
            if external:
                if not callable(step.compensate):
                    raise PlanTransactionError(
                        f"plan step {index} external effects require callable compensation"
                    )
                self._external_step_indices.add(index)
        self._accepted_callbacks = tuple(self._callback_hash(step) for step in self.steps)

    # -- execution ------------------------------------------------------- #

    def commit(self, bus: CommandBus | None = None) -> PlanTransaction | PlanResultSummary:
        """Execute under the bus lock, retaining its complete pre-plan snapshot."""
        if bus is None:
            return self._commit_steps(None)
        with bus._lock:
            if bus._executing:
                raise CommandExecutionError("Re-entrant execution is forbidden")
            if bus._durable_store is not None:
                raise PlanTransactionError(
                    "durable store configured; use bus.dispatch_plan with durable approvals"
                )
            if self.state is not PlanTransactionState.PENDING:
                raise PlanTransactionError("transaction already finished")
            self._preflight(bus)
            fingerprint = _canonical_hash(
                [self._step_input_hash(i, step) for i, step in enumerate(self.steps)]
            )
            prior = bus._plan_summaries.get(self.plan_id)
            if prior is not None:
                for step in self.steps:
                    if step.command is not None:
                        bus._validate_dispatch(_parse(step.command))
            if prior is not None:
                if not self._replay_safe or not prior[1].replay_safe:
                    raise PlanTransactionError(
                        "automatic replay unavailable for opaque callback state"
                    )
                if prior[0] != fingerprint:
                    raise ValueError("plan_id input conflict")
                bus._plan_summaries.move_to_end(self.plan_id)
                cached = bus._plan_cache.get(self.plan_id)
                if cached is not None:
                    bus._plan_cache.move_to_end(self.plan_id)
                    return cached[1]
                return prior[1]
            self._snapshot = _clone_plan_snapshot(vars(bus))
            self._bus = bus
            bus._executing = True
            try:
                result = self._commit_steps(bus)
                bus._plan_commit_epoch += 1
                self._commit_epoch = bus._plan_commit_epoch
                self._committed_hash = bus.state_hash
                self._committed_revision = bus.state_revision
                self._committed_history_len = len(bus._history)
                self._committed_last_id = bus._history[-1].transaction_id if bus._history else None
                bus._plan_cache[self.plan_id] = (fingerprint, self)
                bus._plan_summaries[self.plan_id] = (
                    fingerprint,
                    PlanResultSummary(
                        self.plan_id,
                        self.state,
                        tuple(self.step_records),
                        self._committed_revision,
                        self._replay_safe,
                    ),
                )
                while (
                    len(bus._plan_cache) > MAX_COMMITTED_PLANS
                    or _snapshot_cache_bytes(bus._plan_cache) > MAX_PLAN_SNAPSHOT_BYTES
                ):
                    _, (_, evicted) = bus._plan_cache.popitem(last=False)
                    evicted._snapshot = None
                    evicted._step_results.clear()
                while len(bus._plan_summaries) > MAX_PLAN_SUMMARIES:
                    bus._plan_summaries.popitem(last=False)
                return result
            except BaseException:
                self._restore_snapshot()
                raise
            finally:
                bus._executing = False

    def _restore_snapshot(self) -> None:
        if self._bus is not None and self._snapshot is not None:
            for name, value in _clone_plan_snapshot(self._snapshot).items():
                if name == "_history":
                    value = [EditTransaction.model_validate_json(raw) for raw in value]
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
        if not self._accepted_callbacks:
            self._preflight(bus)
        self.state = PlanTransactionState.EXECUTING
        failed_index: int | None = None
        failed_error: BaseException | None = None
        for index, step in enumerate(self.steps):
            input_hash = ""
            try:
                input_hash = self._step_input_hash(index, step)
                if self._callback_hash(step) != self._accepted_callbacks[index]:
                    raise PlanTransactionError(
                        f"plan step {index} callback state changed after preflight (TOCTOU)"
                    )
                self._started_steps.add(index)
                result = self._execute_step(step, bus)
                self._step_results[index] = result
                output_hash = _canonical_hash(result)
                if _failed_boolean(result):
                    raise PlanTransactionError(
                        f"plan step {index} ({step.operation!r}) returned an unsuccessful result",
                        plan_id=self.plan_id,
                        failed_step_index=index,
                    )
            except BaseException as exc:  # noqa: BLE001 - any step failure rolls back
                failed_index = index
                failed_error = exc
                self._step_records.append(
                    PlanStepRecord(
                        step_index=index,
                        capability_name=step.operation,
                        status="failed",
                        input_hash=input_hash,
                        output_hash=_canonical_hash({"error_type": type(exc).__qualname__}),
                    )
                )
                break
            self._step_records.append(
                PlanStepRecord(
                    step_index=index,
                    capability_name=step.operation,
                    status="succeeded",
                    input_hash=input_hash,
                    output_hash=output_hash,
                )
            )
        if failed_index is None:
            self.state = PlanTransactionState.COMMITTED
            return self
        # Fail-closed: stop forward progress and compensate in exact LIFO order.
        self.state = PlanTransactionState.ROLLING_BACK
        rollback_errors = self._run_compensations()
        self.state = (
            PlanTransactionState.ROLLBACK_FAILED
            if rollback_errors
            else PlanTransactionState.ROLLED_BACK
        )
        _raise_control_error(failed_error, rollback_errors)
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
        """Only an idle pending plan may be rolled back through this entry point."""
        if self._bus is None:
            return self._rollback_pending()
        with self._bus._lock:
            if self._bus._executing:
                raise PlanTransactionError("Re-entrant execution: rollback during execution")
            return self._rollback_pending()

    def _rollback_pending(self) -> PlanTransaction:
        """Manual pending rollback, called under the bus lock when attached."""
        if self.state in _FINISHED_PLAN_STATES:
            raise PlanTransactionError(
                f"plan {self.plan_id!r} already finished in state {self.state.value}; "
                "re-rollback is forbidden",
                plan_id=self.plan_id,
            )
        if self.state in (PlanTransactionState.EXECUTING, PlanTransactionState.ROLLING_BACK):
            raise PlanTransactionError(
                f"plan {self.plan_id!r} is already ROLLING_BACK; re-entrant rollback is forbidden",
                plan_id=self.plan_id,
            )
        self.state = PlanTransactionState.ROLLING_BACK
        rollback_errors = self._run_compensations()
        self.state = (
            PlanTransactionState.ROLLBACK_FAILED
            if rollback_errors
            else PlanTransactionState.ROLLED_BACK
        )
        _raise_control_error(None, rollback_errors)
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

    def rollback_committed_plan(
        self, authorization: TypedCommand | dict[str, Any] | str | bytes | None = None
    ) -> PlanTransaction:
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
                or bus._plan_commit_epoch != self._commit_epoch
                or bus.state_revision != self._committed_revision
                or len(bus._history) != self._committed_history_len
                or (bus._history[-1].transaction_id if bus._history else None)
                != self._committed_last_id
            ):
                raise PlanTransactionError(
                    "bus changed after plan commit (ABA or revision drift detected)"
                )
            if authorization is not None:
                bus._validate_dispatch(_parse(authorization))
            elif not any(step.command is not None for step in self.steps) and bus._authorizer:
                raise AuthorizationError("custom undo requires current authorization command")
            for step in self.steps:
                if step.command is not None:
                    bus._validate_dispatch(_parse(step.command))
            if self._snapshot is None:
                raise PlanTransactionError("rollback snapshot evicted; undo is unavailable")
            bus._executing = True
            try:
                self.state = PlanTransactionState.ROLLING_BACK
                errors = self._run_compensations()
                before = bus._project.model_copy(deep=True)
                before_history = list(bus._history)
                self._restore_snapshot()
                restored = bus._project.model_dump(mode="json")
                restored["state_revision"] = before.state_revision + 1
                bus._project = Project.model_validate(restored)
                bus._history = [
                    *before_history,
                    EditTransaction(
                        transaction_id=uuid4().hex,
                        command_id=self.plan_id,
                        operation="plan.rollback_committed",
                        permission_level=PermissionLevel.IMMEDIATE,
                        parent_revision=before.state_revision,
                        previous_state_hash=before.state_hash,
                        new_state_hash=bus.state_hash,
                        state_before=before.model_dump(mode="json"),
                    ),
                ]
                bus._plan_cache.pop(self.plan_id, None)
                self.state = (
                    PlanTransactionState.ROLLBACK_FAILED
                    if errors
                    else PlanTransactionState.ROLLED_BACK
                )
                bus._plan_summaries[self.plan_id] = (
                    bus._plan_summaries[self.plan_id][0],
                    PlanResultSummary(
                        self.plan_id,
                        self.state,
                        tuple(self.step_records),
                        bus.state_revision,
                        self._replay_safe,
                    ),
                )
                _raise_control_error(None, errors)
                if errors:
                    raise PlanRollbackError("committed undo failed", rollback_errors=tuple(errors))
                return self
            finally:
                bus._executing = False

    # -- internals ------------------------------------------------------- #

    def _step_input_hash(self, index: int, step: PlanStep) -> str:
        evidence = {
            "operation": step.operation,
            "step_index": index,
            "command": _fingerprint(_parse(step.command)) if step.command is not None else None,
            "execute": _callable_evidence(step.execute),
            "compensate": _callable_evidence(step.compensate),
            "rollback_on_self_error": step.rollback_on_self_error,
            "external_effects": step.external_effects,
        }
        if self.admitted_ir_hash is not None:
            if (
                not isinstance(self.admitted_ir_hash, str)
                or len(self.admitted_ir_hash) != 64
                or any(char not in "0123456789abcdef" for char in self.admitted_ir_hash)
            ):
                raise PlanTransactionError("invalid admitted DAG hash")
            evidence["admitted_ir_hash"] = self.admitted_ir_hash
        self._replay_safe = self._replay_safe and _replay_safe(evidence)
        return _canonical_hash(evidence)

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
            or (
                r.status == "failed"
                and r.step_index in self._started_steps
                and (
                    self.steps[r.step_index].rollback_on_self_error
                    or r.step_index in self._external_step_indices
                )
            )
        ]
        for record in reversed(succeeded):
            step = self.steps[record.step_index]
            if step.compensate is None:
                if step.execute is not None:
                    errors.append(ValueError(f"uncompensated step {record.step_index}"))
                continue
            try:
                compensation_result = step.compensate(self._step_results.get(record.step_index))
                if _failed_boolean(compensation_result):
                    raise PlanTransactionError(
                        f"compensation for step {record.step_index} returned an unsuccessful result"
                    )
                output_hash = _canonical_hash(compensation_result)
            except BaseException as exc:  # noqa: BLE001 - collect, never abandon
                errors.append(exc)
                self._rollback_records.append(
                    PlanRollbackRecord(
                        step_index=record.step_index,
                        capability_name=step.operation,
                        status="failed",
                        input_hash=record.input_hash,
                        output_hash=_canonical_hash({"error_type": type(exc).__qualname__}),
                    )
                )
            else:
                self._rollback_records.append(
                    PlanRollbackRecord(
                        step_index=record.step_index,
                        capability_name=step.operation,
                        status="succeeded",
                        input_hash=record.input_hash,
                        output_hash=output_hash,
                    )
                )
        return errors


class StudioExecutionPort(Protocol):
    """Public durable execution boundary consumed by ingress facades."""

    def uses_durable_store(self, store: DurablePlanDedupePort) -> bool: ...

    def durable_approval_requirements(
        self, plan: PlanTransaction, *, policy_version: str, authenticated_actor: ActorIdentity
    ) -> tuple[PlanApprovalRequirement, ...]: ...

    def dispatch_plan(
        self,
        plan: PlanTransaction,
        *,
        approval_attestations: tuple[ApprovalBinding, ...] | None = None,
        policy_version: str | None = None,
        authenticated_actor: ActorIdentity | None = None,
        approval_verifier: ApprovalVerifier | None = None,
        deadline_monotonic_ns: int | None = None,
        lease_ttl_seconds: float = 30.0,
    ) -> PlanTransaction | PlanResultSummary: ...

    def inspect_durable_recovery(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        authenticated_operator: ActorIdentity,
        expected_status: str,
        expected_attempt: int,
    ) -> RecoveryEvidence[Any]: ...

    def recover_durable_plan(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        authenticated_operator: ActorIdentity,
        expected_status: str,
        expected_attempt: int,
        resolution_action: str,
        reason: str,
        recovery_evidence: RecoveryEvidence[Any] | None = None,
    ) -> None: ...


def parse_command(command: TypedCommand | dict[str, Any] | str | bytes) -> TypedCommand:
    """Public canonical envelope parsing seam; revalidates typed inputs."""
    return _parse(command)


def command_fingerprint(command: TypedCommand) -> str:
    """Public semantic command identity, excluding transport-only fields."""
    return _fingerprint(_parse(command))


class CommandBus:
    """Validates typed commands and applies them atomically to central state."""

    def __init__(
        self,
        state: Project | None,
        registry: CapabilityRegistry | None = None,
        resolver: ReferenceResolver | None = None,
        *,
        authorizer: ProjectAuthorizer | None = None,
        allow_experimental: bool = False,
        audio_engine: Any = None,
        engine_ports: Mapping[str, Any] | None = None,
        durable_store: DurablePlanDedupePort | None = None,
        durable_dedupe_store: DurablePlanDedupePort | None = None,
        durable_verifiers: Mapping[str, Callable[[Project, TypedCommand, CommandResult, str], bool]]
        | None = None,
    ) -> None:
        if durable_store is not None and durable_dedupe_store is not None:
            raise ValueError("configure only one durable store")
        self._durable_store = durable_store if durable_store is not None else durable_dedupe_store
        self._durable_verifiers = dict(durable_verifiers or {})
        if any(not callable(verifier) for verifier in self._durable_verifiers.values()):
            raise ValueError("durable verifiers must be callable")
        self._durable_context: dict[str, Any] = {}
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
        if state is None and (self._durable_store is None or authorizer is None):
            raise ValueError("a cold bus requires a durable store and project authorizer")
        self._project: Project = (
            Project.model_validate(state.model_dump(mode="json"))
            if state is not None
            else cast(Project, None)
        )
        self._history: list[EditTransaction] = []
        self._idempotency: dict[tuple[str, str, str], _Reservation] = {}
        self._executing = False
        self._plan_commit_epoch = 0
        self._plan_cache: OrderedDict[str, tuple[str, PlanTransaction]] = OrderedDict()
        self._plan_summaries: OrderedDict[str, tuple[str, PlanResultSummary]] = OrderedDict()

    def uses_durable_store(self, store: DurablePlanDedupePort) -> bool:
        """Read-only wiring check without exposing the store to callers."""
        return store is not None and self._durable_store is store

    def _require_project(self) -> None:
        if self._project is None:
            raise PlanTransactionError("project is not loaded; only committed replay is available")

    @property
    def project(self) -> Project:
        """A deep copy of the central state (safe to read without the lock)."""
        with self._lock:
            self._require_project()
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
            return tuple(record.model_copy(deep=True) for record in self._history)

    @property
    def state_revision(self) -> int:
        with self._lock:
            self._require_project()
            return self._project.state_revision

    @property
    def state_hash(self) -> str:
        with self._lock:
            self._require_project()
            return self._project.state_hash

    def dispatch(self, command: TypedCommand | dict[str, Any] | str | bytes) -> CommandResult:
        """Run the full validate -> authorize -> apply pipeline."""
        parsed = _parse(command)  # 1. Parse + 2a. versioned envelope schema
        with self._lock:
            if self._executing:
                raise CommandExecutionError("nested command dispatch during execution is forbidden")
            self._require_project()
            return self._dispatch_locked(parsed)

    def dispatch_plan(
        self,
        plan: PlanTransaction,
        *,
        approval_attestations: tuple[ApprovalBinding, ...] | None = None,
        policy_version: str | None = None,
        authenticated_actor: ActorIdentity | None = None,
        approval_verifier: ApprovalVerifier | None = None,
        deadline_monotonic_ns: int | None = None,
        lease_ttl_seconds: float = 30.0,
    ) -> PlanTransaction | PlanResultSummary:
        """Execute a plan, requiring the durable lifecycle when a store is configured.

        Durable approval scopes, identity, policy, verifier and monotonic deadline
        must come from trusted request context. Without a store, the original
        in-memory plan execution remains available without those arguments.
        """
        if not isinstance(plan, PlanTransaction):
            raise CommandValidationError("dispatch_plan requires a PlanTransaction")
        with self._lock:
            if self._executing:
                raise CommandExecutionError("nested plan dispatch during execution is forbidden")
            if self._durable_store is not None:
                if (
                    approval_attestations is None
                    or policy_version is None
                    or authenticated_actor is None
                    or approval_verifier is None
                    or deadline_monotonic_ns is None
                ):
                    raise PlanTransactionError("durable plan execution requires approval context")
                return self.dispatch_durable_plan(
                    plan,
                    approval_attestations,
                    policy_version=policy_version,
                    authenticated_actor=authenticated_actor,
                    approval_verifier=approval_verifier,
                    deadline_monotonic_ns=deadline_monotonic_ns,
                    lease_ttl_seconds=lease_ttl_seconds,
                )
            if lease_ttl_seconds != 30.0 or any(
                value is not None
                for value in (
                    approval_attestations,
                    policy_version,
                    authenticated_actor,
                    approval_verifier,
                    deadline_monotonic_ns,
                )
            ):
                raise PlanTransactionError("durable store is not configured")
            return self._dispatch_plan_locked(plan)

    def dispatch_durable_plan(
        self,
        plan: PlanTransaction,
        bindings: tuple[ApprovalBinding, ...],
        *,
        policy_version: str,
        authenticated_actor: ActorIdentity,
        approval_verifier: ApprovalVerifier,
        deadline_monotonic_ns: int,
        lease_ttl_seconds: float = 30.0,
    ) -> PlanResultSummary:
        """Run registered steps with fenced intents and a checksummed receipt.

        The caller authenticates the actor and supplies an independently trusted
        approval verifier. Effect verifiers and external-fencing capability are
        trusted composition-root registrations, never command-envelope claims.
        SQLite fences its own writes; it cannot fence an external destination.
        Receipt hashes detect corruption, not a malicious database writer.
        """
        store = self._durable_store
        if store is None:
            raise PlanTransactionError("durable store is not configured")
        if isinstance(deadline_monotonic_ns, bool) or not isinstance(deadline_monotonic_ns, int):
            raise ValueError("deadline_monotonic_ns must be an integer")

        def check_deadline() -> None:
            if store.monotonic_ns() >= deadline_monotonic_ns:
                raise TimeoutError("durable plan deadline expired")

        with self._lock:
            check_deadline()
            cold_replay = self._try_cold_durable_replay(
                plan,
                bindings,
                policy_version=policy_version,
                authenticated_actor=authenticated_actor,
                approval_verifier=approval_verifier,
                deadline_monotonic_ns=deadline_monotonic_ns,
            )
            if cold_replay is not None:
                return cold_replay
            self._require_project()
            requirements = self.verify_durable_approvals(
                plan,
                bindings,
                policy_version=policy_version,
                authenticated_actor=authenticated_actor,
                now=store.utc_timestamp(),
                verifier=approval_verifier,
            )
            accepted_steps = plan.steps
            input_hashes = tuple(
                plan._step_input_hash(i, step) for i, step in enumerate(accepted_steps)
            )
            commands: list[TypedCommand] = []
            specs: list[OperationSpec] = []
            verifiers: list[Callable[[Project, TypedCommand, CommandResult, str], bool]] = []
            seen: set[tuple[str, str]] = set()
            for step in plan.steps:
                assert step.command is not None
                command = _parse(step.command)
                spec = self._registry.get_spec(command.operation)
                if not command.idempotency_key:
                    raise CommandValidationError("durable steps require idempotency keys")
                key = (command.operation, command.idempotency_key)
                if key in seen or (self._project.project_id, *key) in self._idempotency:
                    raise IdempotencyConflictError("durable step idempotency key is not fresh")
                seen.add(key)
                if command.operation not in self._durable_verifiers:
                    raise PlanTransactionError("operation has no independent durable verifier")
                external = (
                    step.external_effects
                    or spec.external_effects
                    or bool(getattr(spec.handler, "external_effects", False))
                )
                if external and spec.durable_effects is not True:
                    raise PlanTransactionError(
                        "external operation lacks destination fencing support"
                    )
                commands.append(command)
                specs.append(spec)
                verifiers.append(self._durable_verifiers[command.operation])
            first = requirements[0]
            nonces = tuple(binding.nonce for binding in bindings)
            snapshot = _clone_plan_snapshot(vars(self))
            check_deadline()
            try:
                lease = store.reserve_and_start(
                    plan.plan_id,
                    first.plan_hash,
                    project_id=first.project_id,
                    external_effects=bool(plan._external_step_indices),
                    nonce=nonces[0],
                    additional_nonces=nonces[1:],
                    lease_ttl_seconds=lease_ttl_seconds,
                )
            except PlanInDoubtQuarantineError as original:
                try:
                    replay = store.replay_committed(
                        plan.plan_id,
                        first.plan_hash,
                        project_id=first.project_id,
                        external_effects=bool(plan._external_step_indices),
                        nonce=nonces[0],
                        additional_nonces=nonces[1:],
                    )
                except PlanStoreStateError:
                    raise original from None
                check_deadline()
                self.verify_durable_approvals(
                    plan,
                    bindings,
                    policy_version=policy_version,
                    authenticated_actor=authenticated_actor,
                    now=store.utc_timestamp(),
                    verifier=approval_verifier,
                )
                if any(not b.issued_at <= store.utc_timestamp() < b.expires_at for b in bindings):
                    raise ApprovalBindingError("approval expired during verification") from None
                check_deadline()
                if replay.evidence_json is None:
                    raise PlanStoreStateError("committed receipt is missing") from None
                return self._rehydrate_durable_receipt(
                    plan, first, replay.evidence_json, deadline_monotonic_ns=deadline_monotonic_ns
                )
            fence: _DurableFenceArgs = {
                "project_id": first.project_id,
                "attempt": lease.attempt,
                "owner_token": lease.owner_token,
            }
            completed: list[tuple[int, str, CommandResult]] = []
            trace: list[EditTransaction] = []
            uncertain = False
            self._executing = True
            plan.state = PlanTransactionState.EXECUTING
            try:
                for index, (step, command, expected, binding, spec, verifier) in enumerate(
                    zip(
                        accepted_steps,
                        commands,
                        requirements,
                        bindings,
                        specs,
                        verifiers,
                        strict=True,
                    )
                ):
                    if plan.steps != accepted_steps:
                        raise PlanTransactionError("durable plan changed after approval")

                    def heartbeat() -> None:
                        check_deadline()
                        store.heartbeat(
                            plan.plan_id,
                            first.plan_hash,
                            lease_ttl_seconds=lease_ttl_seconds,
                            **fence,
                        )

                    heartbeat()
                    self._validate_durable_boundary(
                        plan,
                        index,
                        step,
                        command,
                        expected,
                        binding,
                        authenticated_actor=authenticated_actor,
                        approval_verifier=approval_verifier,
                    )
                    heartbeat()
                    if (
                        plan.steps != accepted_steps
                        or plan._step_input_hash(index, step) != input_hashes[index]
                    ):
                        raise PlanTransactionError("durable step changed after approval")
                    effect_key = derive_effect_key(
                        project_id=first.project_id,
                        plan_id=plan.plan_id,
                        node_id=expected.step_id,
                        idempotency_key=command.idempotency_key or "",
                        fingerprint=_fingerprint(command),
                    )
                    store.record_compensation_intent(
                        plan.plan_id,
                        first.plan_hash,
                        step_id=expected.step_id,
                        effect_key=effect_key,
                        **fence,
                    )
                    uncertain = True
                    self._durable_context = {
                        "effect_key": effect_key,
                        "deadline_monotonic_ns": deadline_monotonic_ns,
                        "fencing_epoch": lease.fencing_epoch,
                        "durable_heartbeat": heartbeat,
                        "durable_owner_token": lease.owner_token,
                        "durable_plan_id": plan.plan_id,
                        "durable_plan_hash": first.plan_hash,
                        "durable_project_id": first.project_id,
                    }
                    result = self._dispatch_locked(command, expected_spec=spec)
                    self._durable_context = {}
                    heartbeat()
                    if (
                        verifier(
                            self._project.model_copy(deep=True),
                            command.model_copy(deep=True),
                            result.model_copy(deep=True),
                            effect_key,
                        )
                        is not True
                    ):
                        raise PlanTransactionError("independent effect verification failed")
                    heartbeat()
                    digest = _durable_hash(result.model_dump(mode="json"))
                    store.verify_effect_intent(
                        plan.plan_id,
                        first.plan_hash,
                        step_id=expected.step_id,
                        effect_key=effect_key,
                        verification_digest=digest,
                        deadline_monotonic_ns=deadline_monotonic_ns,
                        **fence,
                    )
                    uncertain = False
                    completed.append((index, effect_key, result))
                    trace.append(self._history[-1].model_copy(deep=True))
                    plan._step_records.append(
                        PlanStepRecord(
                            index,
                            step.operation,
                            "succeeded",
                            input_hashes[index],
                            digest,
                        )
                    )
                heartbeat()
                if plan.steps != accepted_steps:
                    raise PlanTransactionError("durable plan changed during execution")
                receipt = self._durable_receipt(plan, first, snapshot, trace)
                # Never commit a receipt that a fresh bus cannot validate and rehydrate.
                probe = CommandBus(
                    snapshot["_project"], registry=self._registry, authorizer=self._authorizer
                )
                probe._history = [
                    EditTransaction.model_validate_json(raw) for raw in snapshot["_history"]
                ]
                probe._idempotency = snapshot["_idempotency"]
                probe._rehydrate_durable_receipt(plan, first, json.dumps(receipt, allow_nan=False))
                plan.state = PlanTransactionState.EXECUTING
                heartbeat()
                store.finish(
                    plan.plan_id,
                    first.plan_hash,
                    evidence=receipt,
                    deadline_monotonic_ns=deadline_monotonic_ns,
                    **fence,
                )
                plan.state = PlanTransactionState.COMMITTED
                return PlanResultSummary(
                    plan.plan_id,
                    plan.state,
                    tuple(plan._step_records),
                    self._project.state_revision,
                )
            except BaseException as original:
                self._durable_context = {}
                errors: list[BaseException] = []
                if completed:
                    try:
                        store.mark_compensating(plan.plan_id, first.plan_hash, **fence)
                    except BaseException as exc:
                        errors.append(exc)
                    else:
                        for index, effect_key, result in reversed(completed):
                            step = accepted_steps[index]
                            succeeded = False
                            try:
                                check_deadline()
                                store.heartbeat(
                                    plan.plan_id,
                                    first.plan_hash,
                                    lease_ttl_seconds=lease_ttl_seconds,
                                    **fence,
                                )
                                if step.compensate is not None:
                                    if plan._callback_hash(step) != plan._accepted_callbacks[index]:
                                        raise PlanTransactionError("durable compensator changed")
                                    outcome = step.compensate(result)
                                    if _failed_boolean(outcome):
                                        raise PlanTransactionError("compensation returned failure")
                                succeeded = True
                            except BaseException as exc:
                                errors.append(exc)
                            try:
                                store.record_compensation_step(
                                    plan.plan_id,
                                    first.plan_hash,
                                    step_id=requirements[index].step_id,
                                    effect_key=effect_key,
                                    succeeded=succeeded,
                                    evidence={"compensated": succeeded},
                                    **fence,
                                )
                            except BaseException as exc:
                                errors.append(exc)
                        if not uncertain:
                            try:
                                store.record_compensation(
                                    plan.plan_id,
                                    first.plan_hash,
                                    succeeded=not errors,
                                    evidence={"compensated": not errors},
                                    **fence,
                                )
                            except BaseException as exc:
                                errors.append(exc)
                    if uncertain:
                        try:
                            store.quarantine(plan.plan_id, first.plan_hash, **fence)
                        except BaseException as exc:
                            errors.append(exc)
                    plan.state = (
                        PlanTransactionState.ROLLBACK_FAILED
                        if errors or uncertain
                        else PlanTransactionState.ROLLED_BACK
                    )
                else:
                    try:
                        store.quarantine(plan.plan_id, first.plan_hash, **fence)
                    except BaseException as exc:
                        errors.append(exc)
                    plan.state = PlanTransactionState.ROLLBACK_FAILED
                for name, value in snapshot.items():
                    if name == "_history":
                        value = [EditTransaction.model_validate_json(raw) for raw in value]
                    setattr(self, name, value)
                _raise_control_error(original, errors)
                if errors:
                    raise PlanRollbackError(
                        "durable plan failed and cleanup is incomplete",
                        plan_id=plan.plan_id,
                        original_error=original,
                        rollback_errors=tuple(errors),
                    ) from original
                raise
            finally:
                self._durable_context = {}
                self._executing = False

    def inspect_durable_recovery(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        authenticated_operator: ActorIdentity,
        expected_status: str,
        expected_attempt: int,
    ) -> RecoveryEvidence[Any]:
        """Inspect using store-configured trusted routes and issue one-use evidence."""
        with self._lock:
            if self._executing or self._durable_store is None or self._authorizer is None:
                raise AuthorizationError("durable recovery authority is not available")
            self._require_project()
            if not isinstance(authenticated_operator, ActorIdentity):
                raise AuthorizationError("authenticated operator is required")
            access = self._authorizer.authorize(authenticated_operator, self._project.project_id)
            if (
                access.actor != authenticated_operator
                or access.project_id != self._project.project_id
            ):
                raise AuthorizationError("authorizer returned a mismatched operator or project")
            access.require_permissions(("project:repair",))
            return self._durable_store.inspect_recovery(
                self._project.project_id,
                plan_id,
                plan_hash,
                expected_status=expected_status,
                expected_attempt=expected_attempt,
                operator_id=authenticated_operator.actor_id,
            )

    def recover_durable_plan(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        authenticated_operator: ActorIdentity,
        expected_status: str,
        expected_attempt: int,
        resolution_action: str,
        reason: str,
        recovery_evidence: RecoveryEvidence[Any] | None = None,
    ) -> None:
        """Ask the trusted store to perform an authorized, audited CAS repair.

        The caller must authenticate the operator independently; the store's
        configured repair authorizer makes a second independent decision.
        """
        with self._lock:
            if self._executing or self._durable_store is None or self._authorizer is None:
                raise AuthorizationError("durable repair authority is not available")
            self._require_project()
            if not isinstance(authenticated_operator, ActorIdentity):
                raise AuthorizationError("authenticated operator is required")
            access = self._authorizer.authorize(authenticated_operator, self._project.project_id)
            if (
                access.actor != authenticated_operator
                or access.project_id != self._project.project_id
            ):
                raise AuthorizationError(
                    "authorizer returned a grant for another operator or project"
                )
            access.require_permissions(("project:repair",))
            self._durable_store.repair_in_doubt(
                self._project.project_id,
                plan_id,
                plan_hash,
                expected_status=expected_status,
                expected_attempt=expected_attempt,
                operator_id=authenticated_operator.actor_id,
                resolution_action=resolution_action,
                reason=reason,
                recovery_evidence=recovery_evidence,
            )

    def _validate_durable_boundary(
        self,
        plan: PlanTransaction,
        index: int,
        step: PlanStep,
        command: TypedCommand,
        expected: PlanApprovalRequirement,
        binding: ApprovalBinding,
        *,
        authenticated_actor: ActorIdentity,
        approval_verifier: ApprovalVerifier,
    ) -> None:
        store = self._durable_store
        if store is None:
            raise PlanTransactionError("durable store is not configured")
        if (
            command.actor != authenticated_actor
            or plan._callback_hash(step) != plan._accepted_callbacks[index]
        ):
            raise AuthorizationError("durable actor or callback changed")
        spec, validated, project_id = self._validate_dispatch(command)
        self._resolver.validate_input_refs(command.input_refs, self._project)
        data = validated.model_dump(mode="json")
        for field_name in spec.reference_fields:
            value: Any = getattr(validated, field_name)
            if value is not None:
                pinned = (
                    value.model_copy(update={"captured_at_command": True})
                    if isinstance(value, Playhead)
                    else self._resolver.resolve(value, self._project)
                )
                data[field_name] = pinned.model_dump(mode="json")
        normalized = json.dumps(
            data, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
        params_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        verify_approval_binding(
            binding,
            project_id=project_id,
            plan_id=plan.plan_id,
            plan_hash=expected.plan_hash,
            actor_id=authenticated_actor.actor_id,
            step_id=expected.step_id,
            capability_version=self._registry.describe(command.operation).version,
            normalized_params_hash=params_hash,
            policy_version=expected.policy_version,
            now=store.utc_timestamp(),
            verifier=approval_verifier,
        )
        if not binding.issued_at <= store.utc_timestamp() < binding.expires_at:
            raise ApprovalBindingError("approval expired during verification")

    def _try_cold_durable_replay(
        self,
        plan: PlanTransaction,
        bindings: tuple[ApprovalBinding, ...],
        *,
        policy_version: str,
        authenticated_actor: ActorIdentity,
        approval_verifier: ApprovalVerifier,
        deadline_monotonic_ns: int,
    ) -> PlanResultSummary | None:
        """Authenticate and validate committed evidence before publishing any state."""
        store = self._durable_store
        if store is None or self._authorizer is None:
            return None
        if self._executing or not isinstance(plan, PlanTransaction):
            raise PlanTransactionError("plan is not available for durable replay")
        if plan.state is not PlanTransactionState.PENDING:
            raise PlanTransactionError("plan is not available for durable replay")
        if not isinstance(authenticated_actor, ActorIdentity):
            raise AuthorizationError("authenticated actor context is required")
        if (
            not isinstance(bindings, tuple)
            or not bindings
            or any(not isinstance(binding, ApprovalBinding) for binding in bindings)
        ):
            raise ApprovalBindingError("invalid approval bindings")
        first = bindings[0]
        if first.actor_id != authenticated_actor.actor_id or first.plan_id != plan.plan_id:
            raise ApprovalBindingError("approval does not match the plan")
        access = self._authorizer.authorize(authenticated_actor, first.project_id)
        if access.actor != authenticated_actor or access.project_id != first.project_id:
            raise AuthorizationError("authorizer returned a mismatched project grant")
        record = store.find_committed(plan.plan_id, first.plan_hash, project_id=first.project_id)
        if record is None:
            return None
        # The authority is checked before parsing or disclosing any receipt data.
        if len(bindings) != len(plan.steps) or len({b.nonce for b in bindings}) != len(bindings):
            raise ApprovalBindingError("one unique approval binding is required per step")
        for index, binding in enumerate(bindings):
            verify_approval_binding(
                binding,
                project_id=first.project_id,
                plan_id=plan.plan_id,
                plan_hash=first.plan_hash,
                actor_id=authenticated_actor.actor_id,
                step_id=f"step-{index}",
                capability_version=binding.capability_version,
                normalized_params_hash=binding.normalized_params_hash,
                policy_version=policy_version,
                now=store.utc_timestamp(),
                verifier=approval_verifier,
            )
        try:
            raw = record.evidence_json
            if raw is None or len(raw.encode("utf-8")) > 65536:
                raise ValueError("invalid receipt size")
            receipt = json.loads(raw, object_pairs_hook=_unique_json_pairs)
            payload = receipt["payload"]
            if receipt["digest"] != _durable_hash(payload):
                raise ValueError("receipt digest mismatch")
            if "before_project" not in payload:
                if self._project is None:
                    raise ValueError("legacy receipt lacks a base snapshot")
                return None
            before = Project.model_validate(payload["before_project"])
            if (
                before.project_id != first.project_id
                or before.state_hash != payload["before_project"]["state_hash"]
                or before.state_hash != payload["before_hash"]
                or before.state_revision != payload["before_revision"]
            ):
                raise ValueError("invalid base project")
            history = [EditTransaction.model_validate(item) for item in payload["before_history"]]
            prior: dict[tuple[str, str, str], _Reservation] = {}
            for item in payload["before_idempotency"]:
                key = tuple(item["key"])
                if (
                    len(key) != 3
                    or any(not isinstance(part, str) for part in key)
                    or key[0] != first.project_id
                    or key in prior
                    or item["result"] is None
                ):
                    raise ValueError("invalid base reservation")
                prior[cast(tuple[str, str, str], key)] = _Reservation(
                    item["fingerprint"], CommandResult.model_validate(item["result"])
                )
            if self._project is not None:
                current = self._project
                if current.project_id != first.project_id:
                    raise ValueError("project mismatch")
                final = Project.model_validate(payload["project"])
                ancestor = any(
                    tx.parent_revision == current.state_revision
                    and tx.previous_state_hash == current.state_hash
                    and Project.model_validate(tx.state_before) == current
                    for tx in history
                )
                if current != before and current != final and not ancestor:
                    raise ValueError("in-memory project is not an ancestor of the receipt")
            probe = CommandBus(
                before,
                registry=self._registry,
                resolver=self._resolver,
                authorizer=self._authorizer,
                allow_experimental=self._allow_experimental,
                audio_engine=self._audio_engine,
                engine_ports=self._engine_ports,
            )
            probe._history = history
            probe._idempotency = prior
            requirements = probe.verify_durable_approvals(
                plan,
                bindings,
                policy_version=policy_version,
                authenticated_actor=authenticated_actor,
                now=store.utc_timestamp(),
                verifier=approval_verifier,
            )
            if bool(plan._external_step_indices) != record.external_effects:
                raise ValueError("external effect declaration differs")
            try:
                result = probe._rehydrate_durable_receipt(plan, requirements[0], raw)
            finally:
                plan.state = PlanTransactionState.PENDING
        except (TypeError, ValueError, KeyError, IndexError, ValidationError) as exc:
            raise PlanStoreStateError("invalid durable receipt") from exc
        if store.monotonic_ns() >= deadline_monotonic_ns:
            raise TimeoutError("durable plan deadline expired")
        authorized = store.replay_committed(
            plan.plan_id,
            first.plan_hash,
            project_id=first.project_id,
            external_effects=record.external_effects,
            nonce=first.nonce,
            additional_nonces=tuple(binding.nonce for binding in bindings[1:]),
        )
        if authorized.evidence_json != raw:
            raise PlanStoreStateError("committed receipt changed during replay")
        for binding, expected in zip(bindings, requirements, strict=True):
            verify_approval_binding(
                binding,
                project_id=expected.project_id,
                plan_id=expected.plan_id,
                plan_hash=expected.plan_hash,
                actor_id=expected.actor_id,
                step_id=expected.step_id,
                capability_version=expected.capability_version,
                normalized_params_hash=expected.normalized_params_hash,
                policy_version=expected.policy_version,
                now=store.utc_timestamp(),
                verifier=approval_verifier,
            )
        if store.monotonic_ns() >= deadline_monotonic_ns:
            raise TimeoutError("durable plan deadline expired")
        self._project, self._history, self._idempotency = (
            probe._project,
            probe._history,
            probe._idempotency,
        )
        plan.state = PlanTransactionState.COMMITTED
        return result

    def _durable_receipt(
        self,
        plan: PlanTransaction,
        expected: PlanApprovalRequirement,
        snapshot: dict[str, Any],
        trace: list[EditTransaction],
    ) -> dict[str, Any]:
        before: Project = snapshot["_project"]
        payload = {
            "version": 1,
            "plan_id": plan.plan_id,
            "plan_hash": expected.plan_hash,
            "before_hash": before.state_hash,
            "before_revision": before.state_revision,
            "before_project": before.model_dump(mode="json"),
            "before_history": [json.loads(raw) for raw in snapshot["_history"]],
            "before_idempotency": [
                {
                    "key": list(key),
                    "fingerprint": value.fingerprint,
                    "result": value.result.model_dump(mode="json") if value.result else None,
                }
                for key, value in sorted(snapshot["_idempotency"].items())
            ],
            "project": self._project.model_dump(mode="json"),
            "history": [r.model_dump(mode="json") for r in self._history],
            "trace": [r.model_dump(mode="json") for r in trace],
            "idempotency": [
                {
                    "key": list(key),
                    "fingerprint": value.fingerprint,
                    "result": value.result.model_dump(mode="json") if value.result else None,
                }
                for key, value in sorted(self._idempotency.items())
            ],
            "steps": [asdict(r) for r in plan._step_records],
        }
        return {"payload": payload, "digest": _durable_hash(payload)}

    def _rehydrate_durable_receipt(
        self,
        plan: PlanTransaction,
        expected: PlanApprovalRequirement,
        evidence_json: str,
        *,
        deadline_monotonic_ns: int | None = None,
    ) -> PlanResultSummary:
        try:
            if len(evidence_json) > 65536 or len(evidence_json.encode("utf-8")) > 65536:
                raise ValueError("receipt exceeds the byte limit")
            receipt = json.loads(evidence_json, object_pairs_hook=_unique_json_pairs)
            payload = receipt["payload"]
            if (
                receipt["digest"] != _durable_hash(payload)
                or type(payload["version"]) is not int
                or payload["version"] != 1
            ):
                raise ValueError("receipt digest or version mismatch")
            if (
                type(payload["before_revision"]) is not int
                or type(payload["project"]["state_revision"]) is not int
                or payload["plan_id"] != plan.plan_id
                or payload["plan_hash"] != expected.plan_hash
                or payload["before_hash"] != self._project.state_hash
                or payload["before_revision"] != self._project.state_revision
            ):
                raise ValueError("receipt does not match the authorized initial state")
            project = Project.model_validate(payload["project"])
            if (
                project.project_id != self._project.project_id
                or project.state_hash != payload["project"]["state_hash"]
                or project.state_revision != self._project.state_revision + len(plan.steps)
            ):
                raise ValueError("receipt project state is invalid")
            history = [EditTransaction.model_validate(item) for item in payload["history"]]
            trace = [EditTransaction.model_validate(item) for item in payload["trace"]]
            if len(trace) != len(plan.steps) or not history or history[-1] != trace[-1]:
                raise ValueError("receipt history is inconsistent")
            records = tuple(PlanStepRecord(**item) for item in payload["steps"])
            if len(records) != len(plan.steps) or any(
                r.step_index != i
                or r.capability_name != plan.steps[i].operation
                or r.status != "succeeded"
                or r.input_hash != plan._step_input_hash(i, plan.steps[i])
                for i, r in enumerate(records)
            ):
                raise ValueError("receipt steps are inconsistent")
            prior = self._project
            for index, tx in enumerate(trace):
                before = Project.model_validate(tx.state_before)
                command = _parse(plan.steps[index].command)  # type: ignore[arg-type]
                if (
                    before != prior
                    or before.state_hash != tx.state_before.get("state_hash")
                    or type(tx.state_before.get("state_revision")) is not int
                    or tx.previous_state_hash != prior.state_hash
                    or tx.parent_revision != prior.state_revision
                    or tx.operation != command.operation
                ):
                    raise ValueError("receipt transaction chain is inconsistent")
                if index + 1 < len(plan.steps):
                    prior = Project.model_validate(trace[index + 1].state_before)
                else:
                    prior = project
                if (
                    tx.new_state_hash != prior.state_hash
                    or prior.state_revision != tx.parent_revision + 1
                ):
                    raise ValueError("receipt transaction result is inconsistent")
            reservations: dict[tuple[str, str, str], _Reservation] = {}
            for item in payload["idempotency"]:
                key = tuple(item["key"])
                if (
                    len(key) != 3
                    or any(not isinstance(part, str) for part in key)
                    or key[0] != project.project_id
                    or key in reservations
                    or item["result"] is None
                ):
                    raise ValueError("receipt idempotency reservation is invalid")
                reservations[cast(tuple[str, str, str], key)] = _Reservation(
                    item["fingerprint"], CommandResult.model_validate(item["result"])
                )
            if len(reservations) != len(self._idempotency) + len(plan.steps):
                raise ValueError("receipt reservation count is inconsistent")
            for key, value in self._idempotency.items():
                if reservations.get(key) != value:
                    raise ValueError("receipt changed a prior reservation")
            for index, step in enumerate(plan.steps):
                command = _parse(step.command)  # type: ignore[arg-type]
                key = (project.project_id, command.operation, command.idempotency_key or "")
                reservation = reservations.get(key)
                tx = trace[index]
                if (
                    reservation is None
                    or reservation.fingerprint != _fingerprint(command)
                    or reservation.result is None
                    or reservation.result.transaction_id != tx.transaction_id
                    or reservation.result.state_revision != tx.parent_revision + 1
                    or reservation.result.state_hash != tx.new_state_hash
                    or records[index].output_hash
                    != _durable_hash(reservation.result.model_dump(mode="json"))
                ):
                    raise ValueError("receipt step reservation is inconsistent")
        except (TypeError, ValueError, KeyError, IndexError, ValidationError) as exc:
            raise PlanStoreStateError("invalid durable receipt") from exc
        if deadline_monotonic_ns is not None:
            if self._durable_store is None:
                raise PlanStoreStateError("durable store is not configured")
            if self._durable_store.monotonic_ns() >= deadline_monotonic_ns:
                raise TimeoutError("durable plan deadline expired")
        self._project, self._history, self._idempotency = project, history, reservations
        plan.state = PlanTransactionState.COMMITTED
        return PlanResultSummary(plan.plan_id, plan.state, records, project.state_revision)

    def durable_approval_requirements(
        self, plan: PlanTransaction, *, policy_version: str, authenticated_actor: ActorIdentity
    ) -> tuple[PlanApprovalRequirement, ...]:
        """Derive approval scopes without reserving or executing the plan.

        ``policy_version`` and ``authenticated_actor`` must come from trusted
        composition or authenticated request context, not the plan's claims.
        Only registry-backed commands with independently authorized actors are
        eligible; legacy arbitrary execute closures are refused here. Calling
        this method grants no execution authority and consumes no nonce.
        """
        if not isinstance(plan, PlanTransaction):
            raise CommandValidationError("approval preflight requires a PlanTransaction")
        validate_plan_identity(policy_version, name="policy_version")
        if not isinstance(authenticated_actor, ActorIdentity):
            raise AuthorizationError("authenticated actor context is required")
        try:
            authenticated_actor = ActorIdentity.model_validate(
                authenticated_actor.model_dump(mode="json")
            )
        except ValidationError as exc:
            raise AuthorizationError("authenticated actor context is invalid") from exc
        with self._lock:
            if self._executing or plan.state is not PlanTransactionState.PENDING:
                raise PlanTransactionError("plan is not available for approval preflight")
            self._require_project()
            if self._authorizer is None:
                raise AuthorizationError("durable approval requires a trusted project authorizer")
            if not 1 <= len(plan.steps) <= 128:
                raise PlanTransactionError("durable plan must contain 1 to 128 steps")
            plan._preflight(self)
            descriptions: list[tuple[str, str, str, str]] = []
            step_hashes: list[str] = []
            for index, step in enumerate(plan.steps):
                if step.command is None:
                    raise PlanTransactionError(
                        "durable approval refuses arbitrary execute callbacks"
                    )
                command = _parse(step.command)
                if command.actor is None:
                    raise AuthorizationError("durable approval requires an authenticated actor")
                if command.actor != authenticated_actor:
                    raise AuthorizationError("command actor differs from authenticated actor")
                spec, validated, project_id = self._validate_dispatch(command)
                self._resolver.validate_input_refs(command.input_refs, self._project)
                input_data = validated.model_dump(mode="json")
                for field_name in spec.reference_fields:
                    raw_value: Any = getattr(validated, field_name)
                    if raw_value is not None:
                        if isinstance(raw_value, Playhead):
                            pinned = raw_value.model_copy(update={"captured_at_command": True})
                        else:
                            pinned = self._resolver.resolve(raw_value, self._project)
                        input_data[field_name] = pinned.model_dump(mode="json")
                normalized = json.dumps(
                    input_data,
                    sort_keys=True,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                params_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                capability_version = self._registry.describe(command.operation).version
                descriptions.append(
                    (project_id, command.actor.actor_id, capability_version, params_hash)
                )
                step_hashes.append(plan._step_input_hash(index, step))
            if not plan._replay_safe:
                raise PlanTransactionError("durable approval refuses opaque callback state")
            plan_hash = _canonical_hash(
                {
                    "project_id": self._project.project_id,
                    "plan_id": plan.plan_id,
                    "state_revision": self._project.state_revision,
                    "state_hash": self._project.state_hash,
                    "history_hash": _canonical_hash(
                        [r.model_dump(mode="json") for r in self._history]
                    ),
                    "policy_version": policy_version,
                    "step_hashes": step_hashes,
                    "scopes": descriptions,
                }
            )
            return tuple(
                PlanApprovalRequirement(
                    project_id,
                    plan.plan_id,
                    plan_hash,
                    actor_id,
                    f"step-{index}",
                    capability_version,
                    params_hash,
                    policy_version,
                )
                for index, (project_id, actor_id, capability_version, params_hash) in enumerate(
                    descriptions
                )
            )

    def verify_durable_approvals(
        self,
        plan: PlanTransaction,
        bindings: tuple[ApprovalBinding, ...],
        *,
        policy_version: str,
        authenticated_actor: ActorIdentity,
        now: float,
        verifier: ApprovalVerifier,
    ) -> tuple[PlanApprovalRequirement, ...]:
        """Revalidate every approval against current bus authority without execution.

        This is preflight only: it does not consume nonces or authorize effects.
        The future durable execution path must atomically consume all nonces and
        recheck each binding at step boundaries before performing any effect.
        """
        with self._lock:
            requirements = self.durable_approval_requirements(
                plan, policy_version=policy_version, authenticated_actor=authenticated_actor
            )
            if not isinstance(bindings, tuple) or len(bindings) != len(requirements):
                raise ApprovalBindingError("one approval binding is required per step")
            if any(not isinstance(binding, ApprovalBinding) for binding in bindings):
                raise ApprovalBindingError("invalid approval binding")
            if len({binding.nonce for binding in bindings}) != len(bindings):
                raise ApprovalBindingError("approval nonces must be unique")
            for binding, expected in zip(bindings, requirements, strict=True):
                verify_approval_binding(
                    binding,
                    project_id=expected.project_id,
                    plan_id=expected.plan_id,
                    plan_hash=expected.plan_hash,
                    actor_id=expected.actor_id,
                    step_id=expected.step_id,
                    capability_version=expected.capability_version,
                    normalized_params_hash=expected.normalized_params_hash,
                    policy_version=expected.policy_version,
                    now=now,
                    verifier=verifier,
                )
            return requirements

    def _dispatch_plan_locked(self, plan: PlanTransaction) -> PlanTransaction | PlanResultSummary:
        """Transactional plan execution (see :meth:`dispatch_plan`)."""
        return plan.commit(self)

    def _validate_dispatch(self, command: TypedCommand) -> tuple[OperationSpec, Any, str]:
        """Pure policy/schema preflight shared by execution and plan redelivery."""
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

        return spec, validated, project_id

    def _dispatch_locked(
        self, command: TypedCommand, *, expected_spec: OperationSpec | None = None
    ) -> CommandResult:
        spec, validated, project_id = self._validate_dispatch(command)
        if expected_spec is not None and spec is not expected_spec:
            raise PlanTransactionError("operation capability changed after durable preflight")

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
            **self._durable_context,
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
                **(
                    {
                        "durable_effect_key": self._durable_context["effect_key"],
                        "durable_fencing_epoch": self._durable_context["fencing_epoch"],
                    }
                    if self._durable_context
                    else {}
                ),
            },
            undo_available=True,
        )
        # All potentially failing validation/construction is complete before
        # either central state or the reservation's result is committed.
        self._project = new_project
        self._history = [*outcome.history, transaction]
        return result


def compose_durable_command_bus(
    project: Project,
    *,
    registry: CapabilityRegistry,
    authorizer: ProjectAuthorizer,
    store: DurablePlanDedupePort,
    verifiers: Mapping[str, Callable[[Project, TypedCommand, CommandResult, str], bool]],
) -> CommandBus:
    """Central construction seam for durable adapters; retain the single bus pipeline."""
    if store is None or authorizer is None:
        raise ValueError("durable composition requires store and project authority")
    return CommandBus(
        project,
        registry=registry,
        authorizer=authorizer,
        durable_dedupe_store=store,
        durable_verifiers=verifiers,
    )
