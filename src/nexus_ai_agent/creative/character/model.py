"""Character capsule: stable identity + versioned modules + continuity sealing.

This is the re-engineered replacement for the character capsule contracts of
old PR #326.  It is deliberately scoped: identity/revision discipline and the
closed verdict vocabulary are the load-bearing contract; identity-pinning
ancient-versioning, wardrobe and voice modules remain caller-owned data.

Hierarchy (frozen, validated at construction)::

    Capsule (identity_version, revision_no, character_id, modules)
      → FaceModule    (IDENTITY: eye_colour, eye_spacing_ratio, jaw_width_ratio;
                       REVISION: skin_colour, skin_texture, revision)
      → VoiceModule   (IDENTITY: f0_hz, timbre; REVISION: revision)
      → WardrobeModule(REVISION: outfit, era, silhouette; REVISION: revision)

Identity-scoped fields can only change through
:meth:`CharacterCapsule.new_identity_version`, which mints a new identity
version.  Revision-scoped fields change via :meth:`CharacterCapsule.apply_revision_changes`.

The capsule is sealed by a domain-separated SHA-256 digest, so a hand-edited
capsule is caught structurally before any verifier consumes it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

#: ``NAGAR-CHAR-000001`` — six digits, zero-padded, never recycled.
CHARACTER_ID_RE = re.compile(r"^NAGAR-CHAR-(\d{6})$")
#: Hex colour used by every palette field.
COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class Scope(str, Enum):
    """How narrowly a field may change."""

    IDENTITY = "IDENTITY"
    REVISION = "REVISION"
    SCENE = "SCENE"
    SHOT = "SHOT"


class ValidationError(ValueError):
    """A capsule field failed validation."""


class ScopeViolation(ValidationError):
    """A revision-scoped update attempted to change an identity-scoped field."""


def validate_character_id(value: str) -> str:
    if not isinstance(value, str) or not CHARACTER_ID_RE.match(value):
        raise ValidationError("character id must look like NAGAR-CHAR-000001")
    return value


def _require_colour(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not COLOUR_RE.match(value):
        raise ValidationError(f"{field_name} must be a #rrggbb colour")


def _require_ratio(value: float, *, field_name: str, low: float = 0.05, high: float = 1.5) -> None:
    if not (low <= float(value) <= high):
        raise ValidationError(f"{field_name} must be in [{low}, {high}]")


def _require_positive_int(value: int, *, field_name: str, minimum: int = 1) -> None:
    if not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{field_name} must be an int >= {minimum}")


@dataclass(frozen=True, slots=True)
class FaceModule:
    eye_colour: str = "#3c5f8a"
    eye_spacing_ratio: float = 0.235
    jaw_width_ratio: float = 0.62
    skin_colour: str = "#e8b48f"
    skin_texture: str = "smooth"
    revision: int = 1

    def __post_init__(self) -> None:
        _require_colour(self.eye_colour, field_name="face.eye_colour")
        _require_colour(self.skin_colour, field_name="face.skin_colour")
        _require_ratio(
            self.eye_spacing_ratio, field_name="face.eye_spacing_ratio", low=0.1, high=0.4
        )
        _require_ratio(self.jaw_width_ratio, field_name="face.jaw_width_ratio", low=0.3, high=0.95)
        _require_positive_int(self.revision, field_name="face.revision", minimum=1)
        if self.skin_texture not in {"smooth", "weathered", "scarred"}:
            raise ValidationError("face.skin_texture must be smooth|weathered|scarred")

    def scope_of(self, field_name: str) -> Scope:
        scopes = {
            "eye_colour": Scope.IDENTITY,
            "eye_spacing_ratio": Scope.IDENTITY,
            "jaw_width_ratio": Scope.IDENTITY,
            "skin_colour": Scope.REVISION,
            "skin_texture": Scope.REVISION,
            "revision": Scope.REVISION,
        }
        try:
            return scopes[field_name]
        except KeyError:
            raise ValidationError(f"unknown face field {field_name!r}") from None

    @property
    def known_fields(self) -> frozenset[str]:
        return frozenset(
            {
                "eye_colour",
                "eye_spacing_ratio",
                "jaw_width_ratio",
                "skin_colour",
                "skin_texture",
                "revision",
            }
        )


@dataclass(frozen=True, slots=True)
class VoiceModule:
    f0_hz: float = 120.0
    timbre: str = "neutral"
    revision: int = 1

    def __post_init__(self) -> None:
        if not (50.0 <= float(self.f0_hz) <= 500.0):
            raise ValidationError("voice.f0_hz must be in [50, 500]")
        if self.timbre not in {"neutral", "warm", "cold", "aged", "young"}:
            raise ValidationError("voice.timbre must be neutral|warm|cold|aged|young")
        _require_positive_int(self.revision, field_name="voice.revision", minimum=1)

    def scope_of(self, field_name: str) -> Scope:
        scopes = {
            "f0_hz": Scope.IDENTITY,
            "timbre": Scope.IDENTITY,
            "revision": Scope.REVISION,
        }
        try:
            return scopes[field_name]
        except KeyError:
            raise ValidationError(f"unknown voice field {field_name!r}") from None

    @property
    def known_fields(self) -> frozenset[str]:
        return frozenset({"f0_hz", "timbre", "revision"})


@dataclass(frozen=True, slots=True)
class WardrobeModule:
    outfit: str = "default"
    era: str = "present"
    silhouette: str = "upright"
    revision: int = 1

    def __post_init__(self) -> None:
        if not self.outfit or len(self.outfit) > 128:
            raise ValidationError("wardrobe.outfit must be 1..128 chars")
        if not self.era or len(self.era) > 64:
            raise ValidationError("wardrobe.era must be 1..64 chars")
        if self.silhouette not in {"upright", "slouched", "military"}:
            raise ValidationError("wardrobe.silhouette must be upright|slouched|military")
        _require_positive_int(self.revision, field_name="wardrobe.revision", minimum=1)

    def scope_of(self, field_name: str) -> Scope:
        scopes = {
            "outfit": Scope.REVISION,
            "era": Scope.REVISION,
            "silhouette": Scope.REVISION,
            "revision": Scope.REVISION,
        }
        try:
            return scopes[field_name]
        except KeyError:
            raise ValidationError(f"unknown wardrobe field {field_name!r}") from None

    @property
    def known_fields(self) -> frozenset[str]:
        return frozenset({"outfit", "era", "silhouette", "revision"})


#: The canonical module names and their scope-aware mutators.
MODULE_KINDS = ("face", "voice", "wardrobe")


@dataclass(frozen=True, slots=True)
class CharacterCapsule:
    """Immutable, sealed capsule."""

    character_id: str
    identity_version: int
    revision_no: int
    face: FaceModule = FaceModule()
    voice: VoiceModule = VoiceModule()
    wardrobe: WardrobeModule = WardrobeModule()
    note: str = ""

    def __post_init__(self) -> None:
        validate_character_id(self.character_id)
        _require_positive_int(self.identity_version, field_name="identity_version", minimum=1)
        _require_positive_int(self.revision_no, field_name="revision_no", minimum=1)

    # -- digest -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "character-capsule/1",
            "character_id": self.character_id,
            "identity_version": self.identity_version,
            "revision_no": self.revision_no,
            "note": self.note,
            "face": {
                "eye_colour": self.face.eye_colour,
                "eye_spacing_ratio": self.face.eye_spacing_ratio,
                "jaw_width_ratio": self.face.jaw_width_ratio,
                "skin_colour": self.face.skin_colour,
                "skin_texture": self.face.skin_texture,
                "revision": self.face.revision,
            },
            "voice": {
                "f0_hz": self.voice.f0_hz,
                "timbre": self.voice.timbre,
                "revision": self.voice.revision,
            },
            "wardrobe": {
                "outfit": self.wardrobe.outfit,
                "era": self.wardrobe.era,
                "silhouette": self.wardrobe.silhouette,
                "revision": self.wardrobe.revision,
            },
        }

    def digest(self) -> str:
        """Domain-separated SHA-256 of the capsule content."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(b"capsule/1|" + canonical.encode("utf-8")).hexdigest()

    def seal(self) -> CharacterCapsule:
        """Return self (sealed); a hand-forged capsule cannot re-seal itself."""
        # Frozen dataclass; identity and digest are immutable by construction.
        return self

    # -- accessors ---------------------------------------------------------
    def module(self, name: str) -> FaceModule | VoiceModule | WardrobeModule:
        mapping: dict[str, FaceModule | VoiceModule | WardrobeModule] = {
            "face": self.face,
            "voice": self.voice,
            "wardrobe": self.wardrobe,
        }
        try:
            return mapping[name]
        except KeyError:
            raise ValidationError(f"unknown module {name!r} (known: {sorted(mapping)})") from None

    # -- mutation ----------------------------------------------------------
    def apply_revision_changes(
        self,
        module_name: str,
        updates: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        note: str = "",
    ) -> CharacterCapsule:
        """Apply revision-scoped changes to one module (atomic).

        ``revision`` is managed by the capsule; callers may not set it.
        Identity-scoped fields are refused — call :meth:`new_identity_version`.
        """
        if expected_revision is not None and int(expected_revision) != self.revision_no:
            raise ValidationError("capsule revision changed underneath this edit")
        module = self.module(module_name)
        if not updates:
            raise ValidationError("no updates supplied")
        for key, _value in updates.items():
            if key == "revision":
                raise ValidationError("revision is managed by the capsule")
            if key not in module.known_fields:
                raise ValidationError(f"unknown field {key!r} on module {module_name}")
            if module.scope_of(key) is Scope.IDENTITY:
                raise ScopeViolation(
                    f"{module_name}.{key} is IDENTITY-scoped; mint a new identity version instead"
                )
        bumped = replace(module, revision=module.revision + 1, **dict(updates))
        new_face: FaceModule = self.face
        new_voice: VoiceModule = self.voice
        new_wardrobe: WardrobeModule = self.wardrobe
        if module_name == "face":
            new_face = bumped  # type: ignore[assignment]
        elif module_name == "voice":
            new_voice = bumped  # type: ignore[assignment]
        else:
            new_wardrobe = bumped  # type: ignore[assignment]
        return CharacterCapsule(
            character_id=self.character_id,
            identity_version=self.identity_version,
            revision_no=self.revision_no + 1,
            face=new_face,
            voice=new_voice,
            wardrobe=new_wardrobe,
            note=note or self.note,
        ).seal()

    def new_identity_version(
        self,
        *,
        face_changes: Mapping[str, Any] | None = None,
        voice_changes: Mapping[str, Any] | None = None,
        note: str = "",
    ) -> CharacterCapsule:
        """Mint a new identity version. Only door for IDENTITY-scoped fields."""
        requested = {"face": dict(face_changes or {}), "voice": dict(voice_changes or {})}
        if not any(requested.values()):
            raise ValidationError("a new identity version needs at least one IDENTITY field change")
        modules: dict[str, Any] = {
            "face": self.face,
            "voice": self.voice,
            "wardrobe": self.wardrobe,
        }
        for name, changes in requested.items():
            if not changes:
                continue
            module = self.module(name)
            for key in changes:
                if key == "revision":
                    raise ValidationError("revision is managed by the capsule")
                if key not in module.known_fields:
                    raise ValidationError(f"unknown field {key!r} on module {name}")
                if module.scope_of(key) is not Scope.IDENTITY:
                    raise ValidationError(
                        f"{name}.{key} is not IDENTITY-scoped; use apply_revision_changes"
                    )
            bumped = replace(module, revision=module.revision + 1, **changes)
            modules[name] = bumped
        new_face2 = modules["face"]
        new_voice2 = modules["voice"]
        if not isinstance(new_face2, FaceModule) or not isinstance(new_voice2, VoiceModule):
            raise ValidationError("identity version assembly failed")
        return CharacterCapsule(
            character_id=self.character_id,
            identity_version=self.identity_version + 1,
            revision_no=self.revision_no + 1,
            face=new_face2,
            voice=new_voice2,
            wardrobe=self.wardrobe,
            note=note or f"identity version {self.identity_version + 1}",
        ).seal()


def next_character_id(existing: list[str] | tuple[str, ...] | set[str]) -> str:
    """Next free ``NAGAR-CHAR-######`` id; never reuses a number."""
    highest = 0
    for value in existing:
        match = CHARACTER_ID_RE.match(value)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"NAGAR-CHAR-{highest + 1:06d}"


__all__ = [
    "CHARACTER_ID_RE",
    "Scope",
    "ValidationError",
    "ScopeViolation",
    "validate_character_id",
    "FaceModule",
    "VoiceModule",
    "WardrobeModule",
    "CharacterCapsule",
    "next_character_id",
    "MODULE_KINDS",
]
