"""Reproducible synthetic corpus: characters, references, shots and drift variants.

Why synthetic, and what it buys
-------------------------------
No licensed face model, no GPU and — more importantly — no consent to use a real
person's likeness exist in this environment. So the corpus is generated: three
fully synthetic characters (one deliberately *look-alike* to the first, one
clearly distinct), each rendered across views, lighting, expressions, outfits and
environments, plus deliberate drift variants (eye spacing, jaw width, skin tone,
hair colour) and canonical story events.

That choice is not a shortcut around measurement: it makes measurement possible
with **ground truth**. Every pixel has a known generating parameter, so a pair can
be *labelled* genuine / impostor / drift, which is exactly what a calibration
artifact needs. The transfer limit is stated everywhere it matters: thresholds
calibrated here do not transfer to photographic faces.

Everything is deterministic: a corpus digest over the manifest makes the benchmark
reproducible bit-for-bit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .audio_motion import VoiceSignature, synthesize_gait, synthesize_voice
from .canon import CanonFact, CanonModule, FactKind, StoryEvent
from .canonical import digest_of, sha256_hex
from .media import encode_wav, save_rgb
from .model import (
    BodyModule,
    CharacterCapsule,
    FaceModule,
    GaitProfile,
    HairModule,
    MotionModule,
    SubjectClass,
    VoiceModule,
)
from .scene import Environment, OverrideDimension, Scene, SceneOverride, Timeline
from .vision import Lighting, RenderSpec, render_full_body, render_portrait
from .wardrobe import Outfit, WardrobeChangeEvent, WardrobeModule

CORPUS_SEED = "nagar-character-capsule-corpus-v1"

OUT_PRIMARY = Outfit(
    outfit_id="OUT-0001",
    version=1,
    label="Field coat",
    top_colour="#3d5a73",
    bottom_colour="#2f3a46",
    accent_colour="#c9a227",
    material="wool",
    accessories=("satchel",),
    era_from_scene=1,
)
OUT_SECONDARY = Outfit(
    outfit_id="OUT-0002",
    version=1,
    label="Evening jacket",
    top_colour="#7a2f3a",
    bottom_colour="#20242b",
    accent_colour="#d8d3c8",
    material="silk",
    accessories=("pendant",),
    era_from_scene=3,
)


@dataclass(frozen=True)
class CharacterSpec:
    """One synthetic character: capsule modules plus the files it generates."""

    character_id: str
    display_name: str
    face: FaceModule
    body: BodyModule
    hair: HairModule
    voice: VoiceModule
    motion: MotionModule
    role: str = "protagonist"
    lookalike_of: str | None = None


CHARACTERS: tuple[CharacterSpec, ...] = (
    CharacterSpec(
        character_id="NAGAR-CHAR-000001",
        display_name="Mahtab Ravi",
        face=FaceModule(
            eye_colour="#3c5f8a",
            eye_spacing_ratio=0.235,
            eye_size_ratio=0.095,
            jaw_width_ratio=0.62,
            nose_length_ratio=0.30,
            nose_width_ratio=0.16,
            mouth_width_ratio=0.33,
            brow_thickness_ratio=0.09,
            skin_colour="#e8b48f",
        ),
        body=BodyModule(height_cm=172.0, shoulder_width_ratio=0.42, hip_width_ratio=0.36),
        hair=HairModule(style="long", colour="#2b1d16", length="long"),
        voice=VoiceModule(f0_hz=186.0, timbre="warm", accent="neutral", speaking_rate=1.0),
        motion=MotionModule(gait=GaitProfile(cadence_steps_per_min=110.0, stride_ratio=0.44)),
    ),
    CharacterSpec(
        character_id="NAGAR-CHAR-000002",
        display_name="Sahar Ravi",
        face=FaceModule(
            eye_colour="#3c5f8a",
            eye_spacing_ratio=0.283,
            eye_size_ratio=0.086,
            jaw_width_ratio=0.525,
            nose_length_ratio=0.335,
            nose_width_ratio=0.192,
            mouth_width_ratio=0.295,
            brow_thickness_ratio=0.09,
            skin_colour="#d49a6d",
        ),
        body=BodyModule(height_cm=168.0, shoulder_width_ratio=0.395, hip_width_ratio=0.375),
        hair=HairModule(style="long", colour="#2b1d16", length="long"),
        voice=VoiceModule(f0_hz=189.0, timbre="warm", accent="neutral", speaking_rate=1.02),
        motion=MotionModule(gait=GaitProfile(cadence_steps_per_min=112.0, stride_ratio=0.445)),
        role="lookalike",
        lookalike_of="NAGAR-CHAR-000001",
    ),
    CharacterSpec(
        character_id="NAGAR-CHAR-000003",
        display_name="Baran Dehqan",
        face=FaceModule(
            eye_colour="#4a3b2a",
            eye_spacing_ratio=0.285,
            eye_size_ratio=0.078,
            jaw_width_ratio=0.78,
            nose_length_ratio=0.36,
            nose_width_ratio=0.22,
            mouth_width_ratio=0.40,
            brow_thickness_ratio=0.14,
            skin_colour="#b07b52",
        ),
        body=BodyModule(height_cm=184.0, shoulder_width_ratio=0.47, hip_width_ratio=0.33),
        hair=HairModule(style="short", colour="#161311", length="short"),
        voice=VoiceModule(f0_hz=112.0, timbre="deep", accent="regional", speaking_rate=0.92),
        motion=MotionModule(gait=GaitProfile(cadence_steps_per_min=96.0, stride_ratio=0.38)),
        role="antagonist",
    ),
)


@dataclass(frozen=True)
class FrameRecord:
    """One generated frame and the metadata a benchmark needs."""

    key: str
    path: str
    character_id: str
    purpose: str  # reference | calibration | eval | drift | impostor
    condition: str
    view: str
    expression: str
    outfit_id: str
    background: str
    key_kelvin: int
    kind: str  # portrait | full_body
    labels: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "path": self.path,
            "character_id": self.character_id,
            "purpose": self.purpose,
            "condition": self.condition,
            "view": self.view,
            "expression": self.expression,
            "outfit_id": self.outfit_id,
            "background": self.background,
            "key_kelvin": self.key_kelvin,
            "kind": self.kind,
            "labels": dict(sorted(self.labels.items())),
        }


@dataclass(frozen=True)
class Corpus:
    """Generated corpus on disk plus its manifest digest."""

    root: Path
    frames: tuple[FrameRecord, ...]
    digest: str
    specs: tuple[CharacterSpec, ...] = CHARACTERS

    def frame(self, key: str) -> FrameRecord:
        for record in self.frames:
            if record.key == key:
                return record
        raise KeyError(key)

    def frames_where(self, **criteria: str) -> tuple[FrameRecord, ...]:
        out = []
        for record in self.frames:
            payload = record.to_dict()
            if all(payload.get(name) == value for name, value in criteria.items()):
                out.append(record)
        return tuple(sorted(out, key=lambda record: record.key))

    def references(self, character_id: str, *, kind: str | None = None) -> tuple[FrameRecord, ...]:
        """Canonical reference frames for a character (``reference``/``reference_alt``).

        ``kind`` filters by framing (``portrait`` / ``full_body``); ``None`` keeps
        both, so a body comparison has a full-body gallery to compare against.
        """
        return tuple(
            record
            for record in self.frames
            if record.character_id == character_id
            and record.purpose in {"reference", "reference_alt"}
            and (kind is None or record.kind == kind)
        )

    def load(self, record: FrameRecord) -> np.ndarray:
        from .media import load_rgb

        return load_rgb(self.root / record.path)

    def load_key(self, key: str) -> np.ndarray:
        return self.load(self.frame(key))

    def spec(self, character_id: str) -> CharacterSpec:
        for spec in self.specs:
            if spec.character_id == character_id:
                return spec
        raise KeyError(character_id)


def build_capsule(spec: CharacterSpec, *, with_canon: bool = True) -> CharacterCapsule:
    """A complete capsule for one synthetic character (wardrobe + canon included)."""
    wardrobe = WardrobeModule(
        revision=1,
        outfits={OUT_PRIMARY.outfit_id: OUT_PRIMARY, OUT_SECONDARY.outfit_id: OUT_SECONDARY},
        sequence_baselines={
            "SEQ-EP1-A": OUT_PRIMARY.outfit_id,
            "SEQ-EP2-A": OUT_SECONDARY.outfit_id,
        },
        change_events=(
            WardrobeChangeEvent(
                event_id="WCE-0001",
                sequence_id="SEQ-EP1-A",
                from_shot_index=3,
                outfit_id=OUT_SECONDARY.outfit_id,
                reason="story beat: the character changes for the evening scene",
            ),
        ),
        scene_rules={"SC-003": OUT_SECONDARY.outfit_id},
    )
    modules: dict[str, Any] = {
        "face": spec.face,
        "body": spec.body,
        "hair": spec.hair,
        "voice": spec.voice,
        "motion": spec.motion,
        "wardrobe": wardrobe,
    }
    if with_canon:
        modules["canon"] = CanonModule(
            revision=1,
            facts={
                "FACT-001": CanonFact(
                    fact_id="FACT-001",
                    predicate="eye_colour",
                    value=spec.face.eye_colour,
                    kind=FactKind.IMMUTABLE,
                ),
                "FACT-002": CanonFact(
                    fact_id="FACT-002",
                    predicate="origin",
                    value="coastal-province",
                    kind=FactKind.IMMUTABLE,
                ),
                "FACT-003": CanonFact(
                    fact_id="FACT-003",
                    predicate="title",
                    value="field researcher",
                    kind=FactKind.MUTABLE,
                ),
                "FACT-004": CanonFact(
                    fact_id="FACT-004",
                    predicate="scar",
                    value="left_cheek",
                    kind=FactKind.MUTABLE,
                    added_at_scene=3,
                ),
            },
            story_events=(
                StoryEvent(
                    event_id="EVT-001",
                    kind="injury",
                    scene_order=3,
                    description="cliff-road fall leaves a scar on the left cheek",
                    effects={"scar": "left_cheek"},
                ),
                StoryEvent(
                    event_id="EVT-002",
                    kind="death",
                    scene_order=9,
                    description="scripted death in the season finale (scene 9)",
                    effects={"alive": "false"},
                ),
            ),
            biography=f"{spec.display_name} works the coastal province and distrusts maps.",
            personality=("watchful", "dry humour"),
            relationships={"NAGAR-CHAR-000003": "estranged colleague"},
        )
    capsule = CharacterCapsule(
        character_id=spec.character_id,
        identity_version=1,
        revision_no=1,
        modules=modules,
    ).seal()
    return capsule


def build_timeline() -> Timeline:
    """Three scenes across two sequences, with a declared wardrobe change."""
    return Timeline(
        [
            Scene(
                scene_id="SC-001",
                episode=1,
                sequence_id="SEQ-EP1-A",
                order=1,
                title="Harbour morning",
                environment=Environment(background_colour="#3a3f4b", key_light_kelvin=5600),
            ),
            Scene(
                scene_id="SC-002",
                episode=1,
                sequence_id="SEQ-EP1-A",
                order=2,
                title="Harbour evening",
                environment=Environment(background_colour="#2f3640", key_light_kelvin=4200),
            ),
            Scene(
                scene_id="SC-003",
                episode=2,
                sequence_id="SEQ-EP2-A",
                order=3,
                title="Cliff road",
                environment=Environment(background_colour="#6b7280", key_light_kelvin=6200),
            ),
        ]
    )


DRIFT_VARIANTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("eye_spacing", {"eye_spacing_ratio": 0.285}),
    ("jaw_width", {"jaw_width_ratio": 0.78}),
    ("skin_tone", {"skin_colour": "#b07b52"}),
    ("eye_colour", {"eye_colour": "#4a3b2a"}),
    ("nose", {"nose_length_ratio": 0.38, "nose_width_ratio": 0.22}),
    ("mouth", {"mouth_width_ratio": 0.42}),
)


def _spec_key(spec: CharacterSpec) -> str:
    return f"{spec.character_id}|{spec.face.digest}|{spec.body.digest}|{spec.hair.digest}"


def _render(
    spec: CharacterSpec,
    *,
    outfit: Outfit,
    view: str = "front",
    expression: str = "neutral",
    lighting: Lighting | None = None,
    environment: Environment | None = None,
    kind: str = "portrait",
    face: FaceModule | None = None,
    scar: bool = False,
) -> np.ndarray:
    render_spec = RenderSpec(
        face=face or spec.face,
        body=spec.body,
        hair=spec.hair,
        outfit=outfit,
        expression=expression,
        view=view,
        lighting=lighting or Lighting(),
        environment=environment or Environment(),
        scar=scar,
    )
    return render_portrait(render_spec) if kind == "portrait" else render_full_body(render_spec)


def build_corpus(root: str | Path, *, write: bool = True) -> Corpus:
    """Generate the whole corpus (idempotent; overwrites identical bytes)."""
    base = Path(root)
    frames_dir = base / "frames"
    audio_dir = base / "audio"
    if write:
        frames_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)
    records: list[FrameRecord] = []

    def emit(
        key: str,
        spec: CharacterSpec,
        image: np.ndarray,
        *,
        purpose: str,
        condition: str,
        view: str,
        expression: str,
        outfit_id: str,
        background: str,
        key_kelvin: int,
        kind: str,
        labels: Mapping[str, str] | None = None,
        character_id: str | None = None,
    ) -> None:
        relative = f"frames/{key}.png"
        if write:
            save_rgb(base / relative, image)
        records.append(
            FrameRecord(
                key=key,
                path=relative,
                character_id=character_id or spec.character_id,
                purpose=purpose,
                condition=condition,
                view=view,
                expression=expression,
                outfit_id=outfit_id,
                background=background,
                key_kelvin=key_kelvin,
                kind=kind,
                labels=dict(labels or {}),
            )
        )

    for spec in CHARACTERS:
        short = spec.character_id.split("-")[-1]
        # references (the gallery the verifier compares against)
        emit(
            f"{short}-ref-front",
            spec,
            _render(spec, outfit=OUT_PRIMARY, view="front"),
            purpose="reference",
            condition="same_camera",
            view="front",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=5600,
            kind="portrait",
        )
        emit(
            f"{short}-ref-three-quarter",
            spec,
            _render(spec, outfit=OUT_PRIMARY, view="three_quarter"),
            purpose="reference_alt",
            condition="different_angle",
            view="three_quarter",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=5600,
            kind="portrait",
            labels={"angle": "32deg"},
        )
        emit(
            f"{short}-ref-profile",
            spec,
            _render(spec, outfit=OUT_PRIMARY, view="profile"),
            purpose="reference_alt",
            condition="different_angle",
            view="profile",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=5600,
            kind="portrait",
            labels={"angle": "72deg", "hard_case": "profile"},
        )
        emit(
            f"{short}-ref-warm",
            spec,
            _render(spec, outfit=OUT_PRIMARY, lighting=Lighting(key_kelvin=3200, intensity=1.15)),
            purpose="reference_alt",
            condition="different_lighting",
            view="front",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=3200,
            kind="portrait",
        )
        emit(
            f"{short}-ref-cool",
            spec,
            _render(spec, outfit=OUT_PRIMARY, lighting=Lighting(key_kelvin=7800, intensity=0.85)),
            purpose="reference_alt",
            condition="different_lighting",
            view="front",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=7800,
            kind="portrait",
        )
        emit(
            f"{short}-ref-smile",
            spec,
            _render(spec, outfit=OUT_PRIMARY, expression="smile"),
            purpose="reference_alt",
            condition="different_expression",
            view="front",
            expression="smile",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=5600,
            kind="portrait",
        )
        emit(
            f"{short}-ref-fullbody",
            spec,
            _render(
                spec,
                outfit=OUT_PRIMARY,
                kind="full_body",
                environment=Environment(background_colour="#8a93a0", key_light_kelvin=5600),
            ),
            purpose="reference_alt",
            condition="same_camera",
            view="front",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#8a93a0",
            key_kelvin=5600,
            kind="full_body",
        )
        emit(
            f"{short}-ref-fullbody-warm",
            spec,
            _render(
                spec,
                outfit=OUT_PRIMARY,
                kind="full_body",
                lighting=Lighting(key_kelvin=3200, intensity=1.12),
                environment=Environment(background_colour="#8a93a0", key_light_kelvin=5600),
            ),
            purpose="reference_alt",
            condition="different_lighting",
            view="front",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#8a93a0",
            key_kelvin=3200,
            kind="full_body",
        )
        emit(
            f"{short}-ref-outfit2",
            spec,
            _render(spec, outfit=OUT_SECONDARY),
            purpose="reference_alt",
            condition="different_outfit",
            view="front",
            expression="neutral",
            outfit_id=OUT_SECONDARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=5600,
            kind="portrait",
        )
        # calibration drift variants: same character, deliberately altered identity
        for variant_name, changes in DRIFT_VARIANTS:
            drifted_face = FaceModule(**{**spec.face.payload(), **changes, "revision": 2})
            emit(
                f"{short}-drift-{variant_name}",
                spec,
                _render(spec, outfit=OUT_PRIMARY, face=drifted_face, expression="neutral"),
                purpose="drift",
                condition="identity_drift",
                view="front",
                expression="neutral",
                outfit_id=OUT_PRIMARY.outfit_id,
                background="#3a3f4b",
                key_kelvin=5600,
                kind="portrait",
                labels={"drift": variant_name},
            )
        # held-out shots used by the benchmark scenarios
        emit(
            f"{short}-shot-angle",
            spec,
            _render(
                spec, outfit=OUT_PRIMARY, view="three_quarter", lighting=Lighting(intensity=1.06)
            ),
            purpose="eval",
            condition="different_angle",
            view="three_quarter",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=5600,
            kind="portrait",
        )
        emit(
            f"{short}-shot-light",
            spec,
            _render(spec, outfit=OUT_PRIMARY, lighting=Lighting(key_kelvin=3400, intensity=1.18)),
            purpose="eval",
            condition="different_lighting",
            view="front",
            expression="neutral",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=3400,
            kind="portrait",
        )
        emit(
            f"{short}-shot-expression",
            spec,
            _render(
                spec,
                outfit=OUT_PRIMARY,
                expression="smile",
                lighting=Lighting(intensity=1.06),
            ),
            purpose="eval",
            condition="different_expression",
            view="front",
            expression="smile",
            outfit_id=OUT_PRIMARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=5600,
            kind="portrait",
        )
        emit(
            f"{short}-shot-outfit2",
            spec,
            _render(spec, outfit=OUT_SECONDARY, lighting=Lighting(intensity=1.06)),
            purpose="eval",
            condition="different_outfit",
            view="front",
            expression="neutral",
            outfit_id=OUT_SECONDARY.outfit_id,
            background="#3a3f4b",
            key_kelvin=5600,
            kind="portrait",
        )
        emit(
            f"{short}-shot-environment",
            spec,
            _render(
                spec,
                outfit=OUT_SECONDARY,
                environment=Environment(background_colour="#6b7280", key_light_kelvin=6200),
                expression="sad",
                scar=True,
            ),
            purpose="eval",
            condition="different_environment",
            view="front",
            expression="sad",
            outfit_id=OUT_SECONDARY.outfit_id,
            background="#6b7280",
            key_kelvin=6200,
            kind="portrait",
            labels={"canon_marker": "scar"},
        )
        emit(
            f"{short}-shot-fullbody",
            spec,
            _render(
                spec,
                outfit=OUT_SECONDARY,
                kind="full_body",
                lighting=Lighting(intensity=1.06),
                environment=Environment(background_colour="#8a93a0", key_light_kelvin=5600),
            ),
            purpose="eval",
            condition="different_pose",
            view="front",
            expression="neutral",
            outfit_id=OUT_SECONDARY.outfit_id,
            background="#8a93a0",
            key_kelvin=5600,
            kind="full_body",
        )

    manifest = {
        "schema": "character-capsule-corpus/1",
        "seed": CORPUS_SEED,
        "frames": [record.to_dict() for record in sorted(records, key=lambda item: item.key)],
        "characters": [
            {
                "character_id": spec.character_id,
                "display_name": spec.display_name,
                "role": spec.role,
                "lookalike_of": spec.lookalike_of,
                "subject_class": SubjectClass.SYNTHETIC.value,
                "spec_key": _spec_key(spec),
                "capsule_digest": build_capsule(spec).digest,
            }
            for spec in CHARACTERS
        ],
        "audio": _write_audio(base, write=write),
        "motion": _motion_manifest(),
    }
    if write:
        import json

        (base / "corpus_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
    corpus_digest = digest_of(manifest)
    return Corpus(
        root=base, frames=tuple(sorted(records, key=lambda item: item.key)), digest=corpus_digest
    )


def _write_audio(base: Path, *, write: bool) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for spec in CHARACTERS:
        signature = VoiceSignature.from_module(spec.voice)
        samples = synthesize_voice(signature)
        data = encode_wav(samples, sample_rate=16000)
        relative = f"audio/{spec.character_id}-voice.wav"
        if write:
            (base / relative).write_bytes(data)
        entries.append(
            {
                "character_id": spec.character_id,
                "path": relative,
                "sha256": sha256_hex(data),
                "sample_rate": 16000,
                "seconds": round(samples.size / 16000.0, 4),
            }
        )
    return entries


def _motion_manifest() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for spec in CHARACTERS:
        sequence = synthesize_gait(spec.motion.gait, seed=spec.character_id)
        entries.append(
            {
                "character_id": spec.character_id,
                "frames": int(sequence.shape[0]),
                "fps": 24.0,
                "digest": digest_of(
                    {
                        "seq": [
                            [[round(float(v), 6) for v in joint] for joint in frame]
                            for frame in sequence
                        ]
                    }
                ),
            }
        )
    return entries


def motion_sequence(character_id: str) -> np.ndarray:
    for spec in CHARACTERS:
        if spec.character_id == character_id:
            return synthesize_gait(spec.motion.gait, seed=spec.character_id)
    raise KeyError(character_id)


def voice_samples(character_id: str) -> np.ndarray:
    for spec in CHARACTERS:
        if spec.character_id == character_id:
            return synthesize_voice(VoiceSignature.from_module(spec.voice))
    raise KeyError(character_id)


def character_split(corpus: Corpus) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split characters into a calibration set and a held-out character.

    * calibration: ``NAGAR-CHAR-000001`` (protagonist) and its look-alike
      ``NAGAR-CHAR-000002`` — the hardest genuine/impostor margin in the corpus,
      so thresholds are not fitted on an easy pair;
    * held out: ``NAGAR-CHAR-000003`` — never used to fit a threshold.
    """
    ids = sorted({record.character_id for record in corpus.frames})
    return tuple(ids[:2]), tuple(ids[2:])


def scene_overrides_for_demo() -> SceneOverride:
    """A legitimate scene-scoped override (expression only)."""
    return SceneOverride(
        scene_id="SC-002",
        choices={OverrideDimension.EXPRESSION: "sad"},
        reason="the character receives bad news in this scene",
    )
