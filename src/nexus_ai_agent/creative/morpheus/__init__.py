"""NAGAR Morpheus — Deterministic 3D character continuity (Waves 1–8)."""
from __future__ import annotations
from nexus_ai_agent.creative.morpheus.cranio_facial_morphable_lock import (
    CephalometricRatios, CranioFacialMorphableLock, IdentityLockResult,
)
from nexus_ai_agent.creative.morpheus.dual_quaternion_skeletal_rig import (
    DualQuaternion, DualQuaternionSkeletalRig, JointLimits,
)
from nexus_ai_agent.creative.morpheus.gaussian_splatting_pbr_rasterizer import (
    AnisotropicGaussian3D, GaussianSplattingPBRRasterizer, RenderOutput,
)
from nexus_ai_agent.creative.morpheus.morpheus_real_media_spine import (
    MorpheusAssetVersionRecord, MorpheusRealMediaSpine,
    compile_and_heal_morpheus_shot, execute_morpheus_staged_attempt,
)
from nexus_ai_agent.creative.morpheus.corotational_fem_soft_tissue import (
    CorotationalFEMSoftTissue, SoftTissueState,
)
from nexus_ai_agent.creative.morpheus.cosserat_hair_marschner_brdf import (
    CosseratHairSystem, evaluate_marschner_hair_scattering,
)
from nexus_ai_agent.creative.morpheus.iridescence_microfiber_cloth_brdf import (
    evaluate_microfiber_cloth_brdf, evaluate_thin_film_iridescence_rgb,
)
from nexus_ai_agent.creative.morpheus.episodic_character_capsule_vault import (
    EpisodicCharacterCapsule, verify_cross_episode_character_continuity,
)
__all__ = [
    "AnisotropicGaussian3D", "CephalometricRatios", "CorotationalFEMSoftTissue",
    "CosseratHairSystem", "CranioFacialMorphableLock", "DualQuaternion",
    "DualQuaternionSkeletalRig", "EpisodicCharacterCapsule",
    "GaussianSplattingPBRRasterizer", "IdentityLockResult", "JointLimits",
    "MorpheusAssetVersionRecord", "MorpheusRealMediaSpine", "RenderOutput",
    "SoftTissueState", "compile_and_heal_morpheus_shot",
    "evaluate_marschner_hair_scattering", "evaluate_microfiber_cloth_brdf",
    "evaluate_thin_film_iridescence_rgb", "execute_morpheus_staged_attempt",
    "verify_cross_episode_character_continuity",
]
