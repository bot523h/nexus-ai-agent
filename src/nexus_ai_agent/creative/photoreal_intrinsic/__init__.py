"""CPU-only deterministic intrinsic rendering primitives."""

from .log_chromophore_tomography_baker import (
    IntrinsicBiophysicalVault,
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
    "PhotorealDNASeal",
    "dctn",
    "idctn",
    "LogChromophoreTomographyBaker",
    "ScreenedPoissonSkinTransport",
    "RiemannianStrainWrinkleTensor",
    "OcularCausticsAndFilmChemistry",
    "PhotorealCharacterShotCompiler",
]
