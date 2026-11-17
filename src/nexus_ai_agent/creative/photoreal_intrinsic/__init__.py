"""CPU-only deterministic intrinsic rendering primitives."""

from .benchmark import benchmark_pipeline
from .log_chromophore_tomography_baker import (
    IntrinsicBiophysicalVault,
    KeyPolicy,
    LogChromophoreTomographyBaker,
    PhotorealDNASeal,
    dctn,
    idctn,
)
from .ocular_caustics_and_film_chemistry import OcularCausticsAndFilmChemistry
from .riemannian_strain_wrinkle_tensor import RiemannianStrainWrinkleTensor
from .screened_poisson_skin_transport import ScreenedPoissonSkinTransport

PhotorealCharacterShotCompiler = OcularCausticsAndFilmChemistry
__all__ = [
    "IntrinsicBiophysicalVault",
    "KeyPolicy",
    "benchmark_pipeline",
    "PhotorealDNASeal",
    "dctn",
    "idctn",
    "LogChromophoreTomographyBaker",
    "ScreenedPoissonSkinTransport",
    "RiemannianStrainWrinkleTensor",
    "OcularCausticsAndFilmChemistry",
    "PhotorealCharacterShotCompiler",
]
