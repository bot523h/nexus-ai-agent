"""Epistemic verification: a measured value carries a budget, not a point.

:mod:`uncertainty_gum` implements the JCGM 100:2008 law of propagation of
uncertainty to first order plus a three-valued conformance decision that
abstains inside the guard band instead of guessing.
:mod:`adapters` wraps the W10X scientific engines (loudness, radiometric cut
detection, chromophore estimation, SLO burn) into that budget.
"""

from __future__ import annotations

from .adapters import (
    ChromophoreUncertainty,
    LoudnessUncertainty,
    SceneCutUncertainty,
    ScientificVerificationBridge,
    SilenceUncertainty,
    SLOBurnUncertainty,
    chromophore_uncertainty,
    loudness_uncertainty,
    scene_cut_uncertainty,
    silence_uncertainty,
    slo_burn_uncertainty,
)
from .perception_gate import (
    PerceptionAdmission,
    PerceptionAdmissionRequest,
    admit_perception,
    gate_scene_cut,
    gate_silence,
)
from .uncertainty_gum import (
    CombinedUncertainty,
    GuardBandDecision,
    GUMCovarianceError,
    GUMError,
    GUMInputError,
    SpecificationLimits,
    UncertaintyInput,
    VerificationOutcome,
    combine,
    evaluate_guard_band,
    evaluate_threshold,
    welch_satterthwaite,
)

__all__ = [
    "ChromophoreUncertainty",
    "CombinedUncertainty",
    "GUMCovarianceError",
    "GUMError",
    "GUMInputError",
    "GuardBandDecision",
    "LoudnessUncertainty",
    "PerceptionAdmission",
    "PerceptionAdmissionRequest",
    "SLOBurnUncertainty",
    "SceneCutUncertainty",
    "ScientificVerificationBridge",
    "SilenceUncertainty",
    "SpecificationLimits",
    "UncertaintyInput",
    "VerificationOutcome",
    "chromophore_uncertainty",
    "admit_perception",
    "gate_scene_cut",
    "gate_silence",
    "combine",
    "evaluate_guard_band",
    "evaluate_threshold",
    "loudness_uncertainty",
    "scene_cut_uncertainty",
    "silence_uncertainty",
    "slo_burn_uncertainty",
    "welch_satterthwaite",
]
