"""Wave 9 speech engines: fail-closed without extras, exact mapping with stubs.

faster-whisper / argostranslate are optional extras — these tests prove both
sides: without the extra the engines fail closed with typed errors, and with
a stubbed backend the mapping (µs, scores, turns, coverage) is exact.  The
pack side proves ``caption`` pending is 0 and the three new pure handlers.
"""

from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

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
from nexus_ai_agent.creative.caption.unavailable_adapter import UnavailableCaptionAdapter
from nexus_ai_agent.creative.packs.caption.models import (
    TranscriptRef,
    TranscriptSegment,
    WordTiming,
)
from nexus_ai_agent.creative.packs.caption.operations import build_caption_registry
from nexus_ai_agent.creative.packs.registry import PackRegistry
from nexus_ai_agent.creative.studio.capabilities import OperationContext
from nexus_ai_agent.creative.studio.models import Project, Timeline, TypedCommand

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
    # Stubbed tests opt in explicitly; the adapter must never fetch model names
    # by default. Clear the bounded process cache to isolate each test.
    monkeypatch.setenv("NEXUS_SPEECH_ALLOW_DOWNLOAD", "1")
    import nexus_ai_agent.adapters.whisper_local as adapter_mod

    with adapter_mod._MODEL_CACHE_LOCK:
        adapter_mod._MODEL_CACHE.clear()
    monkeypatch.setattr(adapter_mod, "_probe_audio_duration_us", lambda _path: 1_000_000)
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
        source_sha256="c" * 64,
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
        command=TypedCommand(command_id="c1", operation=operation),
        input_data=payload,
        history=(),
    )


# ---------------------------------------------------------------------------
# fail-closed without extras
# ---------------------------------------------------------------------------


async def test_unavailable_adapter_never_returns_successful_transcript() -> None:
    adapter = UnavailableCaptionAdapter()
    assert adapter.is_available() is False
    with pytest.raises(CaptionProfileUnavailableError):
        await adapter.transcribe("audio.wav")


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


def test_audio_resource_budget_is_enforced_before_model_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_faster_whisper_stub(monkeypatch)
    import nexus_ai_agent.adapters.whisper_local as adapter_mod

    monkeypatch.setattr(
        adapter_mod,
        "_probe_audio_duration_us",
        lambda _path: adapter_mod._MAX_AUDIO_DURATION_US + 1,
    )
    audio = tmp_path / "long.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(CaptionEngineError, match="2-minute in-memory resource budget"):
        WhisperLocalCaptionEngine(model="tiny").transcribe_sync(audio)
    assert _StubModel.instances == 0


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
    repeated = await engine.transcribe(audio)
    assert first.transcript_id == repeated.transcript_id
    assert first.transcript_id != second.transcript_id
    assert first.transcript_id.startswith("whisper:tiny:")
    assert second.language == "en"  # explicit wins
    assert first.language == "fa"  # detected fallback
    assert first.duration_us == 4_000_000
    assert first.engine == "faster-whisper/CTranslate2"
    assert first.model_name == "tiny"
    assert first.model_digest is None  # no digest claimed without hashing model weights
    assert first.source_sha256 == hashlib.sha256(audio.read_bytes()).hexdigest()
    assert first.parameters["compute_type"] == "int8"
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


def test_model_load_is_lazy_and_shared_between_engine_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_faster_whisper_stub(monkeypatch)
    engine = WhisperLocalCaptionEngine()
    second_engine = WhisperLocalCaptionEngine()
    assert engine.is_available() is True
    assert _StubModel.instances == 0  # no import-time / check-time load
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    engine.transcribe_sync(audio)
    second_engine.transcribe_sync(audio)
    assert _StubModel.instances == 1


async def test_invalid_language_rejected_before_model_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_faster_whisper_stub(monkeypatch)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    engine = WhisperLocalCaptionEngine(model="tiny")
    with pytest.raises(CaptionEngineError, match="invalid language tag"):
        await engine.transcribe(audio, language="fa-IR-../path")
    assert _StubModel.instances == 0


async def test_missing_offline_model_fails_without_hub_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_faster_whisper_stub(monkeypatch)
    monkeypatch.delenv("NEXUS_SPEECH_ALLOW_DOWNLOAD")
    engine = WhisperLocalCaptionEngine(model="model-is-not-a-local-directory")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(CaptionProfileUnavailableError, match="download is opt-in"):
        await engine.transcribe(audio)
    assert _StubModel.instances == 0


async def test_inference_keeps_the_event_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import time

    _install_faster_whisper_stub(monkeypatch)

    def slow_transcribe(self: _StubModel, *_args: Any, **_kwargs: Any) -> Any:
        time.sleep(0.08)
        return [], _StubInfo()

    monkeypatch.setattr(_StubModel, "transcribe", slow_transcribe)
    audio = tmp_path / "slow.wav"
    audio.write_bytes(b"RIFF")
    engine = WhisperLocalCaptionEngine(model="tiny")
    ticked = asyncio.Event()
    asyncio.get_running_loop().call_later(0.005, ticked.set)
    work = asyncio.create_task(engine.transcribe(audio))
    await asyncio.wait_for(ticked.wait(), timeout=0.03)
    assert (await work).segments == ()


def test_decimal_microsecond_rounding_is_half_up_and_finite() -> None:
    from nexus_ai_agent.adapters.whisper_local import _us

    assert _us(0.0000005) == 1
    assert _us(0.0000015) == 2
    assert _us(1.2345674) == 1_234_567
    with pytest.raises(CaptionEngineError, match="invalid engine timestamp"):
        _us(float("nan"))
    with pytest.raises(CaptionEngineError, match="invalid engine timestamp"):
        _us(float("inf"))


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
    assert out.source_sha256 == hashlib.sha256(audio.read_bytes()).hexdigest()
    assert out.parameters["diarization_backend"] == "energy-vad-gap-heuristic"
    assert out.parameters["speaker_identity_claimed"] is False


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
    assert out.source_sha256 == _transcript().source_sha256
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


def test_alignment_rebuilds_a_monotone_exact_cover_from_bad_word_times() -> None:
    registry = build_caption_registry()
    project = _project()
    transcript = TranscriptRef(
        transcript_id="adversarial",
        duration_us=2_000_000,
        segments=(
            TranscriptSegment(
                start_us=500_000,
                end_us=1_500_000,
                text="one two three",
                words=(
                    WordTiming(word="one", start_us=900_000, end_us=1_200_000),
                    WordTiming(word="two", start_us=700_000, end_us=1_300_000),
                    WordTiming(word="three", start_us=9_000_000, end_us=9_100_000),
                ),
            ),
            TranscriptSegment(start_us=1_500_000, end_us=1_500_000, text="zero span", words=()),
            TranscriptSegment(start_us=1_500_000, end_us=1_500_000, text="", words=()),
        ),
    )
    outcome = registry.get_spec("caption.align_words").handler(
        project, _ctx("caption.align_words", {"transcript": transcript})
    )
    segments = outcome.output["transcript"]["segments"]
    words = segments[0]["words"]
    assert words[0]["start_us"] == 500_000
    assert words[-1]["end_us"] == 1_500_000
    assert all(a["end_us"] == b["start_us"] for a, b in zip(words, words[1:], strict=False))
    assert all(500_000 <= w["start_us"] <= w["end_us"] <= 1_500_000 for w in words)
    assert all(w["start_us"] == w["end_us"] == 1_500_000 for w in segments[1]["words"])
    assert segments[2]["words"] == []


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
    assert outcome.output["speaker_identity_claimed"] is False
    assert outcome.output["diarization_backend"] == "deterministic-gap-grouping-heuristic"
    assert outcome.output["transcript"]["parameters"]["speaker_identity_claimed"] is False
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
