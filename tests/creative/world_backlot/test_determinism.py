from __future__ import annotations

import hashlib
import math
from typing import cast

import numpy as np
import pytest

from nexus_ai_agent.creative.world_backlot._determinism import (
    SCHEMA,
    Vec3,
    array_digest,
    canonical_bytes,
    digest,
    finite,
    frozen_floats,
    frozen_pcm,
    integer,
    normal,
    points_array,
    seed_bytes,
    uniform,
    vector,
)


def test_sha256_seed_encoding_is_independent_and_domain_separated() -> None:
    parts = (SCHEMA.encode(), b"str", "نگار".encode(), b"sky")
    expected = hashlib.sha256(b"".join(len(p).to_bytes(8, "big") + p for p in parts)).digest()
    assert seed_bytes("نگار", "sky") == expected
    assert len({seed_bytes(s) for s in (12, "12", b"12")}) == 3
    assert seed_bytes("نگار", "sky") != seed_bytes("نگار", "soil")
    assert seed_bytes(b"ab", "c") != seed_bytes(b"a", "bc")
    assert len(seed_bytes(-1)) == 32


def test_uniform_counter_stream_is_open_prefix_stable_and_statistically_sound() -> None:
    samples = uniform("seed", "test", 20_001)
    np.testing.assert_array_equal(samples[:101], uniform("seed", "test", 101))
    assert uniform(0, "empty", 0).shape == (0,)
    assert np.all((samples > 0) & (samples < 1))
    assert float(np.mean(samples)) == pytest.approx(0.5, abs=0.008)
    assert float(np.var(samples)) == pytest.approx(1 / 12, abs=0.003)
    assert not np.array_equal(samples, uniform("seed", "other", len(samples)))
    key = seed_bytes("seed", "test")
    word = int.from_bytes(hashlib.sha256(key + bytes(8)).digest()[:8], "big")
    assert samples[0] == ((word >> 12) + 0.5) * 2**-52


def test_box_muller_normals_have_the_correct_moments_and_odd_prefix() -> None:
    samples = normal(143, "gaussian", 20_003)
    np.testing.assert_array_equal(samples[:5], normal(143, "gaussian", 5))
    assert normal(0, "empty", 0).size == 0
    assert abs(float(np.mean(samples))) < 0.025
    assert float(np.var(samples)) == pytest.approx(1, abs=0.035)
    assert abs(float(np.corrcoef(samples[:-1], samples[1:])[0, 1])) < 0.035


def test_canonical_hashes_are_shape_sensitive_and_endian_independent() -> None:
    assert canonical_bytes({"b": 2, "a": "ماه"}) == '{"a":"ماه","b":2}'.encode()
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    values = np.arange(6, dtype=np.float64)
    assert array_digest(values) == array_digest(values.astype(">f8"))
    assert array_digest(values) != array_digest(values.reshape(2, 3))
    assert array_digest(np.array([1, 2], dtype="<i2")) == array_digest(
        np.array([1, 2], dtype=">i2")
    )
    with pytest.raises(ValueError):
        canonical_bytes({"value": math.nan})


def test_buffers_have_an_immutable_owner_not_just_a_readonly_flag() -> None:
    source = np.arange(6, dtype=np.float64).reshape(2, 3)
    sealed = frozen_floats(source)
    source[0, 0] = 99
    assert sealed[0, 0] == 0
    pcm = frozen_pcm(np.array([3, 5], dtype=np.int16))
    for values in (sealed, pcm):
        with pytest.raises(ValueError):
            values.setflags(write=True)
        with pytest.raises(ValueError):
            values.flat[0] = 7
    with pytest.raises(ValueError, match="non-finite"):
        frozen_floats(np.array([math.nan]))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, -1, 11])
def test_invalid_finite_scalars_are_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        finite(value, "value", 0, 10)


@pytest.mark.parametrize("value", [True, -1, 11, 1.5])
def test_invalid_integer_limits_are_rejected(value: int) -> None:
    with pytest.raises(ValueError):
        integer(value, "count", 0, 10)


def test_valid_boundary_scalars_and_vectors() -> None:
    assert finite(0, "v", 0, 1) == 0.0
    assert integer(10, "n", 0, 10) == 10
    assert vector((1, -2, 3), "v") == (1.0, -2.0, 3.0)
    with pytest.raises(ValueError):
        vector(cast(Vec3, (1, 2)), "v")
    with pytest.raises(ValueError):
        vector((math.inf, 0, 0), "v")


@pytest.mark.parametrize("seed", [True, 2.5, "x" * 65_537])
def test_invalid_seed_types_and_sizes_are_bounded(seed: object) -> None:
    with pytest.raises(ValueError):
        seed_bytes(cast(str, seed))


@pytest.mark.parametrize("namespace", ["", "x" * 1025])
def test_invalid_namespaces_are_bounded(namespace: str) -> None:
    with pytest.raises(ValueError):
        seed_bytes(0, namespace)


def test_allocations_are_checked_before_random_generation() -> None:
    with pytest.raises(ValueError):
        uniform(0, "too much", 1_500_001)
    with pytest.raises(ValueError):
        normal(0, "too much", 750_001)
    for values in (
        np.zeros((3,)),
        np.zeros((2, 2)),
        np.zeros((32_769, 3)),
        np.full((1, 3), math.nan),
    ):
        with pytest.raises(ValueError):
            points_array(values)
    assert points_array(np.zeros((0, 3))).shape == (0, 3)
