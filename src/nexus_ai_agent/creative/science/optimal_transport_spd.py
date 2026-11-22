"""Bures–Wasserstein-2 optimal transport on the SPD manifold (S_++^d).

Formulations
------------
For two multivariate Gaussians ``mu_i = (m_i, Sigma_i)`` the Bures–Wasserstein
2-distance is::

    W_2^2(mu_1, mu_2) = ||m_1 - m_2||_2^2
        + Tr(Sigma_1 + Sigma_2 - 2 (Sigma_1^{1/2} Sigma_2 Sigma_1^{1/2})^{1/2})

The Monge optimal-transport map between them is affine::

    A = Sigma_1^{-1/2} (Sigma_1^{1/2} Sigma_2 Sigma_1^{1/2})^{1/2} Sigma_1^{-1/2}
    T_Monge(x) = m_2 + A (x - m_1)

``A`` is symmetric positive definite and ``A Sigma_1 A^T = Sigma_2``.

McCann's geodesic interpolation for ``t in [0, 1]`` under the ``(W_2^2, S_++^d)``
geometry is::

    m_t = (1 - t) m_1 + t m_2
    Sigma_t = ((1 - t) I + t A) Sigma_1 ((1 - t) I + t A)^T

with the metric property  ``W_2^2(mu_1, mu_t) = t^2 * W_2^2(mu_1, mu_2)``.

This module is pure NumPy/SciPy + stdlib, total and deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import scipy.linalg
from numpy.typing import NDArray


def _as_mean(mean: np.ndarray | list[float] | tuple[float, ...]) -> NDArray[np.float64]:
    arr = np.asarray(mean, dtype=float).ravel()
    if arr.size == 0:
        raise ValueError("mean must be non-empty")
    if not np.isfinite(arr).all():
        raise ValueError("mean must be finite")
    return arr


def _as_covariance(cov: np.ndarray | list[list[float]]) -> NDArray[np.float64]:
    arr = np.asarray(cov, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] != arr.shape[1]:
        raise ValueError("covariance must be square")
    if arr.size == 0:
        raise ValueError("covariance must be non-empty")
    if not np.isfinite(arr).all():
        raise ValueError("covariance must be finite")
    sym = np.asarray(0.5 * (arr + arr.T), dtype=np.float64)
    # Symmetrise and verify positive-definiteness: smallest eigenvalue > 0.
    eigs = scipy.linalg.eigvalsh(sym)
    if eigs.size == 0 or float(eigs.min()) <= 1e-12:
        raise ValueError("covariance must be positive definite")
    if not np.allclose(sym, arr, atol=1e-8):
        # Allow the upper/lower split sweep; report symmetry explicitly.
        raise ValueError("covariance must be symmetric")
    return sym


def matrix_square_root(cov: np.ndarray) -> NDArray[np.float64]:
    """Positive-definite matrix square root (unique SPD root)."""
    arr = _as_covariance(cov)
    root = scipy.linalg.sqrtm(arr)
    root = np.asarray(root, dtype=float)
    if np.iscomplexobj(root):
        if not np.allclose(root.imag, 0.0, atol=1e-10):
            raise ValueError("matrix square root is complex for a symmetric matrix")
        root = root.real
    # Numerical polish: symmetrize the tiny asymmetry the real arithmetic leaves.
    return np.asarray(0.5 * (root + root.T), dtype=np.float64)


def matrix_inverse(cov: np.ndarray) -> NDArray[np.float64]:
    """Matrix inverse for a positive-definite covariance."""
    arr = _as_covariance(cov)
    return np.asarray(np.linalg.inv(arr), dtype=np.float64)


def bures_wasserstein_2(
    mean1: np.ndarray | list[float] | tuple[float, ...],
    cov1: np.ndarray | list[list[float]],
    mean2: np.ndarray | list[float] | tuple[float, ...],
    cov2: np.ndarray | list[list[float]],
) -> float:
    """Closed-form Bures–Wasserstein-2 distance (squared).

    Returns ``W_2^2(mu_1, mu_2)`` (NOT the distance).
    """
    m1 = _as_mean(mean1)
    m2 = _as_mean(mean2)
    c1 = _as_covariance(cov1)
    c2 = _as_covariance(cov2)
    if m1.size != c1.shape[0] or m2.size != c2.shape[0]:
        raise ValueError("mean dimension must match covariance dimension")
    if c1.shape != c2.shape:
        raise ValueError("covariances must have the same shape")
    m = np.linalg.norm(m1 - m2) ** 2
    root1 = matrix_square_root(c1)
    product = root1 @ c2 @ root1
    root_product = matrix_square_root(product)
    cov_term = float(np.trace(c1) + np.trace(c2) - 2.0 * np.trace(root_product))
    return float(m + cov_term)


def monge_transport_map(
    mean1: np.ndarray | list[float] | tuple[float, ...],
    cov1: np.ndarray | list[list[float]],
    mean2: np.ndarray | list[float] | tuple[float, ...],
    cov2: np.ndarray | list[list[float]],
) -> NDArray[np.float64]:
    """Monge optimal-transport matrix ``A`` from ``(m1, C1)`` to ``(m2, C2)``.

    ``A`` depends only on the covariances; the means enter only through the
    affine map ``T(x) = m2 + A (x - m1)``.  Satisfies ``A C1 A^T = C2`` with
    ``A`` symmetric positive definite.
    """
    m1 = _as_mean(mean1)
    m2 = _as_mean(mean2)
    c1 = _as_covariance(cov1)
    c2 = _as_covariance(cov2)
    if m1.size != c1.shape[0] or m2.size != c2.shape[0]:
        raise ValueError("mean dimension must match covariance dimension")
    if c1.shape != c2.shape:
        raise ValueError("covariances must have the same shape")
    root1 = matrix_square_root(c1)
    c1_inv_half = np.linalg.inv(root1)
    product = root1 @ c2 @ root1
    root_product = matrix_square_root(product)
    a = c1_inv_half @ root_product @ c1_inv_half
    return 0.5 * (a + a.T)


def monge_map_tensor(
    mean2: np.ndarray | list[float] | tuple[float, ...],
    transport_matrix: np.ndarray,
    mean1: np.ndarray | list[float] | tuple[float, ...],
) -> Callable[[np.ndarray | list[float] | tuple[float, ...]], NDArray[np.float64]]:
    """Return the affine Monge map ``T(x) = m_2 + A (x - m_1)`` as a callable."""
    m1 = _as_mean(mean1)
    m2 = _as_mean(mean2)
    a = np.asarray(transport_matrix, dtype=float)
    if a.shape[0] != m1.size or a.shape[1] != m1.size:
        raise ValueError("transport matrix must match mean dimension")

    def map_fn(x: np.ndarray | list[float] | tuple[float, ...]) -> NDArray[np.float64]:
        arr = np.asarray(x, dtype=float).ravel()
        if arr.size != m1.size:
            raise ValueError("input must match mean dimension")
        return m2 + a @ (arr - m1)

    return map_fn


def mccann_geodesic(
    t: float,
    mean1: np.ndarray | list[float] | tuple[float, ...],
    cov1: np.ndarray | list[list[float]],
    mean2: np.ndarray | list[float] | tuple[float, ...],
    cov2: np.ndarray | list[list[float]],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """McCann geodesic interpolation at ``t in [0, 1]``.

    Returns ``(mean_t, cov_t)`` with ``mean_t = (1-t) m_1 + t m_2`` and
    ``cov_t = ((1-t) I + t A) C_1 ((1-t) I + t A)^T`` where ``A`` is the Monge map.
    """
    if not (0.0 <= t <= 1.0):
        raise ValueError("t must lie in [0, 1]")
    m1 = _as_mean(mean1)
    m2 = _as_mean(mean2)
    c1 = _as_covariance(cov1)
    c2 = _as_covariance(cov2)
    if c1.shape != c2.shape:
        raise ValueError("covariances must have the same shape")
    a = monge_transport_map(m1, c1, m2, c2)
    beta = (1.0 - t) * np.eye(c1.shape[0]) + t * a
    mean_t = (1.0 - t) * m1 + t * m2
    cov_t = beta @ c1 @ beta.T
    return mean_t, 0.5 * (cov_t + cov_t.T)


def optimal_transport_distance(
    mean1: np.ndarray | list[float] | tuple[float, ...],
    cov1: np.ndarray | list[list[float]],
    mean2: np.ndarray | list[float] | tuple[float, ...],
    cov2: np.ndarray | list[list[float]],
) -> float:
    """The Wasserstein-2 distance (square root of the squared distance)."""
    w2_sq = bures_wasserstein_2(mean1, cov1, mean2, cov2)
    return float(math.sqrt(max(w2_sq, 0.0)))


__all__ = [
    "matrix_square_root",
    "matrix_inverse",
    "bures_wasserstein_2",
    "optimal_transport_distance",
    "monge_transport_map",
    "monge_map_tensor",
    "mccann_geodesic",
]
