"""Fail-closed validation of ``storyboard.render`` payloads before anything is enqueued."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from nexus_ai_agent.nagar.creative import handoff as h

SCRIPT = "INT. KITCHEN - DAY\nSara: Hello.\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "project_id": "proj-1",
        "job_id": "job-1",
        "screenplay_text": SCRIPT,
        "source_sha256": _digest(SCRIPT),
        "manifest_sha256": None,
        "approval_id": "appr-1",
        "principal_id": "owner",
        "width": 320,
        "height": 180,
        "fps": 12,
        "sample_rate": 16000,
    }
    base.update(overrides)
    return base


def test_valid_payload_is_accepted_and_profile_keeps_numeric_types() -> None:
    request = h.validate_storyboard_render_request(_payload())
    assert request.project_id == "proj-1"
    assert request.profile == {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}
    assert isinstance(request.profile["fps"], int)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (["not", "a", "mapping"], "malformed"),
        (_payload(screenplay_text="   \n\t"), "invalid_screenplay"),
        (_payload(screenplay_text=None), "invalid_screenplay"),
        (_payload(screenplay_text="A\x00B"), "invalid_screenplay"),
        (_payload(screenplay_text="a" * (h.MAX_SCREENPLAY_CHARS + 1)), "invalid_screenplay"),
        (_payload(source_sha256=None), "invalid_digest"),
        (_payload(source_sha256=_digest(SCRIPT).upper()), "invalid_digest"),
        (_payload(source_sha256="0" * 64), "source_digest_mismatch"),
        (_payload(manifest_sha256="not-a-digest"), "invalid_digest"),
        (_payload(approval_id=None), "invalid_approval"),
        (_payload(approval_id=""), "invalid_approval"),
        (_payload(principal_id=None), "invalid_identity"),
        (_payload(principal_id="x" * 129), "invalid_identity"),
    ],
)
def test_malformed_payload_is_refused_with_a_typed_reason(payload: Any, reason: str) -> None:
    with pytest.raises(h.HandoffRefusal) as info:
        h.validate_storyboard_render_request(payload)
    assert info.value.reason == reason
