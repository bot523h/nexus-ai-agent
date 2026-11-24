"""Wave 2: compensated geometry and forensic vector fallback."""

from __future__ import annotations

import math
import sqlite3

import pytest

from nexus_ai_agent.memory import riemannian_vector_fallback as mod
from nexus_ai_agent.memory.riemannian_vector_fallback import (
    ForensicVectorConflictError,
    ForensicVectorIndex,
    ForensicVectorTamperError,
    VectorAuditReport,
    VectorMatch,
    VectorValidationError,
    compensated_dot_product,
    compensated_l2_norm,
    cosine_similarity,
    riemannian_geodesic_distance,
    unit_vector,
    vector_digest,
)


def _lcg_vectors(count: int, dimension: int, seed: int = 7) -> list[list[float]]:
    state = seed
    vectors: list[list[float]] = []
    for _ in range(count):
        vector = []
        for _ in range(dimension):
            state = (1103515245 * state + 12345) % (2**31)
            vector.append((state / (2**31)) * 2.0 - 1.0)
        vectors.append(vector)
    return vectors


def test_validate_rejects_bad_components() -> None:
    for bad in ([True, 1.0], ["x", 1.0], [math.nan], [math.inf], [math.nan, 1.0]):
        with pytest.raises(VectorValidationError):
            compensated_dot_product(bad, [1.0] * len(bad))
    with pytest.raises(VectorValidationError):
        compensated_dot_product([], [])
    with pytest.raises(VectorValidationError):
        compensated_dot_product([1.0], [1.0, 2.0])


def test_dimension_validation() -> None:
    with pytest.raises(VectorValidationError):
        mod._validate_vector([1.0], dimension=0)
    with pytest.raises(VectorValidationError):
        mod._validate_vector([1.0], dimension=True)
    with pytest.raises(VectorValidationError):
        mod._validate_vector([1.0, 2.0], dimension=3)
    assert mod._validate_vector([1.0], dimension=1) == (1.0,)


def test_negative_zero_is_canonicalised() -> None:
    assert mod._validate_vector([-0.0, 1.0]) == (0.0, 1.0)
    assert vector_digest([-0.0, 1.0]) == vector_digest([0.0, 1.0])


def test_compensated_dot_beats_naive_cancellation() -> None:
    u = [1e100, 1.0, -1e100]
    v = [1.0, 1.0, 1.0]
    naive = 0.0
    for a, b in zip(u, v, strict=True):
        naive += a * b
    assert naive == 0.0
    assert compensated_dot_product(u, v) == pytest.approx(1.0)
    assert compensated_dot_product([1.0, 2.0], [3.0, 4.0]) == pytest.approx(11.0)


def test_compensated_norm_is_scaled() -> None:
    assert compensated_l2_norm([0.0, 0.0]) == 0.0
    assert compensated_l2_norm([3.0, 4.0]) == pytest.approx(5.0)
    assert compensated_l2_norm([1e200, 1e200]) == pytest.approx(math.sqrt(2.0) * 1e200)
    assert compensated_l2_norm([1e-200, 1e-200]) == pytest.approx(math.sqrt(2.0) * 1e-200)


def test_unit_vector_and_similarity() -> None:
    assert unit_vector([3.0, 4.0]) == pytest.approx((0.6, 0.8))
    with pytest.raises(VectorValidationError):
        unit_vector([0.0, 0.0])
    with pytest.raises(VectorValidationError):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_geodesic_metric_values_and_triangle_inequality() -> None:
    assert riemannian_geodesic_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(math.pi / 2)
    assert riemannian_geodesic_distance([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(math.pi)
    vectors = _lcg_vectors(12, 5)
    for u in vectors:
        assert 0.0 <= riemannian_geodesic_distance(u, u) <= 1e-9
    for u, v, w in zip(vectors, vectors[1:], vectors[2:], strict=False):
        duv = riemannian_geodesic_distance(u, v)
        dvw = riemannian_geodesic_distance(v, w)
        duw = riemannian_geodesic_distance(u, w)
        assert duw <= duv + dvw + 1e-9


def test_vector_digest_is_stable_and_discriminating() -> None:
    a = vector_digest([1.0, 2.0, 3.0])
    assert len(a) == 64
    assert a == vector_digest([1.0, 2.0, 3.0])
    assert a != vector_digest([1.0, 2.0, 3.5])


def test_vector_match_validation() -> None:
    good = VectorMatch(vector_id="a", distance=0.5, cosine=0.9, digest="0" * 64)
    assert good.vector_id == "a"
    with pytest.raises(VectorValidationError):
        VectorMatch(vector_id="", distance=0.5, cosine=0.9, digest="0" * 64)
    with pytest.raises(VectorValidationError):
        VectorMatch(vector_id="a", distance=math.nan, cosine=0.9, digest="0" * 64)
    with pytest.raises(VectorValidationError):
        VectorMatch(vector_id="a", distance=4.0, cosine=0.9, digest="0" * 64)
    with pytest.raises(VectorValidationError):
        VectorMatch(vector_id="a", distance=0.5, cosine=2.0, digest="0" * 64)
    with pytest.raises(VectorValidationError):
        VectorMatch(vector_id="a", distance=0.5, cosine=0.9, digest="short")


def test_audit_report_clean_flag() -> None:
    assert VectorAuditReport(checked=1, tampered_ids=(), quarantined_ids=()).clean
    assert not VectorAuditReport(checked=1, tampered_ids=("x",), quarantined_ids=()).clean
    assert not VectorAuditReport(checked=1, tampered_ids=(), quarantined_ids=("x",)).clean


def test_index_constructor_validation() -> None:
    with pytest.raises(VectorValidationError):
        ForensicVectorIndex(dimension=0)
    with pytest.raises(VectorValidationError):
        ForensicVectorIndex(dimension=True)
    with pytest.raises(TypeError):
        ForensicVectorIndex(strict=1)  # type: ignore[arg-type]
    index = ForensicVectorIndex(dimension=2)
    try:
        assert index.dimension == 2
    finally:
        index.close()


def test_index_add_is_idempotent_and_conflict_aware() -> None:
    index = ForensicVectorIndex(dimension=2)
    try:
        digest = index.add("a", [1.0, 0.0])
        assert index.add("a", [1.0, 0.0]) == digest
        assert index.dimension == 2
        with pytest.raises(ForensicVectorConflictError):
            index.add("a", [0.0, 1.0])
        with pytest.raises(VectorValidationError):
            index.add("", [1.0, 0.0])
        with pytest.raises(VectorValidationError):
            index.add("b", [0.0, 0.0])
        with pytest.raises(VectorValidationError):
            index.add("b", [1.0, math.nan])
        with pytest.raises(VectorValidationError):
            index.add("b", [1.0, 0.0, 0.0])
        assert len(index) == 1
    finally:
        index.close()


def test_index_dimension_locks_on_first_insert() -> None:
    index = ForensicVectorIndex()
    try:
        index.add("a", [1.0, 2.0, 3.0])
        assert index.dimension == 3
        with pytest.raises(VectorValidationError):
            index.add("b", [1.0, 2.0])
    finally:
        index.close()


def test_index_search_orders_and_tie_breaks() -> None:
    index = ForensicVectorIndex()
    try:
        index.add("z", [1.0, 0.0])
        index.add("a", [1.0, 0.0])
        index.add("m", [0.0, 1.0])
        matches = index.search_knn([1.0, 0.0], k=3)
        assert [match.vector_id for match in matches] == ["a", "z", "m"]
        assert matches[0].distance == pytest.approx(0.0)
        assert matches[2].distance == pytest.approx(math.pi / 2)
        assert len(index.search_knn([1.0, 0.0], k=99)) == 3
        with pytest.raises(VectorValidationError):
            index.search_knn([1.0, 0.0], k=0)
        with pytest.raises(VectorValidationError):
            index.search_knn([1.0, 0.0], k=True)
        with pytest.raises(VectorValidationError):
            index.search_knn([1.0, 0.0, 0.0])
        with pytest.raises(VectorValidationError):
            index.search_knn([0.0, 0.0])
    finally:
        index.close()


def test_index_get_and_missing() -> None:
    index = ForensicVectorIndex(dimension=2)
    try:
        index.add("a", [3.0, 4.0])
        assert index.get("a") == (3.0, 4.0)
        with pytest.raises(KeyError):
            index.get("missing")
    finally:
        index.close()


def _corrupt_payload(index: ForensicVectorIndex, vector_id: str) -> None:
    index.connection.execute(
        "UPDATE forensic_vectors SET payload = ? WHERE vector_id = ?",
        (b"\x00" * 16, vector_id),
    )
    index.connection.commit()


def test_tamper_detection_strict_mode() -> None:
    index = ForensicVectorIndex(dimension=2, strict=True)
    try:
        index.add("a", [1.0, 0.0])
        index.add("b", [0.0, 1.0])
        _corrupt_payload(index, "a")
        with pytest.raises(ForensicVectorTamperError):
            index.get("a")
        with pytest.raises(ForensicVectorTamperError):
            index.search_knn([1.0, 0.0])
    finally:
        index.close()


def test_tamper_detection_audit_mode_quarantines() -> None:
    index = ForensicVectorIndex(dimension=2, strict=False)
    try:
        index.add("a", [1.0, 0.0])
        index.add("b", [0.0, 1.0])
        _corrupt_payload(index, "a")
        matches = index.search_knn([1.0, 0.0], k=10)
        assert [match.vector_id for match in matches] == ["b"]
        assert index.quarantined_ids() == ("a",)
        assert index.search_knn([1.0, 0.0], k=10)[0].vector_id == "b"
        report = index.audit()
        assert report.checked == 2
        assert report.tampered_ids == ("a",)
        assert report.quarantined_ids == ("a",)
        assert report.clean is False
    finally:
        index.close()


def test_audit_clean_store() -> None:
    index = ForensicVectorIndex(dimension=2)
    try:
        index.add("a", [1.0, 0.0])
        index.add("b", [0.0, 1.0])
        report = index.audit()
        assert report.checked == 2
        assert report.tampered_ids == ()
        assert report.quarantined_ids == ()
        assert report.clean is True
    finally:
        index.close()


def test_zero_norm_stored_row_is_quarantined_on_search() -> None:
    index = ForensicVectorIndex(dimension=2)
    try:
        index.connection.execute(
            "INSERT INTO forensic_vectors (vector_id, dimension, digest, payload) "
            "VALUES (?, 2, ?, ?)",
            ("zero", vector_digest([0.0, 0.0]), mod._canonical_bytes([0.0, 0.0])),
        )
        index.connection.commit()
        assert index.search_knn([1.0, 0.0]) == ()
        assert index.quarantined_ids() == ("zero",)
    finally:
        index.close()


def test_unpack_dimension_guard() -> None:
    with pytest.raises(ForensicVectorTamperError):
        mod._unpack(b"\x00" * 4, 2)


def test_len_and_close_roundtrip(tmp_path) -> None:
    path = str(tmp_path / "vectors.sqlite")
    index = ForensicVectorIndex(path, dimension=3)
    index.add("a", [1.0, 2.0, 3.0])
    assert len(index) == 1
    assert isinstance(index.connection, sqlite3.Connection)
    index.close()
    reopened = ForensicVectorIndex(path, dimension=3)
    try:
        assert reopened.get("a") == (1.0, 2.0, 3.0)
    finally:
        reopened.close()
