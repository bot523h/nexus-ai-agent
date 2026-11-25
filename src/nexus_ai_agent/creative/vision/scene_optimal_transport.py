"""Scene pack facade over the temporal optimal-transport implementation."""

from __future__ import annotations

from nexus_ai_agent.creative.temporal.scene_optimal_transport import (
    GaussianColorModel,
    OptimalTransportError,
    OptimalTransportResult,
    apply_transport_map,
    bures_wasserstein_distance_sq,
    geodesic_covariance,
    hurter_driffield_density,
    jacobi_eigh,
    matrix_inverse_spd,
    matrix_inverse_sqrt_spd,
    matrix_sqrt_psd,
    optimal_transport_map,
)
from nexus_ai_agent.creative.temporal.scene_optimal_transport import (
    _matmul as _matmul,
)

__all__ = [
    "GaussianColorModel",
    "OptimalTransportError",
    "OptimalTransportResult",
    "apply_transport_map",
    "bures_wasserstein_distance_sq",
    "geodesic_covariance",
    "hurter_driffield_density",
    "jacobi_eigh",
    "matrix_inverse_spd",
    "matrix_inverse_sqrt_spd",
    "matrix_sqrt_psd",
    "optimal_transport_map",
]
