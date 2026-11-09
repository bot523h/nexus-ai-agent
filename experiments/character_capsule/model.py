"""The Character Capsule data model: identity, independently versioned modules, revisions.

The model is the argument
-------------------------
A character is not a profile document. It is a **stable identity** plus a set of
**independently versioned modules**, each of which declares *at which scope* its
fields may change. Three consequences are load bearing:

* A wardrobe change is a capsule revision that bumps exactly one module. It does
  not touch the face, and it does not mint a new character.
* A change to face geometry cannot be expressed as a revision at all. It is
  :class:`~experiments.character_capsule.errors.ScopeViolation` unless the caller
  asks for a new **identity version** — the only door through which "this is a
  different person now" can be said.
* Every module and every capsule is sealed by a domain-separated digest, so a
  hand-edited capsule is caught structurally before it reaches a verifier.

The ladder::

    CharacterIdentity (immutable, id + subject class)
      → IdentityVersion  (immutable core: face geometry, eye colour, legal name)
        → CapsuleRevision (mutable: wardrobe, hair, voice nuance, canon facts)
          → SceneOverride / ShotReference (per scene / per shot, pinned)

Nothing here performs I/O. Persistence, encryption and consent live in
``storage``/``security``; rendering lives in ``vision``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from enum import Enum
from typing import Any, ClassVar

from .canonical import (
    SCOPE_CAPSULE,
    SCOPE_IDENTITY,
    SCOPE_MODULE,
    canonical_json,
    scoped_digest,
)
from .errors import ScopeViolation, UnknownReference, ValidationError

#: ``NAGAR-CHAR-000001`` — six digits, zero padded, never recycled.
CHARACTER_ID_RE = re.compile(r"^NAGAR-CHAR-(\d{6})$")

#: Hex colour used by every palette field.
COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class Scope(str, Enum):
    """How narrowly a field may be changed, narrowest scope wins.

    ``IDENTITY`` — only a new identity version may change it.
    ``REVISION`` — a capsule revision may change it (approval policy applies).
    ``SCENE``    — a scene override may change it, for that scene only.
    ``SHOT``     — a shot may state it, never written back to the capsule.
    """

    IDENTITY = "IDENTITY"
    REVISION = "REVISION"
    SCENE = "SCENE"
    SHOT = "SHOT"


class SubjectClass(str, Enum):
    """Whose identity the capsule describes — the privacy switch."""

    SYNTHETIC = "SYNTHETIC"
    REAL_PERSON = "REAL_PERSON"


def validate_character_id(value: str) -> str:
    """Return ``value`` if it is a well-formed character id, else raise."""
    if not isinstance(value, str) or not CHARACTER_ID_RE.match(value):
        raise ValidationError("character id must look like NAGAR-CHAR-000001", value=repr(value))
    return value


def _require(condition: bool, message: str, **details: Any) -> None:
    if not condition:
        raise ValidationError(message, **details)


def _require_colour(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not COLOUR_RE.match(value):
        raise ValidationError(f"{field_name} must be a #rrggbb colour", value=repr(value))


def _require_ratio(value: float, *, field_name: str, low: float = 0.05, high: float = 1.5) -> None:
    _require(
        isinstance(value, (int, float)) and low <= float(value) <= high,
        f"{field_name} must be a ratio in [{low}, {high}]",
        value=value,
    )


class _Module:
    """Shared behaviour for capsule modules."""

    MODULE: ClassVar[str] = ""
    FIELD_SCOPES: ClassVar[dict[str, Scope]] = {}
    LABEL: ClassVar[str] = ""

    #: Every module carries a monotonically increasing local revision.
    revision: int

    def payload(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]

    @property
    def digest(self) -> str:
        return scoped_digest(SCOPE_MODULE, {"module": self.MODULE, "payload": self.payload()})

    def with_revision(self, revision: int) -> _Module:
        """Return this module at ``revision`` without touching its content.

        Dataclass modules use ``dataclasses.replace``; the two hand-written
        modules (wardrobe, canon) override this because they validate in
        ``__init__`` and ``replace`` does not apply to them.
        """
        return replace(self, revision=int(revision))  # type: ignore[return-value]

    def scope_of(self, field_name: str) -> Scope:
        try:
            return self.FIELD_SCOPES[field_name]
        except KeyError:
            raise ValidationError(
                f"unknown field {field_name!r} on module {self.MODULE}",
                module=self.MODULE,
                known=sorted(self.FIELD_SCOPES),
            ) from None

    def updated(self, updates: Mapping[str, Any]) -> _Module:
        """Return a new module instance with ``updates`` applied and revision + 1.

        Identity-scoped fields are refused here; callers must go through
        :meth:`CharacterCapsule.new_identity_version`, which is the one place
        that mints an identity version.
        """
        known = {f.name for f in fields(self)}  # type: ignore[arg-type]
        for key in updates:
            if key == "revision":
                raise ValidationError("revision is managed by the capsule, not by callers")
            if key not in known:
                raise ValidationError(
                    f"unknown field {key!r} on module {self.MODULE}", module=self.MODULE
                )
            if self.FIELD_SCOPES.get(key) is Scope.IDENTITY:
                raise ScopeViolation(
                    f"{self.MODULE}.{key} is IDENTITY-scoped; mint a new identity version instead",
                    module=self.MODULE,
                    field=key,
                )
        return replace(self, revision=self.revision + 1, **dict(updates))  # type: ignore[return-value]


@dataclass(frozen=True)
class FaceModule(_Module):
    """Face identity: the part of the capsule that must survive every scene."""

    MODULE: ClassVar[str] = "face"
    LABEL: ClassVar[str] = "Face identity"
    FIELD_SCOPES: ClassVar[dict[str, Scope]] = {
        "revision": Scope.REVISION,
        "eye_colour": Scope.IDENTITY,
        "eye_spacing_ratio": Scope.IDENTITY,
        "eye_size_ratio": Scope.IDENTITY,
        "jaw_width_ratio": Scope.IDENTITY,
        "nose_length_ratio": Scope.IDENTITY,
        "nose_width_ratio": Scope.IDENTITY,
        "mouth_width_ratio": Scope.IDENTITY,
        "brow_thickness_ratio": Scope.REVISION,
        "skin_colour": Scope.REVISION,
        "skin_texture": Scope.REVISION,
        "freckles": Scope.REVISION,
    }

    revision: int = 1
    eye_colour: str = "#3c5f8a"
    eye_spacing_ratio: float = 0.235
    eye_size_ratio: float = 0.095
    jaw_width_ratio: float = 0.62
    nose_length_ratio: float = 0.30
    nose_width_ratio: float = 0.16
    mouth_width_ratio: float = 0.33
    brow_thickness_ratio: float = 0.09
    skin_colour: str = "#e8b48f"
    skin_texture: str = "smooth"
    freckles: bool = False

    def __post_init__(self) -> None:
        _require(int(self.revision) >= 1, "face revision must be >= 1")
        _require_colour(self.eye_colour, field_name="face.eye_colour")
        _require_colour(self.skin_colour, field_name="face.skin_colour")
        _require_ratio(self.eye_spacing_ratio, field_name="face.eye_spacing_ratio")
        _require_ratio(self.eye_size_ratio, field_name="face.eye_size_ratio", low=0.02, high=0.3)
        _require_ratio(self.jaw_width_ratio, field_name="face.jaw_width_ratio", low=0.3, high=0.95)
        _require_ratio(
            self.nose_length_ratio, field_name="face.nose_length_ratio", low=0.1, high=0.5
        )
        _require_ratio(
            self.nose_width_ratio, field_name="face.nose_width_ratio", low=0.05, high=0.4
        )
        _require_ratio(
            self.mouth_width_ratio, field_name="face.mouth_width_ratio", low=0.1, high=0.6
        )
        _require_ratio(
            self.brow_thickness_ratio, field_name="face.brow_thickness_ratio", low=0.01, high=0.3
        )
        _require(
            self.skin_texture in {"smooth", "weathered", "scarred"},
            "face.skin_texture must be smooth|weathered|scarred",
        )


@dataclass(frozen=True)
class BodyModule(_Module):
    """Body identity: proportions and silhouette, versioned separately from the face."""

    MODULE: ClassVar[str] = "body"
    LABEL: ClassVar[str] = "Body identity"
    FIELD_SCOPES: ClassVar[dict[str, Scope]] = {
        "revision": Scope.REVISION,
        "height_cm": Scope.REVISION,
        "shoulder_width_ratio": Scope.IDENTITY,
        "hip_width_ratio": Scope.IDENTITY,
        "limb_thickness_ratio": Scope.IDENTITY,
        "posture": Scope.REVISION,
        "build": Scope.REVISION,
    }

    revision: int = 1
    height_cm: float = 172.0
    shoulder_width_ratio: float = 0.42
    hip_width_ratio: float = 0.36
    limb_thickness_ratio: float = 0.12
    posture: str = "upright"
    build: str = "average"

    def __post_init__(self) -> None:
        _require(120.0 <= float(self.height_cm) <= 230.0, "body.height_cm must be 120..230")
        _require_ratio(
            self.shoulder_width_ratio, field_name="body.shoulder_width_ratio", low=0.2, high=0.8
        )
        _require_ratio(self.hip_width_ratio, field_name="body.hip_width_ratio", low=0.2, high=0.8)
        _require_ratio(
            self.limb_thickness_ratio, field_name="body.limb_thickness_ratio", low=0.04, high=0.3
        )
        _require(
            self.posture in {"upright", "slouched", "military"},
            "body.posture must be upright|slouched|military",
        )
        _require(
            self.build in {"slim", "average", "athletic", "heavy"},
            "body.build must be slim|average|athletic|heavy",
        )


@dataclass(frozen=True)
class HairModule(_Module):
    """Hair: style/colour/length are revision-scoped (a haircut is a revision, not a reboot)."""

    MODULE: ClassVar[str] = "hair"
    LABEL: ClassVar[str] = "Hair"
    FIELD_SCOPES: ClassVar[dict[str, Scope]] = {
        "revision": Scope.REVISION,
        "style": Scope.REVISION,
        "colour": Scope.REVISION,
        "length": Scope.REVISION,
        "consistency_rules": Scope.REVISION,
    }

    revision: int = 1
    style: str = "short"
    colour: str = "#2b1d16"
    length: str = "short"
    consistency_rules: tuple[str, ...] = ("no_unplanned_cut_within_episode",)

    def __post_init__(self) -> None:
        _require(self.style in {"short", "long", "bun", "braid"}, "hair.style unsupported")
        _require_colour(self.colour, field_name="hair.colour")
        _require(self.length in {"short", "medium", "long"}, "hair.length unsupported")

    def payload(self) -> dict[str, Any]:
        data = asdict(self)  # type: ignore[call-overload]
        data["consistency_rules"] = list(self.consistency_rules)
        return data


@dataclass(frozen=True)
class VoiceModule(_Module):
    """Voice identity. Synthetic by default; a real voice needs a consent record."""

    MODULE: ClassVar[str] = "voice"
    LABEL: ClassVar[str] = "Voice identity"
    FIELD_SCOPES: ClassVar[dict[str, Scope]] = {
        "revision": Scope.REVISION,
        "f0_hz": Scope.IDENTITY,
        "timbre": Scope.IDENTITY,
        "accent": Scope.REVISION,
        "speaking_rate": Scope.REVISION,
        "approved_voice_refs": Scope.REVISION,
        "voice_origin": Scope.IDENTITY,
    }

    revision: int = 1
    f0_hz: float = 168.0
    timbre: str = "warm"
    accent: str = "neutral"
    speaking_rate: float = 1.0
    approved_voice_refs: tuple[str, ...] = ()
    voice_origin: str = "SYNTHETIC"

    def __post_init__(self) -> None:
        _require(70.0 <= float(self.f0_hz) <= 320.0, "voice.f0_hz must be 70..320")
        _require(self.voice_origin in {"SYNTHETIC", "CONSENTED_REAL"}, "voice.voice_origin invalid")
        _require(0.5 <= float(self.speaking_rate) <= 1.8, "voice.speaking_rate must be 0.5..1.8")

    def payload(self) -> dict[str, Any]:
        data = asdict(self)  # type: ignore[call-overload]
        data["approved_voice_refs"] = list(self.approved_voice_refs)
        return data


@dataclass(frozen=True)
class GaitProfile:
    """Measured-shape description of how the character moves (revision-scoped)."""

    cadence_steps_per_min: float = 108.0
    stride_ratio: float = 0.42
    arm_swing_ratio: float = 0.18
    hip_sway_ratio: float = 0.06
    head_bob_ratio: float = 0.02

    def __post_init__(self) -> None:
        _require(60.0 <= float(self.cadence_steps_per_min) <= 200.0, "gait.cadence out of range")
        for name in ("stride_ratio", "arm_swing_ratio", "hip_sway_ratio", "head_bob_ratio"):
            value = float(getattr(self, name))
            _require(0.0 <= value <= 1.0, f"gait.{name} must be 0..1", value=value)


@dataclass(frozen=True)
class MotionModule(_Module):
    """Motion: gait, gesture style and pose constraints, versioned on their own."""

    MODULE: ClassVar[str] = "motion"
    LABEL: ClassVar[str] = "Motion"
    FIELD_SCOPES: ClassVar[dict[str, Scope]] = {
        "revision": Scope.REVISION,
        "gait": Scope.REVISION,
        "gesture_style": Scope.REVISION,
        "pose_constraints": Scope.REVISION,
    }

    revision: int = 1
    gait: GaitProfile = field(default_factory=GaitProfile)
    gesture_style: str = "restrained"
    pose_constraints: tuple[str, ...] = ("no_extreme_twist",)

    def __post_init__(self) -> None:
        _require(isinstance(self.gait, GaitProfile), "motion.gait must be a GaitProfile")
        _require(len(self.pose_constraints) > 0, "motion.pose_constraints must not be empty")

    def payload(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "gait": asdict(self.gait),
            "gesture_style": self.gesture_style,
            "pose_constraints": list(self.pose_constraints),
        }


@dataclass(frozen=True)
class AssetRef:
    """A pointer to one immutable, content-addressed reference asset."""

    asset_id: str
    sha256: str
    size_bytes: int
    media_type: str
    role: str

    def __post_init__(self) -> None:
        _require(bool(self.asset_id), "asset_id must not be empty")
        if not self.sha256.startswith("sha256:") or len(self.sha256) != 71:
            raise ValidationError(
                "asset sha256 must be 'sha256:' + 64 hex chars", value=self.sha256
            )
        _require(int(self.size_bytes) > 0, "asset size_bytes must be positive")
        _require(
            self.role in {"face", "body", "hair", "wardrobe", "voice", "motion", "scene", "other"},
            f"unknown asset role {self.role!r}",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChangeIntent:
    """One explicit, attributable change to a capsule."""

    field_path: str  # "face.eye_colour" | "wardrobe" | "canon" ...
    value: Any
    note: str = ""


@dataclass(frozen=True)
class CharacterIdentity:
    """Immutable root: who the character is, and whether a human is behind it."""

    character_id: str
    display_name: str
    subject_class: SubjectClass
    work_title: str
    created_at: str
    policy_version: str = "character-identity/1"

    def __post_init__(self) -> None:
        validate_character_id(self.character_id)
        _require(bool(self.display_name.strip()), "display_name must not be empty")
        _require(bool(self.work_title.strip()), "work_title must not be empty")

    @property
    def digest(self) -> str:
        return scoped_digest(SCOPE_IDENTITY, self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "display_name": self.display_name,
            "subject_class": self.subject_class.value,
            "work_title": self.work_title,
            "created_at": self.created_at,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class IdentityVersion:
    """One sealed identity core. Bumping it is the only way face geometry changes."""

    character_id: str
    version: int
    face: FaceModule
    body: BodyModule
    voice: VoiceModule
    note: str = ""

    def __post_init__(self) -> None:
        validate_character_id(self.character_id)
        _require(int(self.version) >= 1, "identity version must be >= 1")
        if not isinstance(self.face, FaceModule):
            raise ValidationError("identity version requires a FaceModule")
        if not isinstance(self.body, BodyModule):
            raise ValidationError("identity version requires a BodyModule")

    @property
    def digest(self) -> str:
        return scoped_digest(
            SCOPE_IDENTITY,
            {
                "character_id": self.character_id,
                "version": self.version,
                "note": self.note,
                "face": self.face.payload(),
                "body": self.body.payload(),
                "voice": self.voice.payload() if isinstance(self.voice, VoiceModule) else None,
            },
        )


@dataclass(frozen=True)
class CharacterCapsule:
    """Identity version + revision number + independently versioned modules."""

    character_id: str
    identity_version: int
    revision_no: int
    modules: Mapping[str, _Module]
    asset_refs: tuple[AssetRef, ...] = ()
    digest: str = ""
    note: str = ""

    #: Modules every capsule must carry.
    REQUIRED_MODULES: ClassVar[tuple[str, ...]] = ("face", "body", "hair", "voice", "motion")

    def __post_init__(self) -> None:
        validate_character_id(self.character_id)
        _require(int(self.identity_version) >= 1, "identity_version must be >= 1")
        _require(int(self.revision_no) >= 1, "revision_no must be >= 1")
        missing = [name for name in self.REQUIRED_MODULES if name not in self.modules]
        if missing:
            raise ValidationError(f"capsule is missing required modules: {missing}")

    # -- construction -----------------------------------------------------
    @classmethod
    def create(
        cls,
        character_id: str,
        *,
        face: FaceModule | None = None,
        body: BodyModule | None = None,
        hair: HairModule | None = None,
        voice: VoiceModule | None = None,
        motion: MotionModule | None = None,
        note: str = "",
    ) -> CharacterCapsule:
        capsule = cls(
            character_id=character_id,
            identity_version=1,
            revision_no=1,
            modules={
                "face": face or FaceModule(),
                "body": body or BodyModule(),
                "hair": hair or HairModule(),
                "voice": voice or VoiceModule(),
                "motion": motion or MotionModule(),
            },
            note=note,
        )
        return capsule.seal()

    # -- sealing ----------------------------------------------------------
    def payload(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "identity_version": self.identity_version,
            "revision_no": self.revision_no,
            "note": self.note,
            "modules": {name: module.digest for name, module in sorted(self.modules.items())},
            "assets": [ref.to_dict() for ref in self.asset_refs],
        }

    def seal(self) -> CharacterCapsule:
        return replace(self, digest=scoped_digest(SCOPE_CAPSULE, self.payload()))

    def verify(self) -> None:
        """Raise :class:`IntegrityError`-family failure if the seal is stale."""
        from .errors import IntegrityError

        if not self.digest:
            raise IntegrityError("capsule is not sealed")
        expected = scoped_digest(SCOPE_CAPSULE, self.payload())
        if expected != self.digest:
            raise IntegrityError(
                "capsule digest does not match its content", expected=expected, actual=self.digest
            )

    # -- accessors --------------------------------------------------------
    def module(self, name: str) -> _Module:
        try:
            return self.modules[name]
        except KeyError:
            raise UnknownReference(
                f"capsule has no module {name!r}", capsule=self.character_id
            ) from None

    def module_digests(self) -> dict[str, str]:
        return {name: module.digest for name, module in sorted(self.modules.items())}

    def asset(self, asset_id: str) -> AssetRef:
        for ref in self.asset_refs:
            if ref.asset_id == asset_id:
                return ref
        raise UnknownReference(f"capsule has no asset {asset_id!r}", capsule=self.character_id)

    def with_assets(self, refs: Iterable[AssetRef]) -> CharacterCapsule:
        merged = {ref.asset_id: ref for ref in self.asset_refs}
        for ref in refs:
            merged[ref.asset_id] = ref
        return replace(self, asset_refs=tuple(merged[key] for key in sorted(merged))).seal()

    # -- revision ---------------------------------------------------------
    WARDROBE_MODULE: ClassVar[str] = "wardrobe"
    CANON_MODULE: ClassVar[str] = "canon"

    def with_module(self, module: _Module, *, note: str = "") -> CharacterCapsule:
        """Replace one module, bump the capsule revision, re-seal.

        This door is *not* a bypass: every field whose declared scope is
        ``IDENTITY`` is compared against the current module and a difference is a
        :class:`ScopeViolation`. Identity changes go through
        :meth:`new_identity_version`, which is the only code path that advances
        ``identity_version``.
        """
        name = getattr(module, "MODULE", "")
        if not name:
            raise ValidationError("with_module needs a capsule module")
        current = self.module(name)
        if type(module) is not type(current):
            raise ValidationError("module replacement must use the same module type", module=name)
        previous = current.payload()
        proposed = module.payload()
        smuggled = sorted(
            field_name
            for field_name in set(previous) | set(proposed)
            if previous.get(field_name) != proposed.get(field_name)
            and current.FIELD_SCOPES.get(field_name) is Scope.IDENTITY
        )
        if smuggled:
            raise ScopeViolation(
                f"{name}.{', '.join(smuggled)} is IDENTITY-scoped; mint a new identity version "
                "instead of replacing the module inside a revision",
                module=name,
                fields=smuggled,
                identity_version=self.identity_version,
            )
        if previous == proposed:
            raise ValidationError("module replacement carries no change", module=name)
        modules = dict(self.modules)
        modules[name] = module.with_revision(current.revision + 1)
        return replace(
            self,
            modules=modules,
            revision_no=self.revision_no + 1,
            note=note or self.note,
        ).seal()

    def apply_changes(
        self,
        changes: Iterable[ChangeIntent],
        *,
        expected_revision: int | None = None,
        note: str = "",
    ) -> CharacterCapsule:
        """Apply revision-scoped changes atomically (all or nothing)."""
        if expected_revision is not None and int(expected_revision) != self.revision_no:
            from .errors import RevisionConflict

            raise RevisionConflict(
                "capsule revision changed underneath this edit",
                expected=expected_revision,
                actual=self.revision_no,
            )
        pending: dict[str, dict[str, Any]] = {}
        applied: list[ChangeIntent] = []
        for intent in changes:
            module_name, _, field_name = intent.field_path.partition(".")
            if not field_name:
                raise ValidationError(
                    "change field_path must be '<module>.<field>'", field_path=intent.field_path
                )
            module = self.module(module_name)
            scope = module.scope_of(field_name)
            if scope is Scope.IDENTITY:
                raise ScopeViolation(
                    f"{intent.field_path} is IDENTITY-scoped; mint a new identity version instead",
                    field_path=intent.field_path,
                )
            pending.setdefault(module_name, {})[field_name] = intent.value
            applied.append(intent)
        if not applied:
            raise ValidationError("no changes supplied")
        modules = dict(self.modules)
        for module_name, updates in pending.items():
            modules[module_name] = self.module(module_name).updated(updates)
        return replace(
            self,
            modules=modules,
            revision_no=self.revision_no + 1,
            note=note or self.note,
        ).seal()

    #: Modules that carry IDENTITY-scoped fields. Every one of them is reachable
    #: from :meth:`new_identity_version`; leaving voice out was a defect, because
    #: `voice.f0_hz`/`timbre`/`voice_origin` are identity fields with no other door.
    IDENTITY_MODULES: ClassVar[tuple[str, ...]] = ("face", "body", "voice")

    def new_identity_version(
        self,
        *,
        face_changes: Mapping[str, Any] | None = None,
        body_changes: Mapping[str, Any] | None = None,
        voice_changes: Mapping[str, Any] | None = None,
        note: str = "",
    ) -> CharacterCapsule:
        """Mint a new identity version. This is the *only* door for IDENTITY fields."""
        requested = {
            "face": dict(face_changes or {}),
            "body": dict(body_changes or {}),
            "voice": dict(voice_changes or {}),
        }
        if not any(requested.values()):
            raise ValidationError("a new identity version needs at least one identity field change")
        modules = dict(self.modules)
        for name, changes in requested.items():
            if not changes:
                continue
            module = self.module(name)
            for field_name in changes:
                if field_name == "revision":
                    raise ValidationError("revision is managed by the capsule")
                if field_name not in module.FIELD_SCOPES:
                    raise ValidationError(
                        f"unknown field {field_name!r} on module {name}", module=name
                    )
                if module.FIELD_SCOPES[field_name] is not Scope.IDENTITY:
                    raise ValidationError(
                        f"{name}.{field_name} is not IDENTITY-scoped; use apply_changes",
                        module=name,
                        field=field_name,
                    )
            modules[name] = replace(
                module,
                revision=module.revision + 1,
                **{field_name: value for field_name, value in changes.items()},
            )
        return replace(
            self,
            modules=modules,
            identity_version=self.identity_version + 1,
            revision_no=self.revision_no + 1,
            note=note or f"identity version {self.identity_version + 1}",
        ).seal()

    # -- reporting --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "character-capsule/1",
            "character_id": self.character_id,
            "identity_version": self.identity_version,
            "revision_no": self.revision_no,
            "digest": self.digest,
            "note": self.note,
            "modules": {name: module.payload() for name, module in sorted(self.modules.items())},
            "module_digests": self.module_digests(),
            "assets": [ref.to_dict() for ref in self.asset_refs],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")


def next_character_id(existing: Iterable[str]) -> str:
    """Next free ``NAGAR-CHAR-######`` id. Never reuses a number, even after deletion."""
    highest = 0
    for value in existing:
        match = CHARACTER_ID_RE.match(value)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"NAGAR-CHAR-{highest + 1:06d}"
