"""Hyperion 40-probe Popperian falsification ledger.

Each probe states a claim that could be false and tries to falsify it.  A probe
that fails against the implementation is recorded as ``xfail(strict=True)`` with
the measured reason, so a gap is visible in the ledger instead of silently passing.
If a gap is later implemented, the strict xfail turns into a failure and forces
the ledger to be updated.

Probes 1-14: artifact_vault (GF(2^8), Reed-Solomon, Merkle, CDC, on-disk heal)
Probes 15-28: speech_cinema (VAD, DTW, WSOLA, subtitles)
Probes 29-40: color_imaging (Bradford, filmic tonemap, Gaussian OT)
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.speech_cinema import color_imaging as ci
from nexus_ai_agent.creative.speech_cinema.forced_aligner import banded_dtw
from nexus_ai_agent.creative.speech_cinema.isochronous_dubbing import time_scale
from nexus_ai_agent.creative.speech_cinema.subtitle_compiler import (
    AlignedWord,
    parse_srt,
    parse_vtt,
    partition_cues,
    serialize_ass,
    serialize_srt,
    serialize_vtt,
)
from nexus_ai_agent.creative.speech_cinema.vad_detector import detect_voice
from nexus_ai_agent.storage.artifact_vault import cdc_chunker
from nexus_ai_agent.storage.artifact_vault import galois_erasure as gf
from nexus_ai_agent.storage.artifact_vault.vault_manager import (
    ArtifactVault,
    merkle_proof,
    merkle_root,
    verify_merkle_proof,
)

RNG = np.random.default_rng(20261113)


def _payload(size: int, seed: int = 1) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, size, dtype=np.uint8).tobytes()


# ---------------------------------------------------------------- 1-14 vault


def test_p01_gf256_every_nonzero_element_has_a_multiplicative_inverse() -> None:
    for a in range(1, 256):
        assert gf.gf_multiply(a, gf.gf_inverse(a)) == 1


def test_p02_gf256_multiplication_distributes_over_xor() -> None:
    for _ in range(2000):
        a, b, c = (int(v) for v in RNG.integers(0, 256, 3))
        assert gf.gf_multiply(a, b ^ c) == gf.gf_multiply(a, b) ^ gf.gf_multiply(a, c)


def test_p03_gf256_division_by_zero_raises_typed_error() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        gf.gf_inverse(0)


@pytest.mark.parametrize(("k", "m"), [(0, 2), (2, 0), (2, -1), (200, 100)])
def test_p04_invalid_reed_solomon_parameters_are_refused(k: int, m: int) -> None:
    with pytest.raises(ValueError, match="require 1 <= k"):
        gf.encode(b"payload", k, m)


def test_p05_erasing_all_parity_shards_recovers_bit_identical_payload() -> None:
    data = _payload(1000)
    shards = gf.encode(data, 4, 2)
    assert gf.reconstruct(shards[:4], original_size=len(data)) == data


def test_p06_erasing_the_first_data_shards_recovers_bit_identical_payload() -> None:
    data = _payload(1000, seed=2)
    shards = gf.encode(data, 4, 2)
    assert gf.reconstruct(shards[2:], original_size=len(data)) == data


def test_p07_erasing_mixed_data_and_parity_recovers_bit_identical_payload() -> None:
    data = _payload(777, seed=3)
    shards = gf.encode(data, 4, 2)
    survivors = [s for s in shards if s.index not in {0, 4}]
    assert gf.reconstruct(survivors, original_size=len(data)) == data


def test_p08_erasing_m_plus_one_shards_raises_typed_unrecoverable_error() -> None:
    data = _payload(500, seed=4)
    shards = gf.encode(data, 4, 2)
    with pytest.raises(ValueError, match="at least k"):
        gf.reconstruct(shards[:3], original_size=len(data))


def test_p09_one_flipped_bit_in_a_surviving_shard_fails_merkle_inclusion() -> None:
    shards = gf.encode(_payload(600, seed=5), 4, 2)
    leaves = [bytes(s.data) for s in shards]
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, 1)
    assert verify_merkle_proof(leaves[1], 1, proof, root)
    tampered = bytearray(leaves[1])
    tampered[0] ^= 0x01
    assert not verify_merkle_proof(bytes(tampered), 1, proof, root)


def test_p10_tampered_internal_merkle_node_fails_root_verification() -> None:
    leaves = [bytes([i]) * 8 for i in range(6)]
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, 3)
    side, sibling = proof[0]
    flipped = ("0" if sibling[0] != "0" else "1") + sibling[1:]
    tampered = [(side, flipped), *proof[1:]]
    assert verify_merkle_proof(leaves[3], 3, proof, root)
    assert not verify_merkle_proof(leaves[3], 3, tampered, root)


def test_p11_corrupted_shard_on_disk_is_quarantined_and_bit_exactly_reconstructed(
    tmp_path,
) -> None:
    vault = ArtifactVault(tmp_path)
    data = _payload(4096, seed=6)
    vault.store_artifact("probe-heal", data, 4, 2)
    artifact_dir = vault._dir("probe-heal")
    victim = max((p for p in artifact_dir.iterdir() if p.is_file()), key=lambda p: p.stat().st_size)
    raw = bytearray(victim.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    victim.write_bytes(bytes(raw))
    vault.verify_and_heal_artifact("probe-heal")
    assert vault.read_artifact("probe-heal") == data


def test_p12_content_defined_chunking_resynchronizes_after_a_prefix_insert() -> None:
    data = _payload(256 * 1024, seed=7)
    before = [c.sha256 for c in cdc_chunker.chunk_bytes(data)]
    after = {c.sha256 for c in cdc_chunker.chunk_bytes(b"INSERTED-PREFIX" + data)}
    shared = sum(1 for digest in before if digest in after)
    assert shared >= 0.8 * len(before), (shared, len(before))


@pytest.mark.parametrize("artifact_id", ["../escape", "a/b", "a\\b", ".", ""])
def test_p13_artifact_ids_that_escape_the_vault_root_are_refused(
    tmp_path, artifact_id: str
) -> None:
    with pytest.raises(ValueError, match="invalid artifact id"):
        ArtifactVault(tmp_path).store_artifact(artifact_id, b"x", 2, 1)


def test_p14_empty_payload_and_invalid_chunk_bounds_are_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        gf.encode(b"", 2, 1)
    with pytest.raises(ValueError, match="require 0 < min_size"):
        cdc_chunker.GearChunker(min_size=0, avg_size=0, max_size=0)


# ------------------------------------------------------------- 15-28 speech


def test_p15_pure_silence_yields_zero_speech_segments() -> None:
    assert detect_voice(np.zeros(16000, dtype=np.float32), 16000).segments == ()


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_p16_non_finite_samples_are_rejected_with_a_typed_error(bad: float) -> None:
    samples = np.zeros(1600, dtype=np.float32)
    samples[10] = bad
    with pytest.raises(ValueError, match="finite"):
        detect_voice(samples, 16000)


@pytest.mark.parametrize("rate", [0, -5])
def test_p17_non_positive_sample_rate_is_rejected(rate: int) -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        detect_voice(np.zeros(1600, dtype=np.float32), rate)


def test_p18_out_of_range_sakoe_chiba_band_is_handled_deterministically() -> None:
    cost = np.random.default_rng(8).random((6, 6))
    first = banded_dtw(cost, band=-1)
    second = banded_dtw(cost, band=-1)
    assert first == second
    assert first == banded_dtw(cost, band=0)


def test_p19_dtw_path_is_monotone_with_unit_steps() -> None:
    cost = np.random.default_rng(9).random((12, 15))
    _, path = banded_dtw(cost, band=None)
    assert path[0] == (0, 0) and path[-1] == (11, 14)
    for (i0, j0), (i1, j1) in zip(path, path[1:], strict=False):
        assert i1 >= i0 and j1 >= j0
        assert (i1 - i0) in (0, 1) and (j1 - j0) in (0, 1)


def test_p20_identity_time_scale_preserves_the_exact_sample_count() -> None:
    x = np.arange(1234, dtype=np.float32)
    assert time_scale(x, 1234).audio.shape == (1234,)


@pytest.mark.parametrize(("source", "target"), [(1600, 1200), (1600, 2000)])
def test_p21_compression_and_expansion_hit_the_exact_target_count(source: int, target: int) -> None:
    result = time_scale(np.linspace(0, 1, source, dtype=np.float32), target, 16000)
    assert result.audio.shape == (target,)
    assert result.target_frames == target


@pytest.mark.parametrize(("source", "target"), [(100, 20), (100, 400), (100, 0)])
def test_p22_extreme_stretch_ratios_are_refused(source: int, target: int) -> None:
    with pytest.raises(ValueError, match="stretch ratio"):
        time_scale(np.zeros(source, dtype=np.float32), target)


def test_p22b_stretch_ratios_strictly_inside_the_open_range_are_accepted() -> None:
    assert time_scale(np.zeros(100, dtype=np.float32), 21).audio.shape == (21,)
    assert time_scale(np.zeros(100, dtype=np.float32), 399).audio.shape == (399,)


def test_p23_every_cue_respects_the_42_character_single_line_limit() -> None:
    words = [AlignedWord(text=f"word{i}", start=i * 0.3, end=i * 0.3 + 0.25) for i in range(80)]
    cues = partition_cues(words)
    assert cues
    assert all(len(cue.text) <= 42 for cue in cues)
    assert all("\n" not in cue.text for cue in cues)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: no CPS (characters-per-second) check; partition_cues never splits or flags >20 CPS",
    ),
)
def test_p24_reading_speed_above_20_cps_is_split_or_flagged() -> None:
    words = [AlignedWord(text="x" * 40, start=0.0, end=0.5)]
    cues = partition_cues(words)
    assert all(len(c.text) / (c.end - c.start) <= 20 for c in cues) or any(
        getattr(c, "flagged", False) for c in cues
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: shot-cut handling only splits groups; there is no out-point snap to t_cut - 2 frames"
    ),
)
def test_p25_shot_cut_guard_snaps_an_out_point_back_two_frames() -> None:
    words = [AlignedWord(text="hello", start=0.0, end=1.0)]
    cues = partition_cues(words, shot_cuts=[1.0 - 0.02])
    assert cues[0].end == pytest.approx(1.0 - 2 / 25)


@pytest.mark.xfail(
    strict=True,
    reason="GAP: a cue whose words straddle a shot cut is not split; the cut falls inside the cue",
)
def test_p26_no_subtitle_cue_spans_a_shot_cut() -> None:
    words = [AlignedWord(text="a", start=0.0, end=0.40), AlignedWord(text="b", start=0.41, end=1.0)]
    cues = partition_cues(words, shot_cuts=[0.52])
    assert all(not (c.start < 0.52 < c.end) for c in cues)


def test_p27_srt_vtt_and_ass_outputs_are_valid_and_round_trip() -> None:
    cues = partition_cues(
        [AlignedWord(text="hi", start=0.0, end=1.0), AlignedWord(text="there", start=1.1, end=2.0)]
    )
    assert parse_srt(serialize_srt(cues)) == cues
    vtt = serialize_vtt(cues)
    assert vtt.startswith("WEBVTT")
    assert parse_vtt(vtt) == cues
    ass = serialize_ass(cues)
    assert "[Events]" in ass and ass.count("Dialogue:") == len(cues)


@pytest.mark.parametrize(
    "text",
    [
        "1\n00:00:02,000 --> 00:00:01,000\nhi\n",
        "WEBVTT\n\n00:00:01.000 --> 00:00:01.000\nhi\n",
    ],
)
def test_p28_inverted_or_zero_length_timestamps_are_refused(text: str) -> None:
    parser = parse_vtt if text.startswith("WEBVTT") else parse_srt
    with pytest.raises(ValueError, match="ends before it starts"):
        parser(text)


# ------------------------------------------------------------ 29-40 color


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: API maps kelvin->D65 only; identity at eq"
        "ual temperatures measured at ~4.3e-2, not 1e-6"
    ),
)
def test_p29_bradford_at_equal_temperatures_is_identity() -> None:
    image = np.full((4, 4, 3), 0.5)
    result = ci.white_balance_chromatic_adaptation(image, kelvin=6500)
    assert np.max(np.abs(result - image)) < 1e-6


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: no general source->destination Bradford a"
        "daptation, so no 3200K->6500K->3200K round trip"
    ),
    raises=AttributeError,
)
def test_p30_bradford_round_trip_recovers_linear_rgb() -> None:
    adapt = ci.bradford_adapt  # type: ignore[attr-defined]
    image = np.random.default_rng(30).random((4, 4, 3))
    there = adapt(image, 3200, 6500)
    assert np.max(np.abs(adapt(there, 6500, 3200) - image)) < 1e-5


@pytest.mark.parametrize("kelvin", [1000.0, 1999.0, 15001.0, 40000.0])
def test_p31_out_of_range_correlated_colour_temperature_is_refused(kelvin: float) -> None:
    with pytest.raises(ValueError, match="kelvin"):
        ci.white_balance_chromatic_adaptation(np.ones((2, 2, 3)), kelvin=kelvin)


def test_p32_filmic_tonemap_is_bounded_to_unit_range_including_saturated_highlights() -> None:
    samples = np.array(
        [[[100.0, 0.0, 0.0], [0.0, 0.0, 100.0], [100.0, 100.0, 100.0], [0.0, 80.0, 3.0]]]
    )
    out = ci.hdr_to_sdr_filmic_tonemap(samples)
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0


def test_p33_filmic_tonemap_is_strictly_monotone_on_luminance() -> None:
    xs = [0.0, 0.25, 0.5, 1.0, 2.0, 5.0, 11.2, 50.0, 100.0]
    lum = [float(ci.hdr_to_sdr_filmic_tonemap(np.full((1, 1, 3), x))[0, 0, 0]) for x in xs]
    assert all(b > a for a, b in zip(lum, lum[1:], strict=False))


def test_p34_optimal_transport_matches_the_reference_mean() -> None:
    src = np.random.default_rng(34).random((32, 32, 3))
    ref = np.random.default_rng(35).random((32, 32, 3)) * 0.5 + 0.2
    out = ci.shot_to_shot_color_match_ot(src, ref)
    assert np.linalg.norm(out.reshape(-1, 3).mean(axis=0) - ref.reshape(-1, 3).mean(axis=0)) < 1e-4


def test_p35_optimal_transport_matches_the_reference_covariance() -> None:
    src = np.random.default_rng(36).random((32, 32, 3))
    ref = np.random.default_rng(37).random((32, 32, 3)) * np.array([0.9, 0.4, 0.6])
    out = ci.shot_to_shot_color_match_ot(src, ref).reshape(-1, 3)
    ref_flat = ref.reshape(-1, 3)
    error = np.linalg.norm(
        np.cov(out, rowvar=False, bias=True) - np.cov(ref_flat, rowvar=False, bias=True)
    )
    assert error < 1e-3


def test_p36_constant_colour_zero_variance_refuses_without_nan() -> None:
    flat = np.full((8, 8, 3), 0.5)
    with pytest.raises(ValueError, match="positive definite"):
        ci.shot_to_shot_color_match_ot(flat, np.random.default_rng(38).random((8, 8, 3)))


def test_p37_mismatched_channel_counts_are_refused_with_typed_error() -> None:
    with pytest.raises(ValueError, match="RGB dimension"):
        ci.shot_to_shot_color_match_ot(np.ones((4, 4, 3)), np.ones((4, 4, 4)))


def test_p38_negative_linear_radiance_is_refused_not_clamped_to_nan() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        ci.hdr_to_sdr_filmic_tonemap(np.array([[[-1.0, 0.2, 0.2]]]))


def test_p39_partial_blend_interpolates_between_source_and_full_match() -> None:
    src = np.random.default_rng(39).random((16, 16, 3))
    ref = np.random.default_rng(40).random((16, 16, 3))
    full = ci.shot_to_shot_color_match_ot(src, ref, alpha=1.0)
    half = ci.shot_to_shot_color_match_ot(src, ref, alpha=0.5)
    np.testing.assert_allclose(half, 0.5 * src + 0.5 * full, rtol=1e-12, atol=1e-12)


@pytest.mark.parametrize("alpha", [-0.1, 1.1, float("nan")])
def test_p40_out_of_bounds_blend_weight_is_refused(alpha: float) -> None:
    img = np.random.default_rng(41).random((4, 4, 3))
    with pytest.raises(ValueError, match="alpha"):
        ci.shot_to_shot_color_match_ot(img, img, alpha=alpha)
