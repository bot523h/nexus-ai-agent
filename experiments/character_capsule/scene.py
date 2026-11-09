"""Scenes, scene-scoped overrides, and revision-pinned shot references.

The rule this module encodes
----------------------------
A shot never "uses a character". A shot references *one exact capsule revision*,
with the module digests pinned at authoring time. That is the difference between
"the renderer used whatever the capsule said at 14:02" and a reproducible claim.

Overrides are separated from the capsule on purpose, and they are *narrow*: a
scene may override expression, pose, lighting, environment, wardrobe and hair
state. It may **not** override face or body identity — that is not a scene
decision, it is a different character, and attempting it is
:class:`~experiments.character_capsule.errors.ScopeViolation`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .errors import PinMismatch, ScopeViolation, UnknownReference, ValidationError
from .model import COLOUR_RE, CharacterCapsule, _require

SCENE_ID_RE = re.compile(r"^SC-\d{3}$")
SHOT_ID_RE = re.compile(r"^SH-\d{3}$")
SEQUENCE_ID_RE = re.compile(r"^SEQ-[A-Z0-9-]{2,20}$")


class OverrideDimension(str, Enum):
    """What a scene is allowed to say about a character."""

    EXPRESSION = "EXPRESSION"
    POSE = "POSE"
    LIGHTING = "LIGHTING"
    ENVIRONMENT = "ENVIRONMENT"
    WARDROBE = "WARDROBE"
    HAIR_STATE = "HAIR_STATE"
    # Forbidden dimensions exist in the vocabulary so the refusal is explicit
    # and testable, not an unknown-string accident.
    FACE_IDENTITY = "FACE_IDENTITY"
    BODY_IDENTITY = "BODY_IDENTITY"


#: Dimensions a scene may never override.
FORBIDDEN_OVERRIDES: frozenset[OverrideDimension] = frozenset(
    {OverrideDimension.FACE_IDENTITY, OverrideDimension.BODY_IDENTITY}
)

ALLOWED_OVERRIDES: frozenset[OverrideDimension] = frozenset(
    dimension for dimension in OverrideDimension if dimension not in FORBIDDEN_OVERRIDES
)


@dataclass(frozen=True)
class Environment:
    """Measured, declared shooting conditions for a scene."""

    background_colour: str = "#3a3f4b"
    key_light_kelvin: int = 5600
    intensity: float = 1.0

    def __post_init__(self) -> None:
        if not COLOUR_RE.match(self.background_colour):
            raise ValidationError("environment.background_colour must be #rrggbb")
        _require(
            1500 <= int(self.key_light_kelvin) <= 12000, "key_light_kelvin must be 1500..12000"
        )
        _require(0.05 <= float(self.intensity) <= 4.0, "environment.intensity must be 0.05..4.0")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Scene:
    """One scene in the timeline. ``order`` is the story order key."""

    scene_id: str
    episode: int
    sequence_id: str
    order: int
    title: str
    environment: Environment = field(default_factory=Environment)

    def __post_init__(self) -> None:
        if not SCENE_ID_RE.match(self.scene_id):
            raise ValidationError("scene ids look like SC-001", value=self.scene_id)
        if not SEQUENCE_ID_RE.match(self.sequence_id):
            raise ValidationError("sequence ids look like SEQ-EP1-A", value=self.sequence_id)
        _require(int(self.episode) >= 1, "scene episode must be >= 1")
        _require(int(self.order) >= 1, "scene order must be >= 1")
        _require(bool(self.title.strip()), "scene needs a title")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["environment"] = self.environment.to_dict()
        return data


@dataclass(frozen=True)
class SceneOverride:
    """A scene-scoped deviation, with a reason and the dimensions it touches."""

    scene_id: str
    choices: Mapping[OverrideDimension, Any]
    reason: str

    def __post_init__(self) -> None:
        if not SCENE_ID_RE.match(self.scene_id):
            raise ValidationError("override needs a valid scene_id", value=self.scene_id)
        _require(bool(self.choices), "a scene override must change something")
        _require(bool(self.reason.strip()), "a scene override must state a reason")
        for dimension in self.choices:
            if dimension in FORBIDDEN_OVERRIDES:
                raise ScopeViolation(
                    f"{dimension.value} cannot be overridden at scene scope",
                    scene_id=self.scene_id,
                    dimension=dimension.value,
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "reason": self.reason,
            "choices": {dimension.value: value for dimension, value in self.choices.items()},
        }


@dataclass(frozen=True)
class DeclaredShot:
    """What the shot *claims* about itself before any pixels are examined."""

    outfit_id: str
    expression: str = "neutral"
    pose: str = "standing"
    canon_assertions: Mapping[str, str] = field(default_factory=dict)
    scene_override: SceneOverride | None = None

    def __post_init__(self) -> None:
        _require(bool(self.outfit_id.strip()), "shot must declare an outfit")
        _require(
            self.expression in {"neutral", "smile", "sad", "angry", "surprised"},
            f"unsupported expression {self.expression!r}",
        )
        _require(bool(self.pose.strip()), "shot must declare a pose")
        object.__setattr__(self, "canon_assertions", dict(self.canon_assertions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "outfit_id": self.outfit_id,
            "expression": self.expression,
            "pose": self.pose,
            "canon_assertions": dict(sorted(self.canon_assertions.items())),
            "scene_override": self.scene_override.to_dict() if self.scene_override else None,
        }


@dataclass(frozen=True)
class ShotReference:
    """A shot bound to one exact capsule revision and its module digests."""

    shot_id: str
    scene_id: str
    sequence_id: str
    shot_index: int
    character_id: str
    capsule_revision: int
    capsule_digest: str
    module_digests: Mapping[str, str]
    declared: DeclaredShot
    image_asset_id: str
    voice_asset_id: str | None = None
    motion_asset_id: str | None = None

    def __post_init__(self) -> None:
        if not SHOT_ID_RE.match(self.shot_id):
            raise ValidationError("shot ids look like SH-001", value=self.shot_id)
        _require(int(self.shot_index) >= 0, "shot_index must be >= 0")
        _require(int(self.capsule_revision) >= 1, "shot must pin a capsule revision >= 1")
        _require(
            self.capsule_digest.startswith("sha256:") and len(self.capsule_digest) == 71,
            "shot must pin a capsule digest",
        )
        _require(bool(self.image_asset_id.strip()), "shot must reference an image asset")
        _require(
            bool(self.module_digests), "shot must pin the module digests it was authored against"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "scene_id": self.scene_id,
            "sequence_id": self.sequence_id,
            "shot_index": self.shot_index,
            "character_id": self.character_id,
            "capsule_revision": self.capsule_revision,
            "capsule_digest": self.capsule_digest,
            "module_digests": dict(sorted(self.module_digests.items())),
            "declared": self.declared.to_dict(),
            "image_asset_id": self.image_asset_id,
            "voice_asset_id": self.voice_asset_id,
            "motion_asset_id": self.motion_asset_id,
        }


def shot_from_dict(payload: Mapping[str, Any]) -> ShotReference:
    """Rebuild a shot record from JSON, refusing fields it does not understand.

    An unknown key is a *pin the loader silently dropped*: a manifest that says
    ``capsule_digest_v2`` and is loaded without it looks pinned while behaving
    unpinned. Strict loading is what makes the record a boundary.
    """
    known = {
        "shot_id",
        "scene_id",
        "sequence_id",
        "shot_index",
        "character_id",
        "capsule_revision",
        "capsule_digest",
        "module_digests",
        "declared",
        "image_asset_id",
        "voice_asset_id",
        "motion_asset_id",
    }
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ValidationError("shot manifest has unknown fields", fields=unknown)
    declared_payload = payload.get("declared")
    if not isinstance(declared_payload, Mapping):
        raise ValidationError("shot manifest must declare the shot (outfit, expression, pose)")
    declared_known = {"outfit_id", "expression", "pose", "canon_assertions"}
    declared_unknown = sorted(set(declared_payload) - declared_known)
    if declared_unknown:
        raise ValidationError(
            "shot manifest declared block has unknown fields", fields=declared_unknown
        )
    try:
        declared = DeclaredShot(
            outfit_id=str(declared_payload["outfit_id"]),
            expression=str(declared_payload.get("expression", "neutral")),
            pose=str(declared_payload.get("pose", "standing")),
            canon_assertions=dict(declared_payload.get("canon_assertions", {})),
        )
        return ShotReference(
            shot_id=str(payload["shot_id"]),
            scene_id=str(payload["scene_id"]),
            sequence_id=str(payload["sequence_id"]),
            shot_index=int(payload["shot_index"]),
            character_id=str(payload["character_id"]),
            capsule_revision=int(payload["capsule_revision"]),
            capsule_digest=str(payload["capsule_digest"]),
            module_digests={str(k): str(v) for k, v in dict(payload["module_digests"]).items()},
            declared=declared,
            image_asset_id=str(payload["image_asset_id"]),
            voice_asset_id=payload.get("voice_asset_id"),
            motion_asset_id=payload.get("motion_asset_id"),
        )
    except KeyError as exc:
        raise ValidationError(f"shot manifest is missing {exc.args[0]!r}") from exc


class Timeline:
    """Ordered set of scenes; the only source of "story order"."""

    def __init__(self, scenes: Iterable[Scene]) -> None:
        ordered = sorted(scenes, key=lambda scene: scene.order)
        if not ordered:
            raise ValidationError("a timeline needs at least one scene")
        orders = [scene.order for scene in ordered]
        if len(set(orders)) != len(orders):
            raise ValidationError("scene order values must be unique", orders=orders)
        ids = [scene.scene_id for scene in ordered]
        if len(set(ids)) != len(ids):
            raise ValidationError("scene ids must be unique", ids=ids)
        self._scenes = {scene.scene_id: scene for scene in ordered}

    def scene(self, scene_id: str) -> Scene:
        try:
            return self._scenes[scene_id]
        except KeyError:
            raise UnknownReference(
                f"timeline has no scene {scene_id!r}", scene_id=scene_id
            ) from None

    @property
    def scenes(self) -> tuple[Scene, ...]:
        return tuple(
            self._scenes[key] for key in sorted(self._scenes, key=lambda k: self.scene(k).order)
        )


def validate_capsule_pins(shot: ShotReference, capsule: CharacterCapsule) -> None:
    """Refuse a shot whose character, revision, digest or module pins do not match.

    This is deliberately callable *without* a timeline, because a verifier must
    check the pins even when it has no scene context: the pins are the contract
    between the shot and the capsule it was authored against.
    """
    if shot.character_id != capsule.character_id:
        raise PinMismatch(
            "shot references a different character than the supplied capsule",
            shot_character=shot.character_id,
            capsule_character=capsule.character_id,
        )
    if shot.capsule_digest != capsule.digest:
        raise PinMismatch(
            "shot is pinned to a different capsule digest than the supplied capsule",
            pinned=shot.capsule_digest,
            supplied=capsule.digest,
        )
    if shot.capsule_revision > capsule.revision_no:
        raise UnknownReference(
            "shot pins a capsule revision that does not exist yet",
            pinned=shot.capsule_revision,
            available=capsule.revision_no,
        )
    if shot.capsule_revision != capsule.revision_no:
        # A pinned, older revision is legitimate (re-render of a finished shot)
        # but the caller must pass that exact revision; silently upgrading is drift.
        raise PinMismatch(
            "shot is pinned to a different capsule revision than the supplied one",
            pinned=shot.capsule_revision,
            supplied=capsule.revision_no,
        )
    current = capsule.module_digests()
    for name, digest in shot.module_digests.items():
        if name not in current:
            raise UnknownReference(f"shot pins unknown module {name!r}", module=name)
        if current[name] != digest:
            raise PinMismatch(
                "shot module pin does not match the supplied capsule revision",
                module=name,
                pinned=digest,
                actual=current[name],
            )


def validate_shot_reference(
    shot: ShotReference,
    *,
    timeline: Timeline,
    capsule: CharacterCapsule,
) -> None:
    """Refuse a shot whose scene, revision, or module pins do not line up."""
    scene = timeline.scene(shot.scene_id)
    if scene.sequence_id != shot.sequence_id:
        raise ValidationError(
            "shot sequence does not match its scene",
            shot_sequence=shot.sequence_id,
            scene_sequence=scene.sequence_id,
        )
    validate_capsule_pins(shot, capsule)
    override = shot.declared.scene_override
    if override is not None and override.scene_id != shot.scene_id:
        raise ValidationError(
            "scene override belongs to a different scene",
            override_scene=override.scene_id,
            shot_scene=shot.scene_id,
        )
    if shot.declared.outfit_id not in getattr(capsule.modules.get("wardrobe"), "outfits", {}):
        raise UnknownReference(
            f"shot declares an outfit that is not registered: {shot.declared.outfit_id!r}",
            outfit=shot.declared.outfit_id,
        )


def build_shot(
    *,
    shot_id: str,
    scene: Scene,
    capsule: CharacterCapsule,
    shot_index: int,
    outfit_id: str,
    image_asset_id: str,
    expression: str = "neutral",
    pose: str = "standing",
    canon_assertions: Mapping[str, str] | None = None,
    scene_override: SceneOverride | None = None,
    voice_asset_id: str | None = None,
    motion_asset_id: str | None = None,
) -> ShotReference:
    """Author a shot reference pinned to the capsule's current revision."""
    return ShotReference(
        shot_id=shot_id,
        scene_id=scene.scene_id,
        sequence_id=scene.sequence_id,
        shot_index=shot_index,
        character_id=capsule.character_id,
        capsule_revision=capsule.revision_no,
        capsule_digest=capsule.digest,
        module_digests=capsule.module_digests(),
        declared=DeclaredShot(
            outfit_id=outfit_id,
            expression=expression,
            pose=pose,
            canon_assertions=dict(canon_assertions or {}),
            scene_override=scene_override,
        ),
        image_asset_id=image_asset_id,
        voice_asset_id=voice_asset_id,
        motion_asset_id=motion_asset_id,
    )


__all__ = [
    "ALLOWED_OVERRIDES",
    "FORBIDDEN_OVERRIDES",
    "DeclaredShot",
    "Environment",
    "OverrideDimension",
    "Scene",
    "SceneOverride",
    "ShotReference",
    "Timeline",
    "build_shot",
    "validate_shot_reference",
]
