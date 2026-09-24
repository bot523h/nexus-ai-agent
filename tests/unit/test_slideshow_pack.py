"""Wave 2b — the slideshow pack: templates, planning rules, pure operations.

These tests exercise the *pack* only (stdlib + pydantic): no filesystem, no
network, no FFmpeg.  Real-media behaviour is covered by
``test_slideshow_engine.py`` and the end-to-end CLI test.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from nagar_helpers import TEST_ACTOR, TEST_PROVENANCE, authorized_bus
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.registry import PackRegistry
from nexus_ai_agent.creative.packs.slideshow import (
    MIN_SHOT_US,
    AssetEvidence,
    BeatGrid,
    ComposeInput,
    ImageScore,
    build_slideshow_registry,
    distribute_us,
    load_tone_templates,
    plan_slideshow,
    register_slideshow_operations,
    use_tone_library,
)
from nexus_ai_agent.creative.packs.slideshow.operations import (
    OPERATION_COMPOSE,
    OPERATION_RENDER,
    OPERATION_SCAN,
    OPERATION_SCORE,
    OPERATION_SUGGEST_TONE,
    OPERATION_UPSCALE,
    ScanAssetsInput,
    ScoreImagesInput,
    SuggestToneInput,
)
from nexus_ai_agent.creative.packs.slideshow.planning import (
    ROLE_WEIGHTS,
    ordered_evidence_ids,
    shot_weights,
    snap_boundaries,
    suggest_template_id,
)
from nexus_ai_agent.creative.packs.slideshow.templates import (
    TEMPLATES_PATH,
    TemplateError,
)
from nexus_ai_agent.creative.packs.slideshow.templates import (
    load_tone_templates as load_library,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    CommandValidationError,
    PermissionDeniedError,
    PermissionLevel,
    Playhead,
    Timeline,
    new_project,
)

PACKS_DIR = Path(__file__).parents[2] / "src" / "nexus_ai_agent" / "creative" / "packs"
LIBRARY = load_tone_templates()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _evidence(index: int, *, kind: str = "image", duration_us: int = 0) -> AssetEvidence:
    return AssetEvidence(
        evidence_id=f"img{index:02d}" if kind == "image" else f"aud{index:02d}",
        path=f"/tmp/fixture_{index}.{'jpg' if kind == 'image' else 'wav'}",
        content_sha256="sha256:" + f"{index:02x}" * 32,
        media_kind=kind,  # type: ignore[arg-type]
        width=1920 if kind == "image" else None,
        height=1080 if kind == "image" else None,
        duration_us=duration_us,
    )


def _beat_grid(*, bpm: float = 120.0, duration_us: int = 120_000_000) -> BeatGrid:
    period_us = int(60_000_000 / bpm)
    beats = tuple(range(0, duration_us, period_us))
    return BeatGrid(
        duration_us=duration_us,
        tempo_bpm=bpm,
        beats_us=beats,
        strong_beats_us=beats[::4],
        tempo_source="detected",
        alignment_quality="detected",
        confidence=0.8,
    )


def _compose_payload(
    count: int = 12, *, target_us: int = 120_000_000, template: str = "travel_documentary"
) -> dict[str, object]:
    return {
        "assets": [_evidence(index).model_dump(mode="json") for index in range(count)],
        "target_duration_us": target_us,
        "mode": "auto",
        "tone_template_id": template,
        "beat_grid": _beat_grid(duration_us=target_us).model_dump(mode="json"),
    }


def _bus() -> CommandBus:
    project = new_project(
        "proj",
        "slideshow",
        Timeline(timeline_id="tl", duration_us=0, playhead=Playhead(timecode_us=0)),
    )
    return authorized_bus(project, registry=build_slideshow_registry())


def _command(operation: str, payload: dict[str, object], *, confirmed: bool = False) -> dict:
    return {
        "protocol_version": "nagar.command.v1",
        "schema_version": 2,
        "command_id": f"cmd_{operation}_{uuid4().hex}",
        "actor": TEST_ACTOR.model_dump(mode="json"),
        "target": {"project_id": "proj"},
        "provenance": TEST_PROVENANCE.model_dump(mode="json"),
        "session_id": "test",
        "operation": operation,
        "input": payload,
        "confirmed": confirmed,
    }


# ---------------------------------------------------------------------------
# tone template library
# ---------------------------------------------------------------------------


def test_shipped_library_has_twelve_primary_templates() -> None:
    assert len(LIBRARY.ids(tier="primary")) == 12
    assert len(LIBRARY.ids(tier="alternate")) == 2
    assert "travel_documentary" in LIBRARY
    assert "nope" not in LIBRARY


def test_shipped_library_file_is_json_not_yaml() -> None:
    """No YAML dependency: the library must stay plain JSON."""
    raw = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    assert raw["schema"] == "nexus.slideshow.tone-templates.v1"
    assert len(raw["templates"]) == 14


def test_library_rejects_an_unknown_template_id() -> None:
    with pytest.raises(TemplateError, match="unknown tone template"):
        LIBRARY.get("does_not_exist")


def test_library_rejects_a_transition_longer_than_the_shortest_shot(tmp_path: Path) -> None:
    payload = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    payload["templates"][0]["transition"]["duration_seconds"] = 30.0
    broken = tmp_path / "tone_templates.json"
    broken.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TemplateError, match="failed validation"):
        load_library(broken)


def test_library_rejects_invalid_json(tmp_path: Path) -> None:
    broken = tmp_path / "tone_templates.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(TemplateError, match="not valid JSON"):
        load_library(broken)


def test_every_template_declares_a_usable_render_profile() -> None:
    for template in LIBRARY.templates:
        assert 12 <= template.render.fps <= 60
        assert 0 <= template.render.crf <= 51
        assert template.render.audio_bitrate.endswith("k")
        assert template.transition.duration_seconds < template.rhythm.min_shot_seconds
        if template.motion.kind != "static":
            assert template.motion.zoom_to != template.motion.zoom_from


# ---------------------------------------------------------------------------
# planning arithmetic
# ---------------------------------------------------------------------------


def test_distribute_us_sums_exactly_and_keeps_proportions() -> None:
    durations = distribute_us(120_000_000, [1.0, 1.0, 2.0])
    assert sum(durations) == 120_000_000
    assert durations == [30_000_000, 30_000_000, 60_000_000]


def test_distribute_us_is_deterministic_when_remainders_tie() -> None:
    first = distribute_us(100, [1.0, 1.0, 1.0])
    second = distribute_us(100, [1.0, 1.0, 1.0])
    assert first == second
    assert sum(first) == 100


def test_distribute_us_handles_degenerate_weights() -> None:
    durations = distribute_us(9, [0.0, 0.0])
    assert sum(durations) == 9


def test_role_weights_make_the_climax_shorter_than_the_opener() -> None:
    assert ROLE_WEIGHTS["climax"] < ROLE_WEIGHTS["opener"]
    assert shot_weights(("a", "b"), None) == [1.0, 1.0]


def test_snap_boundaries_aligns_to_beats() -> None:
    beats = tuple(range(0, 10_000_000, 1_000_000))
    snapped = snap_boundaries([0, 3_400_000, 6_600_000, 10_000_000], beats, min_gap_us=250_000)
    assert snapped == [0, 3_000_000, 7_000_000, 10_000_000]


def test_snap_boundaries_refuses_when_beats_are_too_close() -> None:
    beats = (0, 100_000, 200_000, 10_000_000)
    assert snap_boundaries([0, 5_000_000, 10_000_000], beats, min_gap_us=3_000_000) is None


def test_snap_boundaries_needs_at_least_two_shots() -> None:
    assert snap_boundaries([0, 10_000_000], (0, 1_000_000), min_gap_us=1) is None


def test_suggest_template_id_follows_the_tempo_table() -> None:
    assert suggest_template_id(LIBRARY, tempo_bpm=60.0) == "calm_reflective"
    assert suggest_template_id(LIBRARY, tempo_bpm=120.0) == "tech_product"
    assert suggest_template_id(LIBRARY, tempo_bpm=180.0) == "celebration_party"
    assert suggest_template_id(LIBRARY, image_count=3) == "calm_reflective"
    assert suggest_template_id(LIBRARY, image_count=30) == "upbeat_energetic"


# ---------------------------------------------------------------------------
# plan_slideshow: the two invariants
# ---------------------------------------------------------------------------


def test_auto_plan_tiles_the_target_exactly() -> None:
    payload = ComposeInput.model_validate(_compose_payload(count=12))
    plan = plan_slideshow(payload, library=LIBRARY)
    assert len(plan.shots) == 12
    assert plan.shots[0].slot.start_us == 0
    assert plan.shots[-1].slot.end_us == payload.target_duration_us
    for previous, current in zip(plan.shots, plan.shots[1:], strict=False):
        assert previous.slot.end_us == current.slot.start_us


def test_auto_plan_is_deterministic() -> None:
    payload = ComposeInput.model_validate(_compose_payload())
    first = plan_slideshow(payload, library=LIBRARY)
    second = plan_slideshow(payload, library=LIBRARY)
    assert first.model_dump() == second.model_dump()


def test_auto_plan_snaps_to_beat_boundaries_and_reports_it() -> None:
    payload = ComposeInput.model_validate(_compose_payload(count=12))
    plan = plan_slideshow(payload, library=LIBRARY)
    beats = set(_beat_grid(duration_us=payload.target_duration_us).beats_us)
    internal = [shot.slot.start_us for shot in plan.shots[1:]]
    assert all(boundary in beats for boundary in internal)
    assert plan.alignment_quality == "detected"
    assert plan.tempo_source == "detected"


def test_plan_without_a_beat_grid_says_so() -> None:
    payload = _compose_payload(count=12)
    payload.pop("beat_grid")
    plan = plan_slideshow(ComposeInput.model_validate(payload), library=LIBRARY)
    assert plan.alignment_quality == "interpolated"
    assert any("no beat grid" in warning for warning in plan.warnings)


def test_sparse_beat_grid_keeps_arithmetic_boundaries() -> None:
    payload = _compose_payload(count=12)
    sparse = _beat_grid(duration_us=120_000_000).model_copy(
        update={"beats_us": (0, 60_000_000, 120_000_000), "strong_beats_us": (0,)}
    )
    payload["beat_grid"] = sparse.model_dump(mode="json")
    plan = plan_slideshow(ComposeInput.model_validate(payload), library=LIBRARY)
    assert any("too sparse" in warning for warning in plan.warnings)
    assert plan.shots[-1].slot.end_us == 120_000_000


def test_manual_plan_requires_the_shots_to_sum_to_the_target() -> None:
    payload = _compose_payload(count=3, target_us=60_000_000)
    payload["mode"] = "manual"
    payload["shots"] = [
        {"evidence_id": "img00", "duration_us": 20_000_000},
        {"evidence_id": "img01", "duration_us": 20_000_000},
        {"evidence_id": "img02", "duration_us": 10_000_000},
    ]
    with pytest.raises(ValueError, match="must tile the target duration exactly"):
        plan_slideshow(ComposeInput.model_validate(payload), library=LIBRARY)


def test_manual_plan_enforces_a_minimum_shot_length() -> None:
    payload = _compose_payload(count=2, target_us=60_000_000)
    payload["mode"] = "manual"
    payload["shots"] = [
        {"evidence_id": "img00", "duration_us": 59_900_000},
        {"evidence_id": "img01", "duration_us": 100_000},
    ]
    with pytest.raises(ValueError, match="at least"):
        plan_slideshow(ComposeInput.model_validate(payload), library=LIBRARY)


def test_auto_plan_refuses_an_impossible_target() -> None:
    payload = _compose_payload(count=20, target_us=60_000_000)
    payload["assets"] = [_evidence(index).model_dump(mode="json") for index in range(400)]
    payload["assets"] = [
        {**item, "evidence_id": f"img{index:03d}"} for index, item in enumerate(payload["assets"])
    ]
    with pytest.raises(ValueError, match="minimum shot length"):
        plan_slideshow(ComposeInput.model_validate(payload), library=LIBRARY)


def test_short_audio_and_small_image_sets_are_reported_as_warnings() -> None:
    payload = _compose_payload(count=4)
    payload["audio"] = _evidence(0, kind="audio", duration_us=30_000_000).model_dump(mode="json")
    plan = plan_slideshow(ComposeInput.model_validate(payload), library=LIBRARY)
    joined = " | ".join(plan.warnings)
    assert "outside the recommended" in joined
    assert "audio is shorter" in joined


def test_manual_mode_keeps_the_user_order() -> None:
    payload = _compose_payload(count=3, target_us=60_000_000)
    payload["mode"] = "manual"
    payload["shots"] = [
        {"evidence_id": "img02", "duration_us": 20_000_000},
        {"evidence_id": "img00", "duration_us": 20_000_000},
        {"evidence_id": "img01", "duration_us": 20_000_000},
    ]
    plan = plan_slideshow(ComposeInput.model_validate(payload), library=LIBRARY)
    assert [shot.evidence_id for shot in plan.shots] == ["img02", "img00", "img01"]
    assert ordered_evidence_ids(ComposeInput.model_validate(payload)) == ("img02", "img00", "img01")


def test_transitions_are_absent_on_the_first_shot() -> None:
    payload = ComposeInput.model_validate(_compose_payload(count=5))
    plan = plan_slideshow(payload, library=LIBRARY)
    assert plan.shots[0].transition_in is None
    assert all(shot.transition_in is not None for shot in plan.shots[1:])


# ---------------------------------------------------------------------------
# operations through the real command bus
# ---------------------------------------------------------------------------


def test_scan_assets_registers_and_is_idempotent() -> None:
    bus = _bus()
    payload = {"assets": [_evidence(index).model_dump(mode="json") for index in range(3)]}
    first = bus.dispatch(_command(OPERATION_SCAN, payload))
    assert len(first.output["registered"]) == 3
    second = bus.dispatch(_command(OPERATION_SCAN, payload))
    assert second.output["registered"] == []
    assert second.output["unchanged"] == ["img00", "img01", "img02"]
    assert len(bus.project.assets) == 3


def test_scan_rejects_a_changed_hash_for_an_known_evidence_id() -> None:
    bus = _bus()
    bus.dispatch(_command(OPERATION_SCAN, {"assets": [_evidence(0).model_dump(mode="json")]}))
    changed = _evidence(0).model_copy(update={"content_sha256": "sha256:" + "ff" * 32})
    with pytest.raises(CommandValidationError, match="different content hash"):
        bus.dispatch(_command(OPERATION_SCAN, {"assets": [changed.model_dump(mode="json")]}))


def test_score_images_orders_by_quality_and_assigns_roles() -> None:
    bus = _bus()
    scores = [
        ImageScore(evidence_id="img00", sharpness=0.2, aesthetic=0.2),
        ImageScore(evidence_id="img01", sharpness=0.9, aesthetic=0.9),
        ImageScore(evidence_id="img02", sharpness=0.5, aesthetic=0.5),
        ImageScore(evidence_id="img03", sharpness=0.4, aesthetic=0.4),
    ]
    result = bus.dispatch(
        _command(OPERATION_SCORE, {"scores": [score.model_dump(mode="json") for score in scores]})
    )
    assert result.output["ordered_evidence_ids"][0] == "img01"
    roles = {item["evidence_id"]: item["suggested_role"] for item in result.output["scores"]}
    assert roles["img01"] == "opener"
    assert roles["img00"] == "closer"
    assert "climax" in roles.values()


def test_score_images_honours_a_valid_proposed_order() -> None:
    bus = _bus()
    scores = [
        ImageScore(evidence_id=f"img{index:02d}", sharpness=0.5, aesthetic=0.5)
        for index in range(4)
    ]
    preferred = ("img03", "img02", "img01", "img00")
    result = bus.dispatch(
        _command(
            OPERATION_SCORE,
            {
                "scores": [score.model_dump(mode="json") for score in scores],
                "preferred_order": list(preferred),
                "source": "gemini",
            },
        )
    )
    assert tuple(result.output["ordered_evidence_ids"]) == preferred
    assert result.output["source"] == "gemini"


def test_score_images_ignores_an_incomplete_proposed_order() -> None:
    bus = _bus()
    scores = [
        ImageScore(evidence_id=f"img{index:02d}", sharpness=0.5, aesthetic=0.5)
        for index in range(4)
    ]
    result = bus.dispatch(
        _command(
            OPERATION_SCORE,
            {
                "scores": [score.model_dump(mode="json") for score in scores],
                "preferred_order": ["img03"],
            },
        )
    )
    assert set(result.output["ordered_evidence_ids"]) == {score.evidence_id for score in scores}


def test_suggest_tone_prefers_request_then_recommendation_then_tempo() -> None:
    bus = _bus()
    requested = bus.dispatch(
        _command(
            OPERATION_SUGGEST_TONE,
            SuggestToneInput(
                image_count=12, tempo_bpm=140.0, requested_template_id="minimal_clean"
            ).model_dump(mode="json"),
        )
    )
    assert requested.output["template_id"] == "minimal_clean"
    recommended = bus.dispatch(
        _command(
            OPERATION_SUGGEST_TONE,
            SuggestToneInput(
                image_count=12, tempo_bpm=140.0, recommended_template_id="nature_ambient"
            ).model_dump(mode="json"),
        )
    )
    assert recommended.output["template_id"] == "nature_ambient"
    heuristic = bus.dispatch(
        _command(
            OPERATION_SUGGEST_TONE,
            SuggestToneInput(image_count=12, tempo_bpm=140.0).model_dump(mode="json"),
        )
    )
    assert heuristic.output["template_id"] == "upbeat_energetic"


def test_suggest_tone_rejects_an_unknown_requested_template() -> None:
    bus = _bus()
    with pytest.raises(CommandValidationError, match="unknown tone template"):
        bus.dispatch(
            _command(
                OPERATION_SUGGEST_TONE,
                SuggestToneInput(image_count=12, requested_template_id="ghost").model_dump(
                    mode="json"
                ),
            )
        )


def test_compose_writes_one_atomic_transaction_and_undo_restores_it() -> None:
    bus = _bus()
    payload = _compose_payload()
    bus.dispatch(_command(OPERATION_SCAN, {"assets": payload["assets"]}))
    revision_before = bus.state_revision
    result = bus.dispatch(_command(OPERATION_COMPOSE, payload))

    assert result.state_revision == revision_before + 1
    project = bus.project
    slideshow_track = next(track for track in project.timeline.tracks if track.kind == "video")
    assert len(slideshow_track.clips) == 12
    assert project.timeline.duration_us == 120_000_000

    undo = bus.dispatch(_command("system.undo", {}))
    assert undo.output["undone_operation"] == OPERATION_COMPOSE
    assert bus.project.timeline.tracks == []


def test_compose_writes_effect_layers_for_every_shot() -> None:
    bus = _bus()
    payload = _compose_payload()
    bus.dispatch(_command(OPERATION_SCAN, {"assets": payload["assets"]}))
    bus.dispatch(_command(OPERATION_COMPOSE, payload))
    track = next(track for track in bus.project.timeline.tracks if track.kind == "video")

    assert {layer.operation for layer in track.effects} == {
        "slideshow.tone",
        "slideshow.render_profile",
    }
    first_clip = track.clips[0]
    assert {layer.operation for layer in first_clip.effects} == {
        "slideshow.motion",
        "slideshow.grade",
    }
    later_clip = track.clips[4]
    assert "slideshow.transition" in {layer.operation for layer in later_clip.effects}
    for clip in track.clips:
        assert clip.source_range.start_us == 0
        for layer in clip.effects:
            assert layer.parameters_hash.startswith("sha256:")
            assert layer.reversible is True


def test_compose_requires_registered_assets() -> None:
    bus = _bus()
    with pytest.raises(CommandValidationError, match="not registered"):
        bus.dispatch(_command(OPERATION_COMPOSE, _compose_payload(count=3, target_us=60_000_000)))


def test_compose_rejects_evidence_that_changed_after_registration() -> None:
    bus = _bus()
    payload = _compose_payload(count=3, target_us=60_000_000)
    bus.dispatch(_command(OPERATION_SCAN, {"assets": payload["assets"]}))
    payload["assets"][0]["content_sha256"] = "sha256:" + "ee" * 32  # type: ignore[index]
    with pytest.raises(CommandValidationError, match="changed after it was registered"):
        bus.dispatch(_command(OPERATION_COMPOSE, payload))


def test_render_is_level_c_and_records_a_derived_asset() -> None:
    bus = _bus()
    payload = _compose_payload(count=3, target_us=60_000_000)
    bus.dispatch(_command(OPERATION_SCAN, {"assets": payload["assets"]}))
    bus.dispatch(_command(OPERATION_COMPOSE, payload))

    render_input = {
        "output_path": "/tmp/master.mp4",
        "output_sha256": "sha256:" + "ab" * 32,
        "duration_us": 60_000_000,
        "parent_asset_ids": ["img00", "img01"],
        "render_ir_hash": "sha256:" + "cd" * 32,
        "state_hash_before_render": bus.state_hash,
        "template_id": "travel_documentary",
    }
    with pytest.raises(PermissionDeniedError, match="requires explicit confirmation"):
        bus.dispatch(_command(OPERATION_RENDER, render_input))

    result = bus.dispatch(_command(OPERATION_RENDER, render_input, confirmed=True))
    derived = [asset for asset in bus.project.assets if asset.is_derived]
    assert len(derived) == 1
    assert result.output["asset_id"] == derived[0].asset_id
    assert derived[0].parent_asset_ids == ("img00", "img01")
    assert derived[0].provenance["output_path"] == "/tmp/master.mp4"
    assert derived[0].provenance["produced_by"] == "nagar.local.slideshow.v1"


def test_render_rejects_unknown_parent_assets() -> None:
    bus = _bus()
    with pytest.raises(CommandValidationError, match="not registered"):
        bus.dispatch(
            _command(
                OPERATION_RENDER,
                {
                    "output_path": "/tmp/master.mp4",
                    "output_sha256": "sha256:" + "ab" * 32,
                    "duration_us": 60_000_000,
                    "parent_asset_ids": ["ghost"],
                },
                confirmed=True,
            )
        )


def test_idempotency_key_replays_without_a_second_transaction() -> None:
    bus = _bus()
    payload = {"assets": [_evidence(0).model_dump(mode="json")]}
    command = {**_command(OPERATION_SCAN, payload), "idempotency_key": "scan-once"}
    first = bus.dispatch(command)
    second = bus.dispatch(command)
    assert first.transaction_id == second.transaction_id
    assert bus.state_revision == 1


def test_operation_inputs_reject_unknown_fields() -> None:
    from pydantic import ValidationError

    for model, payload in (
        (ScanAssetsInput, {"assets": [], "shell": "rm -rf /"}),
        (ScoreImagesInput, {"scores": [], "entrypoint": "x"}),
        (SuggestToneInput, {"image_count": 3, "post_install": "x"}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_pack_registry_activates_the_builtin_pack_for_real() -> None:
    """Wave 2a registered it as *pending*; Wave 2b makes activation real."""
    registry = PackRegistry(build_slideshow_registry(), current_version="3.10.0")
    packs = registry.register_builtin(root=PACKS_DIR)
    pack = next(p for p in packs if p.package_id == "nexus.slideshow.compose")
    assert pack.package_id == "nexus.slideshow.compose"
    assert pack.pending_capabilities == ()
    activated = registry.activate("nexus.slideshow.compose")
    assert activated.active is True
    assert registry.active_packs() == ["nexus.slideshow.compose"]
    assert set(registry.operations("nexus.slideshow.compose")) == {
        OPERATION_SCAN,
        OPERATION_SCORE,
        OPERATION_SUGGEST_TONE,
        OPERATION_COMPOSE,
        OPERATION_RENDER,
        OPERATION_UPSCALE,
    }


def test_wave1_registry_stays_frozen() -> None:
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    operations = set(build_wave1_registry().list_operations())
    assert operations == {
        "media.play",
        "media.pause",
        "timeline.mark",
        "timeline.split_at_playhead",
        "system.undo",
    }


def test_registration_is_deterministic_and_refuses_duplicates() -> None:
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    first = build_slideshow_registry()
    second = build_slideshow_registry()
    assert first.list_operations() == second.list_operations()

    composed = register_slideshow_operations(build_wave1_registry())
    assert set(composed.list_operations()) == set(second.list_operations())
    with pytest.raises(ValueError, match="duplicate operation"):
        register_slideshow_operations(composed)


def test_use_tone_library_can_be_restored() -> None:
    """The composition seam exists and defaults back to the shipped library."""
    use_tone_library(None)
    assert "travel_documentary" in load_tone_templates()


def test_min_shot_us_is_the_documented_floor() -> None:
    assert MIN_SHOT_US == 250_000


def test_compose_input_rejects_audio_in_the_image_list() -> None:
    payload = _compose_payload(count=2, target_us=60_000_000)
    payload["assets"] = [
        _evidence(0).model_dump(mode="json"),
        _evidence(1, kind="audio", duration_us=60_000_000).model_dump(mode="json"),
    ]
    with pytest.raises(ValidationError, match="image evidence only"):
        ComposeInput.model_validate(payload)


def test_permission_levels_match_the_pack_design() -> None:
    registry = build_slideshow_registry()
    assert registry.get_spec(OPERATION_SCAN).permission_level is PermissionLevel.REVERSIBLE
    assert registry.get_spec(OPERATION_SCORE).permission_level is PermissionLevel.IMMEDIATE
    assert registry.get_spec(OPERATION_SUGGEST_TONE).permission_level is PermissionLevel.IMMEDIATE
    assert registry.get_spec(OPERATION_COMPOSE).permission_level is PermissionLevel.REVERSIBLE
    assert registry.get_spec(OPERATION_RENDER).permission_level is PermissionLevel.CONFIRMATION
    assert registry.get_spec(OPERATION_UPSCALE).permission_level is PermissionLevel.REVERSIBLE
    for operation in (
        OPERATION_SCAN,
        OPERATION_SCORE,
        OPERATION_SUGGEST_TONE,
        OPERATION_COMPOSE,
        OPERATION_RENDER,
        OPERATION_UPSCALE,
    ):
        assert registry.get_spec(operation).deterministic is True
