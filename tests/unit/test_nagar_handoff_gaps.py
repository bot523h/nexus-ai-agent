"""Coverage closure for the creative handoff's fail-closed helpers.

Each case pins one refusal the main handoff suite does not reach: an ambiguous
source asset, an unreadable media file, an empty trim window after clamping, a
canonical intent without source text, a non-canonical proposal, a schema lookup
miss, and an empty free-text intent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus_ai_agent.nagar.creative import handoff as h
from nexus_ai_agent.nagar.creative.handoff import (
    CanonicalHandoffFacts,
    HandoffRefusal,
    build_creative_payload,
    run_free_text_intent_durable,
    source_asset_facts,
    stage_source_media,
)


class _FakeProposal:
    def __init__(self, operation: str, inputs: dict) -> None:
        self.operation = operation
        self.input = inputs


def _call(proposal: object, **overrides: object) -> dict:
    kwargs: dict[str, object] = dict(
        workspace_dir="/w/creative_x",
        input_path="/w/creative_x/input.mp4",
        media_duration_us=1_000_000,
        user_id=1,
        chat_id=1,
        lang="en",
        idempotency_key="k",
    )
    kwargs.update(overrides)
    return build_creative_payload(proposal, **kwargs)  # type: ignore[arg-type]


def _project_with_videos(*durations: int) -> object:
    class _Asset:
        def __init__(self, asset_id: str, duration_us: int) -> None:
            self.asset_id = asset_id
            self.media_kind = "video"
            self.duration_us = duration_us

    class _Project:
        def __init__(self) -> None:
            self.assets = [_Asset(f"v{i}", d) for i, d in enumerate(durations)]

    return _Project()


def test_an_ambiguous_source_asset_is_refused() -> None:
    with pytest.raises(KeyError, match="expected exactly one source video asset"):
        source_asset_facts(_project_with_videos(1_000, 2_000), None)


def test_a_source_asset_without_a_measured_duration_is_refused() -> None:
    project = _project_with_videos(1_000)
    project.assets[0].duration_us = 0  # type: ignore[attr-defined]
    with pytest.raises(KeyError, match="no measured duration"):
        source_asset_facts(project, "v0")


def test_an_unreadable_media_path_measures_as_none(tmp_path: Path) -> None:
    assert h._measure(str(tmp_path)) is None  # a directory cannot be read as media


def test_staging_a_source_already_in_place_does_not_copy_it(tmp_path: Path) -> None:
    workspace = tmp_path / "creative_x"
    workspace.mkdir()
    staged = workspace / "input.mp4"
    staged.write_bytes(b"media")
    assert stage_source_media(str(staged), str(workspace)) == str(staged)
    assert staged.read_bytes() == b"media"


def test_staging_a_missing_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HandoffRefusal) as info:
        stage_source_media(str(tmp_path / "nope.mp4"), str(tmp_path / "w"))
    assert info.value.reason == "missing_source"


def test_a_trim_window_emptied_by_the_measured_duration_is_refused() -> None:
    with pytest.raises(HandoffRefusal) as info:
        _call(
            _FakeProposal("timeline.trim", {"in_point_us": 1_500_000, "out_point_us": 2_000_000}),
            media_duration_us=1_000_000,
        )
    assert info.value.reason == "invalid_points"


def test_a_canonical_intent_needs_normalized_source_text() -> None:
    facts = CanonicalHandoffFacts(principal_id="p", channel_binding_id="c", project_id="proj")
    with pytest.raises(HandoffRefusal) as info:
        _call(
            _FakeProposal("timeline.trim", {"in_point_us": 0, "out_point_us": 100}),
            canonical=facts,
            source_text=None,
        )
    assert info.value.reason == "invalid_identity"


def test_a_canonical_proposal_may_only_use_the_staged_source() -> None:
    facts = CanonicalHandoffFacts(principal_id="p", channel_binding_id="c", project_id="proj")
    with pytest.raises(HandoffRefusal) as info:
        _call(
            _FakeProposal(
                "timeline.trim", {"in_point_us": 0, "out_point_us": 100, "clip_asset_id": "other"}
            ),
            canonical=facts,
            source_text="trim it",
        )
    assert info.value.reason == "invalid_proposal"


def test_a_proposal_that_is_not_canonical_json_is_refused() -> None:
    facts = CanonicalHandoffFacts(principal_id="p", channel_binding_id="c", project_id="proj")
    with pytest.raises(HandoffRefusal) as info:
        _call(
            _FakeProposal("timeline.trim", {"in_point_us": 0, "out_point_us": 100}),
            canonical=facts,
            source_text="trim it",
        )
    assert info.value.reason == "invalid_proposal"  # no model_dump: not a typed proposal


def test_a_schema_lookup_miss_degrades_to_an_empty_hint() -> None:
    class _BrokenGateway:
        def _bus_registry(self) -> object:
            raise RuntimeError("no registry wired")

    assert h._schema_for(_BrokenGateway(), "timeline.trim", "input_schema") == {}


def test_an_empty_free_text_intent_is_refused_without_touching_the_queue() -> None:
    outcome = asyncio.run(
        run_free_text_intent_durable(
            "   ",
            gateway=object(),
            queue=object(),
            project=object(),
            source_path="/x.mp4",
            workspace_dir="/w",
            user_id=1,
            chat_id=1,
        )
    )
    assert outcome.status == "refused"
    assert outcome.refusal_reason == "malformed"


def test_an_unreadable_source_measures_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"media")

    def _denied(self: Path, *_a: object, **_k: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", _denied)
    assert h._measure(str(media)) is None


@pytest.mark.parametrize("out_value", ["100", True, 1.5])
def test_a_non_integer_out_point_is_refused(out_value: object) -> None:
    with pytest.raises(HandoffRefusal) as info:
        _call(_FakeProposal("timeline.trim", {"in_point_us": 0, "out_point_us": out_value}))
    assert info.value.reason == "invalid_points"


def test_a_trim_without_a_measured_duration_is_not_clamped() -> None:
    payload = _call(
        _FakeProposal("timeline.trim", {"in_point_us": 0, "out_point_us": 3_000_000}),
        media_duration_us=0,
    )
    assert payload["args"] == ["0", "3"]


def test_intent_text_must_be_a_string() -> None:
    with pytest.raises(TypeError, match="must be a string"):
        h.normalize_intent(42)  # type: ignore[arg-type]


def test_a_named_clip_asset_must_exist_in_the_project() -> None:
    with pytest.raises(KeyError, match="is not in the project"):
        source_asset_facts(_project_with_videos(1_000), "ghost")


class _Actor:
    kind = "user"
    actor_id = "user-7"


class _Gateway:
    project_id = "proj-h"
    actor = _Actor()
    producer = None

    def __init__(self, decision: object) -> None:
        self._decision = decision

    def _bus_registry(self) -> object:
        raise RuntimeError("no registry in this slice")

    def route(self, request: object) -> object:
        return self._decision


class _Decision:
    blocked = True
    reason_code = "policy_blocked"
    explanation = "the policy forbids this route"


def test_a_routing_block_is_a_typed_refusal_and_enqueues_nothing() -> None:
    project = _project_with_videos(1_000)
    project.project_id = "proj-h"  # type: ignore[attr-defined]
    queue_calls: list[object] = []

    class _Queue:
        def enqueue(self, *a: object, **k: object) -> None:
            queue_calls.append((a, k))

    outcome = asyncio.run(
        run_free_text_intent_durable(
            "trim the clip",
            gateway=_Gateway(_Decision()),
            queue=_Queue(),
            project=project,
            source_path="/w/input.mp4",
            workspace_dir="/w",
            user_id=1,
            chat_id=1,
        )
    )
    assert outcome.status == "blocked"
    assert outcome.refusal_reason == "policy_blocked"
    assert queue_calls == []
