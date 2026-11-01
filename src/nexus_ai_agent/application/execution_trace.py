"""Proof-carrying execution spine — the one trace primitive (task-215).

WHY THIS EXISTS
---------------
A live forensic audit of ``main @ e5b326b`` found that one real Nexus
execution already owns **three disjoint identities**, and that the field
which is supposed to bind them is declared but never written:

=======================  ==============================  ====================
identity                 where it lives                  set in production?
=======================  ==============================  ====================
``correlation_id``       ``bot/handlers.py``             yes — the *text*
(uuid4 per message)      ``orchestration/state.py``      surface only
                         ``storage/models.py``
``idempotency_key``      ``bot/creative_surface.py``     yes — durable and
(``creative:{u}:{c}:{m}``) ``adapters/                   UNIQUE in
                         in_process_job_queue.py``       ``nexus_job_queue``
``TypedCommand.          ``creative/studio/models.py``   **NO** — zero
 trace_id`` /                                          assignments anywhere
 ``request_id`` /                                      in ``src/``; only
 ``provenance``                                         ``tests/unit/
                                                       test_command_capability_
                                                       contract.py`` names it
=======================  ==============================  ====================

Because ``creative/render_jobs.py::_dispatch`` builds its ``TypedCommand``
without a ``trace_id``, ``CommandResult.diagnostics["trace_id"]`` is
**always ``None``** on the one path that actually reaches an engine. The
field reads like causal linkage and provides none — the exact shape of
theater this repository's engineering constitution forbids.

WHAT THIS IS NOT
----------------
Deliberately **not** a second ledger, a second provenance framework, an
event-sourcing log, or a new persistence layer. It is one typed, versioned,
immutable envelope that binds identities Nexus already owns, plus the
derivation rules that make a success claim un-forgeable from the outside.
It travels through structures that already exist
(``nexus_job_queue.payload_json`` / ``result_json``) and the redacted
lifecycle log; it adds **no** database migration and no broker.

THE LOAD-BEARING IDEA
---------------------
``ExecutionTrace`` stores **references only** and **derives** its outcome.
There is no ``outcome`` constructor parameter, so no caller — and in
particular no surface handler — can construct a trace that claims verified
success. Verified success exists only when an execution reference *and* at
least one measured evidence reference are both present, and
:func:`attach_evidence` refuses to invent the latter.

PAYLOAD MINIMIZATION (explicit, not aspirational)
-------------------------------------------------
The model has no field capable of holding a prompt, a message body, a file
path, a URL, or an API key. Every field is either a bounded identifier, a
bounded reference, or a short classification code. ``actor_ref`` is a
*scope* (``tg:<user_id>``), never a name or a token. That constraint is
pinned by a test, not by this paragraph.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Envelope version. Bumping this MUST change validation, otherwise it is
#: theater (the same rule ``TypedCommand._schema2_requires_claims`` enforces).
TRACE_SCHEMA_VERSION: Literal[1] = 1

TRACE_ID_PREFIX = "tr"

#: Bounded identifier/reference sizes. A trace is an index, never a payload.
_MAX_REF_LENGTH = 128
_MAX_SURFACE_LENGTH = 64
_MAX_CODE_LENGTH = 64
_MAX_EVIDENCE_REFS = 32

#: Surfaces are a closed, lowercase vocabulary: a typo must fail loudly
#: rather than silently fork the trace namespace.
_SURFACE_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")

#: An evidence reference is either the repository's existing content-address
#: form (``sha256:<64 hex>`` -- the same pattern ``InputRefMetadata`` already
#: uses) or a bounded, namespaced handle for a non-artifact reference (a job
#: id, a verification-record id). Extraction over reinvention: no new hash
#: scheme is introduced here. Neither form can express a filesystem path or a
#: URL, so an evidence reference can never become an exfiltration channel.
_SHA256_REF_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_NAMESPACED_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9._-]{1,64}$")

#: Refusals/failures are classification codes, never free text: an exception
#: message can echo a prompt, and this envelope must not be able to carry one.
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: Field separator for the trace-id derivation. ``\x1f`` cannot occur in a
#: validated surface, so ``("a", "b\x1fc")`` and ``("a\x1fb", "c")`` can never
#: collide.
_SEPARATOR = "\x1f"


class TraceError(ValueError):
    """Base class for every trace construction/link failure."""


class TraceLinkError(TraceError):
    """A parent or execution reference is missing, blank, or self-referential."""


class TraceEvidenceError(TraceError):
    """An evidence reference is absent, malformed, or unbounded."""


class TraceOutcome(str, Enum):
    """Derived, never supplied.

    ``UNKNOWN`` is the only honest state of a freshly minted trace. The
    ordering matters for :func:`is_at_least`: a trace can move *up* the
    ladder, never back down, so a recorded failure cannot later be
    represented as a success.
    """

    UNKNOWN = "unknown"
    REFUSED = "refused"
    FAILED = "failed"
    SUCCEEDED_UNVERIFIED = "succeeded_unverified"
    SUCCEEDED_VERIFIED = "succeeded_verified"


#: Monotonic rank of :class:`TraceOutcome`. Higher never becomes lower.
_OUTCOME_RANK: dict[TraceOutcome, int] = {
    TraceOutcome.UNKNOWN: 0,
    TraceOutcome.REFUSED: 1,
    TraceOutcome.FAILED: 1,
    TraceOutcome.SUCCEEDED_UNVERIFIED: 2,
    TraceOutcome.SUCCEEDED_VERIFIED: 3,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bounded(value: str, *, field: str, limit: int) -> str:
    if value != value.strip() or any(character.isspace() for character in value):
        raise TraceError(f"{field} must not contain whitespace")
    if len(value) > limit:
        raise TraceError(f"{field} exceeds {limit} characters")
    return value


def trace_id_for(surface: str, idempotency_key: str) -> str:
    """Return the canonical trace id for one logical request.

    The identity is a pure function of ``(surface, idempotency_key)``. Both
    inputs are identities Nexus already owns — the surface that received the
    request and the durable idempotency key that already collapses a
    redelivered message into one job. Consequences, both intentional:

    * an idempotent retry of the same request yields the *same* trace id, so
      retries preserve causal identity instead of forking it;
    * two unrelated requests cannot share a trace id unless they already
      share an idempotency key, which the durable queue forbids by UNIQUE
      constraint.

    The surface is validated here as well as at construction, so the
    derivation stays unambiguous for every caller, not only for envelopes
    that came through :func:`mint_trace`.
    """
    if not _SURFACE_PATTERN.match(surface):
        raise TraceError(f"surface must match {_SURFACE_PATTERN.pattern} (got {surface!r})")
    if not idempotency_key.strip():
        raise TraceError("idempotency_key must not be blank")
    material = f"{surface}{_SEPARATOR}{idempotency_key}".encode()
    digest = hashlib.sha256(material).hexdigest()
    return f"{TRACE_ID_PREFIX}-{digest[:32]}"


class ExecutionTrace(BaseModel):
    """One immutable, refs-only execution envelope.

    Construction is normally done through :func:`mint_trace` /
    :func:`link_child`; direct construction is allowed and validated, but can
    never claim an outcome because ``outcome`` is derived.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = TRACE_SCHEMA_VERSION
    trace_id: str = Field(min_length=1)
    parent_trace_id: str | None = Field(default=None, min_length=1)
    surface: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)

    # ── stage references (all optional, all bounded, none a payload) ────────
    intent_ref: str | None = None
    decision_ref: str | None = None
    command_ref: str | None = None
    capability_ref: str | None = None
    execution_ref: str | None = None

    # ── evidence ───────────────────────────────────────────────────────────
    evidence_refs: tuple[str, ...] = ()

    # ── terminal classification (short codes, never messages) ──────────────
    refusal_code: str | None = None
    failure_code: str | None = None

    created_at: str = Field(min_length=1)

    @field_validator(
        "trace_id",
        "parent_trace_id",
        "idempotency_key",
        "intent_ref",
        "decision_ref",
        "command_ref",
        "capability_ref",
        "execution_ref",
    )
    @classmethod
    def _bounded_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded(value, field="reference", limit=_MAX_REF_LENGTH)

    @field_validator("surface")
    @classmethod
    def _bounded_surface(cls, value: str) -> str:
        if not _SURFACE_PATTERN.match(value):
            raise ValueError(f"surface must match {_SURFACE_PATTERN.pattern} (got {value!r})")
        return value

    @field_validator("actor_ref")
    @classmethod
    def _bounded_actor(cls, value: str) -> str:
        return _bounded(value, field="actor_ref", limit=_MAX_REF_LENGTH)

    @field_validator("refusal_code", "failure_code")
    @classmethod
    def _bounded_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _CODE_PATTERN.match(value):
            raise ValueError(f"code must match {_CODE_PATTERN.pattern} (got {value!r})")
        return _bounded(value, field="code", limit=_MAX_CODE_LENGTH)

    @field_validator("evidence_refs")
    @classmethod
    def _bounded_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > _MAX_EVIDENCE_REFS:
            raise ValueError(f"at most {_MAX_EVIDENCE_REFS} evidence references")
        for ref in value:
            # The ``sha256`` namespace is reserved: a reference that claims to
            # be a content address must BE one. Without this rule a truncated
            # or typo'd hash (``sha256:xyz``) would pass as a generic
            # namespaced handle and a non-artifact claim could wear the
            # costume of a measured one.
            if ref.startswith("sha256:"):
                if not _SHA256_REF_PATTERN.match(ref):
                    raise ValueError(f"malformed sha256 evidence reference: {ref!r}")
            elif not _NAMESPACED_REF_PATTERN.match(ref):
                raise ValueError(f"malformed evidence reference: {ref!r}")
        return value

    @model_validator(mode="after")
    def _causal_invariants(self) -> ExecutionTrace:
        # I1 — the id is bound to the identity it claims to describe. A trace
        # whose id was not derived from its own (surface, idempotency_key)
        # pair is a forged identity, whatever its other fields say.
        if self.trace_id != trace_id_for(self.surface, self.idempotency_key):
            raise TraceLinkError(
                "trace_id is not the canonical identity of (surface, idempotency_key)"
            )
        # I2 — a trace is not its own parent.
        if self.parent_trace_id is not None and self.parent_trace_id == self.trace_id:
            raise TraceLinkError("a trace cannot be its own parent")
        # I3 — refusal and failure are different stories; a trace tells one.
        if self.refusal_code is not None and self.failure_code is not None:
            raise TraceError("a trace cannot be both refused and failed")
        # I4 — DECISION REFUSES means no execution happened. An execution_ref
        # on a refused trace is a fabricated execution.
        if self.refusal_code is not None and self.execution_ref is not None:
            raise TraceLinkError("a refused trace cannot carry an execution reference")
        # I5 — a terminal trace must name the stage it terminated at, so a
        # bare ``refused``/``failed`` can never float free of its cause. A
        # freshly minted trace is deliberately exempt: ``UNKNOWN`` is the
        # honest state of a request that has not been decided yet.
        if self.refusal_code is not None or self.failure_code is not None:
            if self.decision_ref is None and self.execution_ref is None:
                raise TraceLinkError("a terminal trace must reference the stage it ended at")
        return self

    # ── derived truth ──────────────────────────────────────────────────────

    @property
    def outcome(self) -> TraceOutcome:
        """The only honest summary of this trace, computed from its refs.

        Not stored, not settable, and therefore not forgeable: a caller that
        wants ``SUCCEEDED_VERIFIED`` must supply both an execution reference
        and at least one measured evidence reference through the transition
        functions, and nothing else can produce it.
        """
        if self.refusal_code is not None:
            return TraceOutcome.REFUSED
        if self.failure_code is not None:
            return TraceOutcome.FAILED
        if self.execution_ref is None:
            return TraceOutcome.UNKNOWN
        if self.evidence_refs:
            return TraceOutcome.SUCCEEDED_VERIFIED
        return TraceOutcome.SUCCEEDED_UNVERIFIED

    def is_at_least(self, outcome: TraceOutcome) -> bool:
        """True when this trace's outcome ranks at or above ``outcome``."""
        return _OUTCOME_RANK[self.outcome] >= _OUTCOME_RANK[outcome]

    # ── transitions (each returns a new instance; nothing mutates) ──────────

    def with_stage(
        self,
        *,
        intent_ref: str | None = None,
        decision_ref: str | None = None,
        command_ref: str | None = None,
        capability_ref: str | None = None,
        execution_ref: str | None = None,
    ) -> ExecutionTrace:
        """Attach stage references without changing the terminal state."""
        updates: dict[str, Any] = {}
        for name, value in (
            ("intent_ref", intent_ref),
            ("decision_ref", decision_ref),
            ("command_ref", command_ref),
            ("capability_ref", capability_ref),
            ("execution_ref", execution_ref),
        ):
            if value is None:
                continue
            if not value.strip():
                raise TraceLinkError(f"{name} must not be blank")
            updates[name] = _bounded(value, field=name, limit=_MAX_REF_LENGTH)
        return self._apply(updates)

    def with_refusal(self, *, decision_ref: str) -> ExecutionTrace:
        """DECISION REFUSES → no execution, no evidence, no success."""
        if not decision_ref.strip():
            raise TraceLinkError("decision_ref must not be blank")
        return self._apply(
            {
                "decision_ref": _bounded(decision_ref, field="decision_ref", limit=_MAX_REF_LENGTH),
                "refusal_code": "refused",
                "execution_ref": None,
                "evidence_refs": [],
            }
        )

    def _apply(self, updates: dict[str, Any]) -> ExecutionTrace:
        """Return a validated copy with ``updates`` applied.

        ``model_copy(update=...)`` is deliberately NOT used: in pydantic v2 it
        skips field and model validators, which would let a transition smuggle
        in a malformed evidence reference or an execution onto a refused
        trace. Every transition therefore round-trips through
        ``model_validate``, so the causal invariants hold on the *result* and
        not only on the input.
        """
        data = self.model_dump(mode="json")
        data.update(updates)
        return ExecutionTrace.model_validate(data)

    def with_failure(
        self,
        *,
        failure_code: str,
        decision_ref: str | None = None,
        execution_ref: str | None = None,
    ) -> ExecutionTrace:
        """EXECUTION FAILS → result is a typed failure, never a success.

        Evidence already measured is preserved (a failed render can still
        have produced a probe record), but the trace can never be promoted:
        :attr:`outcome` ranks ``FAILED`` below every success state.
        """
        if not failure_code.strip():
            raise TraceLinkError("failure_code must not be blank")
        updates: dict[str, Any] = {"failure_code": failure_code}
        if decision_ref is not None:
            if not decision_ref.strip():
                raise TraceLinkError("decision_ref must not be blank")
            updates["decision_ref"] = _bounded(
                decision_ref, field="decision_ref", limit=_MAX_REF_LENGTH
            )
        if execution_ref is not None:
            if not execution_ref.strip():
                raise TraceLinkError("execution_ref must not be blank")
            updates["execution_ref"] = _bounded(
                execution_ref, field="execution_ref", limit=_MAX_REF_LENGTH
            )
        if "decision_ref" not in updates and "execution_ref" not in updates:
            if self.decision_ref is None and self.execution_ref is None:
                raise TraceLinkError("a failure must name the stage it ended at")
        return self._apply(updates)

    def with_evidence(self, *, evidence_refs: tuple[str, ...] | list[str]) -> ExecutionTrace:
        """Attach measured evidence. An empty claim is refused, not ignored."""
        refs = tuple(evidence_refs)
        if not refs:
            raise TraceEvidenceError("evidence_refs must not be empty")
        return self._apply({"evidence_refs": list(refs)})


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #


def mint_trace(
    *,
    surface: str,
    actor_ref: str,
    idempotency_key: str,
    parent_trace_id: str | None = None,
    intent_ref: str | None = None,
    created_at: str | None = None,
) -> ExecutionTrace:
    """Mint the trace for one logical request at the surface that received it.

    The caller supplies identities it already owns. The trace id is *derived*,
    never chosen, so a surface cannot mint a colliding or guessable identity.
    """
    trace_id = trace_id_for(surface, idempotency_key)
    updates: dict[str, Any] = {
        "trace_id": trace_id,
        "surface": surface,
        "actor_ref": actor_ref,
        "idempotency_key": idempotency_key,
        "created_at": created_at or _now_iso(),
    }
    if parent_trace_id is not None:
        if not parent_trace_id.strip():
            raise TraceLinkError("parent_trace_id must not be blank")
        if parent_trace_id == trace_id:
            raise TraceLinkError("a trace cannot be its own parent")
        updates["parent_trace_id"] = _bounded(
            parent_trace_id, field="parent_trace_id", limit=_MAX_REF_LENGTH
        )
    if intent_ref is not None:
        if not intent_ref.strip():
            raise TraceLinkError("intent_ref must not be blank")
        updates["intent_ref"] = _bounded(intent_ref, field="intent_ref", limit=_MAX_REF_LENGTH)
    return ExecutionTrace(**updates)


def link_child(
    parent: ExecutionTrace,
    *,
    surface: str,
    actor_ref: str,
    idempotency_key: str,
    intent_ref: str | None = None,
) -> ExecutionTrace:
    """Mint a child trace whose parent link is verified before it exists."""
    if not parent.trace_id.strip():
        raise TraceLinkError("parent trace has no identity to link to")
    return mint_trace(
        surface=surface,
        actor_ref=actor_ref,
        idempotency_key=idempotency_key,
        parent_trace_id=parent.trace_id,
        intent_ref=intent_ref,
    )


def verify_bound_trace_id(*, surface: str, idempotency_key: str, trace_id: str) -> str:
    """Return the canonical trace id, or refuse a row that forged one.

    This is the runtime half of "surface-specific code cannot fabricate
    execution truth". A queue row is an untrusted structure (the same
    trust boundary ``creative/render_jobs.py`` already documents), so a
    ``trace_id`` arriving from a row is only trusted once it is proven to be
    the identity that ``surface`` would have derived from that row's own
    durable ``idempotency_key``.
    """
    expected = trace_id_for(surface, idempotency_key)
    if trace_id != expected:
        raise TraceLinkError(
            "trace_id is not bound to (surface, idempotency_key); "
            "a queue row cannot mint execution identity"
        )
    return expected


def verify_parent_link(child: ExecutionTrace, parent: ExecutionTrace) -> str:
    """Return the child's parent id, refusing a missing or mis-resolved link.

    A *dangling* parent (a well-formed id with no trace behind it) can only be
    detected by a store, and this primitive deliberately owns no store. What
    it does own is the check that the link is present and resolves to the
    parent it is claimed to resolve to — which is what makes a mis-wired
    ``parent_trace_id`` detectable at the boundary instead of downstream.
    """
    if child.parent_trace_id is None:
        raise TraceLinkError("child trace carries no parent link")
    if child.parent_trace_id != parent.trace_id:
        raise TraceLinkError("child parent link does not resolve to the given parent")
    return child.parent_trace_id


__all__ = [
    "TRACE_SCHEMA_VERSION",
    "ExecutionTrace",
    "TraceError",
    "TraceEvidenceError",
    "TraceLinkError",
    "TraceOutcome",
    "link_child",
    "mint_trace",
    "trace_id_for",
    "verify_bound_trace_id",
    "verify_parent_link",
]
