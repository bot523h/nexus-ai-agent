"""Multi-Episode Zero-Drift Character Identity Capsule and Continuity Healer.

Wave 8 of NAGAR MORPHEUS:
Provides cryptographically sealed CharacterIdentityCapsule freezing 3DMM identity,
cephalometrics, bone rest lengths, soft-tissue tetrahedral volumes, hair strand
metrics, and cloth parameters into an RFC 6962 Merkle tree, coupled with a
multi-episode (N >= 4) forensic continuity auditor and zero-drift healer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from nexus_ai_agent.creative.morpheus.cranio_facial_morphable_lock import (
    CranioFacialBasis,
    compute_cephalometric_ratios,
    lock_and_heal_cranio_facial_identity,
)
from nexus_ai_agent.creative.morpheus.morpheus_real_media_spine import (
    compute_rfc6962_merkle_root,
)


class CapsuleIntegrityTamperError(Exception):
    """Raised when CharacterIdentityCapsule Merkle root or SHA-256 digest is tampered."""


@dataclass(frozen=True)
class CharacterIdentityCapsule:
    """Cryptographically sealed character identity baseline across episodic cuts."""

    character_id: str
    canonical_alpha_id: tuple[float, ...]
    cephalometric_ratios: dict[str, float]
    skeletal_rest_lengths: dict[str, float]
    soft_tissue_rest_volumes: tuple[float, ...]
    hair_strand_count: int
    hair_rest_segment_lengths: tuple[float, ...]
    melanin_absorption: tuple[float, float, float]
    cloth_thin_film_thickness_nm: float
    cloth_sheen_roughness: float
    merkle_root_hex: str
    capsule_sha256: str


def compute_capsule_merkle_and_hash(
    character_id: str,
    canonical_alpha_id: tuple[float, ...],
    cephalometric_ratios: dict[str, float],
    skeletal_rest_lengths: dict[str, float],
    soft_tissue_rest_volumes: tuple[float, ...],
    hair_strand_count: int,
    hair_rest_segment_lengths: tuple[float, ...],
    melanin_absorption: tuple[float, float, float],
    cloth_thin_film_thickness_nm: float,
    cloth_sheen_roughness: float,
) -> tuple[str, str]:
    """Compute RFC 6962 Merkle root and canonical SHA-256 for character capsule fields."""
    leaves: list[bytes] = [
        f"char_id:{character_id}".encode(),
        f"alpha_id:{','.join(f'{x:.8f}' for x in canonical_alpha_id)}".encode(),
        f"ceph:{json.dumps(cephalometric_ratios, sort_keys=True)}".encode(),
        f"bones:{json.dumps(skeletal_rest_lengths, sort_keys=True)}".encode(),
        f"tets:{','.join(f'{x:.8f}' for x in soft_tissue_rest_volumes)}".encode(),
        f"hair_count:{hair_strand_count}".encode(),
        f"hair_lens:{','.join(f'{x:.8f}' for x in hair_rest_segment_lengths)}".encode(),
        f"melanin:{','.join(f'{x:.8f}' for x in melanin_absorption)}".encode(),
        f"film_d:{cloth_thin_film_thickness_nm:.8f}".encode(),
        f"sheen_r:{cloth_sheen_roughness:.8f}".encode(),
    ]
    merkle_root = compute_rfc6962_merkle_root(leaves).hex()

    body = {
        "character_id": character_id,
        "canonical_alpha_id": list(canonical_alpha_id),
        "cephalometric_ratios": cephalometric_ratios,
        "skeletal_rest_lengths": skeletal_rest_lengths,
        "soft_tissue_rest_volumes": list(soft_tissue_rest_volumes),
        "hair_strand_count": hair_strand_count,
        "hair_rest_segment_lengths": list(hair_rest_segment_lengths),
        "melanin_absorption": list(melanin_absorption),
        "cloth_thin_film_thickness_nm": cloth_thin_film_thickness_nm,
        "cloth_sheen_roughness": cloth_sheen_roughness,
        "merkle_root_hex": merkle_root,
    }
    capsule_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    return merkle_root, capsule_hash


def create_character_identity_capsule(
    character_id: str,
    canonical_alpha_id: tuple[float, ...],
    cephalometric_ratios: dict[str, float],
    skeletal_rest_lengths: dict[str, float],
    soft_tissue_rest_volumes: tuple[float, ...],
    hair_strand_count: int,
    hair_rest_segment_lengths: tuple[float, ...],
    melanin_absorption: tuple[float, float, float],
    cloth_thin_film_thickness_nm: float,
    cloth_sheen_roughness: float,
) -> CharacterIdentityCapsule:
    """Create an authenticated, cryptographically sealed CharacterIdentityCapsule."""
    merkle_root, capsule_hash = compute_capsule_merkle_and_hash(
        character_id=character_id,
        canonical_alpha_id=canonical_alpha_id,
        cephalometric_ratios=cephalometric_ratios,
        skeletal_rest_lengths=skeletal_rest_lengths,
        soft_tissue_rest_volumes=soft_tissue_rest_volumes,
        hair_strand_count=hair_strand_count,
        hair_rest_segment_lengths=hair_rest_segment_lengths,
        melanin_absorption=melanin_absorption,
        cloth_thin_film_thickness_nm=cloth_thin_film_thickness_nm,
        cloth_sheen_roughness=cloth_sheen_roughness,
    )
    return CharacterIdentityCapsule(
        character_id=character_id,
        canonical_alpha_id=canonical_alpha_id,
        cephalometric_ratios=cephalometric_ratios,
        skeletal_rest_lengths=skeletal_rest_lengths,
        soft_tissue_rest_volumes=soft_tissue_rest_volumes,
        hair_strand_count=hair_strand_count,
        hair_rest_segment_lengths=hair_rest_segment_lengths,
        melanin_absorption=melanin_absorption,
        cloth_thin_film_thickness_nm=cloth_thin_film_thickness_nm,
        cloth_sheen_roughness=cloth_sheen_roughness,
        merkle_root_hex=merkle_root,
        capsule_sha256=capsule_hash,
    )


def verify_character_capsule_integrity(capsule: CharacterIdentityCapsule) -> bool:
    """Verify that capsule Merkle root and capsule_sha256 match sealed fields."""
    exp_merkle, exp_hash = compute_capsule_merkle_and_hash(
        character_id=capsule.character_id,
        canonical_alpha_id=capsule.canonical_alpha_id,
        cephalometric_ratios=capsule.cephalometric_ratios,
        skeletal_rest_lengths=capsule.skeletal_rest_lengths,
        soft_tissue_rest_volumes=capsule.soft_tissue_rest_volumes,
        hair_strand_count=capsule.hair_strand_count,
        hair_rest_segment_lengths=capsule.hair_rest_segment_lengths,
        melanin_absorption=capsule.melanin_absorption,
        cloth_thin_film_thickness_nm=capsule.cloth_thin_film_thickness_nm,
        cloth_sheen_roughness=capsule.cloth_sheen_roughness,
    )
    return exp_merkle == capsule.merkle_root_hex and exp_hash == capsule.capsule_sha256


@dataclass(frozen=True)
class EpisodeCharacterState:
    """Observed character physical state in a specific episode or scene cut."""

    episode_id: str
    face_mesh: np.ndarray  # shape: (V, 3)
    bone_lengths: dict[str, float]
    tetrahedral_volumes: tuple[float, ...]
    hair_strand_count: int
    hair_segment_lengths: tuple[float, ...]
    wardrobe_parameters: dict[str, float]


@dataclass(frozen=True)
class EpisodicContinuityAuditResult:
    """Full forensic multi-episode continuity audit and zero-drift healing report."""

    character_id: str
    episode_count: int
    pre_heal_identity_drifts: dict[str, float]
    post_heal_identity_drifts: dict[str, float]
    cephalometric_drifts: dict[str, float]
    bone_length_drifts: dict[str, float]
    volume_drifts: dict[str, float]
    is_healed_continuity_exact: bool
    healed_episode_states: dict[str, EpisodeCharacterState]


def audit_and_heal_episodic_character_continuity(
    capsule: CharacterIdentityCapsule,
    episode_states: list[EpisodeCharacterState],
    basis: CranioFacialBasis,
) -> EpisodicContinuityAuditResult:
    """Audit character continuity across N >= 4 episodes and heal identity drift to < 1e-12."""
    if not verify_character_capsule_integrity(capsule):
        raise CapsuleIntegrityTamperError(
            f"Sealed capsule integrity verification failed for character '{capsule.character_id}'"
        )
    if len(episode_states) < 4:
        raise ValueError(
            f"Multi-episode audit requires at least 4 episodes, got {len(episode_states)}"
        )

    pre_drifts: dict[str, float] = {}
    post_drifts: dict[str, float] = {}
    ceph_drifts: dict[str, float] = {}
    bone_drifts: dict[str, float] = {}
    vol_drifts: dict[str, float] = {}
    healed_states: dict[str, EpisodeCharacterState] = {}

    locked_alpha = np.array(capsule.canonical_alpha_id, dtype=np.float64)

    for ep in episode_states:
        # 1. Audit and heal cranio-facial identity
        lock_res = lock_and_heal_cranio_facial_identity(
            observed_mesh=ep.face_mesh,
            locked_alpha_id=locked_alpha,
            basis=basis,
        )
        pre_drifts[ep.episode_id] = lock_res.raw_identity_drift
        post_drifts[ep.episode_id] = lock_res.post_heal_identity_drift

        # Cephalometrics
        cephs_healed = compute_cephalometric_ratios(lock_res.healed_canonical_mesh)
        c_drift = float(
            abs(cephs_healed.interpupillary_to_bizygomatic - capsule.cephalometric_ratios["ip_bz"])
            + abs(cephs_healed.nasion_menton_to_bizygomatic - capsule.cephalometric_ratios["nm_bz"])
            + abs(cephs_healed.cranial_vault_symmetry - capsule.cephalometric_ratios["symmetry"])
        )
        ceph_drifts[ep.episode_id] = c_drift

        # 2. Audit and heal skeletal bone lengths
        b_drift = 0.0
        healed_bones: dict[str, float] = {}
        for b_name, target_len in capsule.skeletal_rest_lengths.items():
            curr_len = ep.bone_lengths.get(b_name, target_len)
            b_drift += abs(curr_len - target_len)
            healed_bones[b_name] = target_len  # heal stretch
        bone_drifts[ep.episode_id] = b_drift

        # 3. Audit and heal soft-tissue volumes
        v_drift = 0.0
        for idx, target_vol in enumerate(capsule.soft_tissue_rest_volumes):
            if idx < len(ep.tetrahedral_volumes):
                v_drift += abs(ep.tetrahedral_volumes[idx] - target_vol)
        vol_drifts[ep.episode_id] = v_drift

        # 4. Construct healed episode state
        healed_states[ep.episode_id] = EpisodeCharacterState(
            episode_id=ep.episode_id,
            face_mesh=lock_res.healed_world_mesh,
            bone_lengths=healed_bones,
            tetrahedral_volumes=capsule.soft_tissue_rest_volumes,
            hair_strand_count=capsule.hair_strand_count,
            hair_segment_lengths=capsule.hair_rest_segment_lengths,
            wardrobe_parameters={
                "film_thickness_nm": capsule.cloth_thin_film_thickness_nm,
                "sheen_roughness": capsule.cloth_sheen_roughness,
            },
        )

    # Prove that every single episode has post-heal identity drift < 1e-12
    exact_continuity = all(drift < 1e-12 for drift in post_drifts.values())

    return EpisodicContinuityAuditResult(
        character_id=capsule.character_id,
        episode_count=len(episode_states),
        pre_heal_identity_drifts=pre_drifts,
        post_heal_identity_drifts=post_drifts,
        cephalometric_drifts=ceph_drifts,
        bone_length_drifts=bone_drifts,
        volume_drifts=vol_drifts,
        is_healed_continuity_exact=exact_continuity,
        healed_episode_states=healed_states,
    )
