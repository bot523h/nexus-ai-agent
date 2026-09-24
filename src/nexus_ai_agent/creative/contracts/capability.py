"""Capability Contract — canonical separation of Capability vs Operation.

Conceptual chain:
    Capability
        ↓
    Operation
        ↓
    Input Schema
        ↓
    Policy
        ↓
    Execution Adapter

Capability is NOT just a string registry; it carries policy, locality,
permissions, and availability.

This module is dependency-light: stdlib + pydantic + studio L1 types only.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class LocalityPolicy(str, Enum):
    """Local/cloud routing policy — product rule: private media must not silently go to cloud."""

    LOCAL_ONLY = "LOCAL_ONLY"
    PREFER_LOCAL = "PREFER_LOCAL"
    CLOUD_ALLOWED = "CLOUD_ALLOWED"
    EXPLICIT_CLOUD = "EXPLICIT_CLOUD"


class ExecutionClass(str, Enum):
    """How the operation executes."""

    PURE_REDUCER = "pure_reducer"
    RENDER_LANE = "render_lane"
    ANALYSIS_ONNX = "analysis_onnx"
    NATIVE_WORKER = "native_worker"
    EXTERNAL_BINARY = "external_binary"  # ffmpeg, etc.


class Reversibility(str, Enum):
    REVERSIBLE = "reversible"
    NON_REVERSIBLE = "non_reversible"
    PREVIEW_REQUIRED = "preview_required"
    REVIEW_REQUIRED = "review_required"


class AvailabilityState(str, Enum):
    AVAILABLE = "AVAILABLE"
    PENDING = "PENDING"
    DEPRECATED = "DEPRECATED"
    BLOCKED = "BLOCKED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class PermissionRequirement(str, Enum):
    """Maps to studio PermissionLevel A/B/C/D but explicit for capability contract."""

    IMMEDIATE = "A"
    REVERSIBLE = "B"
    CONFIRMATION = "C"
    DENIED = "D"


class CapabilityContract(BaseModel):
    """Canonical capability contract — one capability may expose multiple operations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1, description="e.g. nexus.edit.timeline")
    version: str = Field(default="1.0.0", description="capability contract version")
    display_name: str = Field(min_length=1)

    supported_operations: tuple[str, ...] = Field(
        description="Operation IDs this capability exposes, e.g. timeline.trim"
    )
    schema_versions: tuple[str, ...] = Field(
        default=("nagar.command.v1",),
        description="Supported command envelope versions",
    )

    execution_class: ExecutionClass = Field(description="How it executes")
    locality_policy: LocalityPolicy = Field(
        default=LocalityPolicy.LOCAL_ONLY,
        description="Local/cloud routing constraint",
    )
    permission_requirement: PermissionRequirement = Field(
        description="Minimum permission level required"
    )

    required_packs: tuple[str, ...] = Field(
        default=(), description="Pack IDs required for this capability"
    )
    supported_targets: tuple[str, ...] = Field(
        default=("project", "clip", "track", "timeline"),
        description="Target kinds this capability supports",
    )

    preview_available: bool = Field(default=False, description="Can preview without commit?")
    reversibility: Reversibility = Field(default=Reversibility.REVERSIBLE)
    idempotency_guaranteed: bool = Field(
        default=True, description="Does the capability guarantee idempotent semantics via key?"
    )
    availability: AvailabilityState = Field(default=AvailabilityState.AVAILABLE)

    # Policy extras
    requires_confirmation: bool = Field(default=False)
    egress_allowed: bool = Field(default=False, description="Is cloud egress allowed?")
    external_binaries: tuple[str, ...] = Field(default=(), description="e.g. ffmpeg")

    # Evidence
    evidence_location: str = Field(default="", description="Where is this proven?")
    notes: str = Field(default="")

    def is_local_only(self) -> bool:
        return self.locality_policy == LocalityPolicy.LOCAL_ONLY

    def requires_cloud(self) -> bool:
        return self.locality_policy in (
            LocalityPolicy.CLOUD_ALLOWED,
            LocalityPolicy.EXPLICIT_CLOUD,
        )


# Canonical capability contracts for the 6 shipped packs + 2 missing vision packs (as MISSING)
# These are data, not execution — they describe what SHOULD exist.

CANONICAL_CAPABILITIES: tuple[CapabilityContract, ...] = (
    CapabilityContract(
        capability_id="nexus.edit.timeline",
        display_name="Non-destructive timeline editing",
        supported_operations=(
            "timeline.split_at_playhead",
            "timeline.trim",
            "timeline.ripple_delete",
            "timeline.insert_gap",
            "timeline.speed_ramp",
            "timeline.reverse_segment",
            "timeline.freeze_frame",
            "timeline.attach_b_roll",
            "timeline.sync_multicam",
            "timeline.retime_to_music",
        ),
        execution_class=ExecutionClass.PURE_REDUCER,
        locality_policy=LocalityPolicy.LOCAL_ONLY,
        permission_requirement=PermissionRequirement.REVERSIBLE,
        required_packs=("nexus.edit.timeline",),
        supported_targets=("clip", "track", "timeline", "project"),
        preview_available=True,
        reversibility=Reversibility.REVERSIBLE,
        idempotency_guaranteed=True,
        availability=AvailabilityState.AVAILABLE,
        evidence_location="src/nexus_ai_agent/creative/packs/edit/",
    ),
    CapabilityContract(
        capability_id="nexus.vision.portrait",
        display_name="Portrait enhancement",
        supported_operations=(
            "portrait.detect_landmarks",
            "portrait.smooth_skin",
            "portrait.retouch_blemish",
            "portrait.relight_face",
            "portrait.whiten_teeth",
            "portrait.correct_gaze",
            "portrait.enhance_eyes",
            "portrait.mask_hair",
            "portrait.background_blur",
            "portrait.stabilize_face",
        ),
        execution_class=ExecutionClass.ANALYSIS_ONNX,
        locality_policy=LocalityPolicy.LOCAL_ONLY,
        permission_requirement=PermissionRequirement.REVERSIBLE,
        required_packs=("nexus.vision.portrait",),
        supported_targets=("clip", "face_track"),
        preview_available=True,
        reversibility=Reversibility.REVERSIBLE,
        idempotency_guaranteed=True,
        availability=AvailabilityState.NOT_IMPLEMENTED,
        notes="MISSING in runtime 57 — TDD defines but no registry",
    ),
    CapabilityContract(
        capability_id="nexus.vision.scene",
        display_name="Scene understanding",
        supported_operations=(
            "scene.segment_subject",
            "scene.remove_object",
            "scene.replace_sky",
            "scene.remove_background",
            "scene.track_object",
            "scene.track_face",
            "scene.detect_shot_boundaries",
            "scene.find_subject_moment",
            "scene.remove_logo",
            "scene.auto_reframe_subject",
        ),
        execution_class=ExecutionClass.ANALYSIS_ONNX,
        locality_policy=LocalityPolicy.LOCAL_ONLY,
        permission_requirement=PermissionRequirement.REVERSIBLE,
        required_packs=("nexus.vision.scene",),
        supported_targets=("clip", "object_track", "subject"),
        preview_available=True,
        reversibility=Reversibility.PREVIEW_REQUIRED,
        idempotency_guaranteed=True,
        availability=AvailabilityState.NOT_IMPLEMENTED,
        notes="MISSING in runtime 57",
    ),
    CapabilityContract(
        capability_id="nexus.motion.graphics",
        display_name="Motion graphics",
        supported_operations=(
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
        ),
        execution_class=ExecutionClass.RENDER_LANE,
        locality_policy=LocalityPolicy.LOCAL_ONLY,
        permission_requirement=PermissionRequirement.REVERSIBLE,
        required_packs=("nexus.motion.graphics",),
        supported_targets=("clip", "layer", "timeline"),
        preview_available=True,
        reversibility=Reversibility.REVERSIBLE,
        idempotency_guaranteed=True,
        availability=AvailabilityState.AVAILABLE,
        evidence_location="src/nexus_ai_agent/creative/packs/motion/",
    ),
    CapabilityContract(
        capability_id="nexus.audio.studio",
        display_name="Audio studio",
        supported_operations=(
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
        ),
        execution_class=ExecutionClass.ANALYSIS_ONNX,
        locality_policy=LocalityPolicy.LOCAL_ONLY,
        permission_requirement=PermissionRequirement.REVERSIBLE,
        required_packs=("nexus.audio.studio",),
        supported_targets=("audio", "timeline", "track"),
        preview_available=True,
        reversibility=Reversibility.REVERSIBLE,
        idempotency_guaranteed=True,
        availability=AvailabilityState.AVAILABLE,
        evidence_location="src/nexus_ai_agent/creative/packs/audio/",
    ),
    CapabilityContract(
        capability_id="nexus.language.caption",
        display_name="Language caption",
        supported_operations=(
            "caption.transcribe",
            "caption.align_words",
            "caption.diarize",
            "caption.translate_local",
            "caption.generate_srt",
            "caption.generate_ass_rtl",
            "caption.style_vazirmatn",
            "caption.highlight_words",
            "caption.search_transcript",
            "caption.burn_in",
        ),
        execution_class=ExecutionClass.NATIVE_WORKER,
        locality_policy=LocalityPolicy.LOCAL_ONLY,
        permission_requirement=PermissionRequirement.REVERSIBLE,
        required_packs=("nexus.language.caption",),
        supported_targets=("audio", "clip", "transcript"),
        preview_available=True,
        reversibility=Reversibility.REVERSIBLE,
        idempotency_guaranteed=True,
        availability=AvailabilityState.AVAILABLE,
        evidence_location="src/nexus_ai_agent/creative/packs/caption/",
    ),
    CapabilityContract(
        capability_id="nexus.color.delivery",
        display_name="Color and delivery",
        supported_operations=(
            "color.auto_balance",
            "color.adjust_exposure",
            "color.white_balance",
            "color.match_shot",
            "color.apply_lut",
            "color.hdr_tonemap",
            "color.deband_denoise",
            "delivery.make_proxy_480p",
            "delivery.render_master_4k",
            "delivery.export_otio",
        ),
        execution_class=ExecutionClass.RENDER_LANE,
        locality_policy=LocalityPolicy.LOCAL_ONLY,
        permission_requirement=PermissionRequirement.REVERSIBLE,
        required_packs=("nexus.color.delivery",),
        supported_targets=("clip", "project", "timeline"),
        preview_available=True,
        reversibility=Reversibility.REVERSIBLE,
        idempotency_guaranteed=True,
        availability=AvailabilityState.AVAILABLE,
        external_binaries=("ffmpeg",),
        evidence_location="src/nexus_ai_agent/creative/packs/delivery/",
        notes="3 operations missing in runtime: white_balance, hdr_tonemap, deband_denoise",
    ),
    CapabilityContract(
        capability_id="nexus.slideshow.compose",
        display_name="Slideshow compose (extra, not in 70)",
        supported_operations=(
            "slideshow.scan_assets",
            "slideshow.suggest_tone",
            "slideshow.score_images",
            "slideshow.compose",
            "slideshow.render",
            "slideshow.upscale",
        ),
        execution_class=ExecutionClass.RENDER_LANE,
        locality_policy=LocalityPolicy.LOCAL_ONLY,
        permission_requirement=PermissionRequirement.REVERSIBLE,
        required_packs=("nexus.slideshow.compose",),
        supported_targets=("project", "image_set"),
        preview_available=True,
        reversibility=Reversibility.REVERSIBLE,
        idempotency_guaranteed=True,
        availability=AvailabilityState.AVAILABLE,
        external_binaries=("ffmpeg",),
        evidence_location="src/nexus_ai_agent/creative/packs/slideshow/",
        notes="Extra outside 70 — 6 ops",
    ),
)


def capability_by_id(capability_id: str) -> CapabilityContract | None:
    for cap in CANONICAL_CAPABILITIES:
        if cap.capability_id == capability_id:
            return cap
    return None


def all_operations_from_capabilities() -> set[str]:
    ops: set[str] = set()
    for cap in CANONICAL_CAPABILITIES:
        ops.update(cap.supported_operations)
    return ops
