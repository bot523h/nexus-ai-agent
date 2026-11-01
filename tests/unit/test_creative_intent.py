"""Canonical creative intent identity and hostile-input validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.application.creative_intent import CanonicalCreativeIntent


def _intent(**changes: object) -> CanonicalCreativeIntent:
    values: dict[str, object] = {
        "project_id": "project-local",
        "proposal_id": "proposal_" + "a" * 64,
        "source_text_sha256": "sha256:" + "b" * 64,
        "operation": "timeline.trim",
        "input_data": {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
    }
    values.update(changes)
    return CanonicalCreativeIntent.create(**values)  # type: ignore[arg-type]


def test_intent_identity_is_stable_and_content_addressed() -> None:
    first = _intent()
    reordered = _intent(
        input_data={"out_point_us": 1_000_000, "in_point_us": 0, "clip_asset_id": "src"}
    )
    assert first == reordered
    assert first.intent_id.startswith("intent_")
    assert _intent(project_id="other-project").intent_id != first.intent_id
    assert _intent(proposal_id="proposal_" + "c" * 64).intent_id != first.intent_id
    assert _intent(source_text_sha256="sha256:" + "d" * 64).intent_id != first.intent_id
    changed = dict(first.input)
    changed["out_point_us"] = 2_000_000
    assert _intent(input_data=changed).intent_id != first.intent_id


def test_intent_id_cannot_be_forged_independently_of_content() -> None:
    data = _intent().model_dump(mode="json")
    data["intent_id"] = "intent_" + "0" * 64
    with pytest.raises(ValidationError, match="canonical content"):
        CanonicalCreativeIntent.model_validate(data)


@pytest.mark.parametrize("project_id", ["../secret", "a..b", "/absolute", "with space", "x" * 129])
def test_project_is_an_opaque_bounded_local_reference(project_id: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _intent(project_id=project_id)


def test_intent_rejects_extra_fields_and_non_finite_input() -> None:
    data = _intent().model_dump(mode="json")
    data["permissions"] = ["project:write"]
    with pytest.raises(ValidationError):
        CanonicalCreativeIntent.model_validate(data)
    with pytest.raises(ValueError, match="finite JSON|JSON compliant"):
        _intent(input_data={"bad": float("nan")})
