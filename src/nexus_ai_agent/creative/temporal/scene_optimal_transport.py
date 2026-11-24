"""Bures-Wasserstein optimal transport for colour and film photochemistry.

A scene's colour distribution is modelled as a Gaussian on RGB: a mean and a
3x3 covariance in the symmetric positive-definite cone.  The Monge-Kantorovich
map between source and reference is then exact, and the Bures geodesic gives a
principled interpolation path.  Hurter-Driffield supplies the photochemical
density response used when a look emulates film.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

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


class OptimalTransportError(ValueError):
    """Raised when a matrix is not symmetric positive-definite or input is invalid."""


Matrix3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
Vector3 = tuple[float, float, float]

_IDENTITY: Final[Matrix3] = (
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
)


def _as_matrix(matrix: Sequence[Sequence[float]]) -> Matrix3:
    if len(matrix) != 3 or any(len(row) != 3 for row in matrix):
        raise OptimalTransportError("matrix must be 3x3")
    rows: list[tuple[float, float, float]] = []
    for row in matrix:
        values = []
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OptimalTransportError("matrix entries must be real numbers")
            number = float(value)
            if not math.isfinite(number):
                raise OptimalTransportError("matrix entries must be finite")
            values.append(number)
        rows.append((values[0], values[1], values[2]))
    return rows[0], rows[1], rows[2]


def _as_vector(vector: Sequence[float]) -> Vector3:
    if len(vector) != 3:
        raise OptimalTransportError("vector must have three components")
    values = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OptimalTransportError("vector entries must be real numbers")
        number = float(value)
        if not math.isfinite(number):
            raise OptimalTransportError("vector entries must be finite")
        values.append(number)
    return values[0], values[1], values[2]


def _symmetrise(matrix: Matrix3) -> Matrix3:
    return (
        (matrix[0][0], 0.5 * (matrix[0][1] + matrix[1][0]), 0.5 * (matrix[0][2] + matrix[2][0])),
        (0.5 * (matrix[0][1] + matrix[1][0]), matrix[1][1], 0.5 * (matrix[1][2] + matrix[2][1])),
        (0.5 * (matrix[0][2] + matrix[2][0]), 0.5 * (matrix[1][2] + matrix[2][1]), matrix[2][2]),
    )


def _matmul(first: Matrix3, second: Matrix3) -> Matrix3:
    rows = []
    for row in range(3):
        rows.append(
            tuple(sum(first[row][k] * second[k][column] for k in range(3)) for column in range(3))
        )
    return rows[0], rows[1], rows[2]  # type: ignore[return-value]


def _add(
    first: Matrix3, second: Matrix3, *, scale_first: float = 1.0, scale_second: float = 1.0
) -> Matrix3:
    return tuple(
        tuple(
            scale_first * first[row][column] + scale_second * second[row][column]
            for column in range(3)
        )
        for row in range(3)
    )  # type: ignore[return-value]


def _trace(matrix: Matrix3) -> float:
    return matrix[0][0] + matrix[1][1] + matrix[2][2]


def jacobi_eigh(matrix: Sequence[Sequence[float]]) -> tuple[tuple[float, float, float], Matrix3]:
    """Jacobi eigendecomposition of a symmetric 3x3 matrix.

    Returns eigenvalues and eigenvectors as the *columns* of the second value.
    """

    raw = _as_matrix(matrix)
    if any(abs(raw[i][j] - raw[j][i]) > 1e-9 for i in range(3) for j in range(3)):
        raise OptimalTransportError("matrix must be symmetric")
    base = _symmetrise(raw)
    work = [list(row) for row in base]
    vectors = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for _ in range(100):  # pragma: no branch - Jacobi always converges
        off_diagonal = abs(work[0][1]) + abs(work[0][2]) + abs(work[1][2])
        if off_diagonal < 1e-15:
            break
        for p, q in ((0, 1), (0, 2), (1, 2)):
            if abs(work[p][q]) < 1e-18:
                continue
            app, aqq, apq = work[p][p], work[q][q], work[p][q]
            tau = (aqq - app) / (2.0 * apq)
            if tau == 0.0:
                tangent = 1.0
            else:
                tangent = math.copysign(1.0, tau) / (abs(tau) + math.sqrt(tau * tau + 1.0))
            cosine = 1.0 / math.sqrt(tangent * tangent + 1.0)
            sine = tangent * cosine
            for k in range(3):
                if k == p or k == q:
                    continue
                akp, akq = work[k][p], work[k][q]
                work[k][p] = work[p][k] = cosine * akp - sine * akq
                work[k][q] = work[q][k] = sine * akp + cosine * akq
            work[p][p] = cosine * cosine * app - 2.0 * sine * cosine * apq + sine * sine * aqq
            work[q][q] = sine * sine * app + 2.0 * sine * cosine * apq + cosine * cosine * aqq
            work[p][q] = work[q][p] = 0.0
            for k in range(3):
                vkp, vkq = vectors[k][p], vectors[k][q]
                vectors[k][p] = cosine * vkp - sine * vkq
                vectors[k][q] = sine * vkp + cosine * vkq
    eigenvalues = (work[0][0], work[1][1], work[2][2])
    eigenvectors: Matrix3 = (
        (vectors[0][0], vectors[0][1], vectors[0][2]),
        (vectors[1][0], vectors[1][1], vectors[1][2]),
        (vectors[2][0], vectors[2][1], vectors[2][2]),
    )
    return eigenvalues, eigenvectors


def _eigen_transform(
    eigenvalues: tuple[float, float, float], eigenvectors: Matrix3, transform: list[float]
) -> Matrix3:
    diagonal: Matrix3 = (
        (transform[0], 0.0, 0.0),
        (0.0, transform[1], 0.0),
        (0.0, 0.0, transform[2]),
    )
    transposed: Matrix3 = (
        (eigenvectors[0][0], eigenvectors[1][0], eigenvectors[2][0]),
        (eigenvectors[0][1], eigenvectors[1][1], eigenvectors[2][1]),
        (eigenvectors[0][2], eigenvectors[1][2], eigenvectors[2][2]),
    )
    return _symmetrise(_matmul(_matmul(eigenvectors, diagonal), transposed))


def matrix_sqrt_psd(matrix: Sequence[Sequence[float]]) -> Matrix3:
    eigenvalues, eigenvectors = jacobi_eigh(matrix)
    if min(eigenvalues) < -1e-9:
        raise OptimalTransportError("matrix is not positive semi-definite")
    return _eigen_transform(
        eigenvalues, eigenvectors, [math.sqrt(max(value, 0.0)) for value in eigenvalues]
    )


def matrix_inverse_spd(matrix: Sequence[Sequence[float]]) -> Matrix3:
    eigenvalues, eigenvectors = jacobi_eigh(matrix)
    if min(eigenvalues) <= 1e-12:
        raise OptimalTransportError("matrix is not strictly positive-definite")
    return _eigen_transform(eigenvalues, eigenvectors, [1.0 / value for value in eigenvalues])


def matrix_inverse_sqrt_spd(matrix: Sequence[Sequence[float]]) -> Matrix3:
    eigenvalues, eigenvectors = jacobi_eigh(matrix)
    if min(eigenvalues) <= 1e-12:
        raise OptimalTransportError("matrix is not strictly positive-definite")
    return _eigen_transform(
        eigenvalues, eigenvectors, [1.0 / math.sqrt(value) for value in eigenvalues]
    )


@dataclass(frozen=True, slots=True)
class GaussianColorModel:
    mean: Vector3
    covariance: Matrix3

    def __post_init__(self) -> None:
        object.__setattr__(self, "mean", _as_vector(self.mean))
        covariance = _as_matrix(self.covariance)
        if any(abs(covariance[i][j] - covariance[j][i]) > 1e-9 for i in range(3) for j in range(3)):
            raise OptimalTransportError("covariance must be symmetric")
        object.__setattr__(self, "covariance", covariance)

    def regularized(self, ridge: float = 1e-6) -> GaussianColorModel:
        if not math.isfinite(ridge) or ridge < 0.0:
            raise OptimalTransportError("ridge must be finite and non-negative")
        return GaussianColorModel(
            mean=self.mean,
            covariance=_add(self.covariance, _IDENTITY, scale_second=ridge),
        )


@dataclass(frozen=True, slots=True)
class OptimalTransportResult:
    source: GaussianColorModel
    target: GaussianColorModel
    map_matrix: Matrix3
    wasserstein_sq: float

    def __post_init__(self) -> None:
        if not isinstance(self.source, GaussianColorModel):
            raise TypeError("source must be a GaussianColorModel")
        if not isinstance(self.target, GaussianColorModel):
            raise TypeError("target must be a GaussianColorModel")
        object.__setattr__(self, "map_matrix", _as_matrix(self.map_matrix))
        if not math.isfinite(self.wasserstein_sq) or self.wasserstein_sq < -1e-9:
            raise OptimalTransportError("wasserstein_sq must be finite and non-negative")

    def apply(self, point: Sequence[float]) -> Vector3:
        return apply_transport_map(self, point)


def bures_wasserstein_distance_sq(
    source_covariance: Sequence[Sequence[float]],
    target_covariance: Sequence[Sequence[float]],
) -> float:
    """Squared Bures-Wasserstein distance between two SPD covariances."""

    source = _as_matrix(source_covariance)
    target = _as_matrix(target_covariance)
    source_root = matrix_sqrt_psd(source)
    inner = _symmetrise(_matmul(_matmul(source_root, target), source_root))
    middle = matrix_sqrt_psd(inner)
    value = _trace(source) + _trace(target) - 2.0 * _trace(middle)
    return max(0.0, value)


def optimal_transport_map(
    source: GaussianColorModel,
    target: GaussianColorModel,
    *,
    regularization: float = 1e-6,
) -> OptimalTransportResult:
    """Exact Monge-Kantorovich affine map ``T(x) = mu_t + A (x - mu_s)``."""

    if not math.isfinite(regularization) or regularization < 0.0:
        raise OptimalTransportError("regularization must be finite and non-negative")
    source_regular = source.regularized(regularization)
    target_regular = target.regularized(regularization)
    source_root = matrix_sqrt_psd(source_regular.covariance)
    source_inverse_root = matrix_inverse_sqrt_spd(source_regular.covariance)
    inner = _symmetrise(_matmul(_matmul(source_root, target_regular.covariance), source_root))
    inner_root = matrix_sqrt_psd(inner)
    map_matrix = _symmetrise(_matmul(_matmul(source_inverse_root, inner_root), source_inverse_root))
    distance_sq = bures_wasserstein_distance_sq(
        source_regular.covariance, target_regular.covariance
    )
    return OptimalTransportResult(
        source=source_regular,
        target=target_regular,
        map_matrix=map_matrix,
        wasserstein_sq=distance_sq,
    )


def apply_transport_map(result: OptimalTransportResult, point: Sequence[float]) -> Vector3:
    if not isinstance(result, OptimalTransportResult):
        raise TypeError("result must be an OptimalTransportResult")
    vector = _as_vector(point)
    shifted = tuple(vector[index] - result.source.mean[index] for index in range(3))
    return tuple(
        result.target.mean[row]
        + sum(result.map_matrix[row][column] * shifted[column] for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def geodesic_covariance(
    source_covariance: Sequence[Sequence[float]],
    target_covariance: Sequence[Sequence[float]],
    tau: float,
) -> Matrix3:
    """Bures geodesic ``Sigma(tau)`` between two SPD covariances."""

    if not math.isfinite(tau) or not 0.0 <= tau <= 1.0:
        raise OptimalTransportError("tau must lie inside [0, 1]")
    source = _as_matrix(source_covariance)
    target = _as_matrix(target_covariance)
    source_root = matrix_sqrt_psd(source)
    source_inverse_root = matrix_inverse_sqrt_spd(source)
    inner = _symmetrise(_matmul(_matmul(source_root, target), source_root))
    inner_root = matrix_sqrt_psd(inner)
    transport = _symmetrise(_matmul(_matmul(source_inverse_root, inner_root), source_inverse_root))
    interpolated = _add(_IDENTITY, transport, scale_first=1.0 - tau, scale_second=tau)
    return _symmetrise(_matmul(_matmul(interpolated, source), interpolated))


def hurter_driffield_density(
    relative_exposure: float,
    *,
    d_min: float = 0.05,
    d_max: float = 2.0,
    gamma: float = 1.0,
    h0: float = 1.0,
) -> float:
    """Photochemical density response ``D = Dmin + (Dmax-Dmin)/(1+(H0/H)^gamma)``."""

    if not math.isfinite(relative_exposure) or relative_exposure <= 0.0:
        raise OptimalTransportError("relative_exposure must be finite and positive")
    for label, value in (("d_min", d_min), ("d_max", d_max), ("h0", h0)):
        if not math.isfinite(value) or value <= 0.0:
            raise OptimalTransportError(f"{label} must be finite and positive")
    if not math.isfinite(gamma) or gamma <= 0.0:
        raise OptimalTransportError("gamma must be finite and positive")
    if d_max <= d_min:
        raise OptimalTransportError("d_max must exceed d_min")
    response = 1.0 / (1.0 + math.pow(h0 / relative_exposure, gamma))
    return d_min + (d_max - d_min) * response
