"""FFmpeg-backed measurement and rendering for the native audio engine.

Every claim in this module is read back from a file, never inferred:

* :meth:`FfmpegEngine.probe` parses the stream table FFmpeg itself prints;
* :meth:`FfmpegEngine.measure_loudness` runs the **EBU R128 scanner**
  (``ebur128``) and parses its summary — *not* ``loudnorm``'s own report, which
  is a plan produced by the filter rather than an independent measurement;
* :meth:`FfmpegEngine.decode_all` decodes the produced file end-to-end to
  ``s32le`` on stdout and counts the samples it actually got;
* :meth:`FfmpegEngine.signature` computes an acoustic signature from decoded
  PCM in **bounded windows** (:mod:`nexus_ai_agent.creative.audio_engine.numpy_dsp`),
  so a two-hour master costs a fixed amount of memory.

Why a second measurement pass instead of trusting the encoder: FFmpeg's
two-pass ``loudnorm`` is an *estimate* — it applies a computed gain and reports
what it intended.  The only honest way to say "this file is at -14.0 LUFS" is to
decode the finished file and measure it again, which is what the verifier does.

Binary resolution is deliberately delegated to
:func:`nexus_ai_agent.creative.slideshow.ffmpeg.resolve_ffmpeg_bin` — one
allow-list for the whole repository (explicit override → ``NEXUS_FFMPEG_BIN`` →
``PATH`` → the ``imageio-ffmpeg`` wheel).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import numpy as np

from nexus_ai_agent.creative.audio_engine.models import (
    LoudnessMeasurement,
    LoudnessProfile,
    MediaProbe,
    ProcessOutcome,
    SpectralSignature,
)
from nexus_ai_agent.creative.audio_engine.numpy_dsp import (
    analyse_samples,
    decode_s32le,
    resample_mono,
)
from nexus_ai_agent.creative.audio_engine.process import CancelToken, ProcessGovernor
from nexus_ai_agent.creative.audio_engine.workspace import hash_file
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,  # re-exported for callers that gate on availability
    resolve_ffmpeg_bin,
)

__all__ = [
    "AudioEngineError",
    "AudioEngineUnavailableError",
    "AudioEncodingError",
    "AudioMeasurementError",
    "AudioDecodeError",
    "FfmpegEngine",
    "FfmpegUnavailableError",
]

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_STREAM_RE = re.compile(
    r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^)]*\))?: Audio: (?P<codec>[A-Za-z0-9_]+)"
    r"(?: \([^)]*\))?, (?P<rate>\d+) Hz, (?P<layout>[^,]+)"
    r"(?:, (?P<sample_fmt>[A-Za-z0-9]+)(?: \([^)]*\))?)?"
)
_SUMMARY_I_RE = re.compile(r"I:\s*(-?[\d.]+|-inf)\s*LUFS")
_SUMMARY_LRA_RE = re.compile(r"LRA:\s*(-?[\d.]+|-inf)\s*LU")
_SUMMARY_PEAK_RE = re.compile(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS")
_SUMMARY_THRESHOLD_RE = re.compile(r"Threshold:\s*(-?[\d.]+|-inf)\s*LUFS")
_MICROSECONDS_PER_SECOND = 1_000_000


class AudioEngineError(RuntimeError):
    """The native audio engine could not produce a verifiable result."""


class AudioEngineUnavailableError(AudioEngineError):
    """No allow-listed FFmpeg binary could be resolved in this environment."""


class AudioEncodingError(AudioEngineError):
    """FFmpeg ran but did not produce the requested output."""


class AudioMeasurementError(AudioEngineError):
    """The independent measurement pass produced no usable numbers."""


class AudioDecodeError(AudioEngineError):
    """The produced file could not be decoded end-to-end (it is not healthy)."""


#: FFmpeg returns status 0 on several classes of damaged input while writing
#: ``corrupt input packet`` / ``Invalid data`` to stderr on the affected stream.
#: A decode that exits 0 is therefore *not* proof of a healthy file; these
#: markers are what turns a silent truncation into an honest failure.  They are
#: matched against the warning-level stderr of the decode, never against the
#: audio itself.
CORRUPTION_MARKERS: tuple[str, ...] = (
    "corrupt input packet",
    "packet corrupt",
    "invalid data found",
    "error while decoding",
    "truncating packet",
    "invalid frame size",
    "could not find codec parameters",
)


def corruption_marker(stderr: str) -> str | None:
    """Return the first corruption marker in ``stderr`` (``None`` when clean)."""
    lowered = (stderr or "").lower()
    for marker in CORRUPTION_MARKERS:
        if marker in lowered:
            return marker
    return None


def _parse_float(raw: str) -> float | None:
    text = raw.strip().lower()
    if text in ("-inf", "inf", "nan"):
        return None
    try:
        return float(text)
    except ValueError:  # pragma: no cover - regex already constrained the shape
        return None


class FfmpegEngine:
    """Probe / measure / render / decode, all through one governed runner."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        governor: ProcessGovernor | None = None,
        deadline_seconds: float = 900.0,
        max_captured_bytes: int = 256 * 1024,
    ) -> None:
        self._explicit_binary = binary
        self._binary: str | None = None
        self.governor = governor or ProcessGovernor()
        self.deadline_seconds = deadline_seconds
        self.max_captured_bytes = max_captured_bytes

    # -- binary ----------------------------------------------------------- #
    @property
    def binary(self) -> str:
        if self._binary is None:
            try:
                self._binary = resolve_ffmpeg_bin(self._explicit_binary)
            except FfmpegUnavailableError as error:
                raise AudioEngineUnavailableError(str(error)) from error
        return self._binary

    def available(self) -> bool:
        """Is a usable binary resolved?  (Resolution is cached on success.)"""
        try:
            _ = self.binary
        except AudioEngineUnavailableError:
            return False
        return True

    def _run(
        self,
        args: Sequence[str],
        *,
        deadline_seconds: float | None = None,
        cancel: CancelToken | None = None,
        stdout_path: str | None = None,
    ) -> ProcessOutcome:
        argv = [self.binary, "-hide_banner", "-nostdin", *args]
        return self.governor.run(
            argv,
            deadline_seconds=deadline_seconds or self.deadline_seconds,
            cancel=cancel,
            stdout_path=stdout_path,
            max_captured_bytes=self.max_captured_bytes,
        )

    # -- probe ------------------------------------------------------------ #
    def probe(self, path: Path | str, *, cancel: CancelToken | None = None) -> MediaProbe:
        source = Path(path)
        if not source.is_file():
            raise AudioEngineError(f"media file not found: {source}")
        outcome = self._run(["-i", str(source)], cancel=cancel)
        text = outcome.stderr_tail
        duration_us: int | None = None
        duration = _DURATION_RE.search(text)
        if duration is not None:
            hours, minutes, seconds = duration.groups()
            duration_us = int(
                round(
                    (int(hours) * 3600 + int(minutes) * 60 + float(seconds))
                    * _MICROSECONDS_PER_SECOND
                )
            )
        codec = layout = sample_format = None
        channels = None
        rate = None
        stream = _AUDIO_STREAM_RE.search(text)
        if stream is not None:
            codec = stream.group("codec")
            rate = int(stream.group("rate"))
            layout = stream.group("layout").strip()
            sample_format = stream.group("sample_fmt")
            channels = (
                1 if layout.startswith("mono") else 2 if layout.startswith("stereo") else None
            )
        return MediaProbe(
            container_duration_us=duration_us,
            sample_rate=rate,
            channels=channels,
            channel_layout=layout,
            codec=codec,
            sample_format=sample_format,
            size_bytes=source.stat().st_size,
            sha256=hash_file(source),
        )

    # -- loudness --------------------------------------------------------- #
    def measure_loudness(
        self,
        path: Path | str,
        *,
        deadline_seconds: float | None = None,
        cancel: CancelToken | None = None,
    ) -> LoudnessMeasurement:
        """Measure integrated loudness / true peak / LRA with the ``ebur128`` scanner."""
        source = Path(path)
        outcome = self._run(
            ["-i", str(source), "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
            deadline_seconds=deadline_seconds,
            cancel=cancel,
        )
        if not outcome.ok:
            raise AudioMeasurementError(
                f"loudness measurement failed ({outcome.termination}): "
                f"{(outcome.stderr_tail or '').strip()[-400:]}"
            )
        text = outcome.stderr_tail
        summary = text[text.rfind("Summary:") :] if "Summary:" in text else ""
        if not summary:
            raise AudioMeasurementError(
                "ebur128 produced no summary; the file is not decodable audio: "
                f"{text.strip()[-400:]}"
            )
        # The summary repeats labels (Threshold appears under I and under LRA);
        # splitting on the section headers keeps each number with its meaning.
        integrated_block = _section(summary, "Integrated loudness:", "Loudness range:")
        lra_block = _section(summary, "Loudness range:", "True peak:")
        peak_block = _section(summary, "True peak:", None)
        integrated = _SUMMARY_I_RE.search(integrated_block)
        threshold = _SUMMARY_THRESHOLD_RE.search(integrated_block)
        lra = _SUMMARY_LRA_RE.search(lra_block)
        peak = _SUMMARY_PEAK_RE.search(peak_block)
        if integrated is None or peak is None:
            raise AudioMeasurementError("ebur128 summary is missing integrated loudness or peak")
        integrated_lufs = _parse_float(integrated.group(1))
        true_peak = _parse_float(peak.group(1))
        lra_value = _parse_float(lra.group(1)) if lra is not None else None
        threshold_value = _parse_float(threshold.group(1)) if threshold is not None else None
        probe = self.probe(source, cancel=cancel)
        seconds = (probe.container_duration_us or 0) / _MICROSECONDS_PER_SECOND
        if integrated_lufs is None:
            # -inf: the whole programme sits below the gating threshold.  That is
            # silence, not a measurement, and must never become a "0.0 LUFS".
            return LoudnessMeasurement(
                integrated_lufs=None,
                true_peak_dbtp=true_peak,
                loudness_range_lu=lra_value,
                relative_threshold_lufs=threshold_value,
                silent=True,
                measurement_seconds=round(seconds, 3),
            )
        return LoudnessMeasurement(
            integrated_lufs=integrated_lufs,
            true_peak_dbtp=true_peak,
            loudness_range_lu=lra_value,
            relative_threshold_lufs=threshold_value,
            silent=False,
            measurement_seconds=round(seconds, 3),
        )

    def measure_loudnorm_pass(
        self,
        path: Path | str,
        profile: LoudnessProfile,
        *,
        cancel: CancelToken | None = None,
    ) -> dict[str, float] | None:
        """First ``loudnorm`` pass: the measurement JSON for the second pass.

        Returns ``None`` for a silent programme (FFmpeg emits ``-inf`` and an
        ``inf`` target offset; feeding that to a second pass would corrupt the
        audio, so the caller must refuse instead).
        """
        source = Path(path)
        i_arg, tp_arg, lra_arg = profile.ffmpeg_loudnorm_targets()
        outcome = self._run(
            [
                "-i",
                str(source),
                "-af",
                f"loudnorm={i_arg}:{tp_arg}:{lra_arg}:print_format=json",
                "-f",
                "null",
                "-",
            ],
            cancel=cancel,
        )
        if not outcome.ok:
            raise AudioMeasurementError(
                f"loudnorm measurement pass failed ({outcome.termination}): "
                f"{(outcome.stderr_tail or '').strip()[-400:]}"
            )
        text = outcome.stderr_tail
        start, end = text.rfind("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise AudioMeasurementError("loudnorm printed no measurement JSON")
        import json

        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as error:
            raise AudioMeasurementError(f"loudnorm JSON is not parseable: {error}") from error
        keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        values: dict[str, float] = {}
        for key in keys:
            raw = str(payload.get(key, "")).strip().lower()
            if raw in ("-inf", "inf", "nan", "none", ""):
                return None
            try:
                values[key] = float(raw)
            except ValueError as error:
                raise AudioMeasurementError(
                    f"loudnorm JSON field {key!r} is not a number: {raw!r}"
                ) from error
        return values


def _section(text: str, start: str, end: str | None) -> str:
    begin = text.find(start)
    if begin < 0:
        return ""
    if end is None:
        return text[begin:]
    finish = text.find(end, begin + len(start))
    return text[begin:] if finish < 0 else text[begin:finish]


class FfmpegTranscoder(FfmpegEngine):
    """Rendering half of the engine: encode staged files, never final ones."""

    def render(
        self,
        *,
        source: Path | str,
        destination: Path | str,
        audio_filters: Sequence[str],
        profile_args: Sequence[str],
        container_hint: str,
        deadline_seconds: float | None = None,
        cancel: CancelToken | None = None,
    ) -> ProcessOutcome:
        """Encode ``source`` into ``destination`` with an explicit filter chain.

        The destination is a **staging** path chosen by the caller; publication
        is :meth:`RuntimeWorkspace.publish`'s job and happens only after the
        verifier has read the file back.
        """
        args = [
            "-y",
            "-i",
            str(source),
            "-vn",
            "-af",
            ",".join(audio_filters) if audio_filters else "anull",
            *profile_args,
            "-f",
            _container_format(container_hint),
            str(destination),
        ]
        outcome = self._run(args, deadline_seconds=deadline_seconds, cancel=cancel)
        target = Path(destination)
        if not outcome.ok:
            target.unlink(missing_ok=True)
            raise AudioEncodingError(
                f"encode failed ({outcome.termination}, rc={outcome.returncode}): "
                f"{(outcome.stderr_tail or '').strip()[-600:]}"
            )
        if not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise AudioEncodingError("encode reported success but produced no file")
        return outcome

    def assemble_stream_copy(
        self,
        *,
        segments: Sequence[Path | str],
        destination: Path | str,
        list_path: Path | str,
        container_hint: str,
        deadline_seconds: float | None = None,
        cancel: CancelToken | None = None,
    ) -> ProcessOutcome:
        """Concatenate segments **without re-encoding** (zero generation loss).

        Only legal when every segment shares the canonical shape; the caller
        (``continuity.assert_lossless_assembly``) proves that first from the
        segments' own probes, and the verifier re-decodes the joined file.
        """
        if not segments:
            raise AudioEncodingError("cannot assemble an empty segment list")
        listing = Path(list_path)
        lines = []
        for segment in segments:
            path = Path(segment).resolve()
            if "'" in str(path):
                raise AudioEncodingError(f"segment path cannot be quoted in a concat list: {path}")
            lines.append(f"file '{path}'")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        args = [
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-f",
            _container_format(container_hint),
            str(destination),
        ]
        outcome = self._run(args, deadline_seconds=deadline_seconds, cancel=cancel)
        target = Path(destination)
        if not outcome.ok or not target.is_file():
            target.unlink(missing_ok=True)
            raise AudioEncodingError(
                f"stream-copy assembly failed ({outcome.termination}, rc={outcome.returncode}): "
                f"{(outcome.stderr_tail or '').strip()[-600:]}"
            )
        return outcome

    # -- decode ----------------------------------------------------------- #
    def decode_all(
        self,
        path: Path | str,
        *,
        pcm_path: Path | str,
        channels: int,
        deadline_seconds: float | None = None,
        cancel: CancelToken | None = None,
    ) -> tuple[ProcessOutcome, int]:
        """Decode the whole file to ``s32le`` PCM on disk; return (outcome, frames).

        The decode goes to a file (not a pipe) so the length check is exact and
        memory stays flat regardless of programme length.  ``frames`` is
        ``samples / channels``: counting interleaved samples as frames is how a
        stereo master silently appears to be twice as long.
        """
        if channels < 1:
            raise AudioDecodeError("decode_all needs the stream's channel count")
        source = Path(path)
        outcome = self._run(
            ["-loglevel", "warning", "-i", str(source), "-f", "s32le", "-acodec", "pcm_s32le", "-"],
            deadline_seconds=deadline_seconds,
            cancel=cancel,
            stdout_path=str(pcm_path),
        )
        pcm = Path(pcm_path)
        # FFmpeg's exit code is not sufficient on its own: a truncated decode can
        # still exit 0 with a warning.  An empty or non-multiple-of-4 output is
        # always a hard failure.
        if not outcome.ok or not pcm.is_file() or pcm.stat().st_size % 4 != 0:
            raise AudioDecodeError(
                f"decode of {source.name} failed ({outcome.termination}, rc={outcome.returncode}): "
                f"{(outcome.stderr_tail or '').strip()[-600:]}"
            )
        marker = corruption_marker(outcome.stderr_tail)
        if marker is not None:
            raise AudioDecodeError(
                f"decode of {source.name} reported corruption ({marker!r}); "
                "the file is damaged even though FFmpeg exited 0"
            )
        samples = pcm.stat().st_size // 4
        if samples == 0 or samples % channels != 0:
            raise AudioDecodeError(
                f"decode of {source.name} produced {samples} samples, not a whole "
                f"number of {channels}-channel frames"
            )
        return outcome, samples // channels

    def signature(
        self,
        path: Path | str,
        *,
        pcm_path: Path | str,
        max_analysis_seconds: float = 120.0,
        channels: int | None = None,
        analysis_rate: int | None = None,
        cancel: CancelToken | None = None,
    ) -> SpectralSignature:
        """Decode (bounded) and compute the acoustic signature of the file.

        ``channels``/``analysis_rate`` force the *analysis* decode shape (the
        produced file is untouched): use them to compare signatures across
        sources recorded in different layouts or at different rates.
        """
        source = Path(path)
        probe = self.probe(source, cancel=cancel)
        if probe.sample_rate is None or probe.channels is None:
            raise AudioDecodeError(f"{source.name} carries no decodable audio stream")
        channel_count = channels or probe.channels
        window = ["-t", f"{max_analysis_seconds:g}"] if max_analysis_seconds > 0 else []
        rate_args = ["-ar", str(analysis_rate)] if analysis_rate else []
        outcome = self._run(
            [
                "-loglevel",
                "warning",
                *window,
                "-i",
                str(source),
                "-ac",
                str(channel_count),
                *rate_args,
                "-f",
                "s32le",
                "-acodec",
                "pcm_s32le",
                "-",
            ],
            cancel=cancel,
            stdout_path=str(pcm_path),
        )
        pcm = Path(pcm_path)
        if not outcome.ok or not pcm.is_file() or pcm.stat().st_size % 4 != 0:
            raise AudioDecodeError(
                f"signature decode failed for {source.name}: "
                f"{(outcome.stderr_tail or '').strip()[-400:]}"
            )
        marker = corruption_marker(outcome.stderr_tail)
        if marker is not None:
            raise AudioDecodeError(
                f"signature decode of {source.name} reported corruption ({marker!r})"
            )
        samples = decode_s32le(pcm, channels=channel_count)
        if samples.size == 0:
            raise AudioDecodeError(f"signature decode of {source.name} produced zero samples")
        mono = samples.reshape(-1) if channel_count == 1 else samples.mean(axis=1)
        effective_rate = analysis_rate or probe.sample_rate
        analysis = analyse_samples(
            np.asarray(mono, dtype=np.float64),
            sample_rate=effective_rate,
            channels=channel_count,
        )
        total = probe.container_duration_us or 0
        analysed_us = int(round(analysis.seconds_covered * _MICROSECONDS_PER_SECOND))
        return analysis.to_signature(
            sample_rate=effective_rate,
            channels=channel_count,
            truncated=bool(total and analysed_us < total),
        )

    def signature_resampled(
        self,
        path: Path | str,
        *,
        pcm_path: Path | str,
        analysis_rate: int = 48_000,
        max_analysis_seconds: float = 60.0,
        cancel: CancelToken | None = None,
    ) -> SpectralSignature:
        """Mono signature at one analysis rate (comparable across sources).

        Spectral centroid is a Hz measurement; two speakers recorded at 44.1 kHz
        and 96 kHz must be analysed at the *same* rate before their signatures
        can be compared, which is what the continuity comparator relies on.
        """
        return self.signature(
            path,
            pcm_path=pcm_path,
            max_analysis_seconds=max_analysis_seconds,
            channels=1,
            analysis_rate=analysis_rate,
            cancel=cancel,
        )


def _container_format(hint: Literal["flac", "wav"] | str) -> str:
    text = hint.lower().lstrip(".")
    if text in ("flac",):
        return "flac"
    if text in ("wav", "wave", "pcm"):
        return "wav"
    raise AudioEncodingError(f"unsupported container for the native engine: {hint!r}")


__all__ += ["FfmpegTranscoder", "corruption_marker", "resample_mono"]
