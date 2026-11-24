"""Vision packs: biophysical portrait inversion and optimal-transport grading."""

from nexus_ai_agent.creative.packs.vision.biophysical_portrait import (
    HueProtectionResult,
    SkinChromophoreEstimate,
    SkinModelError,
    enforce_skin_hue_protection,
    estimate_chromophores,
    reflectance_from_chromophores,
)
from nexus_ai_agent.creative.packs.vision.scene_optimal_transport import (
    GaussianColorModel,
    OptimalTransportError,
    OptimalTransportResult,
    bures_wasserstein_distance_sq,
    geodesic_covariance,
    hurter_driffield_density,
    optimal_transport_map,
)

__all__ = [
    "GaussianColorModel",
    "HueProtectionResult",
    "OptimalTransportError",
    "OptimalTransportResult",
    "SkinChromophoreEstimate",
    "SkinModelError",
    "bures_wasserstein_distance_sq",
    "enforce_skin_hue_protection",
    "estimate_chromophores",
    "geodesic_covariance",
    "hurter_driffield_density",
    "optimal_transport_map",
    "reflectance_from_chromophores",
]
