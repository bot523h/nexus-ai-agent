"""Deterministic vector geometry and a forensic ``sqlite-vec`` fallback.

The native ``sqlite-vec`` extension is optional.  When it is absent this module
is the retrieval substrate: embeddings are stored as canonical little-endian
``float64`` blobs with a SHA-256 digest, and every read re-hashes the bytes
before the vector is allowed to influence a ranking.  Silent bit-rot therefore
becomes a typed failure instead of a quietly wrong neighbour.

All inner products use Neumaier's compensated summation.  High-dimensional
embeddings routinely lose low-order bits in a naive sum; the compensated form
keeps the result faithful to the inputs so that the angular metric is stable
and obeys the triangle inequality.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

__all__ = [
    "ForensicVectorConflictError",
    "ForensicVectorIndex",
    "ForensicVectorTamperError",
    "VectorAuditReport",
    "VectorMatch",
    "VectorValidationError",
    "compensated_dot_product",
    "compensated_l2_norm",
    "cosine_similarity",
    "riemannian_geodesic_distance",
    "unit_vector",
    "vector_digest",
]

_MIN_NORM: Final[float] = 1e-12


class VectorValidationError(ValueError):
    """Raised when a vector is empty, non-finite, zero-norm or mis-sized."""


class ForensicVectorTamperError(RuntimeError):
    """Raised when stored bytes no longer match their authenticated digest."""


class ForensicVectorConflictError(RuntimeError):
    """Raised when one id is re-used with different vector content."""


def _validate_vector(vector: Sequence[float], dimension: int | None = None) -> tuple[float, ...]:
    values: list[float] = []
    for item in vector:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise VectorValidationError(f"vector component {item!r} is not a real number")
        value = float(item)
        if not math.isfinite(value):
            raise VectorValidationError(f"vector component {item!r} is not finite")
        values.append(0.0 if value == 0.0 else value)
    if not values:
        raise VectorValidationError("vector must not be empty")
    if dimension is not None:
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise VectorValidationError("dimension must be a positive int")
        if len(values) != dimension:
            raise VectorValidationError(
                f"dimension mismatch: expected {dimension}, got {len(values)}"
            )
    return tuple(values)


def compensated_dot_product(u: Sequence[float], v: Sequence[float]) -> float:
    """Neumaier-compensated inner product of two equal-length real vectors."""

    left = _validate_vector(u)
    right = _validate_vector(v)
    if len(left) != len(right):
        raise VectorValidationError(f"dimension mismatch: {len(left)} != {len(right)}")
    total = 0.0
    compensation = 0.0
    for a, b in zip(left, right, strict=True):
        product = a * b
        running = total + product
        if abs(total) >= abs(product):
            compensation += (total - running) + product
        else:
            compensation += (product - running) + total
        total = running
    return total + compensation


def compensated_l2_norm(u: Sequence[float]) -> float:
    """Scaled, compensated Euclidean norm that cannot overflow or underflow."""

    values = _validate_vector(u)
    scale = max(abs(component) for component in values)
    if scale == 0.0:
        return 0.0
    total = 0.0
    compensation = 0.0
    for component in values:
        scaled = component / scale
        square = scaled * scaled
        running = total + square
        if abs(total) >= abs(square):
            compensation += (total - running) + square
        else:
            compensation += (square - running) + total
        total = running
    return scale * math.sqrt(max(total + compensation, 0.0))


def unit_vector(u: Sequence[float]) -> tuple[float, ...]:
    """Project a non-zero vector onto the unit hypersphere."""

    values = _validate_vector(u)
    norm = compensated_l2_norm(values)
    if norm < _MIN_NORM:
        raise VectorValidationError(f"vector norm {norm!r} is below the {_MIN_NORM} floor")
    return tuple(component / norm for component in values)


def cosine_similarity(u: Sequence[float], v: Sequence[float]) -> float:
    """Cosine similarity of two non-zero vectors, clamped to [-1, 1]."""

    left = unit_vector(u)
    right = unit_vector(v)
    if len(left) != len(right):
        raise VectorValidationError(f"dimension mismatch: {len(left)} != {len(right)}")
    return max(-1.0, min(1.0, compensated_dot_product(left, right)))


def riemannian_geodesic_distance(u: Sequence[float], v: Sequence[float]) -> float:
    """Exact angular metric on S^{d-1}: ``arccos(<u_hat, v_hat>)`` in [0, pi]."""

    dot = cosine_similarity(u, v)
    # ``arccos`` is catastrophically ill-conditioned at 1; identical directions
    # must yield exactly zero rather than a ~2e-8 radian artefact.
    if dot >= 1.0 - 1e-12:
        return 0.0
    return math.acos(dot)


def _canonical_bytes(vector: Sequence[float]) -> bytes:
    values = _validate_vector(vector)
    return struct.pack(f"<{len(values)}d", *values)


def vector_digest(vector: Sequence[float]) -> str:
    """SHA-256 over the canonical IEEE-754 little-endian ``float64`` payload."""

    return hashlib.sha256(_canonical_bytes(vector)).hexdigest()


def _unpack(payload: bytes, dimension: int) -> tuple[float, ...]:
    if len(payload) != dimension * 8:
        raise ForensicVectorTamperError(
            f"payload is {len(payload)} bytes, expected {dimension * 8}"
        )
    return cast(tuple[float, ...], struct.unpack(f"<{dimension}d", payload))


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """One ranked neighbour, carrying the digest that justified its inclusion."""

    vector_id: str
    distance: float
    cosine: float
    digest: str

    def __post_init__(self) -> None:
        if not self.vector_id:
            raise VectorValidationError("vector_id must be non-empty")
        if not math.isfinite(self.distance) or not 0.0 <= self.distance <= math.pi:
            raise VectorValidationError("distance must be finite and inside [0, pi]")
        if not math.isfinite(self.cosine) or not -1.0 <= self.cosine <= 1.0:
            raise VectorValidationError("cosine must be finite and inside [-1, 1]")
        if len(self.digest) != 64:
            raise VectorValidationError("digest must be a 64-character SHA-256 hex string")


@dataclass(frozen=True, slots=True)
class VectorAuditReport:
    """Outcome of a full-store forensic sweep."""

    checked: int
    tampered_ids: tuple[str, ...]
    quarantined_ids: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.tampered_ids and not self.quarantined_ids


class ForensicVectorIndex:
    """SQLite-backed exact KNN store with per-row tamper detection."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        dimension: int | None = None,
        strict: bool = True,
    ) -> None:
        if dimension is not None and (
            isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0
        ):
            raise VectorValidationError("dimension must be a positive int")
        if not isinstance(strict, bool):
            raise TypeError("strict must be a bool")
        self._dimension = dimension
        self._strict = strict
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS forensic_vectors ("
            "vector_id TEXT PRIMARY KEY, "
            "dimension INTEGER NOT NULL, "
            "digest TEXT NOT NULL, "
            "payload BLOB NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS forensic_quarantine ("
            "vector_id TEXT PRIMARY KEY, "
            "reason TEXT NOT NULL)"
        )
        self._connection.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        """Raw handle, exposed for forensic tooling and tests."""

        return self._connection

    @property
    def dimension(self) -> int | None:
        return self._dimension

    def _resolve_dimension(self, vector: Sequence[float]) -> tuple[float, ...]:
        values = _validate_vector(vector, self._dimension)
        if self._dimension is None:
            self._dimension = len(values)
        return values

    def _quarantine(self, vector_id: str, reason: str) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO forensic_quarantine (vector_id, reason) VALUES (?, ?)",
            (vector_id, reason),
        )
        self._connection.commit()

    def _is_quarantined(self, vector_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM forensic_quarantine WHERE vector_id = ?", (vector_id,)
        ).fetchone()
        return row is not None

    def add(self, vector_id: str, vector: Sequence[float]) -> str:
        """Insert or idempotently re-assert a vector; return its digest."""

        if not isinstance(vector_id, str) or not vector_id:
            raise VectorValidationError("vector_id must be a non-empty string")
        values = self._resolve_dimension(vector)
        if compensated_l2_norm(values) < _MIN_NORM:
            raise VectorValidationError("zero-norm vectors cannot enter the forensic index")
        digest = hashlib.sha256(_canonical_bytes(values)).hexdigest()
        existing = self._connection.execute(
            "SELECT digest FROM forensic_vectors WHERE vector_id = ?", (vector_id,)
        ).fetchone()
        if existing is not None:
            if cast(str, existing[0]) != digest:
                raise ForensicVectorConflictError(
                    f"vector {vector_id!r} already stored with a different digest"
                )
            return digest
        self._connection.execute(
            "INSERT INTO forensic_vectors (vector_id, dimension, digest, payload) "
            "VALUES (?, ?, ?, ?)",
            (vector_id, len(values), digest, _canonical_bytes(values)),
        )
        self._connection.commit()
        return digest

    def _verified_payload(
        self, vector_id: str, stored_digest: str, payload: bytes, dimension: int
    ) -> tuple[float, ...] | None:
        actual = hashlib.sha256(payload).hexdigest()
        if actual == stored_digest:
            return _unpack(payload, dimension)
        reason = f"digest mismatch: stored {stored_digest}, recomputed {actual}"
        if self._strict:
            raise ForensicVectorTamperError(f"vector {vector_id!r} failed: {reason}")
        self._quarantine(vector_id, reason)
        return None

    def get(self, vector_id: str) -> tuple[float, ...]:
        row = self._connection.execute(
            "SELECT digest, payload, dimension FROM forensic_vectors WHERE vector_id = ?",
            (vector_id,),
        ).fetchone()
        if row is None:
            raise KeyError(vector_id)
        digest, payload, dimension = cast(tuple[str, bytes, int], row)
        values = self._verified_payload(vector_id, digest, payload, dimension)
        if values is None:  # pragma: no cover - strict mode raises above
            raise KeyError(vector_id)
        return values

    def search_knn(self, query: Sequence[float], k: int = 5) -> tuple[VectorMatch, ...]:
        """Rank live rows by exact Riemannian distance with lexicographic ties."""

        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            raise VectorValidationError("k must be a positive int")
        query_values = _validate_vector(query, self._dimension)
        query_unit = unit_vector(query_values)
        rows = self._connection.execute(
            "SELECT vector_id, digest, payload, dimension FROM forensic_vectors ORDER BY vector_id"
        ).fetchall()
        matches: list[VectorMatch] = []
        for raw in rows:
            vector_id, digest, payload, dimension = cast(tuple[str, str, bytes, int], raw)
            if self._is_quarantined(vector_id):
                continue
            values = self._verified_payload(vector_id, digest, payload, dimension)
            if values is None:
                continue
            try:
                candidate_unit = unit_vector(values)
            except VectorValidationError:
                self._quarantine(vector_id, "stored vector is zero-norm or invalid")
                continue
            dot = max(-1.0, min(1.0, compensated_dot_product(query_unit, candidate_unit)))
            matches.append(
                VectorMatch(
                    vector_id=vector_id,
                    distance=math.acos(dot),
                    cosine=dot,
                    digest=digest,
                )
            )
        matches.sort(key=lambda match: (match.distance, match.vector_id))
        return tuple(matches[:k])

    def audit(self) -> VectorAuditReport:
        """Re-hash every row, quarantining corrupt ones and reporting the damage."""

        rows = self._connection.execute(
            "SELECT vector_id, digest, payload, dimension FROM forensic_vectors ORDER BY vector_id"
        ).fetchall()
        tampered: list[str] = []
        for raw in rows:
            vector_id, digest, payload, dimension = cast(tuple[str, str, bytes, int], raw)
            actual = hashlib.sha256(payload).hexdigest()
            if actual != digest:
                tampered.append(vector_id)
                self._quarantine(
                    vector_id, f"digest mismatch: stored {digest}, recomputed {actual}"
                )
        quarantined = self.quarantined_ids()
        return VectorAuditReport(
            checked=len(rows),
            tampered_ids=tuple(tampered),
            quarantined_ids=quarantined,
        )

    def quarantined_ids(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT vector_id FROM forensic_quarantine ORDER BY vector_id"
        ).fetchall()
        return tuple(cast(str, row[0]) for row in rows)

    def __len__(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM forensic_vectors").fetchone()
        if row is None:  # pragma: no cover - COUNT always returns a row
            return 0
        return cast(int, row[0])

    def close(self) -> None:
        self._connection.close()
