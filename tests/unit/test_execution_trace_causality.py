"""Causal integrity of the proof-carrying execution spine (task-215).

Each invariant named in the mission has exactly one test here, and each test
fails if the invariant is removed. ``test_harness_detects_a_positive`` is the
control: it proves the suite is not passing vacuously, the same discipline
``tests/unit/test_chat_memory_reachability.py`` uses.

No network, no clock dependence (``created_at`` is injected), no FFmpeg, no
Telegram. The suite is deterministic by construction.
"""

from __future__ import annotations

import importlib
import json

import pytest
from pydantic import ValidationError

from nexus_ai_agent.application.execution_trace import (
    TRACE_SCHEMA_VERSION,
    ExecutionTrace,
    TraceError,
    TraceEvidenceError,
    TraceLinkError,
    TraceOutcome,
    link_child,
    mint_trace,
    trace_id_for,
    verify_bound_trace_id,
    verify_parent_link,
)

SURFACE = "telegram.creative"
ACTOR = "tg:4242"


def _key(index: int) -> str:
    """A durable idempotency key in the exact shape creative_surface derives."""
    return f"creative:{ACTOR.split(':')[1]}:{index}:{index}"


def _mint(index: int = 1, **kwargs: object) -> ExecutionTrace:
    return mint_trace(
        surface=SURFACE,
        actor_ref=ACTOR,
        idempotency_key=_key(index),
        created_at="2026-09-29T20:00:00Z",
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# control
# --------------------------------------------------------------------------- #


def test_harness_detects_a_positive() -> None:
    """Control: the suite can observe a real violation, so it is not vacuous."""
    canonical = trace_id_for(SURFACE, _key(1))
    assert canonical != "tr-not-the-canonical-identity"
    with pytest.raises(ValidationError):
        ExecutionTrace(
            trace_id="tr-not-the-canonical-identity",
            surface=SURFACE,
            actor_ref=ACTOR,
            idempotency_key=_key(1),
            created_at="2026-09-29T20:00:00Z",
        )


# --------------------------------------------------------------------------- #
# I1 — unrelated executions cannot share a trace accidentally
# --------------------------------------------------------------------------- #


def test_unrelated_executions_never_share_a_trace_id() -> None:
    seen: dict[str, str] = {}
    for index in range(2000):
        key = _key(index)
        trace_id = trace_id_for(SURFACE, key)
        assert trace_id not in seen, f"{key} collided with {seen.get(trace_id)}"
        seen[trace_id] = key
    assert len(seen) == 2000


def test_trace_identity_does_not_depend_on_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identity is a pure function of the logical request, not of time.

    A trace id derived from a timestamp forks on every retry, which is the
    exact defect I6 exists to prevent; a same-second coincidence must not be
    able to hide it.
    """
    module = importlib.import_module("nexus_ai_agent.application.execution_trace")
    first = module.trace_id_for(SURFACE, _key(1))
    monkeypatch.setattr(module, "_now_iso", lambda: "1999-01-01T00:00:00Z")
    assert module.trace_id_for(SURFACE, _key(1)) == first


def test_distinct_surfaces_fork_the_trace_namespace() -> None:
    same_key = _key(7)
    assert trace_id_for("telegram.creative", same_key) != trace_id_for("telegram.chat", same_key)
    assert trace_id_for("api.creative", same_key) != trace_id_for("local.creative", same_key)


def test_trace_id_derivation_is_unambiguous() -> None:
    """The separator cannot be smuggled through a validated surface."""
    with pytest.raises(TraceError):
        trace_id_for("telegram\x1fcreative", _key(1))
    assert trace_id_for("ab", "c") != trace_id_for("a", "bc")


def test_a_trace_cannot_claim_a_foreign_identity() -> None:
    """A trace id must be the identity of its own (surface, key) pair."""
    with pytest.raises(ValidationError):
        ExecutionTrace(
            trace_id=trace_id_for(SURFACE, _key(2)),
            surface=SURFACE,
            actor_ref=ACTOR,
            idempotency_key=_key(1),
            created_at="2026-09-29T20:00:00Z",
        )


# --------------------------------------------------------------------------- #
# I2 — a missing parent/reference is detectable
# --------------------------------------------------------------------------- #


def test_a_blank_parent_link_is_detectable() -> None:
    with pytest.raises(TraceLinkError):
        _mint(1, parent_trace_id="   ")


def test_a_self_parent_is_detectable() -> None:
    trace = _mint(1)
    with pytest.raises(TraceLinkError):
        _mint(1, parent_trace_id=trace.trace_id)


def test_a_child_link_resolves_to_its_parent() -> None:
    parent = _mint(1)
    child = link_child(
        parent,
        surface="api.creative",
        actor_ref=ACTOR,
        idempotency_key=_key(2),
    )
    assert verify_parent_link(child, parent) == parent.trace_id
    with pytest.raises(TraceLinkError):
        verify_parent_link(child, _mint(3))


def test_a_child_without_a_link_is_detectable() -> None:
    orphan = _mint(4)
    with pytest.raises(TraceLinkError):
        verify_parent_link(orphan, _mint(1))


def test_a_blank_stage_reference_is_detectable() -> None:
    trace = _mint(1)
    with pytest.raises(TraceLinkError):
        trace.with_stage(execution_ref="  ")
    with pytest.raises(TraceLinkError):
        trace.with_refusal(decision_ref="")


# --------------------------------------------------------------------------- #
# I3 — success without a result is invalid
# --------------------------------------------------------------------------- #


def test_a_trace_without_an_execution_is_never_successful() -> None:
    trace = _mint(1, intent_ref="edit:trim")
    assert trace.outcome is TraceOutcome.UNKNOWN
    assert not trace.is_at_least(TraceOutcome.SUCCEEDED_UNVERIFIED)
    assert not trace.is_at_least(TraceOutcome.SUCCEEDED_VERIFIED)


def test_a_stage_reference_alone_does_not_promote_a_trace() -> None:
    trace = _mint(1).with_stage(intent_ref="edit:trim", decision_ref="permission:B")
    assert trace.outcome is TraceOutcome.UNKNOWN


def test_an_execution_reference_is_required_for_any_success() -> None:
    trace = _mint(1).with_stage(execution_ref="job-abc123")
    assert trace.outcome is TraceOutcome.SUCCEEDED_UNVERIFIED
    assert trace.is_at_least(TraceOutcome.SUCCEEDED_UNVERIFIED)
    assert not trace.is_at_least(TraceOutcome.SUCCEEDED_VERIFIED)


# --------------------------------------------------------------------------- #
# I4 — a result claiming evidence without evidence is invalid
# --------------------------------------------------------------------------- #


def test_an_empty_evidence_claim_is_refused() -> None:
    trace = _mint(1).with_stage(execution_ref="job-abc123")
    with pytest.raises(TraceEvidenceError):
        trace.with_evidence(evidence_refs=())
    with pytest.raises(TraceEvidenceError):
        trace.with_evidence(evidence_refs=[])


def test_a_malformed_evidence_reference_is_refused() -> None:
    trace = _mint(1).with_stage(execution_ref="job-abc123")
    for bad in (
        "",
        "   ",
        "sha256:xyz",
        "sha256:" + "a" * 63,
        "/etc/passwd",
        "https://x/y",
        "a" * 65,
    ):
        with pytest.raises(ValidationError):
            trace.with_evidence(evidence_refs=(bad,))


def test_verified_success_requires_a_measured_reference() -> None:
    digest = "sha256:" + "0" * 64
    trace = _mint(1).with_stage(execution_ref="job-abc123").with_evidence(evidence_refs=(digest,))
    assert trace.outcome is TraceOutcome.SUCCEEDED_VERIFIED
    assert trace.is_at_least(TraceOutcome.SUCCEEDED_VERIFIED)


def test_evidence_refs_are_bounded() -> None:
    trace = _mint(1).with_stage(execution_ref="job-abc123")
    with pytest.raises(ValidationError):
        trace.with_evidence(evidence_refs=tuple("sha256:" + "0" * 64 for _ in range(33)))


# --------------------------------------------------------------------------- #
# I5 — a failed execution cannot be represented as successful
# --------------------------------------------------------------------------- #


def test_a_failure_is_not_promotable_by_evidence() -> None:
    trace = (
        _mint(1)
        .with_stage(execution_ref="job-abc123")
        .with_failure(failure_code="probe_failed", execution_ref="job-abc123")
        .with_evidence(evidence_refs=("sha256:" + "0" * 64,))
    )
    assert trace.outcome is TraceOutcome.FAILED
    assert not trace.is_at_least(TraceOutcome.SUCCEEDED_UNVERIFIED)
    assert not trace.is_at_least(TraceOutcome.SUCCEEDED_VERIFIED)


def test_a_refusal_is_not_promotable_by_evidence() -> None:
    trace = (
        _mint(1)
        .with_refusal(decision_ref="permission:D")
        .with_evidence(evidence_refs=("sha256:" + "0" * 64,))
    )
    assert trace.outcome is TraceOutcome.REFUSED
    assert not trace.is_at_least(TraceOutcome.SUCCEEDED_UNVERIFIED)


def test_a_refused_trace_cannot_gain_an_execution() -> None:
    refused = _mint(1).with_refusal(decision_ref="permission:D")
    with pytest.raises(ValidationError):
        refused.with_stage(execution_ref="job-abc123")


def test_a_failure_must_name_its_stage() -> None:
    with pytest.raises(TraceLinkError):
        _mint(1).with_failure(failure_code="queue_full")


# --------------------------------------------------------------------------- #
# I6 — idempotent retries preserve the same causal identity
# --------------------------------------------------------------------------- #


def test_a_retry_of_the_same_request_keeps_one_identity() -> None:
    first = _mint(9)
    second = _mint(9)
    assert first == second
    assert first.trace_id == second.trace_id


def test_reapplying_the_same_stage_is_idempotent() -> None:
    trace = _mint(9)
    once = trace.with_stage(execution_ref="job-abc123", command_ref="cmd-1")
    twice = trace.with_stage(execution_ref="job-abc123", command_ref="cmd-1")
    assert once == twice


def test_two_messages_never_share_an_identity() -> None:
    assert _mint(1).trace_id != _mint(2).trace_id


# --------------------------------------------------------------------------- #
# I7 — surface-specific code cannot fabricate execution truth
# --------------------------------------------------------------------------- #


def test_there_is_no_outcome_constructor_parameter() -> None:
    assert "outcome" not in ExecutionTrace.model_fields
    with pytest.raises(ValidationError):
        ExecutionTrace(
            trace_id=trace_id_for(SURFACE, _key(1)),
            surface=SURFACE,
            actor_ref=ACTOR,
            idempotency_key=_key(1),
            created_at="2026-09-29T20:00:00Z",
            outcome=TraceOutcome.SUCCEEDED_VERIFIED,
        )


def test_a_freshly_minted_trace_is_never_successful() -> None:
    for index in range(50):
        assert _mint(index).outcome is TraceOutcome.UNKNOWN


def test_a_queue_row_cannot_mint_execution_identity() -> None:
    """A row's trace_id is trusted only if the surface would have derived it."""
    key = _key(11)
    row_trace_id = trace_id_for(SURFACE, key)
    assert verify_bound_trace_id(surface=SURFACE, idempotency_key=key, trace_id=row_trace_id)
    forged = "tr-" + "f" * 32
    with pytest.raises(TraceLinkError):
        verify_bound_trace_id(surface=SURFACE, idempotency_key=key, trace_id=forged)
    # A row that borrows another request's identity is equally refused.
    with pytest.raises(TraceLinkError):
        verify_bound_trace_id(
            surface=SURFACE, idempotency_key=key, trace_id=trace_id_for(SURFACE, _key(12))
        )


def test_a_row_cannot_widen_its_own_surface() -> None:
    key = _key(11)
    with pytest.raises(TraceLinkError):
        verify_bound_trace_id(
            surface="api.creative", idempotency_key=key, trace_id=trace_id_for(SURFACE, key)
        )


# --------------------------------------------------------------------------- #
# payload minimization + transport
# --------------------------------------------------------------------------- #


def test_the_envelope_has_no_field_that_can_hold_content() -> None:
    forbidden = {
        "prompt",
        "message",
        "text",
        "body",
        "content",
        "url",
        "path",
        "secret",
        "token",
        "key",
        "api_key",
        "payload",
        "input",
        "output",
        "reason",
        "detail",
        "error",
    }
    assert not forbidden & set(ExecutionTrace.model_fields)


def test_a_reference_cannot_smuggle_whitespace_or_a_newline() -> None:
    trace = _mint(1)
    for bad in ("line one\nline two", "tab\there", "trailing ", " leading"):
        with pytest.raises((ValidationError, TraceError)):
            trace.with_stage(execution_ref=bad)


def test_the_actor_reference_is_a_scope_not_an_identity() -> None:
    trace = _mint(1)
    assert trace.actor_ref.startswith("tg:")
    with pytest.raises((ValidationError, TraceError)):
        mint_trace(
            surface=SURFACE,
            actor_ref="Ada Lovelace <ada@example.com>",
            idempotency_key=_key(1),
            created_at="2026-09-29T20:00:00Z",
        )


def test_the_trace_survives_a_queue_row_round_trip() -> None:
    """The trace travels as JSON inside payload_json; it must not drift."""
    trace = (
        _mint(1, intent_ref="edit:trim")
        .with_stage(decision_ref="permission:B", command_ref="cmd-1", execution_ref="job-abc123")
        .with_evidence(evidence_refs=("sha256:" + "0" * 64,))
    )
    row = json.loads(json.dumps({"trace": trace.model_dump(mode="json")}))
    restored = ExecutionTrace.model_validate(row["trace"])
    assert restored == trace
    assert restored.outcome is TraceOutcome.SUCCEEDED_VERIFIED


def test_the_schema_version_is_pinned() -> None:
    assert TRACE_SCHEMA_VERSION == 1
    trace = _mint(1)
    assert trace.schema_version == 1
    with pytest.raises(ValidationError):
        ExecutionTrace.model_validate({**trace.model_dump(mode="json"), "schema_version": 2})


def test_extra_fields_are_refused() -> None:
    trace = _mint(1)
    with pytest.raises(ValidationError):
        ExecutionTrace.model_validate({**trace.model_dump(mode="json"), "surprise": 1})
