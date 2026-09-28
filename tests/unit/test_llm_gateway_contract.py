"""W2 — the gateway contract: requests, responses, usage and policy outcomes.

The contract is what makes "one authority" meaningful: every caller speaks it,
every adapter answers it, and every failure is expressed in it. These tests pin
the invariants that keep it honest — validation at construction (not deep inside
a provider call), usage that is either real or explicitly unknown, and a
degradation flag that cannot be forgotten.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.llm.gateway.contract import (
    AttemptRecord,
    Caller,
    CallerCategory,
    ContentPart,
    FinishReason,
    GenerationParams,
    LLMOperation,
    LLMPriority,
    LLMRequest,
    LLMResponse,
    Message,
    Modality,
    PolicyOutcome,
    Timings,
    Usage,
    UsageSource,
    new_request_id,
)


def _caller() -> Caller:
    return Caller(category=CallerCategory.AGENT, name="test.caller", tenant_id=7)


# ── Caller identity ─────────────────────────────────────────────────────


def test_caller_requires_a_category_and_a_name() -> None:
    with pytest.raises(ValueError):
        Caller(category=CallerCategory.AGENT, name="")
    with pytest.raises(ValueError):
        Caller(category=CallerCategory.AGENT, name="   ")


def test_caller_label_is_stable_and_log_safe() -> None:
    caller = _caller()
    assert caller.label == "agent:test.caller"
    assert caller.tenant_id == 7


def test_caller_name_is_bounded() -> None:
    with pytest.raises(ValueError):
        Caller(category=CallerCategory.AGENT, name="n" * 200)


def test_caller_categories_cover_every_consumer_found_in_the_call_graph() -> None:
    """The Phase-3 inventory, encoded: every real caller path has a category.

    If a new consumer appears without one, it will be forced to pick a category
    rather than sneak in as "unknown" — which is how observability rots.
    """

    names = {member.value for member in CallerCategory}
    assert {
        "agent",
        "surface",
        "summarizer",
        "planner",
        "memory",
        "knowledge",
        "creative",
        "background_job",
        "system",
    } <= names


# ── LLMPriority parity with the legacy queue ────────────────────────────


def test_priority_values_match_the_legacy_request_queue() -> None:
    """A request must cross either scheduler without translation.

    ``features/request_queue.Priority`` is hardened under a separate board task;
    if its values ever drift, this test fails and the drift becomes a decision
    instead of a silent behaviour change.
    """

    from nexus_ai_agent.features.request_queue import Priority

    assert int(LLMPriority.OWNER) == int(Priority.OWNER)
    assert int(LLMPriority.REFERRAL_BONUS) == int(Priority.REFERRAL_BONUS)
    assert int(LLMPriority.NORMAL) == int(Priority.NORMAL)
    assert int(LLMPriority.LOW) == int(Priority.LOW)


# ── request validation happens at construction ──────────────────────────


def test_request_needs_something_to_send() -> None:
    with pytest.raises(ValueError, match="prompt, messages, or parts"):
        LLMRequest(caller=_caller())


def test_request_needs_text_to_embed() -> None:
    with pytest.raises(ValueError):
        LLMRequest(caller=_caller(), operation=LLMOperation.EMBEDDINGS)


def test_purpose_is_bounded_so_it_cannot_become_free_text() -> None:
    with pytest.raises(ValueError):
        LLMRequest(caller=_caller(), prompt="hi", purpose="")
    with pytest.raises(ValueError):
        LLMRequest(caller=_caller(), prompt="hi", purpose="p" * 200)


def test_negative_deadline_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        LLMRequest(caller=_caller(), prompt="hi", deadline_seconds=-1.0)


def test_blank_provider_and_model_pins_are_rejected() -> None:
    """``""`` is not "unpinned", it is a pin to nothing — refuse it loudly."""

    with pytest.raises(ValueError):
        LLMRequest(caller=_caller(), prompt="hi", provider="  ")
    with pytest.raises(ValueError):
        LLMRequest(caller=_caller(), prompt="hi", model="")


def test_message_rejects_an_unknown_role() -> None:
    with pytest.raises(ValueError, match="unsupported message role"):
        Message(role="narrator", content="hi")


def test_metadata_keys_are_bounded() -> None:
    with pytest.raises(ValueError):
        LLMRequest(caller=_caller(), prompt="hi", metadata={"k" * 200: "v"})


def test_metadata_defaults_to_an_immutable_empty_mapping() -> None:
    """A frozen dataclass must not share one mutable default between requests."""

    first = LLMRequest(caller=_caller(), prompt="a")
    second = LLMRequest(caller=_caller(), prompt="b")
    assert dict(first.metadata) == {}
    assert dict(second.metadata) == {}
    with pytest.raises(TypeError):
        first.metadata["injected"] = "x"  # type: ignore[index]


def test_request_ids_are_unique_and_hex_shaped() -> None:
    ids = {new_request_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(request_id) == 16 for request_id in ids)
    assert all(int(request_id, 16) >= 0 for request_id in ids)


# ── modalities and payload accounting ───────────────────────────────────


def test_a_text_only_request_declares_only_text() -> None:
    request = LLMRequest(caller=_caller(), prompt="hi")
    assert request.modalities == frozenset({Modality.TEXT})
    assert request.has_multimodal_input() is False
    assert request.payload_bytes() == 0


def test_an_image_part_makes_the_request_multimodal_and_counts_its_bytes() -> None:
    request = LLMRequest(
        caller=_caller(),
        prompt="describe",
        parts=(ContentPart(mime_type="image/png", data=b"0" * 1234),),
    )
    assert request.modalities == frozenset({Modality.TEXT, Modality.IMAGE})
    assert request.has_multimodal_input() is True
    assert request.payload_bytes() == 1234


def test_parts_inside_a_history_turn_are_counted_too() -> None:
    """Losing a per-turn image from the accounting would under-report payload."""

    request = LLMRequest(
        caller=_caller(),
        messages=(
            Message(role="user", content="look", parts=(ContentPart("image/jpeg", b"1" * 99),)),
        ),
    )
    assert request.has_multimodal_input() is True
    assert request.payload_bytes() == 99
    assert Modality.IMAGE in request.modalities


def test_prompt_size_counts_characters_not_payload_content() -> None:
    """Observability records volume, never the text itself (LAW 10)."""

    request = LLMRequest(
        caller=_caller(),
        prompt="x" * 100,
        system="y" * 50,
        messages=(Message(role="user", content="z" * 25),),
    )
    assert request.prompt_size_chars() == 175


# ── usage: real or unknown, never invented (LAW 11) ─────────────────────


def test_usage_defaults_to_unknown_with_no_numbers() -> None:
    usage = Usage()
    assert usage.source is UsageSource.UNKNOWN
    assert usage.is_known is False
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.estimated_cost_usd is None


def test_provider_usage_is_known_and_carries_the_reported_model() -> None:
    usage = Usage(
        source=UsageSource.PROVIDER,
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        reported_model="gemini-2.0-flash",
    )
    assert usage.is_known is True
    assert usage.reported_model == "gemini-2.0-flash"


def test_unknown_usage_with_numbers_is_still_not_provider_truth() -> None:
    """A number without provenance must not be promoted to ``is_known``."""

    usage = Usage(source=UsageSource.UNKNOWN, input_tokens=99, output_tokens=99)
    assert usage.is_known is False


# ── policy outcome: degradation is recorded, never silent (LAW 8) ───────


def test_policy_outcome_serialises_every_decision_field() -> None:
    outcome = PolicyOutcome(
        route_provider="fake",
        route_model="fake",
        attempts=4,
        retries=3,
        fallback_used=True,
        fallback_from="gemini/gemini-2.0-flash",
        degraded=True,
        circuit_open=True,
        rate_limited_locally=True,
        idempotency_hit=False,
    )
    payload = outcome.as_dict()
    assert payload["degraded"] is True
    assert payload["fallback_from"] == "gemini/gemini-2.0-flash"
    assert payload["attempts"] == 4
    assert payload["retries"] == 3
    assert payload["circuit_open"] is True
    assert payload["rate_limited_locally"] is True
    # No field of the outcome may carry prompt text.
    assert "prompt" not in payload


def test_response_exposes_degradation_at_the_top_level() -> None:
    """A caller must be able to ask ``response.degraded`` without digging."""

    response = LLMResponse(
        request_id="r1",
        text="answer",
        provider="fake",
        model="fake",
        operation=LLMOperation.CHAT,
        caller=_caller(),
        purpose="test",
        finish_reason=FinishReason.STOP,
        usage=Usage(),
        timings=Timings(),
        policy=PolicyOutcome(route_provider="fake", route_model="fake", degraded=True),
        attempts=(
            AttemptRecord(
                index=1,
                provider="fake",
                model="fake",
                started_at=0.0,
                duration_seconds=0.5,
                outcome="success",
            ),
        ),
    )
    assert response.degraded is True
    assert response.structured is None


def test_response_record_dict_never_contains_the_answer_or_the_prompt() -> None:
    """LAW 10: observable by default, and the observation carries no payload.

    A log line that contains the model's answer contains the user's data.
    """

    response = LLMResponse(
        request_id="r1",
        text="the user's private medical history",
        provider="gemini",
        model="gemini-2.0-flash",
        operation=LLMOperation.CHAT,
        caller=Caller(category=CallerCategory.MEMORY, name="features.ai_memory", tenant_id=3),
        purpose="memory-extract",
        usage=Usage(source=UsageSource.PROVIDER, input_tokens=10, output_tokens=4),
    )
    record = response.as_record_dict()
    assert record["request_id"] == "r1"
    assert record["caller"] == "memory:features.ai_memory"
    assert record["tenant_id"] == 3
    assert record["outcome"] == "success"
    blob = str(record)
    assert "medical" not in blob
    assert "private" not in blob


def test_a_degraded_response_says_so_in_its_record() -> None:
    response = LLMResponse(
        request_id="r2",
        text="local answer",
        provider="local-degraded",
        model="fake",
        operation=LLMOperation.CHAT,
        caller=_caller(),
        purpose="test",
        policy=PolicyOutcome(
            route_provider="local-degraded",
            route_model="fake",
            degraded=True,
            fallback_from="gemini/gemini-2.0-flash",
        ),
    )
    assert response.as_record_dict()["outcome"] == "degraded_success"


def test_generation_params_default_to_nothing_so_adapters_keep_their_defaults() -> None:
    params = GenerationParams()
    assert params.temperature is None
    assert params.max_output_tokens is None
    assert params.response_mime_type is None
    assert params.stop_sequences == ()


def test_zero_temperature_is_preserved_and_not_treated_as_unset() -> None:
    """``if params.temperature:`` would silently drop the deterministic case."""

    params = GenerationParams(temperature=0.0)
    assert params.temperature == 0.0
    assert params.temperature is not None
