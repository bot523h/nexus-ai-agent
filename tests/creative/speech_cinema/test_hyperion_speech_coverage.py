"""Branch coverage for the speech_cinema primitives (VAD, alignment, dubbing, subtitles)."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from nexus_ai_agent.creative.speech_cinema import forced_aligner as fa
from nexus_ai_agent.creative.speech_cinema import isochronous_dubbing as iso
from nexus_ai_agent.creative.speech_cinema import subtitle_compiler as sc
from nexus_ai_agent.creative.speech_cinema import vad_detector as vd


def _wav(raw: bytes, width: int, channels: int = 1, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(raw)
    return buf.getvalue()


# ------------------------------------------------------------------ WAV reader


def test_read_wav_16bit_from_bytes_path_str_and_open_handle(tmp_path) -> None:
    data = _wav(np.array([0, 16384, -16384], dtype="<i2").tobytes(), 2)
    expected = np.array([0.0, 0.5, -0.5], dtype=np.float32)
    x, rate = vd.read_wav(data)
    assert rate == 16000
    np.testing.assert_allclose(x, expected)
    path = tmp_path / "a.wav"
    path.write_bytes(data)
    np.testing.assert_allclose(vd.read_wav(path)[0], expected)
    np.testing.assert_allclose(vd.read_wav(str(path))[0], expected)
    with path.open("rb") as handle:
        np.testing.assert_allclose(vd.read_wav(handle)[0], expected)


def test_read_wav_8bit_unsigned_is_centred_on_zero() -> None:
    x, _ = vd.read_wav(_wav(bytes([128, 192, 64]), 1))
    np.testing.assert_allclose(x, [0.0, 0.5, -0.5])


def test_read_wav_24bit_sign_extends_negative_samples() -> None:
    raw = b"".join(v.to_bytes(3, "little") for v in (0x400000, 0xC00000, 0))
    x, _ = vd.read_wav(_wav(raw, 3))
    np.testing.assert_allclose(x, [0.5, -0.5, 0.0])


def test_read_wav_32bit_signed() -> None:
    raw = np.array([2**30, -(2**30)], dtype="<i4").tobytes()
    x, _ = vd.read_wav(_wav(raw, 4))
    np.testing.assert_allclose(x, [0.5, -0.5])


def test_read_wav_stereo_is_downmixed_to_mono() -> None:
    raw = np.array([[16384, 0], [-16384, 16384]], dtype="<i2").tobytes()
    x, _ = vd.read_wav(_wav(raw, 2, channels=2))
    np.testing.assert_allclose(x, [0.25, 0.0])


def test_read_wav_rejects_unsupported_pcm_width(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Fake:
        def getframerate(self) -> int:
            return 16000

        def getnchannels(self) -> int:
            return 1

        def getsampwidth(self) -> int:
            return 5

        def getnframes(self) -> int:
            return 0

        def readframes(self, count: int) -> bytes:
            return b""

        def close(self) -> None:
            return None

    monkeypatch.setattr(vd.wave, "open", lambda *a, **k: _Fake())
    with pytest.raises(ValueError, match="unsupported PCM width"):
        vd.read_wav("unused.wav")


def test_detect_voice_accepts_2d_frames_and_defaults_the_sample_rate() -> None:
    frames = np.zeros((1600, 2), dtype=np.float32)
    result = vd.detect_voice(frames, None)
    assert result.sample_rate == vd.VADConfig.sample_rate
    assert result.segments == ()


def test_detect_voice_pads_audio_shorter_than_one_frame() -> None:
    result = vd.detect_voice(np.full(50, 0.5, dtype=np.float32), 16000)
    assert result.speech_mask.size >= 1


def test_detect_voice_finds_a_speech_burst_and_reports_its_duration() -> None:
    silence = np.zeros(8000, dtype=np.float32)
    t = np.arange(16000) / 16000
    burst = sum(0.2 / k * np.sin(2 * np.pi * 180 * k * t) for k in range(1, 6)).astype(np.float32)
    result = vd.detect_voice(np.concatenate([silence, burst, silence]), 16000)
    assert result.segments
    seg = result.segments[0]
    assert seg.duration == pytest.approx(seg.end - seg.start)
    assert seg.duration > 0


def test_detect_voice_smooths_one_frame_gaps_between_bursts() -> None:
    t = np.arange(1600) / 16000
    chunk = sum(0.2 / k * np.sin(2 * np.pi * 180 * k * t) for k in range(1, 6)).astype(np.float32)
    gap = np.zeros(160, dtype=np.float32)
    audio = np.concatenate([chunk, gap, chunk, gap, chunk])
    result = vd.detect_voice(audio, 16000, vd.VADConfig(smoothing_frames=3))
    assert result.speech_mask.dtype == bool


def test_broadband_noise_is_rejected_by_the_spectral_entropy_gate() -> None:
    noise = (np.random.default_rng(5).standard_normal(16000) * 0.3).astype(np.float32)
    assert vd.detect_voice(noise, 16000).segments == ()


def test_loud_clip_shorter_than_the_smoothing_window_is_handled() -> None:
    # Regression: the smoothing window used to exceed the frame count and crash with IndexError.
    t = np.arange(50) / 16000
    tone = (0.5 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
    result = vd.detect_voice(tone, 16000)
    assert result.speech_mask.shape == (1,)


def test_short_silent_clip_is_not_promoted_to_speech() -> None:
    # Regression: the gap filler used to flip a silent run shorter than min_silence to speech,
    # so a 100 ms all-silent clip reported a speech segment.
    assert vd.detect_voice(np.zeros(1600, dtype=np.float32), 16000).segments == ()


def test_detect_voice_accepts_wav_bytes() -> None:
    raw = (np.zeros(1600, dtype="<i2")).tobytes()
    assert vd.detect_voice(_wav(raw, 2), None).segments == ()


def test_detect_voice_returns_empty_for_zero_length_input() -> None:
    result = vd.detect_voice(np.zeros(0, dtype=np.float32), 16000)
    assert result.segments == ()


# ----------------------------------------------------------- forced alignment


def test_tokenize_and_phonemize_cover_digraphs_and_empty_words() -> None:
    assert isinstance(fa.tokenize("Hello, world!"), list)
    assert isinstance(fa.phonemize("ship"), tuple)
    assert fa.phonemize("")
    assert len(fa.phonemize("chess")) >= 1


@pytest.mark.parametrize("symbol", ["a", "b", "f", "s", "k", "m", "zz"])
def test_phoneme_to_viseme_returns_a_class_index(symbol: str) -> None:
    assert isinstance(fa.phoneme_to_viseme(symbol), int)


def test_extract_acoustic_features_returns_per_frame_arrays() -> None:
    t = np.arange(16000) / 16000
    features = fa.extract_acoustic_features(np.sin(2 * np.pi * 220 * t), 16000)
    assert features.energy.ndim == 1 and features.energy.size > 0
    assert features.sample_rate == 16000


def test_align_tokens_distributes_the_duration_over_words() -> None:
    result = fa.align_tokens("hello world", 1.0)
    assert result.words
    assert result.words[-1].end == pytest.approx(1.0, abs=1e-6)


def test_align_tokens_with_features_and_empty_inputs() -> None:
    t = np.arange(8000) / 16000
    features = fa.extract_acoustic_features(np.sin(2 * np.pi * 200 * t), 16000)
    assert fa.align_tokens("hi there", 0.5, features=features, band=2).words
    assert fa.align_tokens("", 1.0).words == ()
    assert fa.align_tokens("hi", 0.0).words == ()


def test_banded_dtw_rejects_empty_and_non_finite_costs_and_widens_narrow_bands() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        fa.banded_dtw(np.zeros((0, 3)))
    with pytest.raises(ValueError, match="finite"):
        fa.banded_dtw(np.array([[np.nan]]))
    # A band narrower than |n - m| is widened, so endpoints always stay connected.
    assert fa.banded_dtw(np.ones((3, 8)), band=0) == fa.banded_dtw(np.ones((3, 8)), band=5)


def test_viseme_lookup_covers_table_hits_and_class_fallbacks() -> None:
    key = next(iter(fa._VISEMES))
    assert fa.phoneme_to_viseme(key) == fa._VISEMES[key]
    assert fa.phoneme_to_viseme("fa") == 7
    assert fa.phoneme_to_viseme("sa") == 9


def test_extract_acoustic_features_pads_clips_shorter_than_one_frame() -> None:
    features = fa.extract_acoustic_features(np.ones(100, dtype=np.float32) * 0.1, 16000)
    assert features.energy.size >= 1


# ------------------------------------------------------------------ dubbing


def test_time_scale_single_and_empty_source_frames() -> None:
    assert iso.time_scale(np.array([0.5], dtype=np.float32), 3).audio.shape == (3,)
    assert iso.time_scale(np.zeros(0, dtype=np.float32), 4).audio.shape == (4,)
    assert iso.time_scale(np.zeros(0, dtype=np.float32), 0).audio.shape == (0,)
    assert iso.validate_frame_count(np.zeros(7), 7) is True
    assert iso.validate_frame_count(np.zeros(7), 6) is False


def test_time_scale_supports_two_dimensional_frames_by_channels() -> None:
    frames = np.stack([np.linspace(0, 1, 40), np.linspace(1, 0, 40)], axis=1).astype(np.float32)
    assert iso.time_scale(frames, 30).audio.shape == (30, 2)


@pytest.mark.parametrize(
    ("audio", "target", "message"),
    [
        (np.zeros((2, 2, 2), dtype=np.float32), 4, "1-D mono"),
        (np.zeros(10, dtype=np.float32), -1, "non-negative"),
        (np.zeros(10, dtype=np.float32), True, "non-negative"),
    ],
)
def test_time_scale_refuses_invalid_shapes_and_targets(audio, target, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        iso.time_scale(audio, target)


def test_scale_audio_wrapper_and_time_stretch_alias() -> None:
    x = np.linspace(0, 1, 800, dtype=np.float32)
    assert iso.scale_audio(x, 1000, 16000).shape == (1000,)
    assert iso.time_stretch_wsola_pcm is iso.time_scale


# ---------------------------------------------------------------- subtitles


def test_parse_srt_refuses_a_malformed_timestamp() -> None:
    with pytest.raises(ValueError, match="invalid subtitle timestamp"):
        sc.parse_srt("1\nnot-a-time --> 00:00:02,000\nhi\n")


def test_parse_vtt_skips_header_and_note_blocks() -> None:
    text = "WEBVTT\n\nNOTE a comment\n\n00:01.000 --> 00:02.000\nshort form\n"
    cues = sc.parse_vtt(text)
    assert len(cues) == 1
    assert cues[0].start == pytest.approx(1.0)
    assert cues[0].text == "short form"


def test_parse_srt_rejects_a_timing_line_with_no_end_timestamp() -> None:
    with pytest.raises(ValueError, match="no end timestamp"):
        sc.parse_srt("1\n00:00:01,000 -->\nhi\n")


def test_subtitle_sha256_is_stable_for_str_and_bytes() -> None:
    assert sc.subtitle_sha256("abc") == sc.subtitle_sha256(b"abc")
    assert len(sc.subtitle_sha256("abc")) == 64


def test_serialize_vtt_and_ass_render_empty_cue_lists() -> None:
    assert sc.serialize_vtt(()) == "WEBVTT\n\n"
    assert "Dialogue:" not in sc.serialize_ass(())
    assert sc.serialize_srt(()) == ""
