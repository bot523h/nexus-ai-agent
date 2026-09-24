"""Wave 9 speech engines: fail-closed without extras, exact mapping with stubs.

faster-whisper / argostranslate are optional extras — these tests prove both
sides: without the extra the engines fail closed with typed errors, and with
a stubbed backend the mapping (µs, scores, turns, coverage) is exact.  The
pack side proves ``caption`` pending is 0 and the three new pure handlers.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from nagar_helpers import command_for

from nexus_ai_agent.adapters.whisper_local import (
    ArgosLocalTranslator,
    TranslateProfileUnavailableError,
    WhisperLocalCaptionEngine,
    energy_anchors,
    turns_from_anchors,
)
from nexus_ai_agent.application.ports.caption_engine import (
    CaptionEngineError,
    CaptionProfileUnavailableError,
)
from nexus_ai_agent.creative.packs.caption.models import (
    TranscriptRef,
    TranscriptSegment,
    WordTiming,
)
from nexus_ai_agent.creative.packs.caption.operations import build_caption_registry
from nexus_ai_agent.creative.packs.registry import PackRegistry
from nexus_ai_agent.creative.studio.capabilities import OperationContext
from nexus_ai_agent.creative.studio.models import Project, Timeline

# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------


class _StubWord:
    def __init__(self, word: str, start: float, end: float, probability: float) -> None:
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class _StubSegment:
    def __init__(
        self,
        start: float,
        end: float,
        text: str,
        words: list[_StubWord] | None = None,
        avg_logprob: float = -0.2,
    ) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.words = words
        self.avg_logprob = avg_logprob


class _StubInfo:
    language = "fa"
    duration = 4.0


class _StubModel:
    instances = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).instances += 1
        self.args = args
        self.kwargs = kwargs

    def transcribe(self, *args: Any, **kwargs: Any) -> Any:
        words = [_StubWord("hello", 0.1, 0.4, 0.9), _StubWord("world", 0.5, 0.9, 0.8)]
        return (
            [
                _StubSegment(0.0, 1.0, "hello world", words),
                _StubSegment(2.0, 3.0, "no word timings here", None),
            ],
            _StubInfo(),
        )


def _install_faster_whisper_stub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    _StubModel.instances = 0
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _StubModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return module


def _install_argos_stub(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, str, str]]) -> None:
    module = types.ModuleType("argostranslate.translate")

    def _translate(text: str, source: str, target: str) -> str:
        calls.append((text, source, target))
        return f"[{target}]{text}"

    module.translate = _translate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "argostranslate.translate", module)


def _transcript() -> TranscriptRef:
    return TranscriptRef(
        transcript_id="tr_01",
        source_asset_id="audio_1",
        language="en",
        duration_us=4_000_000,
        segments=(
            TranscriptSegment(start_us=0, end_us=1_000_000, text="hello world"),
            TranscriptSegment(start_us=1_100_000, end_us=2_000_000, text="second line here"),
            TranscriptSegment(start_us=3_500_000, end_us=4_000_000, text="far away"),
        ),
    )


def _project() -> Project:
    return Project(project_id="p1", name="t", timeline=Timeline(timeline_id="t1", duration_us=0))


def _ctx(operation: str, payload: dict[str, Any]) -> OperationContext:
    return OperationContext(
        command=command_for("p1", command_id="c1", operation=operation),
        input_data=payload,
        history=(),
    )


# ---------------------------------------------------------------------------
# fail-closed without extras
# ---------------------------------------------------------------------------


async def test_unavailable_without_speech_extra_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    engine = WhisperLocalCaptionEngine()
    assert engine.is_available() is False
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(CaptionProfileUnavailableError) as exc_info:
        await engine.transcribe(audio)
    assert exc_info.value.code == "caption_profile_unavailable"
    assert "nexus-ai-agent[speech]" in str(exc_info.value)


async def test_missing_audio_file_is_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_faster_whisper_stub(monkeypatch)
    engine = WhisperLocalCaptionEngine()
    assert engine.is_available() is True
    with pytest.raises(CaptionEngineError, match="audio file not found"):
        await engine.transcribe(tmp_path / "ghost.wav")


# ---------------------------------------------------------------------------
# mapping with a stubbed backend
# ---------------------------------------------------------------------------


async def test_transcribe_maps_segments_words_and_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_faster_whisper_stub(monkeypatch)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF....")
    engine = WhisperLocalCaptionEngine(model="tiny")
    first = await engine.transcribe(audio)
    second = await engine.transcribe(audio, language="en")
    assert first.transcript_id == second.transcript_id
    assert first.transcript_id.startswith("whisper:tiny:")
    assert second.language == "en"  # explicit wins
    assert first.language == "fa"  # detected fallback
    assert first.duration_us == 4_000_000
    seg0, seg1 = first.segments
    assert (seg0.start_us, seg0.end_us) == (0, 1_000_000)
    assert (
        seg0.words[0].model_dump()
        == WordTiming(word="hello", start_us=100_000, end_us=400_000, score=0.9).model_dump()
    )
    assert seg0.score == pytest.approx(0.8)
    # Segment without word timings gets an exact-cover projection.
    assert [w.word for w in seg1.words] == ["no", "word", "timings", "here"]
    assert seg1.words[0].start_us == seg1.start_us
    assert seg1.words[-1].end_us == seg1.end_us


def test_model_load_is_lazy_and_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_faster_whisper_stub(monkeypatch)
    engine = WhisperLocalCaptionEngine()
    assert engine.is_available() is True
    assert _StubModel.instances == 0  # no import-time / check-time load
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    engine.transcribe_sync(audio)
    engine.transcribe_sync(audio)
    assert _StubModel.instances == 1


# ---------------------------------------------------------------------------
# energy VAD + anchor turns (pure)
# ---------------------------------------------------------------------------


def _tone_run(sample_rate: int = 16000) -> np.ndarray:
    tone = (np.sin(2 * np.pi * 440 * np.arange(8000) / 16000) * 20000).astype(np.int16)
    silence = np.zeros(8000, dtype=np.int16)
    return np.concatenate([tone, silence, tone])


def test_energy_anchors_finds_two_speech_runs() -> None:
    anchors = energy_anchors(_tone_run())
    assert len(anchors) == 2
    (s0, e0), (s1, e1) = anchors
    assert abs(s0 - 0) < 60_000 and abs(e0 - 500_000) < 60_000
    assert abs(s1 - 1_000_000) < 60_000 and abs(e1 - 1_500_000) < 60_000
    assert energy_anchors(np.zeros(16000, dtype=np.int16)) == ()
    assert energy_anchors(np.zeros(0, dtype=np.int16)) == ()


def test_turns_from_anchors_merges_near_runs_and_alternates() -> None:
    segments = _transcript().segments
    # Two close runs merge (gap 100ms <= 800ms) -> one turn; far run -> second.
    turns = turns_from_anchors(
        segments, ((0, 1_000_000), (1_100_000, 2_000_000), (3_500_000, 4_000_000))
    )
    assert [t.speaker for t in turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert (turns[0].start_us, turns[0].end_us) == (0, 2_000_000)
    assert turns[0].text == "hello world second line here"
    single = turns_from_anchors(segments, ())
    assert len(single) == 1 and single[0].speaker == "SPEAKER_00"


def test_adapter_diarize_stamps_merged_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_faster_whisper_stub(monkeypatch)
    import nexus_ai_agent.adapters.whisper_local as adapter_mod

    long_tone = np.concatenate([_tone_run(), _tone_run()])  # ~3s, 4 runs
    monkeypatch.setattr(adapter_mod, "decode_mono_16k", lambda _p: long_tone)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    engine = WhisperLocalCaptionEngine()
    out = engine.diarize_sync(audio, gap_threshold_us=100_000)
    assert len(out.speaker_turns) >= 2
    for seg in out.segments:
        assert seg.speaker is not None and seg.speaker.startswith("SPEAKER_")
        assert all(w.speaker == seg.speaker for w in seg.words)
    assert out.transcript_id.endswith(":diarized")


# ---------------------------------------------------------------------------
# offline translation
# ---------------------------------------------------------------------------


async def test_argos_missing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "argostranslate.translate", None)
    translator = ArgosLocalTranslator()
    assert translator.is_available() is False
    with pytest.raises(TranslateProfileUnavailableError) as exc_info:
        await translator.translate_transcript(_transcript(), "fa")
    assert exc_info.value.code == "translate_profile_unavailable"


async def test_argos_stub_translates_and_reprojects_words(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []
    _install_argos_stub(monkeypatch, calls)
    translator = ArgosLocalTranslator()
    assert translator.is_available() is True
    out = await translator.translate_transcript(_transcript(), "fa-IR")
    assert out.language == "fa-ir".split("-")[0]
    assert calls[0] == ("hello world", "en", "fa")
    assert out.segments[0].text == "[fa]hello world"
    # Timings preserved; words re-projected with exact cover.
    assert (out.segments[0].start_us, out.segments[0].end_us) == (0, 1_000_000)
    assert out.segments[0].words[0].start_us == 0
    assert out.segments[0].words[-1].end_us == 1_000_000


# ---------------------------------------------------------------------------
# pack side: registration + the three pure handlers
# ---------------------------------------------------------------------------


def test_caption_pending_is_zero_and_activates() -> None:
    registry = build_caption_registry()
    for op in ("caption.align_words", "caption.diarize", "caption.translate_local"):
        assert registry.get_spec(op).deterministic is True
    packs = PackRegistry(registry)
    caption = next(p for p in packs.register_builtin() if p.package_id == "nexus.language.caption")
    assert caption.pending_capabilities == ()
    assert packs.activate(caption.package_id).active is True


def test_align_words_handler_projects_exact_cover_and_clamps() -> None:
    registry = build_caption_registry()
    project = _project()
    spec = registry.get_spec("caption.align_words")
    outcome = spec.handler(project, _ctx("caption.align_words", {"transcript": _transcript()}))
    assert outcome.output["projected_segment_count"] == 3
    assert outcome.output["aligned_word_count"] == 2 + 3 + 2
    words = outcome.output["transcript"]["segments"][0]["words"]
    assert words[0]["start_us"] == 0
    assert words[-1]["end_us"] == 1_000_000
    # Out-of-bounds words are clamped into the segment span.
    bad = TranscriptRef(
        transcript_id="bad",
        language="en",
        segments=(
            TranscriptSegment(
                start_us=1_000_000,
                end_us=2_000_000,
                text="x",
                words=(WordTiming(word="x", start_us=0, end_us=9_000_000),),
            ),
        ),
    )
    clamped = spec.handler(project, _ctx("caption.align_words", {"transcript": bad}))
    only = clamped.output["transcript"]["segments"][0]["words"][0]
    assert (only["start_us"], only["end_us"]) == (1_000_000, 2_000_000)


def test_diarize_handler_merges_then_stamps_no_stale_labels() -> None:
    registry = build_caption_registry()
    project = _project()
    stale = TranscriptRef(
        transcript_id="stale",
        language="en",
        segments=(
            TranscriptSegment(start_us=0, end_us=500_000, text="one", speaker="SPEAKER_09"),
            TranscriptSegment(start_us=600_000, end_us=1_000_000, text="two", speaker="SPEAKER_07"),
            TranscriptSegment(
                start_us=5_000_000, end_us=5_500_000, text="three", speaker="SPEAKER_00"
            ),
        ),
    )
    spec = registry.get_spec("caption.diarize")
    outcome = spec.handler(project, _ctx("caption.diarize", {"transcript": stale}))
    assert outcome.output["turn_count"] == 2
    segs = outcome.output["transcript"]["segments"]
    # First two merged into ONE turn: stale per-segment labels are gone.
    assert segs[0]["speaker"] == segs[1]["speaker"] == "SPEAKER_00"
    assert segs[2]["speaker"] == "SPEAKER_01"


def test_translate_local_handler_reports_honest_coverage() -> None:
    registry = build_caption_registry()
    project = _project()
    spec = registry.get_spec("caption.translate_local")
    outcome = spec.handler(
        project,
        _ctx(
            "caption.translate_local",
            {
                "transcript": _transcript(),
                "target_language": "fa",
                "glossary": {"hello": "سلام", "world": "دنیا"},
            },
        ),
    )
    assert outcome.output["target_language"] == "fa"
    assert outcome.output["glossary_hits"] == 2
    assert outcome.output["glossary_total"] == 7
    assert outcome.output["coverage"] == pytest.approx(2 / 7)
    first = outcome.output["transcript"]["segments"][0]
    assert first["text"] == "سلام دنیا"
    # Unknown words pass through; timings untouched.
    assert outcome.output["transcript"]["segments"][2]["text"] == "far away"
    assert outcome.output["transcript"]["segments"][0]["end_us"] == 1_000_000
