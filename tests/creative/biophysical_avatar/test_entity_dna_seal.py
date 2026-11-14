from __future__ import annotations

import json

import pytest

from nexus_ai_agent.creative.biophysical_avatar import EntityDNASeal as PublicEntityDNASeal
from nexus_ai_agent.creative.biophysical_avatar.entity_dna_seal import (
    CRANIOFACIAL_DIM,
    GARMENT_DIM,
    LOCOMOTION_DIM,
    MORPHOLOGY_DIM,
    TOTAL_DNA_DIM,
    VOCAL_DIM,
    EntityDNASeal,
    ParametricAvatarCatalog,
    ShotState,
    canonical_bone_lengths,
    verify_entity_invariance,
)


def test_public_package_api_exports_the_seal_type() -> None:
    assert PublicEntityDNASeal is EntityDNASeal


def test_seal_has_canonical_dimensions_is_immutable_and_hashes_repeatably() -> None:
    catalog = ParametricAvatarCatalog()
    first = catalog.profile(17, description="red cloak")
    repeat = catalog.profile(17, description="red cloak")
    other = catalog.profile(18, description="red cloak")

    assert (CRANIOFACIAL_DIM, MORPHOLOGY_DIM, GARMENT_DIM, VOCAL_DIM, LOCOMOTION_DIM) == (
        64,
        24,
        32,
        16,
        18,
    )
    assert first.dimension == TOTAL_DNA_DIM == 154
    assert len(first.vector) == 154
    assert first.vector == (
        first.craniofacial + first.morphology + first.garment + first.vocal + first.locomotion
    )
    assert first.dna_sha256 == repeat.dna_sha256
    assert first.dna_sha256 != other.dna_sha256
    assert len(first.dna_sha256) == 64
    with pytest.raises((AttributeError, TypeError)):
        first.seed = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.craniofacial[0] = 0.0  # type: ignore[index]


def test_canonical_json_round_trip_and_tampering_detection() -> None:
    seal = ParametricAvatarCatalog().profile(9, description="night watch")
    decoded = EntityDNASeal.from_json(seal.to_json())

    assert decoded == seal
    assert decoded.canonical_json == seal.canonical_json
    assert decoded.dna_sha256 == seal.dna_sha256
    assert '"schema":"nexus.entity-dna.v1"' in seal.canonical_json
    tampered = json.loads(seal.to_json())
    tampered["garment"][0] = 0.99
    with pytest.raises(ValueError, match="SHA-256"):
        EntityDNASeal.from_json(json.dumps(tampered))
    with pytest.raises(ValueError, match="invalid"):
        EntityDNASeal.from_json("{")
    with pytest.raises(ValueError, match="object"):
        EntityDNASeal.from_json("[]")
    with pytest.raises(ValueError, match="unsupported"):
        EntityDNASeal.from_json(json.dumps({"schema": "other"}))
    missing_hash = json.loads(seal.to_json())
    del missing_hash["dna_sha256"]
    with pytest.raises(ValueError, match="unsupported"):
        EntityDNASeal.from_json(json.dumps(missing_hash))


def test_vector_shape_type_and_finite_validation() -> None:
    catalog = ParametricAvatarCatalog()
    seal = catalog.profile(0)
    with pytest.raises(ValueError, match="exactly 64"):
        EntityDNASeal((0.0,), seal.morphology, seal.garment, seal.vocal, seal.locomotion)
    malformed = list(seal.craniofacial)
    malformed[2] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        EntityDNASeal(malformed, seal.morphology, seal.garment, seal.vocal, seal.locomotion)
    with pytest.raises(ValueError, match="numeric sequence"):
        EntityDNASeal("not numbers", seal.morphology, seal.garment, seal.vocal, seal.locomotion)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite numbers"):
        EntityDNASeal(
            [object()] * CRANIOFACIAL_DIM,  # type: ignore[list-item]
            seal.morphology,
            seal.garment,
            seal.vocal,
            seal.locomotion,
        )
    with pytest.raises(ValueError, match="description"):
        EntityDNASeal(
            seal.craniofacial,
            seal.morphology,
            seal.garment,
            seal.vocal,
            seal.locomotion,
            description=object(),  # type: ignore[arg-type]
        )


def test_catalog_is_deterministic_for_descriptions_and_lazily_scales() -> None:
    catalog = ParametricAvatarCatalog("test-domain")
    by_description = catalog.from_description("  a silver astronaut  ")
    repeated = catalog.from_description("a silver astronaut")
    assert by_description.dna_sha256 == repeated.dna_sha256
    assert catalog.profile(None, description="a silver astronaut").dna_sha256 == repeated.dna_sha256
    assert catalog.seed_from_description("a silver astronaut") == repeated.seed
    assert len({profile.dna_sha256 for profile in catalog.profiles(1_100)}) == 1_100
    assert list(catalog.profiles(0)) == []
    assert len(tuple(catalog.profiles(3, starting_seed=4, description_prefix="unit-"))) == 3
    with pytest.raises(ValueError, match="namespace"):
        ParametricAvatarCatalog("")
    with pytest.raises(ValueError, match="description"):
        catalog.from_description("  ")
    with pytest.raises(ValueError, match="non-negative"):
        list(catalog.profiles(-1))
    with pytest.raises(ValueError, match="seed"):
        catalog.profile(True)
    with pytest.raises(ValueError, match="description"):
        catalog.profile(1, description=object())  # type: ignore[arg-type]


def test_bone_lengths_are_positive_and_match_morphology_signature() -> None:
    seal = ParametricAvatarCatalog().profile(4)
    lengths = canonical_bone_lengths(seal.morphology)
    signature = seal.invariant_signature()
    assert len(lengths) == 17
    assert all(length > 0.0 for length in lengths)
    assert lengths == signature.bone_lengths
    with pytest.raises(ValueError, match="exactly 24"):
        canonical_bone_lengths((0.0,))
    with pytest.raises(ValueError, match="finite"):
        canonical_bone_lengths((float("inf"),) + (0.0,) * 23)


def test_entity_invariance_holds_across_ten_camera_motion_and_light_states() -> None:
    seal = ParametricAvatarCatalog().profile(902, description="hero")
    for index in range(10):
        shot = ShotState.for_entity(
            seal,
            camera_yaw=index * 0.618,
            camera_pitch=-0.4 + index * 0.07,
            linear_speed=float(index * 4),
            light_intensity=0.1 + index * 0.23,
        )
        assert verify_entity_invariance(seal, shot)
        assert verify_entity_invariance(
            seal,
            {
                "bone_lengths": shot.bone_lengths,
                "skull_distances": shot.skull_distances,
                "garment_palette": shot.garment_palette,
                "garment_cut": shot.garment_cut,
                "vocal_tract_length": shot.vocal_tract_length,
                "camera_yaw": shot.camera_yaw,
                "linear_speed": shot.linear_speed,
            },
        )


def test_invariance_rejects_drift_and_bad_shot_payloads() -> None:
    seal = ParametricAvatarCatalog().profile(31)
    good = ShotState.for_entity(seal)
    changed_bones = list(good.bone_lengths)
    changed_bones[5] += 2e-9
    assert not verify_entity_invariance(
        seal,
        ShotState(
            tuple(changed_bones),
            good.skull_distances,
            good.garment_palette,
            good.garment_cut,
            good.vocal_tract_length,
        ),
    )
    assert not verify_entity_invariance(seal, {"bone_lengths": good.bone_lengths})
    assert not verify_entity_invariance(seal, 7)  # type: ignore[arg-type]
    assert not verify_entity_invariance(seal, good, tolerance=-1.0)
    assert not verify_entity_invariance(seal, good, tolerance=float("nan"))
    assert not verify_entity_invariance(seal, {"vocal_tract_length": True})
    changed_palette = list(good.garment_palette)
    changed_palette[0] = "#000000"
    assert not verify_entity_invariance(
        seal,
        ShotState(
            good.bone_lengths,
            good.skull_distances,
            tuple(changed_palette),
            good.garment_cut,
            good.vocal_tract_length,
        ),
    )
    assert not verify_entity_invariance(
        seal,
        ShotState(
            good.bone_lengths,
            good.skull_distances,
            good.garment_palette,
            good.garment_cut,
            good.vocal_tract_length + 1e-6,
        ),
    )
    with pytest.raises(ValueError, match="identity measurements"):
        ShotState((), (), (), (), 0.0)
    with pytest.raises(ValueError, match="finite"):
        ShotState(
            good.bone_lengths,
            good.skull_distances,
            good.garment_palette,
            good.garment_cut,
            float("nan"),
        )
    with pytest.raises(ValueError, match="positive"):
        ShotState(
            (0.0,) + good.bone_lengths[1:],
            good.skull_distances,
            good.garment_palette,
            good.garment_cut,
            good.vocal_tract_length,
        )
    with pytest.raises(ValueError, match="garment cut"):
        ShotState(
            good.bone_lengths,
            good.skull_distances,
            good.garment_palette,
            (float("nan"),) + good.garment_cut[1:],
            good.vocal_tract_length,
        )
    with pytest.raises(ValueError, match="palette"):
        ShotState(
            good.bone_lengths,
            good.skull_distances,
            ("red", "red", "red", "red", object()),  # type: ignore[arg-type]
            good.garment_cut,
            good.vocal_tract_length,
        )
    with pytest.raises(ValueError, match="camera_yaw"):
        ShotState(
            good.bone_lengths,
            good.skull_distances,
            good.garment_palette,
            good.garment_cut,
            good.vocal_tract_length,
            camera_yaw=float("nan"),
        )


def test_shot_mapping_converts_numeric_strings_and_fails_closed_on_invalid_types() -> None:
    seal = ParametricAvatarCatalog().profile(333)
    identity = seal.invariant_signature()
    mapping: dict[str, object] = {
        "bone_lengths": identity.bone_lengths,
        "skull_distances": identity.skull_distances,
        "garment_palette": identity.garment_palette,
        "garment_cut": identity.garment_cut,
        "vocal_tract_length": str(identity.vocal_tract_length),
        "camera_yaw": "1.25",
    }
    assert verify_entity_invariance(seal, mapping)
    mapping["camera_yaw"] = True
    assert not verify_entity_invariance(seal, mapping)
    mapping["camera_yaw"] = object()
    assert not verify_entity_invariance(seal, mapping)
    mapping["camera_yaw"] = 0.0
    mapping["bone_lengths"] = 3
    assert not verify_entity_invariance(seal, mapping)
