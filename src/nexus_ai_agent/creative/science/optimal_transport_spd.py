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

Policy note — the ``lambda_max / lambda_min <= 1e10`` ill-conditioning floor in
``_as_covariance`` is a **finite-precision stability policy**, not an intrinsic
mathematical necessity: the SPD manifold is perfectly regular for strictly
positive-definite covariances of arbitrarily high condition number.  The floor
exists so that the central differences taken from the eigendecomposition stay
meaningful inside this module's precision envelope (LU/eigh with double
precision cannot trust eigenvalues spanning more than ~10 orders of magnitude);
it is deliberately fail-closed and must be documented as a numerical-stability
cutoff, never as a geometric invariant.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray


def _as_mean(mean: np.ndarray | list[float] | tuple[float, ...]) -> NDArray[np.float64]:
    arr = np.asarray(mean).ravel()
    if np.iscomplexobj(arr):
        raise ValueError("mean must be real (complex input rejected)")
    arr = np.asarray(mean, dtype=float).ravel()
    if arr.size == 0:
        raise ValueError("mean must be non-empty")
    if not np.isfinite(arr).all():
        raise ValueError("mean must be finite")
    return arr


def _as_covariance(cov: np.ndarray | list[list[float]]) -> NDArray[np.float64]:
    """Validate a covariance matrix and return its symmetrised (real, SPD-checked) form.

    Fail-closed checks in fixed order: complex input -> rejected; non-square /
    empty shape -> rejected; non-finite entries -> rejected; asymmetry ->
    rejected *before* any symmetrization (the naive upper/lower-triangle
    eigvalsh check cannot catch an asymmetric matrix); then eigenvalues
    computed on the symmetrised matrix with ``lambda_min > 0`` required and an
    ill-conditioning floor (``lambda_max / lambda_min <= 1e10``).

    The ``1e10`` floor is a finite-precision stability policy, not an intrinsic
    mathematical requirement of the Bures–Wasserstein geometry — strictly
    positive-definite covariances of arbitrary condition number are valid
    points on the SPD manifold.  It is enforced so that the eigendecomposition
    stays inside double precision's envelope; it is intentionally fail-closed,
    not a geometric invariant.
    """
    if np.iscomplexobj(cov):
        raise ValueError("covariance must be real (complex input rejected)")
    arr = np.asarray(cov)
    if not np.issubdtype(arr.dtype, np.number) or np.iscomplexobj(arr):
        raise ValueError("covariance must be a real numeric matrix")
    arr = np.asarray(arr, dtype=float)
    if arr.ndim != 2 or arr.shape[0] != arr.shape[1]:
        raise ValueError("covariance must be square")
    if arr.size == 0:
        raise ValueError("covariance must be non-empty")
    if not np.isfinite(arr).all():
        raise ValueError("covariance must be finite")
    # Symmetry gate BEFORE any symmetrization.
    asymmetry = float(np.max(np.abs(arr - arr.T))) if arr.size else 0.0
    scale = float(np.max(np.abs(arr))) if arr.size else 0.0
    if asymmetry > 1e-12 + 1e-10 * scale:
        raise ValueError(f"covariance must be symmetric (max |K - K^T| = {asymmetry:.3e})")
    sym = np.asarray(0.5 * (arr + arr.T), dtype=np.float64)
    eigs, _ = np.linalg.eigh(sym)
    lam_min = float(eigs.min())
    lam_max = float(eigs.max())
    if lam_min <= 0.0:
        raise ValueError("covariance must be positive definite (lambda_min <= 0)")
    if lam_max / lam_min > 1e10:
        raise ValueError(
            f"covariance is ill-conditioned (lambda_max / lambda_min = "
            f"{lam_max / lam_min:.3e} > 1e10)"
        )
    return sym


def matrix_square_root(cov: np.ndarray) -> NDArray[np.float64]:
    """Positive-definite matrix square root (unique SPD root).

    Computed via eigen-decomposition ``Sigma = Q Lambda Q^T`` and ``R = Q sqrt(Lambda) Q^T``;
    no complex roots are ever produced (the input is validated as SPD beforehand).
    """
    arr = _as_covariance(cov)
    eigvals, eigvecs = np.linalg.eigh(arr)
    root = eigvecs @ np.diag(np.sqrt(eigvals)) @ eigvecs.T
    root = np.asarray(0.5 * (root + root.T), dtype=np.float64)
    # Defensive invariant: validated SPD input in real arithmetic cannot
    # produce a complex/non-finite root — this guards against numerical drift
    # in the LAPACK path, not expected states.
    if np.iscomplexobj(root) or not np.isfinite(root).all():  # pragma: no cover
        raise ValueError("matrix square root is complex or non-finite for a validated SPD matrix")
    return root


def matrix_inverse_square_root(cov: np.ndarray) -> NDArray[np.float64]:
    """SPD inverse square root ``Sigma^{-1/2}`` via eigen-decomposition."""
    arr = _as_covariance(cov)
    eigvals, eigvecs = np.linalg.eigh(arr)
    root = eigvecs @ np.diag(1.0 / np.sqrt(eigvals)) @ eigvecs.T
    root = np.asarray(0.5 * (root + root.T), dtype=np.float64)
    # Defensive invariant (see matrix_square_root).
    if np.iscomplexobj(root) or not np.isfinite(root).all():  # pragma: no cover
        raise ValueError("matrix inverse square root is complex or non-finite")
    return root


def matrix_inverse(cov: np.ndarray) -> NDArray[np.float64]:
    """Matrix inverse for a positive-definite covariance (eigen-decomposition-based)."""
    arr = _as_covariance(cov)
    eigvals, eigvecs = np.linalg.eigh(arr)
    return np.asarray(eigvecs @ np.diag(1.0 / eigvals) @ eigvecs.T, dtype=np.float64)


def bures_wasserstein_2(
    mean1: np.ndarray | list[float] | tuple[float, ...],
    cov1: np.ndarray | list[list[float]],
    mean2: np.ndarray | list[float] | tuple[float, ...],
    cov2: np.ndarray | list[list[float]],
) -> float:
    """Closed-form Bures–Wasserstein-2 distance (squared).

    Returns ``W_2^2(mu_1, mu_2)`` (NOT the distance).  Slightly negative values
    are machine noise and are clamped to ``0.0``; larger negatives are reported
    or raised (fail-closed).
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
    w2_sq = float(m + cov_term)
    # Clamp machine-noise negativity to 0; report (raise on) anything bigger.
    scale = float(np.trace(c1) + np.trace(c2) + m)
    neg_tol = 1e-12 * max(scale, 1.0)
    if w2_sq < 0.0:
        if w2_sq >= -neg_tol:
            return 0.0
        # Defensive: the closed form is non-negative for valid SPD input.
        raise ValueError(  # pragma: no cover
            f"W_2^2 came out negative by {-w2_sq:.3e} (tolerance {neg_tol:.3e}); "
            "this indicates a numerical failure, refusing to report a bogus distance"
        )
    return w2_sq


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
    c1_inv_half = matrix_inverse_square_root(c1)
    product = root1 @ c2 @ root1
    root_product = matrix_square_root(product)
    a = c1_inv_half @ root_product @ c1_inv_half
    a = np.asarray(0.5 * (a + a.T), dtype=np.float64)
    # Structural checks before returning: symmetry not just on the off-diagonals.
    if float(np.max(np.abs(a - a.T))) > 1e-10:  # pragma: no cover
        raise ValueError("Monge map is not symmetric (numerical failure)")
    a_eigs = np.linalg.eigvalsh(a)
    if float(a_eigs.min()) <= 0.0:  # pragma: no cover
        raise ValueError("Monge map is not positive definite (numerical failure)")
    residual = float(np.linalg.norm(a @ c1 @ a.T - c2, ord="fro"))
    target = float(np.linalg.norm(c2, ord="fro"))
    if residual > 1e-8 * max(target, 1.0):  # pragma: no cover
        raise ValueError(f"Monge map violates A C_1 A^T = C_2 (residual = {residual:.3e})")
    return a


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
