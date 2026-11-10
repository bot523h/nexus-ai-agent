"""Unit tests: Scope Signature — CIE-Lab histogram fingerprint + cosine similarity."""
from __future__ import annotations

import math
import unittest.mock as mock
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# cosine_similarity tests
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors_give_one(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity
        v = [1.0, 0.0, 0.0, 1.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-9

    def test_orthogonal_vectors_give_zero(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        assert abs(cosine_similarity(v1, v2)) < 1e-9

    def test_opposite_vectors_give_minus_one(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity
        v1 = [1.0, 0.0]
        v2 = [-1.0, 0.0]
        assert abs(cosine_similarity(v1, v2) - (-1.0)) < 1e-9

    def test_zero_vector_returns_zero(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity
        z = [0.0, 0.0, 0.0]
        assert cosine_similarity(z, z) == 0.0

    def test_different_lengths_raise(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity
        with pytest.raises(ValueError):
            cosine_similarity([1.0, 2.0], [1.0])

    def test_symmetry(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity
        a = [0.3, 0.7, 0.1]
        b = [0.6, 0.2, 0.9]
        assert abs(cosine_similarity(a, b) - cosine_similarity(b, a)) < 1e-12

    def test_normalized_input_still_one(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import cosine_similarity
        v = [1.0 / math.sqrt(3)] * 3
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# ScopeSignature model tests
# ---------------------------------------------------------------------------

class TestScopeSignatureModel:
    def _make_sig(self, **kw):
        from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature
        defaults = dict(
            scope_digest="sha256:" + "a" * 64,
            histogram=tuple([1.0 / 192] * 192),
            motion_mad=0.05,
            frame_count=10,
            sig_digest="sha256:" + "b" * 64,
        )
        defaults.update(kw)
        return ScopeSignature(**defaults)

    def test_valid_construction(self) -> None:
        sig = self._make_sig()
        assert sig.frame_count == 10

    def test_histogram_wrong_length_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature
        with pytest.raises(Exception):
            ScopeSignature(
                scope_digest="sha256:" + "a" * 64,
                histogram=(0.1,) * 10,  # wrong length
                motion_mad=0.0,
                frame_count=1,
                sig_digest="sha256:" + "b" * 64,
            )

    def test_frozen(self) -> None:
        sig = self._make_sig()
        with pytest.raises(Exception):
            sig.frame_count = 999  # type: ignore[misc]

    def test_extra_fields_rejected(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature
        with pytest.raises(Exception):
            ScopeSignature(
                scope_digest="sha256:" + "a" * 64,
                histogram=tuple([0.0] * 192),
                motion_mad=0.0,
                frame_count=1,
                sig_digest="sha256:" + "b" * 64,
                rogue_field="bad",
            )


# ---------------------------------------------------------------------------
# ScopeMatch model tests
# ---------------------------------------------------------------------------

class TestScopeMatchModel:
    def test_valid_match(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatch
        m = ScopeMatch(
            query_digest="sha256:" + "a" * 64,
            candidate_digest="sha256:" + "b" * 64,
            similarity=0.95,
        )
        assert m.similarity == pytest.approx(0.95)

    def test_similarity_out_of_range(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatch
        with pytest.raises(Exception):
            ScopeMatch(
                query_digest="sha256:" + "a" * 64,
                candidate_digest="sha256:" + "b" * 64,
                similarity=1.5,
            )


# ---------------------------------------------------------------------------
# SignatureError tests
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
# compute_scope_signature tests (mocked ffmpeg)
# ---------------------------------------------------------------------------

# A synthetic raw RGB frame: 2×2 pixels, 3 bytes each = 12 bytes
# [R,G,B, R,G,B, R,G,B, R,G,B]
FAKE_FRAME_BYTES = bytes([128, 64, 32, 200, 100, 50, 10, 200, 150, 255, 0, 128])


def _make_scope(start_us=0, end_us=2_000_000):
    from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope
    return VideoTimeScope(asset_id="a1", effect="keep", start_us=start_us, end_us=end_us)


def _fake_ffmpeg_success(*args, **kwargs):
    result = mock.Mock()
    result.stdout = FAKE_FRAME_BYTES
    result.returncode = 0
    return result


def _fake_ffmpeg_empty(*args, **kwargs):
    result = mock.Mock()
    result.stdout = b""
    result.returncode = 0
    return result


class TestComputeScopeSignature:
    def test_returns_scope_signature(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature, compute_scope_signature
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success), \
             mock.patch("nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"):
            sig = compute_scope_signature(src, _make_scope(), source_duration_us=5_000_000)
        assert isinstance(sig, ScopeSignature)
        assert len(sig.histogram) == 192
        assert sig.scope_digest == _make_scope().scope_digest
        assert sig.sig_digest.startswith("sha256:")

    def test_histogram_is_l2_normalized(self, tmp_path: Path) -> None:
        """L2-norm of the histogram must equal 1.0 (ready for cosine similarity)."""
        from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature
        import math
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success), \
             mock.patch("nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"):
            sig = compute_scope_signature(src, _make_scope(), source_duration_us=5_000_000)
        l2_norm = math.sqrt(sum(x * x for x in sig.histogram))
        assert abs(l2_norm - 1.0) < 1e-6

    def test_deterministic(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success), \
             mock.patch("nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"):
            sig1 = compute_scope_signature(src, _make_scope(), source_duration_us=5_000_000)
            sig2 = compute_scope_signature(src, _make_scope(), source_duration_us=5_000_000)
        assert sig1.sig_digest == sig2.sig_digest

    def test_empty_frame_raises(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import SignatureError, compute_scope_signature
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_empty), \
             mock.patch("nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", return_value="ffmpeg"), \
             pytest.raises(SignatureError) as exc:
            compute_scope_signature(src, _make_scope(), source_duration_us=5_000_000)
        assert exc.value.reason == "empty_frames"

    def test_ffmpeg_not_found_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import SignatureError, compute_scope_signature
        with mock.patch("nexus_ai_agent.nagar.ibpc.signature.resolve_ffmpeg_bin", side_effect=FileNotFoundError), \
             pytest.raises(SignatureError) as exc:
            compute_scope_signature(Path("/x.mp4"), _make_scope(), source_duration_us=5_000_000)
        assert exc.value.reason == "ffmpeg_not_found"


# ---------------------------------------------------------------------------
# ScopeMatchEngine tests
# ---------------------------------------------------------------------------

class TestScopeMatchEngine:
    def _make_sig(self, hist_offset: float = 0.0):
        """Create a ScopeSignature with a slightly shifted histogram."""
        from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature
        import hashlib, json
        total = 192
        base = 1.0 / total
        hist = tuple(max(0.0, base + (hist_offset if i < 10 else 0.0)) for i in range(total))
        # re-normalize
        s = sum(hist)
        hist = tuple(x / s for x in hist)
        raw = json.dumps({"h": list(hist)}, sort_keys=True).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        return ScopeSignature(
            scope_digest="sha256:" + hashlib.sha256(f"scope_{hist_offset}".encode()).hexdigest(),
            histogram=hist,
            motion_mad=0.01,
            frame_count=5,
            sig_digest=digest,
        )

    def test_find_similar_returns_matches(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine
        query = self._make_sig(0.0)
        candidates = [self._make_sig(0.001 * i) for i in range(5)]
        engine = ScopeMatchEngine(candidates)
        matches = engine.find_similar(query, top_k=3, threshold=0.0)
        assert len(matches) == 3

    def test_find_similar_sorted_by_similarity(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine
        query = self._make_sig(0.0)
        candidates = [self._make_sig(0.05 * i) for i in range(4)]
        engine = ScopeMatchEngine(candidates)
        matches = engine.find_similar(query, top_k=4, threshold=0.0)
        sims = [m.similarity for m in matches]
        assert sims == sorted(sims, reverse=True)

    def test_threshold_filters_low_matches(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine
        query = self._make_sig(0.0)
        # very different candidates
        candidates = [self._make_sig(0.5 * i) for i in range(3)]
        engine = ScopeMatchEngine(candidates)
        # high threshold should filter out all
        matches = engine.find_similar(query, top_k=10, threshold=0.9999)
        # at least the identical one (i=0) should be there
        assert all(m.similarity >= 0.9999 for m in matches)

    def test_empty_library_returns_empty(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine
        query = self._make_sig()
        engine = ScopeMatchEngine([])
        assert engine.find_similar(query) == []

    def test_top_k_zero_returns_empty(self) -> None:
        from nexus_ai_agent.nagar.ibpc.signature import ScopeMatchEngine
        query = self._make_sig()
        engine = ScopeMatchEngine([self._make_sig(0.1)])
        assert engine.find_similar(query, top_k=0) == []
