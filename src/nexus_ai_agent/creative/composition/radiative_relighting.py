"""Wave 9 — radiative-transfer relighting for episodic compositing.

When a subject is composited into a new environment across episodes, naive alpha
blending leaves the directional lighting mismatched. This module solves that from
first principles of radiative transfer and computational photography, using only
NumPy:

* :class:`SphericalHarmonicsLightProbe` — an order-2 (9 real SH coefficients per
  RGB channel) irradiance model with the exact real orthonormal SH basis and the
  Ramamoorthi–Hanrahan Lambertian irradiance transfer
  (:math:`\\hat A_0=\\pi,\\ \\hat A_1=2\\pi/3,\\ \\hat A_2=\\pi/4`). Irradiance is
  evaluated either as a SH sum or through the equivalent symmetric ``4x4``
  irradiance matrix per channel, :math:`\\mathcal E_c(\\mathbf n)=\\tilde{\\mathbf n}^T
  \\mathbf M_c\\tilde{\\mathbf n}` with :math:`\\tilde{\\mathbf n}=(1,n_x,n_y,n_z)`.
* :func:`reconstruct_integrable_normal_field` — Frankot–Chellappa (IEEE TPAMI
  1988) Fourier-domain projection of a (possibly non-integrable) gradient field
  onto the curl-free subspace, then unit-normal recovery.
* :func:`relight_subject_to_environment` — intrinsic albedo/shading split,
  energy-conserving SH ratio relighting, and a Cook-Torrance GGX/Schlick/Smith
  specular rim driven by the SH dipole key-light direction.

Everything is deterministic, offline, and owns no project state. Every numeric
input is validated and the module fails closed on malformed or degenerate input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = [
    "SH_BASIS_SIZE",
    "SH_L0",
    "SH_L1",
    "SH_L2A",
    "SH_L2B",
    "SH_L2C",
    "IRRADIANCE_TRANSFER",
    "MAX_RELIGHT_PIXELS",
    "RelightingError",
    "DegenerateNormalsError",
    "SingularSystemError",
    "SphericalHarmonicsLightProbe",
    "sh_basis",
    "sh_irradiance",
    "irradiance_matrix",
    "irradiance_from_matrix",
    "dominant_light_direction",
    "estimate_sh_from_environment_frame",
    "estimate_sh_from_subject_normals",
    "reconstruct_integrable_normal_field",
    "cook_torrance_specular",
    "relight_subject_to_environment",
    "ambient_probe",
]

SH_BASIS_SIZE = 9

#: Normalisation constants of the real orthonormal SH basis up to l = 2.
SH_L0 = 0.5 * math.sqrt(1.0 / math.pi)  # Y_0,0
SH_L1 = math.sqrt(3.0 / (4.0 * math.pi))  # Y_1,m
SH_L2A = 0.5 * math.sqrt(15.0 / math.pi)  # Y_2,-2 / Y_2,-1 / Y_2,1
SH_L2B = 0.25 * math.sqrt(5.0 / math.pi)  # Y_2,0
SH_L2C = 0.25 * math.sqrt(15.0 / math.pi)  # Y_2,2

#: Ramamoorthi–Hanrahan Lambertian irradiance transfer coefficients.
IRRADIANCE_TRANSFER: tuple[float, float, float] = (
    math.pi,  # A_0
    2.0 * math.pi / 3.0,  # A_1
    math.pi / 4.0,  # A_2
)

#: The per-band transfer repeated for the nine basis functions.
_A_BY_INDEX = np.array(
    [
        IRRADIANCE_TRANSFER[0],
        IRRADIANCE_TRANSFER[1],
        IRRADIANCE_TRANSFER[1],
        IRRADIANCE_TRANSFER[1],
        IRRADIANCE_TRANSFER[2],
        IRRADIANCE_TRANSFER[2],
        IRRADIANCE_TRANSFER[2],
        IRRADIANCE_TRANSFER[2],
        IRRADIANCE_TRANSFER[2],
    ],
    dtype=np.float64,
)

#: Bounded work: refuse relighting inputs larger than this many pixels.
MAX_RELIGHT_PIXELS = 4_194_304  # 2048 x 2048
_UNIT_TOLERANCE = 1e-3
_EPS = 1e-9


class RelightingError(Exception):
    """Base class for every radiative-relighting failure."""


class DegenerateNormalsError(RelightingError):
    """A normal field is non-unit, malformed, or otherwise unusable."""


class SingularSystemError(RelightingError):
    """A least-squares system is singular or numerically unusable."""


def _validate_unit_normals(normals: np.ndarray) -> np.ndarray:
    n = np.asarray(normals, dtype=np.float64)
    if n.ndim < 2 or n.shape[-1] != 3:
        raise DegenerateNormalsError(f"normals must have a trailing size-3 axis, got {n.shape}")
    if not np.all(np.isfinite(n)):
        raise DegenerateNormalsError("normals contain NaN/Inf")
    length = np.sqrt((n * n).sum(axis=-1))
    if float(np.max(np.abs(length - 1.0))) > _UNIT_TOLERANCE:
        raise DegenerateNormalsError("normals must be unit length within 1e-3")
    return n


def _validate_rgb(image: np.ndarray, *, name: str) -> np.ndarray:
    a = np.asarray(image, dtype=np.float64)
    if a.ndim != 3 or a.shape[-1] != 3:
        raise RelightingError(f"{name} must be an (H, W, 3) array, got {a.shape}")
    if not np.all(np.isfinite(a)):
        raise RelightingError(f"{name} contains NaN/Inf")
    if a.shape[0] * a.shape[1] > MAX_RELIGHT_PIXELS:
        raise RelightingError(f"{name} exceeds the bounded pixel budget")
    return a


def sh_basis(normals: np.ndarray) -> np.ndarray:
    """The nine real orthonormal SH basis values for unit normals.

    Returns an ``(..., 9)`` array in the order
    ``[Y00, Y1,-1, Y10, Y11, Y2,-2, Y2,-1, Y20, Y21, Y22]``.
    """
    n = _validate_unit_normals(normals)
    nx, ny, nz = n[..., 0], n[..., 1], n[..., 2]
    out = np.empty(n.shape[:-1] + (SH_BASIS_SIZE,), dtype=np.float64)
    out[..., 0] = SH_L0
    out[..., 1] = SH_L1 * ny
    out[..., 2] = SH_L1 * nz
    out[..., 3] = SH_L1 * nx
    out[..., 4] = SH_L2A * nx * ny
    out[..., 5] = SH_L2A * ny * nz
    out[..., 6] = SH_L2B * (3.0 * nz * nz - 1.0)
    out[..., 7] = SH_L2A * nx * nz
    out[..., 8] = SH_L2C * (nx * nx - ny * ny)
    return out


def _validate_coefficients(coefficients: np.ndarray) -> np.ndarray:
    c = np.asarray(coefficients, dtype=np.float64)
    if c.shape != (SH_BASIS_SIZE, 3):
        raise RelightingError(f"SH coefficients must be (9, 3), got {c.shape}")
    if not np.all(np.isfinite(c)):
        raise RelightingError("SH coefficients contain NaN/Inf")
    return c


def sh_irradiance(coefficients: np.ndarray, normals: np.ndarray) -> np.ndarray:
    """Lambertian irradiance ``E_c(n) = sum_lm A_l L_lm Y_lm(n)`` per channel."""
    c = _validate_coefficients(coefficients)
    basis = sh_basis(normals)  # (..., 9)
    weighted = basis * _A_BY_INDEX  # (..., 9)
    irradiance: np.ndarray = np.einsum("...i,ic->...c", weighted, c)
    return irradiance


def irradiance_matrix(coefficients: np.ndarray) -> np.ndarray:
    """The symmetric ``4x4`` irradiance matrix per channel.

    ``E_c(n) = n~^T M_c n~`` with ``n~ = (1, nx, ny, nz)`` equals
    :func:`sh_irradiance` exactly on the unit sphere. The construction is the
    unique quadratic form whose polynomial equals the SH irradiance polynomial
    (see the equivalence unit test).
    """
    c = _validate_coefficients(coefficients)
    a0, a1, a2 = IRRADIANCE_TRANSFER
    m = np.zeros((3, 4, 4), dtype=np.float64)
    for ch in range(3):
        l0 = c[0, ch]
        l1m1, l10, l11 = c[1, ch], c[2, ch], c[3, ch]
        l2m2, l2m1, l20, l21, l22 = c[4, ch], c[5, ch], c[6, ch], c[7, ch], c[8, ch]
        m[ch, 0, 0] = a0 * SH_L0 * l0 - a2 * SH_L2B * l20
        m[ch, 0, 1] = m[ch, 1, 0] = a1 * SH_L1 * l11 / 2.0
        m[ch, 0, 2] = m[ch, 2, 0] = a1 * SH_L1 * l1m1 / 2.0
        m[ch, 0, 3] = m[ch, 3, 0] = a1 * SH_L1 * l10 / 2.0
        m[ch, 1, 1] = a2 * SH_L2C * l22
        m[ch, 2, 2] = -a2 * SH_L2C * l22
        m[ch, 3, 3] = 3.0 * a2 * SH_L2B * l20
        m[ch, 1, 2] = m[ch, 2, 1] = a2 * SH_L2A * l2m2 / 2.0
        m[ch, 1, 3] = m[ch, 3, 1] = a2 * SH_L2A * l21 / 2.0
        m[ch, 2, 3] = m[ch, 3, 2] = a2 * SH_L2A * l2m1 / 2.0
    return m


def irradiance_from_matrix(matrix: np.ndarray, normals: np.ndarray) -> np.ndarray:
    """Evaluate ``n~^T M_c n~`` for every pixel/channel."""
    n = _validate_unit_normals(normals)
    m = np.asarray(matrix, dtype=np.float64)
    if m.shape != (3, 4, 4):
        raise RelightingError(f"irradiance matrix must be (3, 4, 4), got {m.shape}")
    tilde = np.stack([np.ones(n.shape[:-1]), n[..., 0], n[..., 1], n[..., 2]], axis=-1)
    irradiance: np.ndarray = np.einsum("...i,cij,...j->...c", tilde, m, tilde)
    return irradiance


@dataclass(frozen=True)
class SphericalHarmonicsLightProbe:
    """An order-2 SH light probe: 9 coefficients for each of R, G, B."""

    coefficients: np.ndarray

    def __post_init__(self) -> None:
        object.__setattr__(self, "coefficients", _validate_coefficients(self.coefficients))

    def irradiance(self, normals: np.ndarray) -> np.ndarray:
        return sh_irradiance(self.coefficients, normals)

    def irradiance_via_matrix(self, normals: np.ndarray) -> np.ndarray:
        return irradiance_from_matrix(irradiance_matrix(self.coefficients), normals)

    @property
    def dominant_direction(self) -> tuple[float, float, float]:
        return dominant_light_direction(self)

    def scaled(self, factor: float) -> SphericalHarmonicsLightProbe:
        return SphericalHarmonicsLightProbe(self.coefficients * float(factor))


def ambient_probe(radiance: float) -> SphericalHarmonicsLightProbe:
    """A uniform (ambient-only) environment of the given radiance.

    The constant-radiance environment has a single non-zero SH coefficient
    ``L_00 = radiance * sqrt(4 pi)``, so its irradiance is the constant
    ``pi * radiance`` everywhere. This is the canonical target for harmonising a
    composited foreground to an environment of a declared brightness.
    """
    if radiance < 0.0:
        raise RelightingError("ambient radiance must be non-negative")
    coefficients = np.zeros((SH_BASIS_SIZE, 3), dtype=np.float64)
    coefficients[0] = radiance * math.sqrt(4.0 * math.pi)
    return SphericalHarmonicsLightProbe(coefficients)


def dominant_light_direction(probe: SphericalHarmonicsLightProbe) -> tuple[float, float, float]:
    """The key-light direction from the ``l = 1`` SH dipole vector.

    ``(L_1,1, L_1,-1, L_1,0)`` map to ``(x, y, z)``; the returned vector is unit
    length. A zero dipole is a real (ambient-only) case and returns ``(0, 0, 1)``.
    """
    c = probe.coefficients
    dipole = np.array([c[3].mean(), c[1].mean(), c[2].mean()], dtype=np.float64)
    norm = float(np.linalg.norm(dipole))
    if norm < _EPS:
        return (0.0, 0.0, 1.0)
    v = dipole / norm
    return (float(v[0]), float(v[1]), float(v[2]))


def _directions_equirectangular(height: int, width: int) -> np.ndarray:
    """Unit direction for every pixel of an equirectangular map (theta, phi)."""
    theta = (np.arange(height) + 0.5) / height * np.pi  # polar [0, pi]
    phi = (np.arange(width) + 0.5) / width * 2.0 * np.pi  # azimuth [0, 2pi]
    tt, pp = np.meshgrid(theta, phi, indexing="ij")
    st = np.sin(tt)
    return np.stack([st * np.cos(pp), np.cos(tt), st * np.sin(pp)], axis=-1)


def estimate_sh_from_environment_frame(frame_rgb: np.ndarray) -> SphericalHarmonicsLightProbe:
    """Project an equirectangular environment frame onto the SH basis.

    ``L_lm,c = sum_pixels L_c(dir) Y_lm(dir) sin(theta) dtheta dphi`` with the
    solid-angle weight, normalised so the discrete sphere integrates to exactly
    :math:`4\\pi` (so a constant radiance ``c`` yields ``L_00 = c sqrt(4 pi)``).
    The result is a :class:`SphericalHarmonicsLightProbe`.
    """
    frame = _validate_rgb(frame_rgb, name="environment frame")
    height, width = frame.shape[0], frame.shape[1]
    dirs = _directions_equirectangular(height, width)
    basis = sh_basis(dirs)  # (H, W, 9)
    theta = (np.arange(height) + 0.5) / height * np.pi
    weight = np.sin(theta)[:, None]  # (H, 1) area element up to a constant
    weight = weight / (float(weight.mean()) * height * width) * (4.0 * np.pi)
    coefficients = np.einsum("hw,hwk,hwc->kc", weight, basis, frame)
    return SphericalHarmonicsLightProbe(coefficients)


def estimate_sh_from_subject_normals(
    subject_rgb: np.ndarray,
    normal_map: np.ndarray,
    mask: np.ndarray,
    *,
    ridge_lambda: float = 1e-3,
) -> SphericalHarmonicsLightProbe:
    """Tikhonov-regularised SH fit of a subject's observed shading.

    Solves ``(Phi^T Phi + lambda I) L = Phi^T y`` for the nine SH coefficients
    per channel, where ``Phi`` rows are ``A_i Y_i(n)`` over the masked pixels.
    """
    subject = _validate_rgb(subject_rgb, name="subject")
    n = _validate_unit_normals(normal_map)
    if n.shape[:2] != subject.shape[:2]:
        raise DegenerateNormalsError("normal map shape does not match the subject")
    m = np.asarray(mask, dtype=np.float64)
    if m.shape != subject.shape[:2]:
        raise DegenerateNormalsError("mask shape does not match the subject")
    if ridge_lambda < 0.0:
        raise RelightingError("ridge_lambda must be non-negative")
    inside = m > 0.5
    if int(inside.sum()) < SH_BASIS_SIZE:
        raise DegenerateNormalsError("fewer masked pixels than SH coefficients")

    basis = sh_basis(n)[inside]  # (N, 9)
    phi = basis * _A_BY_INDEX  # (N, 9)
    y = subject[inside]  # (N, 3)
    gram = phi.T @ phi + ridge_lambda * np.eye(SH_BASIS_SIZE)
    rhs = phi.T @ y
    try:
        coefficients = np.linalg.solve(gram, rhs)
    except np.linalg.LinAlgError as error:
        raise SingularSystemError("SH normal-fit system is singular") from error
    return SphericalHarmonicsLightProbe(coefficients)


def reconstruct_integrable_normal_field(
    p: np.ndarray, q: np.ndarray, *, epsilon: float = 1e-9
) -> tuple[np.ndarray, np.ndarray]:
    """Frankot–Chellappa integrable surface + unit normals from gradients.

    ``p = dz/dx`` and ``q = dz/dy`` need not be integrable; the field is projected
    onto the curl-free subspace in the 2D FFT domain and the depth surface is
    recovered. Returns ``(Z, normals)`` with ``normals`` an ``(H, W, 3)`` unit
    field in :math:`S^2`.
    """
    px = np.asarray(p, dtype=np.float64)
    py = np.asarray(q, dtype=np.float64)
    if px.ndim != 2 or px.shape != py.shape:
        raise RelightingError("p and q must be matching 2-D gradient fields")
    if not (np.all(np.isfinite(px)) and np.all(np.isfinite(py))):
        raise RelightingError("gradient field contains NaN/Inf")
    if epsilon <= 0.0:
        raise RelightingError("epsilon must be positive")
    height, width = px.shape
    wx = np.fft.fftfreq(width) * 2.0 * np.pi
    wy = np.fft.fftfreq(height) * 2.0 * np.pi
    wx_grid, wy_grid = np.meshgrid(wx, wy)
    p_hat = np.fft.fft2(px)
    q_hat = np.fft.fft2(py)
    denom = wx_grid * wx_grid + wy_grid * wy_grid + epsilon
    z_hat = (-1j * wx_grid * p_hat - 1j * wy_grid * q_hat) / denom
    z_hat[0, 0] = 0.0
    surface = np.real(np.fft.ifft2(z_hat))
    dz_dy, dz_dx = np.gradient(surface)
    nx = -dz_dx
    ny = -dz_dy
    nz = np.ones_like(surface)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    normals = np.stack([nx / norm, ny / norm, nz / norm], axis=-1)
    return surface, normals


def _ggx_d(hdotn: np.ndarray, alpha: float) -> np.ndarray:
    a2 = alpha * alpha
    denom = hdotn * hdotn * (a2 - 1.0) + 1.0
    result: np.ndarray = a2 / (np.pi * np.maximum(denom * denom, _EPS))
    return result


def _smith_g(ndotv: np.ndarray, ndotl: np.ndarray, alpha: float) -> np.ndarray:
    def g1(cosine: np.ndarray) -> np.ndarray:
        cos2 = cosine * cosine
        tan2 = (1.0 - cos2) / np.maximum(cos2, _EPS)
        shadow: np.ndarray = 2.0 / (1.0 + np.sqrt(1.0 + alpha * alpha * tan2))
        return shadow

    geometry: np.ndarray = g1(ndotv) * g1(ndotl)
    return geometry


def _schlick_f(vdotn: float, f0: np.ndarray) -> np.ndarray:
    return f0 + (1.0 - f0) * (1.0 - vdotn) ** 5


def cook_torrance_specular(
    normals: np.ndarray,
    view_dir: tuple[float, float, float],
    light_dir: tuple[float, float, float],
    *,
    roughness: float = 0.35,
    f0: tuple[float, float, float] = (0.04, 0.04, 0.04),
) -> np.ndarray:
    """Cook-Torrance microfacet specular (GGX D, Schlick F, Smith G)."""
    n = _validate_unit_normals(normals)
    if not 0.0 < roughness <= 1.0:
        raise RelightingError("roughness must be in (0, 1]")
    view = np.array(view_dir, dtype=np.float64)
    light = np.array(light_dir, dtype=np.float64)
    if abs(float(np.linalg.norm(view)) - 1.0) > _UNIT_TOLERANCE:
        raise RelightingError("view_dir must be unit length")
    if abs(float(np.linalg.norm(light)) - 1.0) > _UNIT_TOLERANCE:
        raise RelightingError("light_dir must be unit length")
    ndotv = np.clip((n * view).sum(axis=-1), 0.0, 1.0)
    ndotl = np.clip((n * light).sum(axis=-1), 0.0, 1.0)
    half = view + light
    half_norm = float(np.linalg.norm(half))
    half = half / (half_norm if half_norm > _EPS else 1.0)
    hdotn = np.clip((n * half).sum(axis=-1), 0.0, 1.0)
    vdoth = np.clip(float(np.dot(view, half)), 0.0, 1.0)
    alpha = max(roughness * roughness, 1e-4)
    d = _ggx_d(hdotn, alpha)
    g = _smith_g(ndotv, ndotl, alpha)
    f = _schlick_f(vdoth, np.array(f0, dtype=np.float64))
    denom = np.maximum(4.0 * ndotv * ndotl, _EPS)
    spec = (d * g)[..., None] * f / denom[..., None]
    return np.where(ndotl[..., None] > 0.0, spec * ndotl[..., None], 0.0)


def _split_albedo_shading(subject: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Intrinsic split: per-pixel chromatic albedo times the observed shading.

    ``shading`` is the (clipped) luminance and ``albedo = subject / shading``, so
    ``albedo * shading == subject`` exactly: an identity ratio is a no-op.
    """
    luminance = (subject * np.array([0.2126, 0.7152, 0.0722])).sum(axis=-1)
    inside = mask > 0.5
    if not np.any(inside):
        raise RelightingError("mask selects no pixels")
    if float(luminance[inside].mean()) < _EPS:
        raise RelightingError("subject luminance is zero inside the mask")
    shading = np.clip(luminance, _EPS, None)
    albedo = subject / shading[..., None]
    return albedo, shading[..., None]


def relight_subject_to_environment(
    subject_rgb: np.ndarray,
    normal_map: np.ndarray,
    mask: np.ndarray,
    *,
    source_probe: SphericalHarmonicsLightProbe,
    target_probe: SphericalHarmonicsLightProbe,
    roughness: float = 0.35,
    specular_strength: float = 0.25,
) -> np.ndarray:
    """Relight a masked subject from ``source_probe`` to ``target_probe``.

    Applies the energy-conserving irradiance ratio
    ``E(N, L_dst) / E(N, L_src)`` to the intrinsic albedo, then adds a
    Cook-Torrance specular rim driven by the target key-light direction. Output is
    clipped to ``[0, 1]`` and the unmasked region is returned unchanged.
    """
    subject = _validate_rgb(subject_rgb, name="subject")
    n = _validate_unit_normals(normal_map)
    if n.shape[:2] != subject.shape[:2]:
        raise DegenerateNormalsError("normal map shape does not match the subject")
    m = np.asarray(mask, dtype=np.float64)
    if m.shape != subject.shape[:2]:
        raise DegenerateNormalsError("mask shape does not match the subject")
    if not 0.0 <= specular_strength <= 1.0:
        raise RelightingError("specular_strength must be in [0, 1]")

    albedo, shading = _split_albedo_shading(subject, m)
    src_irr = source_probe.irradiance(n)
    dst_irr = target_probe.irradiance(n)
    ratio = np.maximum(dst_irr, _EPS) / np.maximum(src_irr, _EPS)
    relit = albedo * shading * ratio

    key = target_probe.dominant_direction
    view = (0.0, 0.0, 1.0)
    spec = cook_torrance_specular(n, view, key, roughness=roughness)
    relit = relit + specular_strength * spec

    m3 = m[..., None]
    blended: np.ndarray = np.clip(relit * m3 + subject * (1.0 - m3), 0.0, 1.0)
    return blended.astype(np.float32)
