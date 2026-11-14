from fractions import Fraction

import numpy as np

from nexus_ai_agent.creative.speech_cinema.forced_aligner import (
    align_tokens,
    banded_dtw,
    phoneme_to_viseme,
)
from nexus_ai_agent.creative.speech_cinema.isochronous_dubbing import time_scale
from nexus_ai_agent.creative.speech_cinema.subtitle_compiler import (
    AlignedWord,
    parse_srt,
    partition_cues,
    serialize_srt,
    serialize_vtt,
    subtitle_sha256,
)
from nexus_ai_agent.creative.speech_cinema.vad_detector import VADConfig, detect_voice


def test_vad_fraction_segments_and_snr():
    x = np.zeros(16000, dtype=np.float32)
    x[4000:10000] = 0.3
    r = detect_voice(
        x,
        16000,
        VADConfig(
            energy_threshold_db=-35, entropy_threshold=1.0, min_speech_ms=40, min_silence_ms=40
        ),
    )
    assert r.segments and all(isinstance(s.start, Fraction) for s in r.segments)
    assert r.snr_db >= 0


def test_alignment_monotonic_and_visemes():
    r = align_tokens("Hello world", 2.0)
    assert len(r.words) == 2
    assert r.words[0].end <= r.words[1].start
    assert all(p.end >= p.start and p.viseme >= 0 for w in r.words for p in w.phonemes)
    assert phoneme_to_viseme("a") != phoneme_to_viseme("m")


def test_dtw_and_exact_frame_count_multichannel():
    cost = np.eye(4)
    total, path = banded_dtw(cost, 1)
    assert total == 2.0 and path[0] == (0, 0)
    x = np.arange(20, dtype=np.float32).reshape(10, 2)
    result = time_scale(x, 17)
    assert result.audio.shape == (17, 2)


def test_subtitle_partition_shot_guard_and_codecs():
    words = [AlignedWord("One", 0, 0.5), AlignedWord("two", 0.5, 1), AlignedWord("Three", 1.1, 1.6)]
    cues = partition_cues(words, shot_cuts=[1.0])
    assert len(cues) == 2
    srt = serialize_srt(cues)
    assert parse_srt(srt) == cues
    assert serialize_vtt(cues).startswith("WEBVTT")
    assert subtitle_sha256(srt) == subtitle_sha256(srt.encode())
