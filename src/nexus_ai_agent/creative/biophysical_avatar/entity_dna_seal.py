"""Immutable identity vectors and cross-shot invariance checks for avatars.

The seal stores only compact numeric parameters.  It deliberately does not
store meshes, textures, audio, or per-shot poses, so a catalogue can recreate
an avatar deterministically without a bulky asset database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

__all__ = [
    "CRANIOFACIAL_DIM",
    "GARMENT_DIM",
    "LOCOMOTION_DIM",
    "MORPHOLOGY_DIM",
    "VOCAL_DIM",
    "TOTAL_DNA_DIM",
    "EntityDNASeal",
    "EntityInvariantSignature",
    "ParametricAvatarCatalog",
    "ShotState",
    "canonical_bone_lengths",
    "verify_entity_invariance",
]

CRANIOFACIAL_DIM = 64
MORPHOLOGY_DIM = 24
GARMENT_DIM = 32
VOCAL_DIM = 16
LOCOMOTION_DIM = 18
TOTAL_DNA_DIM = CRANIOFACIAL_DIM + MORPHOLOGY_DIM + GARMENT_DIM + VOCAL_DIM + LOCOMOTION_DIM

Vector: TypeAlias = tuple[float, ...]

# One length per non-root joint in the public 18-joint skeleton order.  Values
# are in metres and sum to a plausible adult stature along the torso and legs.
_BASE_BONE_LENGTHS: tuple[float, ...] = (
    0.24,
    0.28,
    0.24,
    0.22,
    0.31,
    0.26,
    0.22,
    0.31,
    0.26,
    0.11,
    0.43,
    0.42,
    0.16,
    0.11,
    0.43,
    0.42,
    0.16,
)


def _finite_vector(name: str, values: Sequence[float], length: int) -> Vector:
    """Return a finite, canonical float tuple of exactly ``length`` items."""
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a numeric sequence")
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must contain finite numbers") from exc
    if len(result) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} values must be finite")
    return tuple(0.0 if value == 0.0 else value for value in result)


def _bounded(value: float) -> float:
    """Keep arbitrary finite DNA values in physically meaningful ranges."""
    return math.tanh(value)


def canonical_bone_lengths(morphology: Sequence[float]) -> Vector:
    """Derive the 17 immutable skeletal segment lengths from a 24-vector.

    Pose, camera, and motion parameters are never inputs to this function.
    ``tanh`` gives unbounded finite DNA coefficients a smooth, strictly
    positive physical interpretation.
    """
    values = _finite_vector("morphology", morphology, MORPHOLOGY_DIM)
    height = 1.0 + 0.10 * _bounded(values[0])
    shoulders = 1.0 + 0.12 * _bounded(values[1])
    torso = 1.0 + 0.14 * _bounded(values[2])
    legs = 1.0 + 0.14 * _bounded(values[3])
    arms = 1.0 + 0.14 * _bounded(values[4])
    head = 1.0 + 0.10 * _bounded(values[5])
    pelvis = 1.0 + 0.10 * _bounded(values[6])
    feet = 1.0 + 0.10 * _bounded(values[7])
    neck = 1.0 + 0.12 * _bounded(values[10])
    upper_arm = 1.0 + 0.12 * _bounded(values[11])
    forearm = 1.0 + 0.12 * _bounded(values[12])
    thigh = 1.0 + 0.12 * _bounded(values[13])
    shin = 1.0 + 0.12 * _bounded(values[14])
    foot_length = 1.0 + 0.12 * _bounded(values[15])
    chest = 1.0 + 0.08 * _bounded(values[16])
    waist = 1.0 + 0.08 * _bounded(values[17])
    asymmetry = 1.0 + 0.025 * _bounded(values[19])
    hip_width = 1.0 + 0.08 * _bounded(values[21])
    factors = (
        height * torso,
        height * torso * chest,
        height * head * neck,
        height * shoulders * chest,
        height * arms * upper_arm * asymmetry,
        height * arms * forearm * asymmetry,
        height * shoulders * chest,
        height * arms * upper_arm / asymmetry,
        height * arms * forearm / asymmetry,
        height * pelvis * waist * hip_width,
        height * legs * thigh * asymmetry,
        height * legs * shin * asymmetry,
        height * feet * foot_length,
        height * pelvis * waist * hip_width,
        height * legs * thigh / asymmetry,
        height * legs * shin / asymmetry,
        height * feet * foot_length,
    )
    return tuple(base * factor for base, factor in zip(_BASE_BONE_LENGTHS, factors, strict=True))


@dataclass(frozen=True, slots=True)
class EntityInvariantSignature:
    """Per-entity measurements which must not drift between camera shots."""

    bone_lengths: Vector
    skull_distances: Vector
    garment_palette: tuple[str, ...]
    garment_cut: Vector
    vocal_tract_length: float


@dataclass(frozen=True, slots=True)
class EntityDNASeal:
    """Canonical immutable 154-dimensional identity for one avatar."""

    craniofacial: Vector
    morphology: Vector
    garment: Vector
    vocal: Vector
    locomotion: Vector
    seed: str = "0"
    description: str = ""

    def __post_init__(self) -> None:
        for name, length in (
            ("craniofacial", CRANIOFACIAL_DIM),
            ("morphology", MORPHOLOGY_DIM),
            ("garment", GARMENT_DIM),
            ("vocal", VOCAL_DIM),
            ("locomotion", LOCOMOTION_DIM),
        ):
            object.__setattr__(self, name, _finite_vector(name, getattr(self, name), length))
        if not isinstance(self.description, str):
            raise ValueError("description must be a string")

    @property
    def dimension(self) -> int:
        """Number of scalar coefficients in the identity vector."""
        return TOTAL_DNA_DIM

    @property
    def vector(self) -> Vector:
        """Return the canonical concatenation ``alpha | beta | gamma | nu | mu``."""
        return self.craniofacial + self.morphology + self.garment + self.vocal + self.locomotion

    def canonical_payload(self) -> dict[str, object]:
        """Return the JSON-compatible payload covered by the identity hash."""
        return {
            "schema": "nexus.entity-dna.v1",
            "seed": self.seed,
            "description": self.description,
            "craniofacial": list(self.craniofacial),
            "morphology": list(self.morphology),
            "garment": list(self.garment),
            "vocal": list(self.vocal),
            "locomotion": list(self.locomotion),
        }

    @property
    def canonical_json(self) -> str:
        """Stable UTF-8-compatible JSON (sorted keys, compact separators)."""
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @property
    def dna_sha256(self) -> str:
        """SHA-256 of the canonical identity payload, excluding the digest."""
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        """Serialize the payload together with its independently verifiable hash."""
        payload = self.canonical_payload()
        payload["dna_sha256"] = self.dna_sha256
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, encoded: str) -> EntityDNASeal:
        """Decode a seal and reject malformed or tampered serialized data."""
        try:
            raw = json.loads(encoded)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("invalid EntityDNASeal JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("EntityDNASeal JSON must be an object")
        expected_keys = {
            "schema",
            "seed",
            "description",
            "craniofacial",
            "morphology",
            "garment",
            "vocal",
            "locomotion",
            "dna_sha256",
        }
        if set(raw) != expected_keys or raw.get("schema") != "nexus.entity-dna.v1":
            raise ValueError("unsupported or incomplete EntityDNASeal payload")
        seal = cls(
            craniofacial=raw["craniofacial"],
            morphology=raw["morphology"],
            garment=raw["garment"],
            vocal=raw["vocal"],
            locomotion=raw["locomotion"],
            seed=raw["seed"],
            description=raw["description"],
        )
        digest = raw.get("dna_sha256")
        if not isinstance(digest, str) or not hmac.compare_digest(digest, seal.dna_sha256):
            raise ValueError("EntityDNASeal SHA-256 verification failed")
        return seal

    def invariant_signature(self) -> EntityInvariantSignature:
        """Derive stable geometric, material, and vocal measurements from DNA."""
        face = self.craniofacial
        skull_bases = (0.145, 0.118, 0.095, 0.071, 0.046, 0.063, 0.112, 0.088)
        skull_indices = (0, 1, 3, 8, 13, 20, 27, 36)
        skull_distances = tuple(
            base * (1.0 + 0.12 * _bounded(face[index]))
            for base, index in zip(skull_bases, skull_indices, strict=True)
        )
        colors: list[str] = []
        for offset in (0, 3, 6, 9, 12):
            channels = tuple(
                max(
                    0,
                    min(255, int(round((0.5 + 0.5 * _bounded(self.garment[offset + axis])) * 255))),
                )
                for axis in range(3)
            )
            colors.append("#{:02X}{:02X}{:02X}".format(*channels))
        garment_cut = tuple(0.5 + 0.25 * _bounded(value) for value in self.garment[15:23])
        vtl_scale = 0.75 + 0.60 * (0.5 + 0.5 * _bounded(self.vocal[0]))
        vocal_length = 0.17 * vtl_scale
        return EntityInvariantSignature(
            bone_lengths=canonical_bone_lengths(self.morphology),
            skull_distances=skull_distances,
            garment_palette=tuple(colors),
            garment_cut=garment_cut,
            vocal_tract_length=vocal_length,
        )


@dataclass(frozen=True, slots=True)
class ShotState:
    """A shot's identity measurements plus freely varying presentation state.

    Use :meth:`for_entity` to create a shot that carries the seal's exact
    identity measurements while varying camera, motion, or lighting values.
    """

    bone_lengths: Vector
    skull_distances: Vector
    garment_palette: tuple[str, ...]
    garment_cut: Vector
    vocal_tract_length: float
    camera_yaw: float = 0.0
    camera_pitch: float = 0.0
    linear_speed: float = 0.0
    light_intensity: float = 1.0

    def __post_init__(self) -> None:
        bones = tuple(float(value) for value in self.bone_lengths)
        skull = tuple(float(value) for value in self.skull_distances)
        cut = tuple(float(value) for value in self.garment_cut)
        if len(bones) != 17 or len(skull) != 8 or len(cut) != 8:
            raise ValueError("shot identity measurements have invalid dimensions")
        if not all(math.isfinite(value) and value > 0.0 for value in (*bones, *skull)):
            raise ValueError("shot bone lengths and skull distances must be finite and positive")
        if not all(math.isfinite(value) for value in cut):
            raise ValueError("shot garment cut values must be finite")
        if len(self.garment_palette) != 5 or not all(
            isinstance(color, str) for color in self.garment_palette
        ):
            raise ValueError("shot garment palette must contain five color strings")
        if not math.isfinite(self.vocal_tract_length) or self.vocal_tract_length <= 0.0:
            raise ValueError("vocal tract length must be finite and positive")
        for name in ("camera_yaw", "camera_pitch", "linear_speed", "light_intensity"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        object.__setattr__(self, "bone_lengths", bones)
        object.__setattr__(self, "skull_distances", skull)
        object.__setattr__(self, "garment_cut", cut)
        object.__setattr__(self, "garment_palette", tuple(self.garment_palette))

    @classmethod
    def for_entity(
        cls,
        seal: EntityDNASeal,
        *,
        camera_yaw: float = 0.0,
        camera_pitch: float = 0.0,
        linear_speed: float = 0.0,
        light_intensity: float = 1.0,
    ) -> ShotState:
        """Build a shot whose identity values are taken from ``seal``."""
        identity = seal.invariant_signature()
        return cls(
            bone_lengths=identity.bone_lengths,
            skull_distances=identity.skull_distances,
            garment_palette=identity.garment_palette,
            garment_cut=identity.garment_cut,
            vocal_tract_length=identity.vocal_tract_length,
            camera_yaw=camera_yaw,
            camera_pitch=camera_pitch,
            linear_speed=linear_speed,
            light_intensity=light_intensity,
        )


def _as_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise ValueError(f"{name} must be numeric")


def _shot_from_mapping(values: Mapping[str, object]) -> ShotState:
    required = (
        "bone_lengths",
        "skull_distances",
        "garment_palette",
        "garment_cut",
        "vocal_tract_length",
    )
    if any(key not in values for key in required):
        raise ValueError("shot_state is missing an identity measurement")
    try:
        return ShotState(
            bone_lengths=values["bone_lengths"],  # type: ignore[arg-type]
            skull_distances=values["skull_distances"],  # type: ignore[arg-type]
            garment_palette=tuple(values["garment_palette"]),  # type: ignore[arg-type]
            garment_cut=values["garment_cut"],  # type: ignore[arg-type]
            vocal_tract_length=_as_float(values["vocal_tract_length"], "vocal_tract_length"),
            camera_yaw=_as_float(values.get("camera_yaw", 0.0), "camera_yaw"),
            camera_pitch=_as_float(values.get("camera_pitch", 0.0), "camera_pitch"),
            linear_speed=_as_float(values.get("linear_speed", 0.0), "linear_speed"),
            light_intensity=_as_float(values.get("light_intensity", 1.0), "light_intensity"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("shot_state contains invalid values") from exc


def verify_entity_invariance(
    dna_seal: EntityDNASeal,
    shot_state: ShotState | Mapping[str, object],
    *,
    tolerance: float = 1e-9,
) -> bool:
    """Verify that one shot preserves the seal's identity measurements.

    Camera angles, velocity, and illumination are intentionally ignored; bone
    lengths, skull distances, garment palette/cut, and vocal-tract length are
    compared against the canonical DNA-derived signature.  Invalid input
    fails closed rather than raising.
    """
    if not isinstance(dna_seal, EntityDNASeal) or not math.isfinite(tolerance) or tolerance < 0.0:
        return False
    try:
        shot = shot_state if isinstance(shot_state, ShotState) else _shot_from_mapping(shot_state)
    except (TypeError, ValueError, OverflowError):
        return False
    expected = dna_seal.invariant_signature()

    def close(left: Sequence[float], right: Sequence[float]) -> bool:
        return len(left) == len(right) and all(
            abs(actual - canonical) <= tolerance
            for actual, canonical in zip(left, right, strict=True)
        )

    return (
        close(shot.bone_lengths, expected.bone_lengths)
        and close(shot.skull_distances, expected.skull_distances)
        and shot.garment_palette == expected.garment_palette
        and close(shot.garment_cut, expected.garment_cut)
        and abs(shot.vocal_tract_length - expected.vocal_tract_length) <= tolerance
    )


class ParametricAvatarCatalog:
    """Stateless deterministic generator for arbitrarily many compact seals."""

    def __init__(self, namespace: str = "nexus-biophysical-avatar-v1") -> None:
        if not namespace:
            raise ValueError("namespace must not be empty")
        self._namespace = namespace

    @staticmethod
    def seed_from_description(description: str) -> str:
        """Turn a director's text brief into a stable, non-secret seed token."""
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must not be empty")
        digest = hashlib.sha256(description.strip().encode("utf-8")).hexdigest()
        return f"description:{digest}"

    def profile(
        self,
        seed: int | str | None = 0,
        *,
        description: str = "",
    ) -> EntityDNASeal:
        """Generate one identity with no file or global RNG state.

        A text-only call may use ``seed=None``; its description is then its
        deterministic seed.  A supplied seed and description both remain part
        of the canonical seal.
        """
        if not isinstance(description, str):
            raise ValueError("description must be a string")
        description = description.strip()
        if seed is None:
            seed_token = self.seed_from_description(description)
        elif isinstance(seed, (int, str)) and not isinstance(seed, bool):
            seed_token = str(seed)
        else:
            raise ValueError("seed must be an integer, string, or None")

        def make_vector(label: str, length: int) -> Vector:
            values: list[float] = []
            for index in range(length):
                material = f"{self._namespace}\0{seed_token}\0{label}\0{index}".encode()
                word = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
                unit = (word + 0.5) / (2**64)
                values.append(2.0 * unit - 1.0)
            return tuple(values)

        return EntityDNASeal(
            craniofacial=make_vector("craniofacial", CRANIOFACIAL_DIM),
            morphology=make_vector("morphology", MORPHOLOGY_DIM),
            garment=make_vector("garment", GARMENT_DIM),
            vocal=make_vector("vocal", VOCAL_DIM),
            locomotion=make_vector("locomotion", LOCOMOTION_DIM),
            seed=seed_token,
            description=description,
        )

    def from_description(self, description: str) -> EntityDNASeal:
        """Generate a profile directly from a director's descriptive brief."""
        return self.profile(None, description=description)

    def profiles(
        self,
        count: int,
        *,
        starting_seed: int = 0,
        description_prefix: str = "",
    ) -> Iterator[EntityDNASeal]:
        """Lazily generate consecutive profiles without retaining a catalogue."""
        if count < 0:
            raise ValueError("count must be non-negative")
        for offset in range(count):
            seed = starting_seed + offset
            yield self.profile(seed, description=f"{description_prefix}{seed}")
