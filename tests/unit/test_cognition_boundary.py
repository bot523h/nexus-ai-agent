"""Unit + adversarial tests for the Nagar cognition boundary.

The boundary's whole job is to make reasoning *optional* and *authority-free*.
These tests pin exactly that:

* NullCognition is a real Model Kill Test — it refuses, never fabricates;
* ``parse_proposal`` fails closed on every adversarial shape the mission
  names (malformed JSON, schema violation, extra fields, NaN/Infinity,
  forged authority, disallowed operation, unknown version);
* a proposal can never carry authority into the command envelope;
* the bridge builds a *candidate* command that still needs the bus to
  authorize it.
"""

from __future__ import annotations

import json

import pytest

from nexus_ai_agent.creative.studio.models import ActorIdentity
from nexus_ai_agent.nagar.cognition import (
    PROPOSAL_SCHEMA_VERSION,
    CognitionBudget,
    CognitionContext,
    CognitionPort,
    NullCognition,
    ProducerIdentity,
    ProposalProvenance,
    ProposalSchema,
    Refusal,
    RefusalReason,
    TypedProposal,
    parse_proposal,
    proposal_to_command,
)

# -- helpers ---------------------------------------------------------------


def _schema(*operations: str, schema_id: str = "nagar.test.v1") -> ProposalSchema:
    return ProposalSchema(
        schema_id=schema_id,
        schema_version=1,
        allowed_operations=frozenset(operations or {"timeline.trim"}),
    )


def _provenance() -> ProposalProvenance:
    return ProposalProvenance(
        producer=ProducerIdentity(kind="model", name="test-model"),
        created_at="2026-01-01T00:00:00Z",
        source="test",
    )


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_id": "nagar.test.v1",
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "operation": "timeline.trim",
        "input": {"clip_asset_id": "clip_1", "in_point_us": 0, "out_point_us": 1_000_000},
        "rationale": "trim to the requested window",
        "confidence": 0.8,
        "provenance": _provenance().model_dump(mode="json"),
    }
    payload.update(overrides)
    return payload


# -- NullCognition: the Model Kill Test ------------------------------------


async def test_null_cognition_always_refuses_and_never_fabricates() -> None:
    provider = NullCognition()
    result = await provider.propose(
        CognitionContext(subject_id="p1"),
        _schema(),
        CognitionBudget(),
    )
    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.PRODUCER_REFUSED
    assert result.provenance is not None
    assert result.provenance.source == "null"


async def test_null_cognition_is_a_cognition_port() -> None:
    assert isinstance(NullCognition(), CognitionPort)


async def test_null_cognition_does_not_raise_even_with_an_empty_schema() -> None:
    # A schema that offers nothing must still yield an honest refusal, not a
    # crash: the substrate stays alive with no model and no candidates.
    provider = NullCognition()
    result = await provider.propose(
        CognitionContext(subject_id="p1"),
        ProposalSchema(schema_id="empty", schema_version=1, allowed_operations=frozenset()),
        CognitionBudget(max_attempts=1, max_wall_clock_seconds=0.001),
    )
    assert isinstance(result, Refusal)


# -- parse_proposal: happy path --------------------------------------------


def test_parse_accepts_a_valid_proposal_from_json_text() -> None:
    parsed = parse_proposal(json.dumps(_valid_payload()), _schema())
    assert isinstance(parsed, TypedProposal)
    assert parsed.operation == "timeline.trim"
    assert parsed.confidence == 0.8


def test_parse_accepts_an_already_typed_proposal() -> None:
    typed = TypedProposal.model_validate(_valid_payload())
    assert parse_proposal(typed, _schema()) is typed or isinstance(
        parse_proposal(typed, _schema()), TypedProposal
    )


def test_parse_passes_through_an_explicit_refusal() -> None:
    refusal = Refusal(reason=RefusalReason.PRODUCER_REFUSED, detail="cannot")
    assert parse_proposal(refusal, _schema()) is refusal


# -- parse_proposal: adversarial shapes ------------------------------------


def test_malformed_json_is_refused() -> None:
    assert parse_proposal("{not json", _schema()).reason is RefusalReason.MALFORMED  # type: ignore[union-attr]


def test_non_object_json_is_refused() -> None:
    assert parse_proposal("[1, 2, 3]", _schema()).reason is RefusalReason.MALFORMED  # type: ignore[union-attr]


def test_unsupported_type_is_refused() -> None:
    assert parse_proposal(42, _schema()).reason is RefusalReason.MALFORMED  # type: ignore[arg-type,union-attr]


def test_schema_violation_missing_required_field_is_refused() -> None:
    payload = _valid_payload()
    del payload["operation"]
    result = parse_proposal(payload, _schema())
    assert result.reason is RefusalReason.SCHEMA_VIOLATION  # type: ignore[union-attr]


def test_extra_field_is_refused_by_forbid() -> None:
    result = parse_proposal(_valid_payload(extra_field="smuggled"), _schema())
    assert result.reason is RefusalReason.SCHEMA_VIOLATION  # type: ignore[union-attr]


def test_unknown_schema_version_is_refused_not_coerced() -> None:
    result = parse_proposal(_valid_payload(schema_version=99), _schema())
    assert result.reason is RefusalReason.UNSUPPORTED_SCHEMA_VERSION  # type: ignore[union-attr]


def test_schema_id_mismatch_is_refused() -> None:
    result = parse_proposal(_valid_payload(schema_id="another.question"), _schema())
    assert result.reason is RefusalReason.SCHEMA_VIOLATION  # type: ignore[union-attr]


def test_operation_outside_the_offered_set_is_refused() -> None:
    result = parse_proposal(_valid_payload(operation="system.undo"), _schema())
    assert result.reason is RefusalReason.DISALLOWED_OPERATION  # type: ignore[union-attr]


def test_empty_allow_set_refuses_every_operation() -> None:
    empty = ProposalSchema(
        schema_id="nagar.test.v1", schema_version=1, allowed_operations=frozenset()
    )
    result = parse_proposal(_valid_payload(), empty)
    assert result.reason is RefusalReason.DISALLOWED_OPERATION  # type: ignore[union-attr]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_refused(constant: str) -> None:
    text = (
        '{"schema_id":"nagar.test.v1","schema_version":1,'
        '"operation":"timeline.trim","input":{"x":' + constant + "},"
        '"provenance":{"producer":{"kind":"m","name":"m"},"created_at":"t"}}'
    )
    result = parse_proposal(text, _schema())
    assert result.reason is RefusalReason.NON_FINITE  # type: ignore[union-attr]


def test_non_finite_in_a_python_mapping_is_refused() -> None:
    payload = _valid_payload()
    payload["confidence"] = float("nan")
    result = parse_proposal(payload, _schema())
    assert result.reason is RefusalReason.NON_FINITE  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "field",
    ["actor", "permissions", "authorization", "execution_policy", "confirmed", "shell", "sudo"],
)
def test_authority_fields_are_refused_by_name(field: str) -> None:
    result = parse_proposal(_valid_payload(**{field: {"any": "thing"}}), _schema())
    assert result.reason is RefusalReason.AUTHORITY_FIELD  # type: ignore[union-attr]


def test_authority_field_reason_names_the_field() -> None:
    result = parse_proposal(_valid_payload(actor={"kind": "user", "actor_id": "root"}), _schema())
    assert "actor" in result.detail  # type: ignore[union-attr]


def test_confidence_out_of_range_is_refused() -> None:
    assert (
        parse_proposal(_valid_payload(confidence=1.5), _schema()).reason
        is RefusalReason.SCHEMA_VIOLATION
    )  # type: ignore[union-attr]
    assert (
        parse_proposal(_valid_payload(confidence=-0.1), _schema()).reason
        is RefusalReason.SCHEMA_VIOLATION
    )  # type: ignore[union-attr]


def test_parse_never_raises_for_bad_producer_output() -> None:
    # A broad set of hostile inputs must all return a Refusal, never raise.
    for bad in [None, 3.14, b"\xff\xfe", "{}", [], {"operation": 5}]:
        result = parse_proposal(bad, _schema())  # type: ignore[arg-type]
        assert isinstance(result, Refusal)


def test_bool_schema_version_is_not_coerced_to_int() -> None:
    # ``True == 1`` in Python; a bool must never satisfy the int version gate.
    result = parse_proposal(_valid_payload(schema_version=True), _schema())
    assert result.reason is RefusalReason.UNSUPPORTED_SCHEMA_VERSION  # type: ignore[union-attr]


def test_float_schema_version_is_not_coerced_to_int() -> None:
    result = parse_proposal(_valid_payload(schema_version=1.0), _schema())
    assert result.reason is RefusalReason.UNSUPPORTED_SCHEMA_VERSION  # type: ignore[union-attr]


def test_bool_confidence_is_refused_not_coerced() -> None:
    result = parse_proposal(_valid_payload(confidence=True), _schema())
    assert result.reason is RefusalReason.SCHEMA_VIOLATION  # type: ignore[union-attr]


def test_oversized_input_is_refused() -> None:
    result = parse_proposal(_valid_payload(input={"x": "a" * 600_000}), _schema())
    assert result.reason is RefusalReason.SCHEMA_VIOLATION  # type: ignore[union-attr]


def test_non_finite_in_a_python_input_mapping_is_refused() -> None:
    result = parse_proposal(_valid_payload(input={"x": float("inf")}), _schema())
    assert result.reason is RefusalReason.NON_FINITE  # type: ignore[union-attr]


def test_authority_like_keys_inside_input_are_left_to_the_operation_schema() -> None:
    # ``input`` is the operation's own payload; the boundary does not police
    # its keys (the operation's Pydantic model does).  This pins that the
    # boundary's authority check is on the *proposal envelope*, not a blanket
    # string scan that would be trivially bypassed.
    result = parse_proposal(_valid_payload(input={"actor": "x"}), _schema())
    assert isinstance(result, TypedProposal)


# -- bridge: a proposal becomes a *candidate* command ----------------------


def test_bridge_builds_a_schema2_command_without_self_granted_authority() -> None:
    parsed = parse_proposal(_valid_payload(), _schema())
    assert isinstance(parsed, TypedProposal)
    command = proposal_to_command(
        parsed,
        actor=ActorIdentity(kind="user", actor_id="u1"),
        project_id="proj-1",
        operation_schema_version=1,
    )
    # The command is schema 2, so the bus will demand a trusted authorizer.
    assert command.schema_version == 2
    assert command.actor == ActorIdentity(kind="user", actor_id="u1")
    assert command.target.project_id == "proj-1"
    assert command.operation == "timeline.trim"
    assert command.provenance is not None
    assert command.provenance.source == "agent"
    # The proposal cannot choose its own execution policy.
    assert command.execution_policy.network_access is False
    assert command.confirmed is False


def test_bridge_carries_the_proposal_input_verbatim() -> None:
    parsed = parse_proposal(_valid_payload(), _schema())
    assert isinstance(parsed, TypedProposal)
    command = proposal_to_command(
        parsed,
        actor=ActorIdentity(kind="service", actor_id="worker"),
        project_id="proj-1",
        operation_schema_version=1,
    )
    assert command.input == parsed.input


def test_bridge_passes_an_explicit_idempotency_key() -> None:
    parsed = parse_proposal(_valid_payload(), _schema())
    assert isinstance(parsed, TypedProposal)
    command = proposal_to_command(
        parsed,
        actor=ActorIdentity(kind="service", actor_id="worker"),
        project_id="proj-1",
        operation_schema_version=1,
        idempotency_key="creative:1:2:3",
    )
    assert command.idempotency_key == "creative:1:2:3"
