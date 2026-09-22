"""Wave 5 — closing the TDD operation gap (audio × 2, motion × 3, timeline × 1).

The Phase-6 TDD fixes 71 operations.  Wave 5 closes the six that had no contract
in the substrate at all:

| Operation | TDD contract | Level |
|---|---|---|
| ``motion.apply_mask`` | ``ClipRef + MaskRef + feather → MaskedLayer`` | B |
| ``motion.warp`` | ``ClipRef + MeshCurve → EffectLayerRef`` | B |
| ``motion.add_particles`` | ``EmitterSpec + TimeRangeUS → ParticleLayerRef`` | B |
| ``audio.remove_vocal`` | ``AudioRef + stem_policy → StemSet`` | B |
| ``audio.align_music`` | ``TimelineRange + MusicRef + BeatGrid → OffsetMap`` | B |
| ``timeline.sync_multicam`` | ``ClipSet + anchor_policy → OffsetMap + confidence`` | A |

Every test here asserts one of three properties that the TDD makes normative:

* **purity** — the command bus' promise ("a failing handler leaves the central
  state untouched") only holds if handlers never mutate their input;
* **determinism** — the specs are declared ``deterministic=True``, so identical
  input must produce identical content hashes and identical evidence;
* **evidence** — the impure apply lane must not have to re-derive anything, so
  the handler output carries the plan (mesh plan, stem ids, offset map, sample
  table) rather than a bare id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    OPERATION_ALIGN_MUSIC,
    OPERATION_REMOVE_VOCAL,
)
from nexus_ai_agent.creative.packs.audio.operations import build_audio_registry
from nexus_ai_agent.creative.packs.edit.models import (
    EDIT_PACKAGE_ID,
    OPERATION_SYNC_MULTICAM,
)
from nexus_ai_agent.creative.packs.edit.operations import build_edit_registry
from nexus_ai_agent.creative.packs.motion.models import (
    MOTION_PACKAGE_ID,
    OPERATION_ADD_PARTICLES,
    OPERATION_APPLY_MASK,
    OPERATION_WARP,
    ApplyMaskInput,
    WarpInput,
)
from nexus_ai_agent.creative.packs.motion.operations import build_motion_registry
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)

PACKS_ROOT = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent" / "creative" / "packs"

NEW_OPERATIONS = (
    OPERATION_APPLY_MASK,
    OPERATION_WARP,
    OPERATION_ADD_PARTICLES,
    OPERATION_REMOVE_VOCAL,
    OPERATION_ALIGN_MUSIC,
    OPERATION_SYNC_MULTICAM,
)


def _project() -> Project:
    """A multicam project: two video angles, a voice track, music and a mask."""
    assets = [
        AssetRecord(
            asset_id="cam_a",
            media_kind="video",
            content_sha256="sha256:camafeed",
            duration_us=6_000_000,
        ),
        AssetRecord(
            asset_id="cam_b",
            media_kind="video",
            content_sha256="sha256:cambfeed",
            duration_us=9_000_000,
        ),
        AssetRecord(
            asset_id="voice_01",
            media_kind="audio",
            content_sha256="sha256:voicefeed",
            duration_us=8_000_000,
        ),
        AssetRecord(
            asset_id="music_01",
            media_kind="audio",
            content_sha256="sha256:musicfeed",
            duration_us=12_000_000,
        ),
        AssetRecord(
            asset_id="mask_01",
            media_kind="image",
            content_sha256="sha256:maskfeed",
            duration_us=0,
        ),
    ]
    timeline = Timeline(timeline_id="tl_wave5", duration_us=12_000_000)
    return new_project("proj_wave5", "Wave 5", timeline).model_copy(update={"assets": assets})


def _bus(registry) -> CommandBus:  # noqa: ANN001 - test helper
    return CommandBus(_project(), registry=registry)


def _dispatch(bus: CommandBus, operation: str, payload: dict[str, object]) -> object:
    return bus.dispatch(
        TypedCommand(command_id=f"cmd_{operation}", operation=operation, input=payload)
    )


def _asset(project: Project, asset_id: str) -> AssetRecord:
    matches = [record for record in project.assets if record.asset_id == asset_id]
    assert len(matches) == 1, f"{asset_id!r} appears {len(matches)} times"
    return matches[0]


# ---------------------------------------------------------------------------
# registry / manifest / level contract
# ---------------------------------------------------------------------------


def test_new_operations_are_registered_with_the_contract_levels() -> None:
    """Level A = read-only analysis, Level B = reversible derivation."""
    levels = {
        OPERATION_APPLY_MASK: (
            build_motion_registry(),
            PermissionLevel.REVERSIBLE,
            MOTION_PACKAGE_ID,
        ),
        OPERATION_WARP: (build_motion_registry(), PermissionLevel.REVERSIBLE, MOTION_PACKAGE_ID),
        OPERATION_ADD_PARTICLES: (
            build_motion_registry(),
            PermissionLevel.REVERSIBLE,
            MOTION_PACKAGE_ID,
        ),
        OPERATION_REMOVE_VOCAL: (
            build_audio_registry(),
            PermissionLevel.REVERSIBLE,
            AUDIO_PACKAGE_ID,
        ),
        OPERATION_ALIGN_MUSIC: (
            build_audio_registry(),
            PermissionLevel.REVERSIBLE,
            AUDIO_PACKAGE_ID,
        ),
        OPERATION_SYNC_MULTICAM: (
            build_edit_registry(),
            PermissionLevel.IMMEDIATE,
            EDIT_PACKAGE_ID,
        ),
    }
    for operation_id, (registry, level, pack_id) in levels.items():
        spec = registry.get_spec(operation_id)
        assert spec.permission_level == level, operation_id
        assert pack_id in spec.required_packs, operation_id
        assert spec.deterministic is True, operation_id


def test_manifests_advertise_the_new_capabilities() -> None:
    expectations = {
        "motion": ("motion.apply_mask", "motion.warp", "motion.add_particles"),
        "audio": ("audio.remove_vocal", "audio.align_music"),
        "edit": ("timeline.sync_multicam",),
    }
    for pack, capabilities in expectations.items():
        manifest = json.loads(
            (PACKS_ROOT / pack / "pack.manifest.json").read_text(encoding="utf-8")
        )
        for capability in capabilities:
            assert capability in manifest["capabilities"], f"{pack}: {capability}"


def test_motion_and_audio_domains_are_complete_against_the_tdd_table() -> None:
    """The three TDD domains this wave touches must be fully implemented."""
    motion = set(build_motion_registry().list_operations())
    audio = set(build_audio_registry().list_operations())
    edit = set(build_edit_registry().list_operations())

    assert {op for op in motion if op.startswith("motion.")} == {
        "motion.add_transition",
        "motion.keyframe_transform",
        "motion.add_parallax",
        "motion.apply_mask",
        "motion.add_glow",
        "motion.add_motion_blur",
        "motion.stabilize",
        "motion.warp",
        "motion.add_title",
        "motion.add_particles",
    }
    assert {op for op in audio if op.startswith("audio.")} == {
        "audio.detect_beats",
        "audio.beat_sync_cut",
        "audio.remove_noise",
        "audio.remove_vocal",
        "audio.duck_music",
        "audio.normalize_loudness",
        "audio.eq_voice",
        "audio.deess",
        "audio.align_music",
        "audio.time_stretch",
    }
    assert {op for op in edit if op.startswith("timeline.")} >= {
        "timeline.trim",
        "timeline.ripple_delete",
        "timeline.insert_gap",
        "timeline.speed_ramp",
        "timeline.reverse_segment",
        "timeline.freeze_frame",
        "timeline.attach_b_roll",
        "timeline.retime_to_music",
        "timeline.sync_multicam",
        "timeline.split_at_playhead",
    }


def test_input_models_reject_unknown_fields() -> None:
    """A pack contract is strict: an unknown key must fail, never be ignored."""
    with pytest.raises(ValidationError):
        ApplyMaskInput.model_validate(
            {"clip_asset_id": "cam_a", "mask_asset_id": "mask_01", "surprise": 1}
        )
    with pytest.raises(ValidationError):
        WarpInput.model_validate(
            {"clip_asset_id": "cam_a", "control_points": [{"row": 0, "col": 0}], "mesh": "3x3"}
        )


# ---------------------------------------------------------------------------
# motion.apply_mask
# ---------------------------------------------------------------------------


def test_apply_mask_derives_a_masked_clip_with_mask_provenance() -> None:
    bus = _bus(build_motion_registry())
    result = _dispatch(
        bus,
        OPERATION_APPLY_MASK,
        {
            "clip_asset_id": "cam_a",
            "mask_asset_id": "mask_01",
            "feather_px": 12.5,
            "output_asset_id": "cam_a_masked",
        },
    )
    assert result.status == "applied"
    assert result.output["asset_id"] == "cam_a_masked"
    assert result.output["feather_px"] == 12.5
    assert result.output["invert"] is False

    masked = _asset(bus.project, "cam_a_masked")
    assert masked.media_kind == "video"
    assert masked.parent_asset_ids == ("cam_a", "mask_01")
    assert masked.duration_us == 6_000_000
    assert masked.provenance["processor"] == "nagar.motion.graphics.mask.v1"
    assert masked.provenance["mask_sha256"] == "sha256:maskfeed"
    assert masked.content_sha256 == result.output["content_sha256"]


def test_apply_mask_requires_an_image_mask() -> None:
    bus = _bus(build_motion_registry())
    with pytest.raises(CommandValidationError, match="requires an image mask asset"):
        _dispatch(
            bus,
            OPERATION_APPLY_MASK,
            {"clip_asset_id": "cam_a", "mask_asset_id": "voice_01"},
        )


def test_apply_mask_rejects_unknown_assets_and_self_masking() -> None:
    bus = _bus(build_motion_registry())
    with pytest.raises(CommandValidationError, match="unknown mask asset"):
        _dispatch(bus, OPERATION_APPLY_MASK, {"clip_asset_id": "cam_a", "mask_asset_id": "ghost"})
    with pytest.raises(CommandValidationError, match="unknown video asset"):
        _dispatch(bus, OPERATION_APPLY_MASK, {"clip_asset_id": "ghost", "mask_asset_id": "mask_01"})
    with pytest.raises(ValidationError, match="must differ"):
        ApplyMaskInput.model_validate(
            {"clip_asset_id": "cam_a", "mask_asset_id": "cam_a", "invert": True}
        )


def test_apply_mask_is_deterministic_and_sensitive_to_its_parameters() -> None:
    def digest(**overrides: object) -> str:
        bus = _bus(build_motion_registry())
        payload = {
            "clip_asset_id": "cam_a",
            "mask_asset_id": "mask_01",
            "output_asset_id": "cam_a_masked",
            **overrides,
        }
        return str(_dispatch(bus, OPERATION_APPLY_MASK, payload).output["content_sha256"])

    assert digest() == digest()
    assert digest(feather_px=30.0) != digest()
    assert digest(invert=True) != digest()


# ---------------------------------------------------------------------------
# motion.warp
# ---------------------------------------------------------------------------


def test_warp_emits_a_normalized_mesh_plan() -> None:
    bus = _bus(build_motion_registry())
    result = _dispatch(
        bus,
        OPERATION_WARP,
        {
            "clip_asset_id": "cam_a",
            "mesh_rows": 3,
            "mesh_cols": 3,
            "control_points": [
                {"row": 0, "col": 0, "dx": 0.0, "dy": 0.0},
                {"row": 0, "col": 2, "dx": 0.25, "dy": -0.1, "time_offset_us": 500_000},
                {"row": 2, "col": 2, "dx": -0.5, "dy": 0.5, "time_offset_us": 1_000_000},
            ],
            "output_asset_id": "cam_a_warped",
        },
    )
    assert result.status == "applied"
    assert result.output["mesh"] == "3x3"
    assert result.output["control_point_count"] == 3
    assert result.output["mesh_plan"][2]["displacement"] == pytest.approx(0.707107, rel=1e-5)
    assert result.output["max_displacement"] == pytest.approx(0.707107, rel=1e-5)

    warped = _asset(bus.project, "cam_a_warped")
    assert warped.provenance["processor"] == "nagar.motion.graphics.warp.v1"
    assert warped.provenance["mesh"] == "3x3"
    assert warped.duration_us == 6_000_000


@pytest.mark.parametrize(
    ("points", "message"),
    [
        ([{"row": 3, "col": 0}], "outside the 3x3 mesh"),
        (
            [{"row": 1, "col": 1}, {"row": 1, "col": 1, "time_offset_us": 0}],
            "duplicate control point",
        ),
        (
            [{"row": 1, "col": 1, "time_offset_us": 500}, {"row": 1, "col": 1}],
            "must be ordered",
        ),
    ],
)
def test_warp_validates_the_mesh_curve(points: list[dict[str, int]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        WarpInput.model_validate({"clip_asset_id": "cam_a", "control_points": points})


def test_warp_requires_a_video_clip() -> None:
    bus = _bus(build_motion_registry())
    with pytest.raises(CommandValidationError, match="requires a video asset"):
        _dispatch(
            bus,
            OPERATION_WARP,
            {"clip_asset_id": "voice_01", "control_points": [{"row": 0, "col": 0, "dx": 0.1}]},
        )


# ---------------------------------------------------------------------------
# motion.add_particles
# ---------------------------------------------------------------------------


def test_add_particles_emits_a_deterministic_sample_table() -> None:
    bus = _bus(build_motion_registry())
    payload = {
        "clip_asset_id": "cam_b",
        "emitter": {"particles": 40, "seed": 7, "velocity_px_s": 220.0, "spread_deg": 90.0},
        "start_us": 1_000_000,
        "end_us": 3_000_000,
        "output_asset_id": "cam_b_particles",
    }
    result = _dispatch(bus, OPERATION_ADD_PARTICLES, payload)
    assert result.status == "applied"
    assert result.output["window_us"] == 2_000_000
    assert result.output["emission_rate_hz"] == 20.0
    assert result.output["window_clamped"] is False
    samples = result.output["particle_samples"]
    assert len(samples) == 8  # bounded evidence, not all 40
    assert samples[0]["index"] == 0
    assert all(0.0 <= sample["x"] <= 1.0 for sample in samples)

    layer = _asset(bus.project, "cam_b_particles")
    assert layer.media_kind == "video"
    assert layer.duration_us == 2_000_000
    assert layer.provenance["processor"] == "nagar.motion.graphics.particles.v1"
    assert layer.provenance["emitter"]["seed"] == 7

    second = _dispatch(
        _bus(build_motion_registry()),
        OPERATION_ADD_PARTICLES,
        payload,
    )
    assert second.output["content_sha256"] == result.output["content_sha256"]
    assert second.output["particle_samples"] == samples

    other_seed = _dispatch(
        _bus(build_motion_registry()),
        OPERATION_ADD_PARTICLES,
        {**payload, "emitter": {**payload["emitter"], "seed": 8}},
    )
    assert other_seed.output["particle_samples"] != samples


def test_add_particles_clamps_the_window_to_the_clip_duration() -> None:
    bus = _bus(build_motion_registry())
    result = _dispatch(
        bus,
        OPERATION_ADD_PARTICLES,
        {"clip_asset_id": "cam_a", "start_us": 5_000_000, "end_us": 8_000_000},
    )
    assert result.output["window_clamped"] is True
    assert result.output["window_us"] == 1_000_000


def test_add_particles_rejects_a_window_outside_the_clip() -> None:
    bus = _bus(build_motion_registry())
    with pytest.raises(CommandValidationError, match="window starts at"):
        _dispatch(
            bus,
            OPERATION_ADD_PARTICLES,
            {"clip_asset_id": "cam_a", "start_us": 6_000_000, "end_us": 7_000_000},
        )
    # The bus validates the payload before the handler runs and wraps the model
    # error, so the caller sees one typed error class for every bad input.
    with pytest.raises(CommandValidationError, match="must be > start_us"):
        _dispatch(
            bus,
            OPERATION_ADD_PARTICLES,
            {"clip_asset_id": "cam_a", "start_us": 2_000_000, "end_us": 2_000_000},
        )


# ---------------------------------------------------------------------------
# audio.remove_vocal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("policy", "stems"), [("two_stem", 2), ("four_stem", 4)])
def test_remove_vocal_derives_one_asset_per_stem(policy: str, stems: int) -> None:
    bus = _bus(build_audio_registry())
    result = _dispatch(
        bus,
        OPERATION_REMOVE_VOCAL,
        {
            "audio_asset_id": "music_01",
            "stem_policy": policy,
            "strength": 0.9,
            "output_asset_id": f"music_{policy}",
        },
    )
    assert result.status == "applied"
    assert result.output["stem_count"] == stems
    assert set(result.output["stem_asset_ids"]) in (
        {"vocals", "accompaniment"},
        {"vocals", "drums", "bass", "other"},
    )

    hashes = set()
    for stem, asset_id in result.output["stem_asset_ids"].items():
        record = _asset(bus.project, asset_id)
        assert record.media_kind == "audio"
        assert record.parent_asset_ids == ("music_01",)
        assert record.duration_us == 12_000_000
        assert record.provenance["processor"] == "nagar.audio.studio.stems.v1"
        assert record.provenance["stem"] == stem
        assert record.provenance["stem_policy"] == policy
        hashes.add(record.content_sha256)
    assert len(hashes) == stems, "every stem must have its own content hash"


def test_remove_vocal_is_deterministic_and_requires_audio_media() -> None:
    payload = {
        "audio_asset_id": "music_01",
        "stem_policy": "two_stem",
        "output_asset_id": "stems",
    }
    first = _dispatch(_bus(build_audio_registry()), OPERATION_REMOVE_VOCAL, payload)
    second = _dispatch(_bus(build_audio_registry()), OPERATION_REMOVE_VOCAL, payload)
    assert first.output["content_sha256"] == second.output["content_sha256"]

    with pytest.raises(CommandValidationError, match="requires an audio asset"):
        _dispatch(
            _bus(build_audio_registry()),
            OPERATION_REMOVE_VOCAL,
            {"audio_asset_id": "cam_a"},
        )
    with pytest.raises(CommandValidationError, match="unknown audio asset"):
        _dispatch(_bus(build_audio_registry()), OPERATION_REMOVE_VOCAL, {"audio_asset_id": "ghost"})


# ---------------------------------------------------------------------------
# audio.align_music
# ---------------------------------------------------------------------------


def test_align_music_first_beat_snaps_forwards() -> None:
    bus = _bus(build_audio_registry())
    result = _dispatch(
        bus,
        OPERATION_ALIGN_MUSIC,
        {
            "music_asset_id": "music_01",
            "tempo_bpm": 120.0,
            "first_beat_us": 0,
            "target_start_us": 1_100_000,
            "max_shift_us": 500_000,
        },
    )
    assert result.output["beat_period_us"] == 500_000
    assert result.output["aligned_beat_index"] == 3  # 1.5 s
    assert result.output["beat_time_us"] == 1_500_000
    assert result.output["offset_us"] == 400_000
    assert result.output["clamped"] is False
    assert result.output["confidence"] == pytest.approx(0.2)
    assert not any(record.asset_id.startswith("aligned") for record in bus.project.assets)


def test_align_music_nearest_beat_and_clamping() -> None:
    nearest = _dispatch(
        _bus(build_audio_registry()),
        OPERATION_ALIGN_MUSIC,
        {
            "music_asset_id": "music_01",
            "tempo_bpm": 120.0,
            "target_start_us": 1_100_000,
            "anchor": "nearest_beat",
            "max_shift_us": 500_000,
        },
    )
    assert nearest.output["beat_time_us"] == 1_000_000
    assert nearest.output["offset_us"] == -100_000
    assert nearest.output["confidence"] == pytest.approx(0.8)

    clamped = _dispatch(
        _bus(build_audio_registry()),
        OPERATION_ALIGN_MUSIC,
        {
            "music_asset_id": "music_01",
            "tempo_bpm": 120.0,
            "target_start_us": 1_100_000,
            "max_shift_us": 100_000,
        },
    )
    assert clamped.output["raw_offset_us"] == 400_000
    assert clamped.output["offset_us"] == 100_000
    assert clamped.output["clamped"] is True
    assert clamped.output["confidence"] == pytest.approx(0.0)


def test_align_music_offset_grid_is_exact_for_non_integer_tempos() -> None:
    """tempo 90 → one beat every 666_667us (rounded), never a float in the plan."""
    result = _dispatch(
        _bus(build_audio_registry()),
        OPERATION_ALIGN_MUSIC,
        {
            "music_asset_id": "music_01",
            "tempo_bpm": 90.0,
            "first_beat_us": 250_000,
            "target_start_us": 2_300_000,
            "anchor": "nearest_beat",
        },
    )
    assert result.output["beat_period_us"] == 666_667
    assert result.output["aligned_beat_index"] == 3
    assert result.output["beat_time_us"] == 2_250_001  # 250_000 + 3 * 666_667
    assert result.output["offset_us"] == -49_999
    assert result.output["confidence"] == pytest.approx(0.9)


def test_align_music_accepts_video_containers_and_rejects_unknown_assets() -> None:
    ok = _dispatch(
        _bus(build_audio_registry()),
        OPERATION_ALIGN_MUSIC,
        {"music_asset_id": "cam_a", "tempo_bpm": 60.0, "target_start_us": 900_000},
    )
    assert ok.output["beat_period_us"] == 1_000_000
    assert ok.output["beat_time_us"] == 1_000_000

    with pytest.raises(CommandValidationError, match="unknown music asset"):
        _dispatch(
            _bus(build_audio_registry()),
            OPERATION_ALIGN_MUSIC,
            {"music_asset_id": "ghost", "tempo_bpm": 60.0},
        )


# ---------------------------------------------------------------------------
# timeline.sync_multicam
# ---------------------------------------------------------------------------


def _multicam_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "clip_asset_ids": ["cam_a", "cam_b"],
        "anchors": [
            {"clip_asset_id": "cam_a", "anchor_us": 1_000_000},
            {"clip_asset_id": "cam_b", "anchor_us": 1_400_000},
        ],
        "anchor_policy": "first_clip",
        "max_offset_us": 5_000_000,
    }
    payload.update(overrides)
    return payload


def test_sync_multicam_offsets_every_clip_against_the_reference() -> None:
    bus = _bus(build_edit_registry())
    result = _dispatch(bus, OPERATION_SYNC_MULTICAM, _multicam_payload())
    assert result.status == "applied"
    assert result.output["reference_clip_id"] == "cam_a"
    offsets = {entry["clip_asset_id"]: entry for entry in result.output["offset_map"]}
    assert offsets["cam_a"]["offset_us"] == 0
    assert offsets["cam_a"]["is_reference"] is True
    assert offsets["cam_b"]["offset_us"] == 400_000
    assert result.output["clamped_clips"] == []
    assert result.output["confidence"] == pytest.approx(0.92)


def test_sync_multicam_is_level_a_and_never_mutates_the_project() -> None:
    bus = _bus(build_edit_registry())
    before = len(bus.project.assets)
    result = _dispatch(bus, OPERATION_SYNC_MULTICAM, _multicam_payload())
    assert result.status == "applied"
    assert len(bus.project.assets) == before
    # A Level-A operation returns evidence only: no derived asset id, no transaction
    # payload of its own (the bus still records the applied command in history).
    assert "asset_id" not in result.output
    assert result.output["offset_map"]


def test_sync_multicam_longest_clip_and_explicit_reference() -> None:
    longest = _dispatch(
        _bus(build_edit_registry()),
        OPERATION_SYNC_MULTICAM,
        _multicam_payload(anchor_policy="longest_clip"),
    )
    assert longest.output["reference_clip_id"] == "cam_b"  # 9 s beats 6 s
    offsets = {entry["clip_asset_id"]: entry for entry in longest.output["offset_map"]}
    assert offsets["cam_a"]["offset_us"] == -400_000
    assert offsets["cam_b"]["offset_us"] == 0

    explicit = _dispatch(
        _bus(build_edit_registry()),
        OPERATION_SYNC_MULTICAM,
        _multicam_payload(anchor_policy="explicit", reference_clip_id="cam_b"),
    )
    assert explicit.output["reference_clip_id"] == "cam_b"


def test_sync_multicam_clamps_and_reports_zero_confidence_beyond_the_window() -> None:
    result = _dispatch(
        _bus(build_edit_registry()),
        OPERATION_SYNC_MULTICAM,
        _multicam_payload(max_offset_us=100_000),
    )
    entry = next(e for e in result.output["offset_map"] if e["clip_asset_id"] == "cam_b")
    assert entry["raw_offset_us"] == 400_000
    assert entry["offset_us"] == 100_000
    assert entry["clamped"] is True
    assert result.output["clamped_clips"] == ["cam_b"]
    assert result.output["confidence"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "clip_asset_ids": ["cam_a", "cam_b", "cam_c"],
                "anchors": [
                    {"clip_asset_id": "cam_a", "anchor_us": 0},
                    {"clip_asset_id": "cam_b", "anchor_us": 0},
                ],
            },
            "missing anchors",
        ),
        (
            {
                "anchors": [
                    {"clip_asset_id": "cam_a", "anchor_us": 0},
                    {"clip_asset_id": "cam_a", "anchor_us": 10},
                ]
            },
            "one marker per clip",
        ),
        (
            {
                "anchors": [
                    {"clip_asset_id": "cam_a", "anchor_us": 0},
                    {"clip_asset_id": "cam_b", "anchor_us": 0},
                    {"clip_asset_id": "cam_c", "anchor_us": 0},
                ]
            },
            "not in the set",
        ),
        ({"clip_asset_ids": ["cam_a", "cam_a"]}, "must be unique"),
        ({"anchor_policy": "explicit"}, "requires reference_clip_id"),
        ({"reference_clip_id": "cam_b"}, "only meaningful"),
    ],
)
def test_sync_multicam_request_validation(overrides: dict[str, object], message: str) -> None:
    from nexus_ai_agent.creative.packs.edit.models import SyncMulticamInput

    with pytest.raises(ValidationError, match=message):
        SyncMulticamInput.model_validate(_multicam_payload(**overrides))


def test_sync_multicam_rejects_unknown_or_non_video_clips() -> None:
    with pytest.raises(CommandValidationError, match="unknown clip"):
        _dispatch(
            _bus(build_edit_registry()),
            OPERATION_SYNC_MULTICAM,
            _multicam_payload(
                clip_asset_ids=["cam_a", "ghost"],
                anchors=[
                    {"clip_asset_id": "cam_a", "anchor_us": 0},
                    {"clip_asset_id": "ghost", "anchor_us": 0},
                ],
            ),
        )
    with pytest.raises(CommandValidationError, match="requires video clips"):
        _dispatch(
            _bus(build_edit_registry()),
            OPERATION_SYNC_MULTICAM,
            _multicam_payload(
                clip_asset_ids=["cam_a", "voice_01"],
                anchors=[
                    {"clip_asset_id": "cam_a", "anchor_us": 0},
                    {"clip_asset_id": "voice_01", "anchor_us": 0},
                ],
            ),
        )


# ---------------------------------------------------------------------------
# purity: the bus promise holds for every new handler
# ---------------------------------------------------------------------------


#: Operations that add a derived asset; the rest are analysis-only by contract.
DERIVING_OPERATIONS = frozenset(
    {OPERATION_APPLY_MASK, OPERATION_WARP, OPERATION_ADD_PARTICLES, OPERATION_REMOVE_VOCAL}
)


def test_handlers_never_mutate_the_state_they_receive() -> None:
    """The bus promise ("no implicit mutation") is the handler's contract.

    Asserted on the handler itself: the bus legitimately bumps ``state_revision``
    and installs the returned project, so a bus-level comparison would test the
    wrong layer.
    """
    from nexus_ai_agent.creative.studio.capabilities import OperationContext

    cases = [
        (
            build_motion_registry(),
            OPERATION_APPLY_MASK,
            {"clip_asset_id": "cam_a", "mask_asset_id": "mask_01"},
        ),
        (
            build_motion_registry(),
            OPERATION_WARP,
            {"clip_asset_id": "cam_a", "control_points": [{"row": 0, "col": 0, "dx": 0.2}]},
        ),
        (
            build_motion_registry(),
            OPERATION_ADD_PARTICLES,
            {"clip_asset_id": "cam_a", "start_us": 0, "end_us": 1_000_000},
        ),
        (build_audio_registry(), OPERATION_REMOVE_VOCAL, {"audio_asset_id": "music_01"}),
        (
            build_audio_registry(),
            OPERATION_ALIGN_MUSIC,
            {"music_asset_id": "music_01", "tempo_bpm": 100.0},
        ),
        (build_edit_registry(), OPERATION_SYNC_MULTICAM, _multicam_payload()),
    ]
    for registry, operation, payload in cases:
        project = _project()
        snapshot = project.model_dump()
        spec = registry.get_spec(operation)
        validated = spec.input_model(**payload)
        context = OperationContext(
            command=TypedCommand(command_id=f"cmd_{operation}", operation=operation, input=payload),
            input_data=validated.model_dump(),
            history=(),
        )
        outcome = spec.handler(project, context)
        assert project.model_dump() == snapshot, f"{operation} mutated its input"
        assert outcome.output, f"{operation} returned no evidence"
        # Only the deriving operations add an asset; the analysis operations
        # (``audio.align_music``, ``timeline.sync_multicam``) return evidence and
        # the state untouched — level B does not imply a derivation.
        derived = outcome.project.model_dump() != snapshot
        assert derived == (operation in DERIVING_OPERATIONS), f"{operation}: derived={derived}"
