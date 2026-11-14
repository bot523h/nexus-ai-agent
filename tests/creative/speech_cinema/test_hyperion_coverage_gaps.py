"""Branch coverage for speech_cinema guards that the wider suites did not reach."""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.speech_cinema import forced_aligner as fa
from nexus_ai_agent.creative.speech_cinema import subtitle_compiler as sc


def test_partition_refuses_a_non_positive_frame_rate() -> None:
    word = sc.AlignedWord(text="hi", start=0.0, end=0.5)
    with pytest.raises(ValueError, match="fps must be positive"):
        sc.partition_cues([word], fps=0.0)


def test_partition_of_no_words_is_empty() -> None:
    assert sc.partition_cues([]) == ()


def test_unfixable_straddle_keeps_original_times_and_is_flagged() -> None:
    # The start is clamped to the cut (within 3 frames before it), which leaves no room
    # for the cue to end before the cut.  The guard must then keep the original times
    # and flag the cue rather than emit an inverted span.
    word = sc.AlignedWord(text="hi", start=0.80, end=0.81)
    (cue,) = sc.partition_cues([word], shot_cuts=[0.85])
    assert (cue.start, cue.end) == (0.80, 0.81)
    assert cue.flagged


def test_straddling_word_is_snapped_before_the_cut_and_not_flagged_by_the_guard() -> None:
    word = sc.AlignedWord(text="hi", start=0.0, end=0.5)
    (cue,) = sc.partition_cues([word], shot_cuts=[0.2], max_cps=1000.0)
    assert cue.end == pytest.approx(0.2 - 2 / 25)
    assert not cue.flagged


def test_phoneme_with_unknown_symbol_and_vowel_initial_falls_back_to_class_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fa, "_VISEMES", {})
    assert fa.phoneme_to_viseme("ax") == 1
