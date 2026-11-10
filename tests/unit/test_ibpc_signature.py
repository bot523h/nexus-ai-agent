"""Unit tests: Scope Signature — 64-bin CIE-L*a*b* histogram + motion + cosine.

Spec: docs/superpowers/specs/2026-11-10-nagar-triad-design.md §3.
All ffmpeg calls are mocked here; the real-FFmpeg integration lives in
tests/integration/test_ibpc_signature_ffmpeg.py.
"""

from __future__ import annotations

import hashlib
import math
import types
import unittest.mock as mock
from pathlib import Path

import pytest
from pydantic import ValidationError

_SHA64_A = "sha256:" + "a" * 64
_SHA64_B = "sha256:" + "b" * 64
_SHA64_C = "sha256:" + "c" * 64


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors_give_one(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        v = [1.0, 0.0, 0.0, 1.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-9

    def test_orthogonal_vectors_give_zero(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9

    def test_opposite_vectors_clamp_to_zero(self) -> None:
        """Spec §3.4 clamps the result to [0, 1] — a negative cosine is 0.0."""
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
        # a real negative dot is clamped, not returned negative
        assert cosine_similarity([1.0], [1.0]) == 1.0

    def test_zero_vector_returns_zero(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        z = [0.0] * 64
        assert cosine_similarity(z, z) == 0.0
        assert cosine_similarity(z, [1.0] * 64) == 0.0

    def test_different_lengths_raise(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        with pytest.raises(ValueError):
            cosine_similarity([1.0, 2.0], [1.0])

    def test_negative_element_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        with pytest.raises(ValueError):
            cosine_similarity([1.0, -0.5], [1.0, 0.5])

    def test_non_finite_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        with pytest.raises(ValueError):
            cosine_similarity([float("nan"), 1.0], [1.0, 1.0])
        with pytest.raises(ValueError):
            cosine_similarity([float("inf"), 1.0], [1.0, 1.0])

    def test_symmetry(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        a = [0.3, 0.7, 0.1]
        b = [0.6, 0.2, 0.9]
        assert abs(cosine_similarity(a, b) - cosine_similarity(b, a)) < 1e-12

    def test_float_overshoot_is_clamped_to_one(self) -> None:
        """The upper clamp is load-bearing, not decorative.

        For a 64-element vector the exact self-similarity can round *above* 1.0
        (``dot / (|a| * |b|) == 1.0000000000000002``).  A cosine must never
        exceed 1.0 — it would violate ``ScopeMatch.cosine_similarity``'s
        ``le=1.0`` contract — so the clamp must absorb the FP overshoot.
        """
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        v = [float(i) for i in range(1, 65)]
        # precondition: the unclamped quotient really does overshoot
        dot = sum(x * y for x, y in zip(v, v, strict=True))
        norm = math.sqrt(sum(x * x for x in v))
        assert dot / (norm * norm) > 1.0
        assert cosine_similarity(v, v) <= 1.0

    def test_result_within_unit_interval(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity

        a = [0.1] * 64
        b = [1.0] * 64
        r = cosine_similarity(a, b)
        assert 0.0 <= r <= 1.0


# ---------------------------------------------------------------------------
# CIE L*a*b* conversion + binning
# ---------------------------------------------------------------------------


class TestLabConversion:
    def test_white_is_l100_neutral(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import _rgb8_to_lab

        lab_l, lab_a, lab_b = _rgb8_to_lab(255, 255, 255)
        # the sRGB→XYZ matrix is rounded, so white lands within ~4e-6 of L*=100
        assert abs(lab_l - 100.0) < 1e-4
        assert abs(lab_a) < 1e-4
        assert abs(lab_b) < 1e-4

    def test_black_is_l0_neutral(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import _rgb8_to_lab

        lab_l, lab_a, lab_b = _rgb8_to_lab(0, 0, 0)
        assert abs(lab_l) < 1e-9
        assert abs(lab_a) < 1e-9
        assert abs(lab_b) < 1e-9

    def test_red_has_positive_a(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import _rgb8_to_lab

        _l, lab_a, _b = _rgb8_to_lab(255, 0, 0)
        assert lab_a > 0


class TestHistogramBinning:
    def test_split_is_l32_a16_b16(self) -> None:
        from nexus_ai_agent.nagar.ibpc import signature as sig

        assert sig._L_BINS == 32
        assert sig._A_BINS == 16
        assert sig._B_BINS == 16
        assert sig._HIST_LEN == 64

    def test_luminance_bins_are_half_open(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import _bin_index

        assert _bin_index(0.0, 0.0, 100.0, 32) == 0
        assert _bin_index(99.9, 0.0, 100.0, 32) == 31
        assert _bin_index(100.0, 0.0, 100.0, 32) == 31  # clamped into the last bin

    def test_chroma_bins_include_negatives(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import _bin_index

        assert _bin_index(-128.0, -128.0, 127.0, 16) == 0
        assert _bin_index(127.0, -128.0, 127.0, 16) == 15


# ---------------------------------------------------------------------------
# ScopeSignature model
# ---------------------------------------------------------------------------


def _make_signature_obj(**kw):
    from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature

    defaults = dict(
        scope_digest=_SHA64_A,
        asset_id="a1",
        start_us=0,
        end_us=1_000_000,
        color_histogram=tuple([1.0 / 8.0] * 64),
        motion_magnitude=0.05,
        sample_frames=8,
        computed_at="2026-01-01T00:00:00+00:00",
        signature_digest=_SHA64_B,
    )
    defaults.update(kw)
    return ScopeSignature(**defaults)


class TestScopeSignatureModel:
    def test_valid_construction(self) -> None:
        sig = _make_signature_obj()
        assert sig.sample_frames == 8
        assert len(sig.color_histogram) == 64

    def test_histogram_wrong_length_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_signature_obj(color_histogram=(0.1,) * 63)

    def test_histogram_non_finite_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_signature_obj(color_histogram=tuple([float("nan")] * 63 + [1.0]))

    def test_histogram_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_signature_obj(color_histogram=tuple([-0.1] + [0.0] * 63))

    def test_frozen(self) -> None:
        sig = _make_signature_obj()
        with pytest.raises(ValidationError):
            sig.sample_frames = 999  # type: ignore[misc]

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_signature_obj(rogue_field="bad")

    def test_digest_pattern_enforced(self) -> None:
        with pytest.raises(ValidationError):
            _make_signature_obj(signature_digest="not-a-digest")


# ---------------------------------------------------------------------------
# ScopeMatch model
# ---------------------------------------------------------------------------


class TestScopeMatchModel:
    def test_valid_match(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatch

        m = ScopeMatch(
            candidate_scope_digest=_SHA64_B,
            cosine_similarity=0.95,
            motion_delta=0.1,
        )
        assert m.cosine_similarity == pytest.approx(0.95)

    def test_similarity_out_of_range_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatch

        with pytest.raises(ValidationError):
            ScopeMatch(
                candidate_scope_digest=_SHA64_B,
                cosine_similarity=1.5,
                motion_delta=0.0,
            )

    def test_negative_motion_delta_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatch

        with pytest.raises(ValidationError):
            ScopeMatch(
                candidate_scope_digest=_SHA64_B,
                cosine_similarity=0.5,
                motion_delta=-0.1,
            )


# ---------------------------------------------------------------------------
# SignatureError
# ---------------------------------------------------------------------------


class TestSignatureError:
    def test_has_reason(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import SignatureError

        err = SignatureError("empty_scope")
        assert err.reason == "empty_scope"
        assert isinstance(err, RuntimeError)

    def test_with_detail(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import SignatureError

        err = SignatureError("ffmpeg_not_found", "binary missing")
        assert "ffmpeg_not_found" in str(err)
        assert "binary missing" in str(err)


# ---------------------------------------------------------------------------
# compute_scope_signature (mocked ffmpeg)
# ---------------------------------------------------------------------------

# 2x2 rgb24 frame = 12 bytes.
FRAME_A = bytes([128, 64, 32, 200, 100, 50, 10, 200, 150, 255, 0, 128]) * 3600  # 160x90 rgb24
FRAME_BLACK = bytes(12) * 3600  # 160x90 rgb24
FRAME_WHITE = bytes([255] * 12) * 3600  # 160x90 rgb24


def _make_scope(start_us=0, end_us=2_000_000):
    from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope

    return VideoTimeScope(asset_id="a1", effect="keep", start_us=start_us, end_us=end_us)


def _fake_ffmpeg_const(frame: bytes):
    def _run(*args, **kwargs):
        result = mock.Mock()
        result.stdout = frame
        result.returncode = 0
        return result

    return _run


def _fake_ffmpeg_seq(frames):
    it = iter(frames)

    def _run(*args, **kwargs):
        result = mock.Mock()
        result.stdout = next(it)
        result.returncode = 0
        return result

    return _run


def _patch_ffmpeg(run_side_effect):
    return (
        mock.patch("subprocess.run", side_effect=run_side_effect),
        mock.patch(
            "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin",
            return_value="ffmpeg",
        ),
    )


class TestComputeScopeSignature:
    def test_returns_scope_signature_with_64_bins(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature, compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        p1, p2 = _patch_ffmpeg(_fake_ffmpeg_const(FRAME_A))
        with p1, p2:
            sig = compute_scope_signature(src, _make_scope())
        assert isinstance(sig, ScopeSignature)
        assert len(sig.color_histogram) == 64
        assert sig.scope_digest == _make_scope().scope_digest
        assert sig.signature_digest.startswith("sha256:")

    def test_histogram_is_l2_normalized(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        p1, p2 = _patch_ffmpeg(_fake_ffmpeg_const(FRAME_A))
        with p1, p2:
            sig = compute_scope_signature(src, _make_scope())
        l2 = math.sqrt(sum(x * x for x in sig.color_histogram))
        assert abs(l2 - 1.0) < 1e-6

    def test_all_values_finite_and_non_negative(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        p1, p2 = _patch_ffmpeg(_fake_ffmpeg_const(FRAME_A))
        with p1, p2:
            sig = compute_scope_signature(src, _make_scope())
        assert all(math.isfinite(x) and x >= 0.0 for x in sig.color_histogram)

    def test_deterministic_digest(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        p1, p2 = _patch_ffmpeg(_fake_ffmpeg_const(FRAME_A))
        with p1, p2:
            sig1 = compute_scope_signature(src, _make_scope())
            sig2 = compute_scope_signature(src, _make_scope())
        assert sig1.signature_digest == sig2.signature_digest

    def test_computed_at_excluded_from_digest(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc import signature as sigmod

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")

        class _Stamped:
            def __init__(self, s: str) -> None:
                self._s = s

            def isoformat(self) -> str:
                return self._s

        fake_dt = mock.Mock()
        fake_dt.now.side_effect = [
            _Stamped("2020-01-01T00:00:00+00:00"),
            _Stamped("2031-12-31T23:59:59+00:00"),
        ]
        p1, p2 = _patch_ffmpeg(_fake_ffmpeg_const(FRAME_A))
        with p1, p2, mock.patch.object(sigmod, "datetime", fake_dt):
            sig1 = sigmod.compute_scope_signature(src, _make_scope())
            sig2 = sigmod.compute_scope_signature(src, _make_scope())
        assert sig1.computed_at != sig2.computed_at
        assert sig1.signature_digest == sig2.signature_digest

    def test_motion_zero_for_identical_frames(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        p1, p2 = _patch_ffmpeg(_fake_ffmpeg_const(FRAME_A))
        with p1, p2:
            sig = compute_scope_signature(src, _make_scope())
        assert sig.motion_magnitude == pytest.approx(0.0)

    def test_motion_positive_for_alternating_frames(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        frames = [FRAME_BLACK, FRAME_WHITE] * 4
        p1, p2 = _patch_ffmpeg(_fake_ffmpeg_seq(frames))
        with p1, p2:
            sig = compute_scope_signature(src, _make_scope())
        assert sig.motion_magnitude > 0.0
        assert sig.motion_magnitude <= 255.0

    def test_empty_frame_raises(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import SignatureError, compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        p1, p2 = _patch_ffmpeg(_fake_ffmpeg_const(b""))
        with p1, p2, pytest.raises(SignatureError) as exc:
            compute_scope_signature(src, _make_scope())
        assert exc.value.reason == "empty_frames"

    def test_empty_scope_raises(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import SignatureError, compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        degenerate = types.SimpleNamespace(
            start_us=2_000_000, end_us=2_000_000, scope_digest=_SHA64_A, asset_id="a1"
        )
        with pytest.raises(SignatureError) as exc:
            compute_scope_signature(src, degenerate)
        assert exc.value.reason == "empty_scope"

    def test_ffmpeg_not_found_raises(self) -> None:
        from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError
        from nexus_ai_agent.nagar.ibpc.signature import SignatureError, compute_scope_signature

        # the resolver raises FfmpegUnavailableError (a RuntimeError, NOT an OSError)
        with (
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin",
                side_effect=FfmpegUnavailableError("none"),
            ),
            pytest.raises(SignatureError) as exc,
        ):
            compute_scope_signature(Path("/x.mp4"), _make_scope())
        assert exc.value.reason == "ffmpeg_not_found"

    def test_max_frames_out_of_range_raises(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with pytest.raises(ValueError):
            compute_scope_signature(src, _make_scope(), max_frames=1)
        with pytest.raises(ValueError):
            compute_scope_signature(src, _make_scope(), max_frames=33)

    def test_timeout_translated(self, tmp_path: Path) -> None:
        import subprocess

        from nexus_ai_agent.nagar.ibpc.signature import SignatureError, compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with (
            mock.patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)
            ),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
            pytest.raises(SignatureError) as exc,
        ):
            compute_scope_signature(src, _make_scope())
        assert exc.value.reason == "timeout"


# ---------------------------------------------------------------------------
# ScopeMatchEngine
# ---------------------------------------------------------------------------


def _l2(vec):
    norm = sum(x * x for x in vec) ** 0.5
    return tuple(x / norm for x in vec) if norm else tuple(vec)


def _make_engine_sig(seed: str, histogram=None, motion: float = 0.01):
    from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature

    if histogram is None:
        histogram = tuple([1.0 / 8.0] * 64)  # L2 norm == 1
    return ScopeSignature(
        scope_digest="sha256:" + hashlib.sha256(seed.encode()).hexdigest(),
        asset_id="a1",
        start_us=0,
        end_us=1_000_000,
        color_histogram=_l2(histogram),
        motion_magnitude=motion,
        sample_frames=5,
        computed_at="2026-01-01T00:00:00+00:00",
        signature_digest=_SHA64_C,
    )


class TestScopeMatchEngine:
    def test_identical_candidate_scores_one(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")
        cand = _make_engine_sig("c", histogram=query.color_histogram)
        matches = ScopeMatchEngine().find_similar(query, [cand])
        assert len(matches) == 1
        assert matches[0].cosine_similarity == pytest.approx(1.0)

    def test_sorted_descending(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")
        # candidate 0 identical, others drift away from the query distribution
        cands = [_make_engine_sig(f"c{i}", histogram=query.color_histogram) for i in range(4)]
        matches = ScopeMatchEngine().find_similar(query, cands, threshold=0.0, max_results=4)
        sims = [m.cosine_similarity for m in matches]
        assert sims == sorted(sims, reverse=True)

    def test_default_threshold_filters(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")
        orth = tuple([0.0] * 32 + [1.0 / 8.0] * 32)  # disjoint support → cosine 0
        cand = _make_engine_sig("c", histogram=orth)
        assert ScopeMatchEngine().find_similar(query, [cand]) == []

    def test_max_results_limits(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")
        cands = [_make_engine_sig(f"c{i}", histogram=query.color_histogram) for i in range(5)]
        matches = ScopeMatchEngine().find_similar(query, cands, threshold=0.0, max_results=3)
        assert len(matches) == 3

    def test_max_results_zero_returns_empty(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")
        cands = [_make_engine_sig("c", histogram=query.color_histogram)]
        assert ScopeMatchEngine().find_similar(query, cands, threshold=0.0, max_results=0) == []

    def test_empty_candidates_returns_empty(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        assert ScopeMatchEngine().find_similar(_make_engine_sig("q"), []) == []

    def test_threshold_out_of_range_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")
        with pytest.raises(ValueError):
            ScopeMatchEngine().find_similar(query, [], threshold=1.5)
        with pytest.raises(ValueError):
            ScopeMatchEngine().find_similar(query, [], threshold=-0.1)

    def test_negative_max_results_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")
        with pytest.raises(ValueError):
            ScopeMatchEngine().find_similar(query, [], max_results=-1)

    def test_tie_break_is_deterministic(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")
        # two identical-histogram candidates → equal similarity → digest tie-break
        c1 = _make_engine_sig("zzz", histogram=query.color_histogram)
        c2 = _make_engine_sig("aaa", histogram=query.color_histogram)
        out1 = ScopeMatchEngine().find_similar(query, [c1, c2], threshold=0.0, max_results=2)
        out2 = ScopeMatchEngine().find_similar(query, [c2, c1], threshold=0.0, max_results=2)
        assert [m.candidate_scope_digest for m in out1] == [m.candidate_scope_digest for m in out2]
        assert out1[0].candidate_scope_digest < out1[1].candidate_scope_digest

    def test_motion_delta_is_absolute_difference(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q", motion=0.5)
        cand = _make_engine_sig("c", histogram=query.color_histogram, motion=0.1)
        match = ScopeMatchEngine().find_similar(query, [cand])[0]
        assert match.motion_delta == pytest.approx(0.4)

    def test_dissimilar_candidate_is_filtered(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        query = _make_engine_sig("q")  # uniform 64-bin histogram
        # disjoint support (first 32 bins only) → cosine ≈ 0.707 < 0.99
        disjoint = tuple([1.0] * 32 + [0.0] * 32)
        cand = _make_engine_sig("c", histogram=disjoint)
        assert ScopeMatchEngine().find_similar(query, [cand], threshold=0.99) == []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_all_symbols_exist(self) -> None:
        from nexus_ai_agent.nagar.ibpc import signature as sig

        for name in sig.__all__:
            assert hasattr(sig, name), name

    def test_expected_exports(self) -> None:
        from nexus_ai_agent.nagar.ibpc import signature as sig

        expected = {
            "SIGNATURE_ALGORITHM_VERSION",
            "SIGNATURE_SCHEMA_VERSION",
            "ScopeMatch",
            "ScopeMatchEngine",
            "ScopeSignature",
            "SignatureError",
            "compute_scope_signature",
            "cosine_similarity",
        }
        assert expected <= set(sig.__all__)


# ---------------------------------------------------------------------------
# Stage 4 contract (NAGAR-IBPC-TRIAD-CLOSURE-01): exact spec validation,
# frame-size and exit checks, a temporal motion test, and digest verification.
# ---------------------------------------------------------------------------

_INTEGRITY_DIGEST = "sha256:" + "c" * 64


def _integrity_sig(**overrides):
    from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature

    kw = dict(
        scope_digest="sha256:" + "a" * 64,
        asset_id="a1",
        start_us=0,
        end_us=2_000_000,
        color_histogram=tuple([1.0 / 8.0] * 64),
        motion_magnitude=1.5,
        sample_frames=4,
        computed_at="2026-11-10T00:00:00+00:00",
        signature_digest=_INTEGRITY_DIGEST,
    )
    kw.update(overrides)
    return ScopeSignature(**kw)


class TestSignatureContract:
    def test_unnormalised_histogram_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _integrity_sig(color_histogram=tuple([0.5] * 64))

    def test_infinite_motion_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _integrity_sig(motion_magnitude=float("inf"))

    def test_motion_above_the_8bit_range_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _integrity_sig(motion_magnitude=256.0)

    def test_scope_must_be_non_empty(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _integrity_sig(start_us=2_000_000, end_us=2_000_000)

    def test_nan_similarity_is_rejected(self) -> None:
        from pydantic import ValidationError

        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatch

        with pytest.raises(ValidationError):
            ScopeMatch(
                candidate_scope_digest="sha256:" + "c" * 64,
                cosine_similarity=float("nan"),
                motion_delta=0.0,
            )

    def test_nan_threshold_is_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine

        q = _integrity_sig()
        with pytest.raises(ValueError):
            ScopeMatchEngine().find_similar(q, [], threshold=float("nan"))


class TestMotionIsTemporal:
    def test_motion_depends_on_frame_order_while_histogram_does_not(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import _accumulate_histogram, _motion_magnitude

        a = bytes([255, 0, 0]) * 4  # red
        b = bytes([0, 0, 255]) * 4  # blue
        alternating = [a, b, a, b]
        grouped = [a, a, b, b]
        # identical multiset of frames → identical colour histogram …
        assert _accumulate_histogram(alternating) == _accumulate_histogram(grouped)
        # … but the temporal change differs: 3 changes vs 1 change over 3 pairs
        m_alt = _motion_magnitude(alternating)
        m_grp = _motion_magnitude(grouped)
        assert m_alt > 0.0
        assert abs(m_alt - 3.0 * m_grp) < 1e-9

    def test_static_scope_has_exactly_zero_motion(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import _motion_magnitude

        a = bytes([10, 200, 90]) * 4
        assert _motion_magnitude([a, a, a]) == 0.0

    def test_full_swing_is_bounded_by_the_8bit_range(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import _motion_magnitude

        black = bytes(12)
        white = bytes([255] * 12)
        assert _motion_magnitude([black, white]) == pytest.approx(255.0)


class TestCaptureIntegrity:
    def _run(self, stdout: bytes, returncode: int, tmp_path: Path) -> str:
        from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope
        from nexus_ai_agent.nagar.ibpc.signature import SignatureError, compute_scope_signature

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        scope = VideoTimeScope(asset_id="a1", effect="keep", start_us=0, end_us=2_000_000)

        def _proc(*args, **kwargs):
            r = mock.Mock()
            r.stdout = stdout
            r.returncode = returncode
            return r

        with (
            mock.patch("subprocess.run", side_effect=_proc),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
            pytest.raises(SignatureError) as exc,
        ):
            compute_scope_signature(src, scope, max_frames=2)
        return exc.value.reason

    def test_truncated_frame_is_refused_not_silently_measured(self, tmp_path: Path) -> None:
        assert self._run(FRAME_A[:100], 0, tmp_path) == "bad_frame_size"

    def test_nonzero_exit_with_full_size_bytes_is_refused(self, tmp_path: Path) -> None:
        assert self._run(FRAME_A, 1, tmp_path) == "ffmpeg_failed"

    def test_empty_sample_is_refused_not_dropped(self, tmp_path: Path) -> None:
        assert self._run(b"", 0, tmp_path) == "empty_frames"


class TestSignatureDigestVerification:
    def test_verifier_accepts_untampered_and_refuses_altered_content(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import (
            SignatureError,
            _stable_digest,
            verify_scope_signature,
        )

        hist = tuple([1.0 / 8.0] * 64)
        digest = _stable_digest(
            scope_digest="sha256:" + "a" * 64,
            asset_id="a1",
            start_us=0,
            end_us=2_000_000,
            histogram=hist,
            motion_magnitude=1.5,
            sample_frames=4,
        )
        sig = _integrity_sig(signature_digest=digest)
        verify_scope_signature(sig)  # no raise

        altered = sig.model_copy(update={"motion_magnitude": 2.0})
        with pytest.raises(SignatureError) as exc:
            verify_scope_signature(altered)
        assert exc.value.reason == "signature_digest_mismatch"

