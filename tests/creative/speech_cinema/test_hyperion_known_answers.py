"""Known-answer tests (KATs) and golden vectors for Hyperion.

Each expected value is derived from the published specification (RFC 6962, GF(2^8)
with polynomial 0x11D, Bradford/ICC constants, the filmic formula) or from a frozen
golden master, and is written independently of the implementation under test.
These pin constants and boundaries that behavioural properties alone cannot see.
"""

from __future__ import annotations

import hashlib
import itertools

import numpy as np

from nexus_ai_agent.creative.speech_cinema import color_imaging as ci
from nexus_ai_agent.creative.speech_cinema import forced_aligner as fa
from nexus_ai_agent.creative.speech_cinema import isochronous_dubbing as iso
from nexus_ai_agent.creative.speech_cinema import subtitle_compiler as sc
from nexus_ai_agent.storage.artifact_vault import cdc_chunker as cdc
from nexus_ai_agent.storage.artifact_vault import galois_erasure as gf
from nexus_ai_agent.storage.artifact_vault import vault_manager as vm


def test_kat_gf256_reduction_uses_primitive_polynomial_0x11d() -> None:
    # x^7 * x = x^8 = x^4 + x^3 + x^2 + 1 (0x1D) under 0x11D; under 0x11B it would be 0x1B.
    assert gf.gf_multiply(0x80, 0x02) == 0x1D
    assert gf.PRIMITIVE_POLYNOMIAL == 0x11D


def test_kat_rfc6962_two_leaf_root_uses_0x00_and_0x01_domain_prefixes() -> None:
    a, b = b"alpha", b"beta"
    leaf_a = hashlib.sha256(b"\x00" + a).digest()
    leaf_b = hashlib.sha256(b"\x00" + b).digest()
    expected = hashlib.sha256(b"\x01" + leaf_a + leaf_b).hexdigest()
    assert vm.merkle_root([a, b]) == expected


def test_kat_reed_solomon_generator_is_systematic_and_mds() -> None:
    # The generator is a systematic Vandermonde code: identity on the first k rows, and every
    # k rows invertible (MDS).  The previous powers-of-(r+1) recurrence was not MDS.
    gen = gf._generator(3, 2)
    assert gen[:3].tolist() == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    for keep in itertools.combinations(range(5), 3):
        gf.matrix_invert(gen[list(keep)])  # raises if singular


def test_golden_master_cdc_chunk_boundaries_are_frozen() -> None:
    # GOLDEN MASTER, not an independent oracle: pinned from the reference implementation so that
    # any change to the gear table, mask or rolling hash is visible as a diff.  Small chunk
    # bounds keep cut points content-defined (not forced by max_size), so the table is exercised.
    data = bytes((i * 131 + 7) % 256 for i in range(200_000))
    chunks = cdc.GearChunker(min_chunk_bytes=64, avg_chunk_bytes=256, max_chunk_bytes=4096).chunks(
        data
    )
    digest = hashlib.sha256(b"".join(c.sha256.encode() for c in chunks)).hexdigest()
    assert len(chunks) == 782
    assert digest == "1114bf46f9cb6e624e0a9b0ec05067f44821fba79446b34243a4e821cd521638"


def test_kat_reed_solomon_accepts_exactly_255_total_shards() -> None:
    shards = gf.encode(b"x" * 256, 200, 55)
    assert len(shards) == 255
    assert gf.reconstruct(shards[55:], original_size=256) == b"x" * 256


def test_kat_dtw_optimum_on_an_identity_cost_matrix_is_zero() -> None:
    cost, path = fa.banded_dtw(np.array([[0.0, 1.0], [1.0, 0.0]]), band=None)
    assert cost == 0.0
    assert path == [(0, 0), (1, 1)]


def test_kat_wsola_linear_ramp_interpolates_exactly() -> None:
    result = iso.time_scale(np.arange(4, dtype=np.float32), 7).audio
    np.testing.assert_allclose(result, np.linspace(0, 3, 7), rtol=0, atol=1e-6)


def test_kat_filmic_shoulder_matches_the_published_formula_at_unit_luminance() -> None:
    a, b, c, d, e = 0.22, 0.30, 0.10, 0.20, 0.01
    curve = (1.0 * (a * 1.0 + b)) / (1.0 * (c * 1.0 + d) + e)
    expected = curve / (a / c) / 1.0  # luminance of white is exactly 1
    out = ci.hdr_to_sdr_filmic_tonemap(np.ones((1, 1, 3)))
    np.testing.assert_allclose(out[0, 0], [expected] * 3, rtol=0, atol=1e-12)


def test_kat_bradford_constants_match_the_icc_convention() -> None:
    np.testing.assert_array_equal(
        ci._BRADFORD,
        np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]]),
    )


def test_kat_matrix_sqrt_inverse_is_the_reciprocal_root_of_a_diagonal_matrix() -> None:
    cov = np.diag([4.0, 9.0, 16.0])
    np.testing.assert_allclose(ci.matrix_sqrt_spd(cov), np.diag([2.0, 3.0, 4.0]), atol=1e-12)
    np.testing.assert_allclose(
        ci.matrix_sqrt_spd(cov, inverse=True), np.diag([0.5, 1 / 3, 0.25]), atol=1e-12
    )


def test_kat_subtitle_42_character_line_is_allowed_and_43_splits() -> None:
    fits = [
        sc.AlignedWord(text="a" * 20, start=0.0, end=0.5),
        sc.AlignedWord(text="b" * 21, start=0.6, end=2.5),  # 42 chars over 2.5 s = 16.8 CPS
    ]
    assert len(sc.partition_cues(fits)) == 1
    overflows = [
        sc.AlignedWord(text="a" * 20, start=0.0, end=0.5),
        sc.AlignedWord(text="b" * 22, start=0.6, end=1.0),
    ]
    assert len(sc.partition_cues(overflows)) == 2


def test_kat_one_digit_fraction_is_a_tenth_not_a_thousandth() -> None:
    cues = sc.parse_vtt("WEBVTT\n\n00:00:01.5 --> 00:00:02.25\nx\n")
    assert cues[0].start == 1.5
    assert cues[0].end == 2.25
