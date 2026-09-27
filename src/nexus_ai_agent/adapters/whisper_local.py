"""Local speech engines behind the caption ports (Wave 9, ``[speech]``/``[translate]``).

* :class:`WhisperLocalCaptionEngine` implements
  :class:`~nexus_ai_agent.application.ports.caption_engine.CaptionEnginePort`
  with `faster-whisper <https://github.com/SYSTRAN/faster-whisper>`_ — the
  CTranslate2 backend (int8, CPU-only, no torch).  The heavy dependency is
  imported lazily: without the ``[speech]`` extra the engine reports
  unavailable and fails closed with the same typed error as
  :class:`UnavailableCaptionAdapter`.
* :class:`ArgosLocalTranslator` translates transcripts fully offline with
  `argostranslate <https://github.com/argosopentech/argos-translate>`_
  (``[translate]`` extra, official Persian support).

Offline-first rules:

* blocking work (model load, transcribe, decode, translate) always runs in
  :func:`asyncio.to_thread` — the caller's event loop never blocks;
* no network happens on the request path: model download requires explicit
  consent (``allow_download=True`` or ``NEXUS_SPEECH_ALLOW_DOWNLOAD=1``),
  otherwise the HuggingFace hub is forced offline so a missing model fails
  fast with a typed error instead of hanging;
* diarization reuses the pack's pure helpers (merge-then-stamp) — the energy
  VAD here only produces speech anchors, never speaker labels.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from collections import OrderedDict
from contextlib import nullcontext
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import numpy as np

from nexus_ai_agent.application.ports.caption_engine import (
    CaptionEngineError,
    CaptionEnginePort,
    CaptionProfileUnavailableError,
)
from nexus_ai_agent.creative.packs.caption.models import (
    SpeakerTurn,
    TranscriptRef,
    TranscriptSegment,
    WordTiming,
)
from nexus_ai_agent.creative.packs.caption.operations import (
    assign_speakers,
    project_word_timings,
)

SPEECH_EXTRA_HINT = "pip install 'nexus-ai-agent[speech]'"
TRANSLATE_EXTRA_HINT = "pip install 'nexus-ai-agent[translate]'"

_MICROSECONDS_PER_SECOND = 1_000_000
_MAX_AUDIO_DURATION_US = 2 * 60 * _MICROSECONDS_PER_SECOND
_MODEL_CACHE_LOCK = threading.RLock()
_MODEL_CACHE: OrderedDict[tuple[str, str, str, int, str | None], tuple[Any, threading.Lock]] = (
    OrderedDict()
)
_MODEL_CACHE_SIZE = 2
_CPU_INFERENCE_LOCK = threading.Lock()


class TranslateProfileUnavailableError(CaptionEngineError):
    """Raised when offline translation is requested without the ``[translate]`` extra."""

    code: str = "translate_profile_unavailable"


def _us(seconds: float) -> int:
    """Deterministic decimal half-up conversion; reject non-finite engine values."""
    if not math.isfinite(seconds) or seconds < 0:
        raise CaptionEngineError(f"invalid engine timestamp: {seconds!r}")
    return int(
        (Decimal(str(seconds)) * _MICROSECONDS_PER_SECOND).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _sha256_file(path: Path) -> str:
    """Hash file bytes in bounded memory for content-addressed provenance."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise CaptionEngineError(f"could not hash audio source: {error}") from error
    return digest.hexdigest()


def _probe_audio_duration_us(audio_path: Path) -> int:
    """Read container/stream metadata without decoding the audio payload."""
    try:
        av = importlib.import_module("av")
        with av.open(str(audio_path)) as container:
            duration = container.duration
            if duration is not None:
                return _us(float(duration) / float(av.time_base))
            stream_durations = [
                float(stream.duration * stream.time_base)
                for stream in container.streams.audio
                if stream.duration is not None and stream.time_base is not None
            ]
            if stream_durations:
                return _us(max(stream_durations))
    except CaptionEngineError:
        raise
    except Exception as error:
        raise CaptionEngineError(f"could not probe audio duration: {error}") from error
    raise CaptionEngineError("audio duration metadata is unavailable; refusing unbounded inference")


def _language_code(value: str | None) -> str | None:
    """Validate a practical BCP-47 language tag and return Whisper's ISO code."""
    if value is None:
        return None
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
        raise CaptionEngineError(f"invalid language tag: {value!r}")
    return value.split("-")[0].lower()


def _confidence(value: float) -> float:
    """Validate a backend word probability without manufacturing a fallback."""
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise CaptionEngineError(f"invalid engine confidence: {value!r}")
    return value


def _map_raw_segment(raw: Any, index: int) -> TranscriptSegment:
    start_us = _us(raw.start)
    end_us = _us(raw.end)
    text = raw.text.strip()
    words = tuple(
        WordTiming(
            word=word.word.strip() or word.word,
            start_us=_us(word.start),
            end_us=_us(word.end),
            score=_confidence(float(word.probability)),
        )
        for word in (raw.words or ())
        if word.word.strip()
    )
    avg_logprob = float(getattr(raw, "avg_logprob", -1.0))
    if math.isnan(avg_logprob):
        raise CaptionEngineError("faster-whisper returned NaN avg_logprob")
    # This bounded transform is not a calibrated posterior confidence.
    score = 0.0 if avg_logprob == float("-inf") else max(0.0, min(1.0, 1.0 + avg_logprob))
    return TranscriptSegment(
        start_us=start_us,
        end_us=end_us,
        text=text,
        words=words or project_word_timings(text, start_us, end_us),
        segment_id=f"seg_{index:04d}",
        score=round(score, 4),
    )


def energy_anchors(
    pcm_mono_16k: np.ndarray,
    *,
    sample_rate: int = 16000,
    frame_ms: int = 20,
    threshold_db: float = -40.0,
    min_silence_ms: int = 300,
    min_speech_ms: int = 200,
) -> tuple[tuple[int, int], ...]:
    """Speech anchors from mono PCM via an energy VAD with hysteresis (pure).

    Returns ``((start_us, end_us), ...)`` speech runs.  Pure numpy — no I/O —
    so it is unit-testable on synthetic signals.  This is a v1 heuristic, not
    a neural diarizer: anchors mark *speech vs silence*, speaker identity is
    assigned downstream by the pack's gap heuristic.
    """
    if pcm_mono_16k.size == 0:
        return ()
    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    frames = pcm_mono_16k.astype(np.float64)
    pad = (-len(frames)) % frame_len
    if pad:
        frames = np.concatenate([frames, np.zeros(pad)])
    windows = frames.reshape(-1, frame_len)
    rms = np.sqrt(np.mean(windows**2, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms / 32768.0 + 1e-12)
    speech = db > threshold_db

    min_silence_frames = max(1, min_silence_ms // frame_ms)
    min_speech_frames = max(1, min_speech_ms // frame_ms)
    runs: list[list[int]] = []
    in_run = False
    silence = 0
    for index, is_speech in enumerate(speech.tolist()):
        if is_speech:
            silence = 0
            if not in_run:
                in_run = True
                runs.append([index, index])
            else:
                runs[-1][1] = index
        elif in_run:
            silence += 1
            runs[-1][1] = index
            if silence >= min_silence_frames:
                runs[-1][1] = index - silence
                in_run = False
    if in_run:
        runs[-1][1] = len(speech) - 1
    total_us = int(round(len(pcm_mono_16k) / sample_rate * _MICROSECONDS_PER_SECOND))
    out: list[tuple[int, int]] = []
    for start_f, end_f in runs:
        if end_f - start_f + 1 < min_speech_frames:
            continue
        start_us = int(round(start_f * frame_len / sample_rate * _MICROSECONDS_PER_SECOND))
        end_us = int(round((end_f + 1) * frame_len / sample_rate * _MICROSECONDS_PER_SECOND))
        out.append((max(0, start_us), min(total_us, end_us)))
    return tuple(out)


def turns_from_anchors(
    segments: tuple[TranscriptSegment, ...],
    anchors: tuple[tuple[int, int], ...],
    *,
    gap_threshold_us: int = 800_000,
    max_speakers: int = 2,
) -> tuple[SpeakerTurn, ...]:
    """Group segments into speaker turns by speech-anchor overlap (pure).

    Anchor runs separated by more than ``gap_threshold_us`` open a new turn;
    a segment joins the turn whose anchor run contains its midpoint (or the
    nearest run); speakers alternate via the pack's :func:`assign_speakers`.
    Segments are *not* relabeled here — callers stamp via the pack op so a
    merged turn can never carry stale labels.
    """
    if not segments:
        return ()
    if not anchors:
        labels = assign_speakers(1, max_speakers)
        return (
            SpeakerTurn(
                speaker=labels[0],
                start_us=segments[0].start_us,
                end_us=segments[-1].end_us,
                text=" ".join(s.text.strip() for s in segments if s.text.strip()) or None,
            ),
        )
    merged: list[list[int]] = [[anchors[0][0], anchors[0][1]]]
    for start, end in anchors[1:]:
        if start - merged[-1][1] > gap_threshold_us:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    buckets: list[list[TranscriptSegment]] = [[] for _ in merged]
    for seg in segments:
        mid = (seg.start_us + seg.end_us) // 2
        best = 0
        best_dist = abs(mid - (merged[0][0] + merged[0][1]) // 2)
        for i, (start, end) in enumerate(merged):
            if start <= mid <= end:
                best = i
                break
            dist = 0 if mid < start else mid - end
            dist = min(dist, abs(mid - start), abs(mid - end))
            if dist < best_dist:
                best_dist = dist
                best = i
        buckets[best].append(seg)
    non_empty = [(run, bucket) for run, bucket in zip(merged, buckets, strict=True) if bucket]
    labels = assign_speakers(len(non_empty), max_speakers)
    return tuple(
        SpeakerTurn(
            speaker=label,
            start_us=min(s.start_us for s in bucket),
            end_us=max(s.end_us for s in bucket),
            text=" ".join(s.text.strip() for s in bucket if s.text.strip()) or None,
        )
        for label, (_run, bucket) in zip(labels, non_empty, strict=True)
    )


def _resolve_decode_ffmpeg() -> str:
    override = os.environ.get("NEXUS_FFMPEG_BIN")
    if override:
        if Path(override).is_file():
            return override
        raise CaptionEngineError(f"NEXUS_FFMPEG_BIN is not a file: {override!r}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise CaptionEngineError(
        "diarize needs an FFmpeg binary for PCM decode: install ffmpeg or set "
        "NEXUS_FFMPEG_BIN (transcribe via faster-whisper is unaffected)"
    )


def decode_mono_16k(audio_path: str | Path) -> np.ndarray:
    """Decode any audio file to mono 16 kHz int16 PCM (one process, no shell)."""
    binary = _resolve_decode_ffmpeg()
    source = Path(audio_path)
    if not source.is_file():
        raise CaptionEngineError(f"audio file not found: {source}")
    with tempfile.TemporaryDirectory(prefix="nexus_vad_") as tmp:
        wav_path = Path(tmp) / "mono16k.wav"
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(wav_path),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0 or not wav_path.is_file():
            raise CaptionEngineError(
                f"PCM decode failed for {source}: {(result.stderr or '').strip()[-400:]}"
            )
        with wave.open(str(wav_path), "rb") as handle:
            frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype=np.int16).copy()


class WhisperLocalCaptionEngine(CaptionEnginePort):
    """faster-whisper (CTranslate2, int8 CPU) behind :class:`CaptionEnginePort`."""

    def __init__(
        self,
        model: str = "small",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 4,
        download_root: str | Path | None = None,
        allow_download: bool = False,
        vad_filter: bool = False,
    ) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._download_root = str(download_root) if download_root else None
        self._allow_download = allow_download
        self._vad_filter = vad_filter
        if cpu_threads < 1:
            raise ValueError("cpu_threads must be >= 1")
        self._model: Any | None = None
        self._inference_lock = threading.Lock()
        self._lock = threading.Lock()
        self._available: bool | None = None

    # -- availability ------------------------------------------------------
    def is_available(self) -> bool:
        """True iff the ``[speech]`` extra is importable (no model load)."""
        if self._available is None:
            try:
                importlib.import_module("faster_whisper")
            except ImportError:
                self._available = False
            else:
                self._available = True
        return self._available

    def _require_available(self) -> Any:
        try:
            module = importlib.import_module("faster_whisper")
        except ImportError as error:
            raise CaptionProfileUnavailableError(
                "caption_profile_unavailable: faster-whisper is not installed; "
                f"{SPEECH_EXTRA_HINT} ({error})"
            ) from error
        return module.WhisperModel

    def _load_model(self) -> Any:
        """Load/cache a bounded number of models without implicit hub access."""
        with self._lock, _MODEL_CACHE_LOCK:
            if self._model is not None:
                return self._model
            model_cls = self._require_available()
            download_enabled = self._allow_download or (
                os.environ.get("NEXUS_SPEECH_ALLOW_DOWNLOAD") == "1"
            )
            local_model = Path(self._model_name).expanduser()
            if not download_enabled and not local_model.is_dir():
                raise CaptionProfileUnavailableError(
                    "caption_profile_unavailable: offline mode requires model to be a local "
                    f"directory (missing: {self._model_name!r}); model download is opt-in"
                )
            cache_key = (
                str(local_model.resolve()) if local_model.is_dir() else self._model_name,
                self._device,
                self._compute_type,
                self._cpu_threads,
                self._download_root,
            )
            cached = _MODEL_CACHE.get(cache_key)
            if cached is not None:
                self._model, self._inference_lock = cached
                _MODEL_CACHE.move_to_end(cache_key)
                return self._model
            try:
                self._model = model_cls(
                    str(local_model.resolve()) if local_model.is_dir() else self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                    cpu_threads=self._cpu_threads,
                    download_root=self._download_root,
                )
            except Exception as error:
                if not download_enabled and not local_model.is_dir():
                    raise CaptionProfileUnavailableError(
                        f"caption_profile_unavailable: local model unavailable: {error}"
                    ) from error
                raise CaptionEngineError(
                    f"could not load faster-whisper model {self._model_name!r}: {error}"
                ) from error
            self._inference_lock = threading.Lock()
            _MODEL_CACHE[cache_key] = (self._model, self._inference_lock)
            while len(_MODEL_CACHE) > _MODEL_CACHE_SIZE:
                _MODEL_CACHE.popitem(last=False)
            return self._model

    # -- port --------------------------------------------------------------
    async def transcribe(
        self, audio_path: str | Path, *, language: str | None = None
    ) -> TranscriptRef:
        """Transcribe locally; blocking inference runs in :func:`to_thread`."""
        return await asyncio.to_thread(self.transcribe_sync, audio_path, language=language)

    def transcribe_sync(
        self, audio_path: str | Path, *, language: str | None = None
    ) -> TranscriptRef:
        """Blocking transcribe core (always call via :func:`to_thread`)."""
        source = Path(audio_path)
        if not source.is_file():
            raise CaptionEngineError(f"audio file not found: {source}")
        language = _language_code(language)
        self._require_available()
        duration_us = _probe_audio_duration_us(source)
        if duration_us > _MAX_AUDIO_DURATION_US:
            raise CaptionEngineError(
                "audio exceeds the 2-minute in-memory resource budget; split it into shorter chunks"
            )
        source_stat = source.stat()
        source_sha256 = _sha256_file(source)
        model = self._load_model()
        try:
            cpu_lock = _CPU_INFERENCE_LOCK if self._device == "cpu" else nullcontext()
            with cpu_lock, self._inference_lock:
                segments_iter, info = model.transcribe(
                    str(source),
                    language=language,
                    word_timestamps=True,
                    vad_filter=self._vad_filter,
                )
                # Consume the lazy backend generator while the model lock is held;
                # map directly to canonical segments instead of retaining a second
                # full list of backend objects for long recordings.
                segments = [_map_raw_segment(raw, i) for i, raw in enumerate(segments_iter)]
        except CaptionEngineError:
            raise
        except Exception as error:
            raise CaptionEngineError(f"faster-whisper transcribe failed: {error}") from error

        final_stat = source.stat()
        if (final_stat.st_size, final_stat.st_mtime_ns) != (
            source_stat.st_size,
            source_stat.st_mtime_ns,
        ):
            raise CaptionEngineError(
                "audio source changed during inference; refusing stale provenance"
            )
        detected = getattr(info, "language", None) or None
        duration_s = float(getattr(info, "duration", 0.0) or 0.0)
        tail_us = max((s.end_us for s in segments), default=0)
        try:
            engine_version = importlib.metadata.version("faster-whisper")
        except importlib.metadata.PackageNotFoundError:
            engine_version = None
        effective_language = language or detected or "fa"
        identity = json.dumps(
            [
                source_sha256,
                engine_version,
                self._model_name,
                effective_language,
                self._device,
                self._compute_type,
                self._cpu_threads,
                self._vad_filter,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        identity_sha256 = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return TranscriptRef(
            transcript_id=f"whisper:{self._model_name}:{identity_sha256[:16]}",
            source_asset_id="",
            language=effective_language,
            segments=tuple(segments),
            duration_us=max(tail_us, _us(duration_s)),
            speaker_turns=(),
            engine="faster-whisper/CTranslate2",
            engine_version=engine_version,
            model_name=self._model_name,
            model_digest=None,
            source_sha256=source_sha256,
            parameters={
                "device": self._device,
                "compute_type": self._compute_type,
                "cpu_threads": self._cpu_threads,
                "word_timestamps": True,
                "vad_filter": self._vad_filter,
                "language_requested": language,
                "language_resolved": effective_language,
            },
        )

    async def diarize(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        max_speakers: int = 2,
        gap_threshold_us: int = 800_000,
    ) -> TranscriptRef:
        """Transcribe + energy-VAD turns (v1 heuristic, fully local)."""
        return await asyncio.to_thread(
            self.diarize_sync,
            audio_path,
            language=language,
            max_speakers=max_speakers,
            gap_threshold_us=gap_threshold_us,
        )

    def diarize_sync(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        max_speakers: int = 2,
        gap_threshold_us: int = 800_000,
    ) -> TranscriptRef:
        """Blocking diarize core (always call via :func:`to_thread`)."""
        transcript = self.transcribe_sync(audio_path, language=language)
        pcm = decode_mono_16k(audio_path)
        anchors = energy_anchors(pcm)
        turns = turns_from_anchors(
            transcript.segments,
            anchors,
            gap_threshold_us=gap_threshold_us,
            max_speakers=max_speakers,
        )
        # Stamp via turn membership (merge-then-stamp): a segment takes the
        # label of the turn whose span contains its midpoint.
        stamped: list[TranscriptSegment] = []
        for seg in transcript.segments:
            mid = (seg.start_us + seg.end_us) // 2
            label = turns[0].speaker if turns else "SPEAKER_00"
            for turn in turns:
                if turn.start_us <= mid <= turn.end_us:
                    label = turn.speaker
                    break
            stamped.append(
                seg.model_copy(
                    update={
                        "speaker": label,
                        "words": tuple(w.model_copy(update={"speaker": label}) for w in seg.words),
                    }
                )
            )
        return TranscriptRef(
            transcript_id=f"{transcript.transcript_id}:diarized",
            source_asset_id=transcript.source_asset_id,
            language=transcript.language,
            segments=tuple(stamped),
            duration_us=transcript.duration_us,
            speaker_turns=turns,
            engine=transcript.engine,
            engine_version=transcript.engine_version,
            model_name=transcript.model_name,
            model_digest=transcript.model_digest,
            source_sha256=transcript.source_sha256,
            parameters={
                **transcript.parameters,
                "diarization_backend": "energy-vad-gap-heuristic",
                "speaker_identity_claimed": False,
            },
        )


class ArgosLocalTranslator:
    """Fully-offline neural translation (argos-translate, ``[translate]`` extra)."""

    def __init__(self, *, source_language: str | None = None) -> None:
        self._source_language = source_language
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is None:
            try:
                importlib.import_module("argostranslate.translate")
            except ImportError:
                self._available = False
            else:
                self._available = True
        return self._available

    async def translate_transcript(
        self, transcript: TranscriptRef, target_language: str
    ) -> TranscriptRef:
        return await asyncio.to_thread(self.translate_transcript_sync, transcript, target_language)

    def translate_transcript_sync(
        self, transcript: TranscriptRef, target_language: str
    ) -> TranscriptRef:
        try:
            module = importlib.import_module("argostranslate.translate")
        except ImportError as error:
            raise TranslateProfileUnavailableError(
                f"translate_profile_unavailable: argostranslate is not installed; "
                f"{TRANSLATE_EXTRA_HINT} ({error})"
            ) from error
        target = _language_code(target_language)
        source = _language_code(self._source_language or transcript.language)
        assert target is not None and source is not None
        try:
            translated: list[TranscriptSegment] = []
            for seg in transcript.segments:
                if not seg.text.strip():
                    translated.append(seg.model_copy(update={"text": "", "words": ()}))
                    continue
                text = module.translate(seg.text, source, target)
                if not isinstance(text, str):
                    raise CaptionEngineError("Argos returned a non-text translation")
                translated.append(
                    seg.model_copy(
                        update={
                            "text": text,
                            "words": project_word_timings(text, seg.start_us, seg.end_us),
                        }
                    )
                )
        except Exception as error:
            raise CaptionEngineError(
                f"argos translate failed ({source}->{target}): {error}"
            ) from error
        try:
            translator_version = importlib.metadata.version("argostranslate")
        except importlib.metadata.PackageNotFoundError:
            translator_version = None
        return TranscriptRef(
            transcript_id=f"{transcript.transcript_id}:t-{target}",
            source_asset_id=transcript.source_asset_id,
            language=target,
            segments=tuple(translated),
            duration_us=transcript.duration_us,
            speaker_turns=transcript.speaker_turns,
            engine="Argos Translate",
            engine_version=translator_version,
            model_name=f"{source}->{target}",
            model_digest=None,
            source_sha256=transcript.source_sha256,
            parameters={"source_language": source, "target_language": target},
        )


__all__ = [
    "SPEECH_EXTRA_HINT",
    "TRANSLATE_EXTRA_HINT",
    "ArgosLocalTranslator",
    "TranslateProfileUnavailableError",
    "WhisperLocalCaptionEngine",
    "decode_mono_16k",
    "energy_anchors",
    "turns_from_anchors",
]
