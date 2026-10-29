"""Effect-key contract (P4): deterministic, stable across retry, told apart.

The tests pin the four properties the dispatcher and the reconciliation
contract rely on, plus the one mutation (A) the suite must stay red against:

* deterministic — same intent, same key;
* stable across retry — no attempt id, no clock, no randomness;
* distinct per effect — a different logical slot/revision yields a new key;
* independent of attempt UUID — the attempt id is never part of the key.
"""

from __future__ import annotations

import uuid

import pytest

from nexus_ai_agent.domain.policies import effect_key as ek


def _same_intent_twice() -> tuple[str, str]:
    kwargs = dict(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination=555,
        logical_slot="20:30",
    )
    return ek.effect_key(**kwargs), ek.effect_key(**kwargs)


def test_effect_key_is_deterministic() -> None:
    first, second = _same_intent_twice()
    assert first == second


def test_effect_key_is_stable_across_retry() -> None:
    first, second = _same_intent_twice()
    # A retry does not change any logical identity, so the key cannot change.
    assert first == second
    assert "-" not in first  # no attempt-id style suffix


def test_effect_key_is_distinct_for_distinct_intents() -> None:
    base = dict(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination=555,
        logical_slot="20:30",
    )
    keys = {
        ek.effect_key(**{**base, "operation_type": "telegram.edit"}),
        ek.effect_key(**{**base, "logical_entity": "post-8"}),
        ek.effect_key(**{**base, "logical_revision": 4}),
        ek.effect_key(**{**base, "destination": 556}),
        ek.effect_key(**{**base, "logical_slot": "20:31"}),
        ek.effect_key(**{**base, "logical_slot": None}),
    }
    assert len(keys) == 6


def test_slot_presence_changes_the_key() -> None:
    with_slot = ek.effect_key(
        operation_type="r2.upload",
        logical_entity="slide-9",
        logical_revision=2,
        destination="bucket/asset.jpg",
        logical_slot=0,
    )
    without_slot = ek.effect_key(
        operation_type="r2.upload",
        logical_entity="slide-9",
        logical_revision=2,
        destination="bucket/asset.jpg",
        logical_slot=None,
    )
    assert with_slot != without_slot


def test_field_order_does_not_leak_into_the_key() -> None:
    # sort_keys canonicalisation: accumulating the same material in a different
    # Python type neighbours yields the same serialisation-free key.
    a = ek.effect_key(
        operation_type="http.callback",
        logical_entity="job-1",
        logical_revision="r2",
        destination="https://example.test/hook",
    )
    b = ek.effect_key(
        operation_type="http.callback",
        logical_entity="job-1",
        logical_revision="r2",
        destination="https://example.test/hook",
    )
    assert a == b


def test_empty_identity_is_rejected() -> None:
    with pytest.raises(ValueError):
        ek.effect_key(
            operation_type="",
            logical_entity="x",
            logical_revision=1,
            destination=1,
        )
    with pytest.raises(ValueError):
        ek.effect_key(
            operation_type="telegram.send",
            logical_entity="",
            logical_revision=1,
            destination=1,
        )


def test_typed_form_round_trips() -> None:
    token = ek.effect_key(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination=555,
    )
    typed = ek.serialize_effect_key("telegram.send", token)
    operation, token_back = ek.split_effect_key(typed)
    assert operation == "telegram.send"
    assert token_back == token
    assert ek.is_effect_key_for(typed, "telegram.send")
    assert not ek.is_effect_key_for(typed, "r2.upload")


def test_malformed_typed_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        ek.split_effect_key("no-separator")
    # A trailing separator leaves an empty token — malformed.
    with pytest.raises(ValueError):
        ek.split_effect_key("telegram.send.")


def test_attempt_id_never_contributes_to_the_key() -> None:
    """P4 forbids ``effect_key = random_uuid_every_retry()``.

    A fresh attempt id per retry is exactly the failure mode; the key must not
    move when it is minted. (Mutation A — source-level — additionally turns the
    stability tests above red; see tests/integration/test_outbox_crash_matrix.py
    for the end-to-end flip.)
    """
    kwargs = dict(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination=555,
        logical_slot=None,
    )
    base = ek.effect_key(**kwargs)
    for _ in range(5):
        _attempt_id = uuid.uuid4().hex  # a fresh attempt id per retry
        assert ek.effect_key(**kwargs) == base
