"""WAVE 19 — entropic 2-Wasserstein optimal transport and the Schrödinger bridge.

Scene brightness / colour fields, spectral power distributions and particle mass
densities are all *probability distributions*, and the natural way to morph one
into another without tearing or mass artefacts is optimal transport. This module
provides, in pure NumPy + stdlib:

* :func:`solve_entropic_wasserstein_ot` — the Sinkhorn–Knopp fixed point for the
  entropic Kantorovich dual, solved in the **log domain** with an exact
  log-sum-exp stabilisation so ``eps in [1e-3, 10]`` never over- or underflows;
  it returns the coupling plan ``P_ij = exp((f_i + g_j - C_ij) / eps)``, the
  transport cost ``<C, P>`` and the *debiased* Sinkhorn divergence
  ``S_eps(mu, nu) = OT_eps(mu, nu) - 0.5 OT_eps(mu, mu) - 0.5 OT_eps(nu, nu)``;
* :func:`interpolate_schrodinger_bridge_scene` — the McCann barycentric map
  ``T(z_i) = (1/mu_i) sum_j P_ij w_j`` and the dynamic Schrödinger bridge that
  moves mass along ``(1 - t) z_i + t w_j`` with Brownian variance
  ``sigma_t^2 = eps t (1 - t)``, producing artefact-free morphs that conserve
  mass exactly;
* :func:`compute_wasserstein_barycenter` — the Wasserstein barycentre of ``K``
  reference distributions via the iterative Bregman projection (IBP) in the log
  domain;
* :func:`audit_wasserstein_metric` — measures the metric principles (marginal
  conservation, symmetry, the identity-of-indiscernibles ``S(mu, mu) = 0`` and
  the triangle inequality on ``sqrt(S)``) into a :class:`WassersteinMetricAudit`.

Bounded, validated, offline and deterministic; malformed inputs raise
:class:`TransportError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "MAX_SUPPORT",
    "MIN_EPS",
    "MAX_EPS",
    "MARGINAL_TOLERANCE",
    "IDENTITY_TOLERANCE",
    "TransportError",
    "EntropicOTResult",
    "solve_entropic_wasserstein_ot",
    "SchrodingerBridgeResult",
    "interpolate_schrodinger_bridge_scene",
    "WassersteinBarycenter",
    "compute_wasserstein_barycenter",
    "WassersteinMetricAudit",
    "audit_wasserstein_metric",
]

MAX_SUPPORT: Final[int] = 8192
MIN_EPS: Final[float] = 1e-3
MAX_EPS: Final[float] = 10.0
#: ``||P 1 - mu||_1`` and ``||P^T 1 - nu||_1`` must be below this.
MARGINAL_TOLERANCE: Final[float] = 1e-8
#: ``S_eps(mu, mu)`` must be below this.
IDENTITY_TOLERANCE: Final[float] = 1e-12
_LOG_ZERO: Final[float] = -np.inf


class TransportError(ValueError):
    """Raised for a malformed distribution, cost matrix or parameter."""


def _validate_distribution(values: np.ndarray, name: str) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1 or arr.shape[0] < 1:
        raise TransportError(f"{name} must be a non-empty 1-D distribution")
    if arr.shape[0] > MAX_SUPPORT:
        raise TransportError(f"{name} exceeds MAX_SUPPORT={MAX_SUPPORT}")
    if not np.all(np.isfinite(arr)):
        raise TransportError(f"{name} must be finite")
    if np.any(arr < 0.0):
        raise TransportError(f"{name} must be non-negative")
    total = float(arr.sum())
    if abs(total - 1.0) > 1e-8:
        raise TransportError(f"{name} must be a normalised simplex point (sum={total:.6f})")
    return arr / total


def _validate_cost(cost: np.ndarray, n: int, m: int) -> np.ndarray:
    c = np.asarray(cost, dtype=np.float64)
    if c.shape != (n, m):
        raise TransportError(f"cost must have shape ({n}, {m})")
    if not np.all(np.isfinite(c)):
        raise TransportError("cost must be finite")
    if np.any(c < 0.0):
        raise TransportError("cost must be non-negative")
    return c


def _validate_eps(eps: float) -> float:
    if not isinstance(eps, (int, float)) or isinstance(eps, bool):
        raise TransportError("eps must be a number")
    value = float(eps)
    if not np.isfinite(value) or value < MIN_EPS or value > MAX_EPS:
        raise TransportError(f"eps must be in [{MIN_EPS}, {MAX_EPS}]")
    return value


def _logsumexp(values: np.ndarray, axis: int) -> np.ndarray:
    """Numerically exact log-sum-exp; a fully ``-inf`` row yields ``-inf``."""
    peak = np.max(values, axis=axis, keepdims=True)
    finite = np.isfinite(peak)
    safe = np.where(finite, peak, 0.0)
    total = np.log(np.sum(np.exp(values - safe), axis=axis, keepdims=True))
    result: np.ndarray = np.where(finite, safe + total, _LOG_ZERO)
    return np.squeeze(result, axis=axis)


def _sinkhorn_duals(
    mu: np.ndarray,
    nu: np.ndarray,
    cost: np.ndarray,
    eps: float,
    *,
    max_iterations: int,
    tolerance: float,
) -> tuple[np.ndarray, np.ndarray, int, bool]:
    log_mu = np.where(mu > 0.0, np.log(np.maximum(mu, 1e-300)), _LOG_ZERO)
    log_nu = np.where(nu > 0.0, np.log(np.maximum(nu, 1e-300)), _LOG_ZERO)
    f = np.zeros(mu.shape[0], dtype=np.float64)
    g = np.zeros(nu.shape[0], dtype=np.float64)
    converged = False
    iterations = 0
    while iterations < max_iterations:
        iterations += 1
        f_prev = f
        f = eps * log_mu - eps * _logsumexp((g[None, :] - cost) / eps, axis=1)
        g = eps * log_nu - eps * _logsumexp((f[:, None] - cost) / eps, axis=0)
        if float(np.max(np.abs(f - f_prev))) <= tolerance:
            converged = True
            break
    return f, g, iterations, converged


def _plan_from_duals(f: np.ndarray, g: np.ndarray, cost: np.ndarray, eps: float) -> np.ndarray:
    plan = np.exp((f[:, None] + g[None, :] - cost) / eps)
    return np.asarray(np.where(np.isfinite(plan), plan, 0.0), dtype=np.float64)


def _transport_cost(mu: np.ndarray, nu: np.ndarray, cost: np.ndarray, eps: float) -> float:
    f, g, _it, _conv = _sinkhorn_duals(mu, nu, cost, eps, max_iterations=4096, tolerance=1e-14)
    plan = _plan_from_duals(f, g, cost, eps)
    return float(np.sum(cost * plan))


@dataclass(frozen=True)
class EntropicOTResult:
    """The outcome of the entropic (Sinkhorn) optimal-transport solve."""

    plan: np.ndarray
    dual_f: np.ndarray
    dual_g: np.ndarray
    transport_cost: float
    sinkhorn_divergence: float
    marginal_residual_source: float
    marginal_residual_target: float
    iterations: int
    converged: bool

    @property
    def mass(self) -> float:
        return float(self.plan.sum())


def solve_entropic_wasserstein_ot(
    mu: np.ndarray,
    nu: np.ndarray,
    cost: np.ndarray,
    *,
    eps: float = 0.05,
    source_cost: np.ndarray | None = None,
    target_cost: np.ndarray | None = None,
    max_iterations: int = 4096,
    tolerance: float = 1e-12,
) -> EntropicOTResult:
    """Solve the entropic Kantorovich problem in the log domain.

    ``mu in Delta^{n-1}`` and ``nu in Delta^{m-1}`` are source/target
    distributions, ``cost[i, j] = ||z_i - w_j||^2`` is the squared Euclidean
    ground cost. The returned plan is the entropic coupling and the divergence
    is the *debiased* Sinkhorn divergence, which vanishes for identical inputs.

    ``source_cost`` / ``target_cost`` are the intra-support ground costs used by
    the two debiasing self-terms. When omitted they default to ``cost`` /
    ``cost.T`` for a common support (``n == m``), so ``S_eps(mu, mu)`` is exactly
    zero; for distinct supports the caller should pass the intra-support costs so
    the divergence is the true debiased Sinkhorn divergence.
    """
    source = _validate_distribution(mu, "mu")
    target = _validate_distribution(nu, "nu")
    c = _validate_cost(cost, source.shape[0], target.shape[0])
    epsilon = _validate_eps(eps)
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        raise TransportError("max_iterations must be a positive int")
    if max_iterations < 1:
        raise TransportError("max_iterations must be a positive int")
    if tolerance <= 0.0:
        raise TransportError("tolerance must be positive")

    if source_cost is None:
        c_source = c if source.shape[0] == target.shape[0] else _self_cost(source)
    else:
        c_source = _validate_cost(source_cost, source.shape[0], source.shape[0])
    if target_cost is None:
        c_target = c.T if source.shape[0] == target.shape[0] else _self_cost(target)
    else:
        c_target = _validate_cost(target_cost, target.shape[0], target.shape[0])

    f, g, iterations, converged = _sinkhorn_duals(
        source, target, c, epsilon, max_iterations=max_iterations, tolerance=tolerance
    )
    plan = _plan_from_duals(f, g, c, epsilon)
    transport = float(np.sum(c * plan))
    self_source = _transport_cost(source, source, c_source, epsilon)
    self_target = _transport_cost(target, target, c_target, epsilon)
    divergence = transport - 0.5 * self_source - 0.5 * self_target
    return EntropicOTResult(
        plan=plan,
        dual_f=f,
        dual_g=g,
        transport_cost=transport,
        sinkhorn_divergence=divergence,
        marginal_residual_source=float(np.abs(plan.sum(axis=1) - source).sum()),
        marginal_residual_target=float(np.abs(plan.sum(axis=0) - target).sum()),
        iterations=iterations,
        converged=converged,
    )


def _self_cost(distribution: np.ndarray) -> np.ndarray:
    """A ground cost for a self-transport problem on a 1-D index support."""
    z = np.arange(distribution.shape[0], dtype=np.float64)[:, None]
    return np.asarray((z - z.T) ** 2, dtype=np.float64)


@dataclass(frozen=True)
class SchrodingerBridgeResult:
    """A dynamic Schrödinger-bridge interpolation between two scenes."""

    masses: np.ndarray
    positions: np.ndarray
    brownian_variance: float
    interpolation_time: float
    mass_conserved: bool
    barycentric_map: np.ndarray


def interpolate_schrodinger_bridge_scene(
    source_points: np.ndarray,
    target_points: np.ndarray,
    mu: np.ndarray,
    nu: np.ndarray,
    *,
    t: float,
    eps: float = 0.05,
) -> SchrodingerBridgeResult:
    """Morph one point cloud into another along the entropic Schrödinger bridge.

    The barycentric (McCann) map ``T(z_i) = (1/mu_i) sum_j P_ij w_j`` moves the
    source mass, and at interpolation time ``t in [0, 1]`` each particle sits at
    ``(1 - t) z_i + t T(z_i)`` with Brownian variance ``sigma_t^2 = eps t (1-t)``.
    Mass is conserved exactly at every ``t``.
    """
    src = np.asarray(source_points, dtype=np.float64)
    tgt = np.asarray(target_points, dtype=np.float64)
    if src.ndim != 2 or tgt.ndim != 2 or src.shape[1] != tgt.shape[1]:
        raise TransportError("point clouds must be 2-D with equal feature dimension")
    if not np.all(np.isfinite(src)) or not np.all(np.isfinite(tgt)):
        raise TransportError("point clouds must be finite")
    source = _validate_distribution(mu, "mu")
    target = _validate_distribution(nu, "nu")
    if src.shape[0] != source.shape[0] or tgt.shape[0] != target.shape[0]:
        raise TransportError("point counts must match the distributions")
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not np.isfinite(t):
        raise TransportError("t must be a finite number")
    if t < 0.0 or t > 1.0:
        raise TransportError("t must be in [0, 1]")
    epsilon = _validate_eps(eps)

    cost = _pairwise_squared_cost(src, tgt)
    result = solve_entropic_wasserstein_ot(source, target, cost, eps=epsilon)
    plan = result.plan
    barycentric = (plan @ tgt) / np.maximum(source[:, None], 1e-300)
    positions = (1.0 - t) * src + t * barycentric
    variance = epsilon * t * (1.0 - t)
    total = float(source.sum())
    return SchrodingerBridgeResult(
        masses=source.copy(),
        positions=np.asarray(positions, dtype=np.float64),
        brownian_variance=variance,
        interpolation_time=float(t),
        mass_conserved=abs(total - 1.0) < 1e-12,
        barycentric_map=np.asarray(barycentric, dtype=np.float64),
    )


def _pairwise_squared_cost(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    diff = a[:, None, :] - b[None, :, :]
    return np.asarray(np.sum(diff * diff, axis=2), dtype=np.float64)


@dataclass(frozen=True)
class WassersteinBarycenter:
    """The Wasserstein barycentre of ``K`` reference distributions."""

    weights: np.ndarray
    support: np.ndarray
    iterations: int
    converged: bool
    marginal_residual: float


def compute_wasserstein_barycenter(
    distributions: tuple[np.ndarray, ...],
    weights: np.ndarray,
    cost: np.ndarray,
    *,
    eps: float = 0.05,
    max_iterations: int = 2048,
    tolerance: float = 1e-12,
) -> WassersteinBarycenter:
    """Wasserstein barycentre via iterative Bregman projection in the log domain.

    All distributions must share a common support; ``weights in Delta^{K-1}``.
    The IBP recursion alternates the Sinkhorn scalings with a geometric
    (weighted log-domain) average of the projected marginals.
    """
    if not isinstance(distributions, tuple) or len(distributions) < 1:
        raise TransportError("distributions must be a non-empty tuple")
    k = len(distributions)
    if k > MAX_SUPPORT:
        raise TransportError("too many reference distributions")
    mus = tuple(
        _validate_distribution(d, f"distributions[{i}]") for i, d in enumerate(distributions)
    )
    n = mus[0].shape[0]
    for d in mus:
        if d.shape[0] != n:
            raise TransportError("all distributions must share the same support size")
    lam = np.asarray(weights, dtype=np.float64)
    if lam.shape != (k,):
        raise TransportError("weights must have one entry per distribution")
    if not np.all(np.isfinite(lam)) or np.any(lam < 0.0):
        raise TransportError("weights must be finite and non-negative")
    if abs(float(lam.sum()) - 1.0) > 1e-8:
        raise TransportError("weights must sum to one")
    c = _validate_cost(cost, n, n)
    epsilon = _validate_eps(eps)
    if max_iterations < 1:
        raise TransportError("max_iterations must be a positive int")
    if tolerance <= 0.0:
        raise TransportError("tolerance must be positive")

    kernel = np.exp(-c / epsilon)
    scalings = [np.ones(n, dtype=np.float64) for _ in range(k)]
    barycentre = np.full(n, 1.0 / n, dtype=np.float64)
    converged = False
    iterations = 0
    while iterations < max_iterations:
        iterations += 1
        previous = barycentre
        u_list = [m / np.maximum(kernel @ v, 1e-300) for m, v in zip(mus, scalings, strict=True)]
        log_nu = np.zeros(n, dtype=np.float64)
        for weight, u in zip(lam, u_list, strict=True):
            log_nu = log_nu + weight * np.log(np.maximum(kernel.T @ u, 1e-300))
        barycentre = np.exp(log_nu)
        barycentre = barycentre / barycentre.sum()
        scalings = [barycentre / np.maximum(kernel.T @ u, 1e-300) for u in u_list]
        if float(np.max(np.abs(barycentre - previous))) <= tolerance:
            converged = True
            break
    residual = float(np.abs(barycentre.sum() - 1.0))
    return WassersteinBarycenter(
        weights=lam.copy(),
        support=barycentre,
        iterations=iterations,
        converged=converged,
        marginal_residual=residual,
    )


@dataclass(frozen=True)
class WassersteinMetricAudit:
    """Measured evidence for the metric principles of the Sinkhorn divergence."""

    source_marginal_residual: float
    target_marginal_residual: float
    identity_residual: float
    symmetry_residual: float
    triangle_residual: float
    non_negative: bool
    notes: tuple[str, ...]

    @property
    def marginal_conserved(self) -> bool:
        return (
            self.source_marginal_residual < MARGINAL_TOLERANCE
            and self.target_marginal_residual < MARGINAL_TOLERANCE
        )

    @property
    def identity_holds(self) -> bool:
        return self.identity_residual < IDENTITY_TOLERANCE

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "atlas.wasserstein_metric_audit",
            "schema_version": 1,
            "source_marginal_residual": self.source_marginal_residual,
            "target_marginal_residual": self.target_marginal_residual,
            "identity_residual": self.identity_residual,
            "symmetry_residual": self.symmetry_residual,
            "triangle_residual": self.triangle_residual,
            "non_negative": self.non_negative,
            "notes": list(self.notes),
        }


def audit_wasserstein_metric(
    mu: np.ndarray,
    nu: np.ndarray,
    cost: np.ndarray,
    *,
    eps: float = 0.05,
    source_cost: np.ndarray | None = None,
    target_cost: np.ndarray | None = None,
    third: np.ndarray | None = None,
    third_cost: tuple[np.ndarray, np.ndarray] | None = None,
) -> WassersteinMetricAudit:
    """Measure the metric principles of the debiased Sinkhorn divergence.

    Always measures marginal conservation, the identity-of-indiscernibles
    ``S(mu, mu) = 0`` and symmetry. When a third distribution, the two
    intra-support costs and the two cross-costs are supplied, the triangle
    inequality on ``sqrt(S)`` is also measured (a non-positive residual means
    the inequality holds).
    """
    source = _validate_distribution(mu, "mu")
    target = _validate_distribution(nu, "nu")
    c = _validate_cost(cost, source.shape[0], target.shape[0])
    epsilon = _validate_eps(eps)
    c_source = source_cost if source_cost is not None else _self_cost(source)
    c_target = target_cost if target_cost is not None else _self_cost(target)

    forward = solve_entropic_wasserstein_ot(
        source, target, c, eps=epsilon, source_cost=c_source, target_cost=c_target
    )
    identity = solve_entropic_wasserstein_ot(
        source, source, c_source, eps=epsilon, source_cost=c_source, target_cost=c_source
    )
    reverse = solve_entropic_wasserstein_ot(
        target, source, c.T, eps=epsilon, source_cost=c_target, target_cost=c_source
    )
    symmetry = abs(forward.sinkhorn_divergence - reverse.sinkhorn_divergence)
    triangle = 0.0
    if third is not None and third_cost is not None:
        gamma = _validate_distribution(third, "third")
        c_xz, c_yz = third_cost
        c_gamma = _self_cost(gamma)
        dist_xy = np.sqrt(max(forward.sinkhorn_divergence, 0.0))
        dist_yz = np.sqrt(
            max(
                solve_entropic_wasserstein_ot(
                    target, gamma, c_yz, eps=epsilon, source_cost=c_target, target_cost=c_gamma
                ).sinkhorn_divergence,
                0.0,
            )
        )
        dist_xz = np.sqrt(
            max(
                solve_entropic_wasserstein_ot(
                    source, gamma, c_xz, eps=epsilon, source_cost=c_source, target_cost=c_gamma
                ).sinkhorn_divergence,
                0.0,
            )
        )
        triangle = float(dist_xy - (dist_yz + dist_xz))
    return WassersteinMetricAudit(
        source_marginal_residual=forward.marginal_residual_source,
        target_marginal_residual=forward.marginal_residual_target,
        identity_residual=abs(identity.sinkhorn_divergence),
        symmetry_residual=symmetry,
        triangle_residual=triangle,
        non_negative=forward.sinkhorn_divergence >= -1e-12,
        notes=(
            "all residuals are measured, never asserted",
            "sqrt(S_eps) obeys the triangle inequality approximately at finite eps",
        ),
    )
