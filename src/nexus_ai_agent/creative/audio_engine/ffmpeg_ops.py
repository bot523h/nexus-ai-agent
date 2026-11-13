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

import hashlib
import json
import os
import re
import shutil
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
    "parse_audio_stream_channels",
    "probe_banner_channels",
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

#: ``N channels`` / ``N channel`` / ``N ch`` as printed by FFmpeg 5.x and 6.x.
_COUNT_LAYOUT_RE = re.compile(r"^(\d+)\s*ch(?:annels?)?\b", re.IGNORECASE)

#: Surround notation: ``2.1``, ``5.1(side)``, ``7.1.4``.
_SURROUND_LAYOUT_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:\(.*\))?$")

#: Named layouts whose channel count is not in the name.
_NAMED_LAYOUTS: dict[str, int] = {
    "mono": 1,
    "stereo": 2,
    "downmix": 2,
    "quad": 4,
    "quadraphonic": 4,
    "hexagonal": 6,
    "octagonal": 8,
}


def probe_banner_channels(
    banner: str,
) -> tuple[int | None, int | None, str | None, str | None] | None:
    """Parse one ``ffmpeg -i`` banner's audio stream line.

    Returns ``(channels, sample_rate, codec, layout)`` or ``None`` when no audio
    stream line is present. Split out from `probe()` so the version-dependent
    banner shape can be tested against verbatim lines from several FFmpeg
    releases without invoking the binary — the parser is the part that changes
    between versions, not the subprocess call around it.
    """
    stream = _AUDIO_STREAM_RE.search(banner)
    if stream is None:
        return None
    layout = stream.group("layout").strip()
    return (
        parse_audio_stream_channels(layout),
        int(stream.group("rate")),
        stream.group("codec"),
        layout,
    )


def parse_audio_stream_channels(layout_token: str) -> int | None:
    """Channel count from an ``ffmpeg -i`` banner's channel-layout token.

    The token after ``<rate> Hz,`` is **not stable across FFmpeg versions**, and
    the difference is not cosmetic. As measured here:

    ===========  =====================================  ============
    version      banner fragment                        token
    ===========  =====================================  ============
    9.0.2        ``48000 Hz, mono, s16``                 ``mono``
    9.0.2        ``48000 Hz, stereo, fltp``              ``stereo``
    6.1.1        ``48000 Hz, 1 channels, s16``           ``1 channels``
    6.1.1        ``48000 Hz, 2 channels, flt``           ``2 channels``
    ===========  =====================================  ============

    A parser that only understands ``mono``/``stereo`` returns ``None`` on 6.1.1,
    and ``channels is None`` makes ``probe()`` fail closed — every artifact is
    then rejected as "carries no decodable audio stream". A sandbox that ships
    9.x cannot see this; it is a genuine portability defect, not an environment
    problem, and it is why this is a function with its own tests rather than a
    ternary inside ``probe()``.

    ``ffprobe`` is not an option: decision r5 forbids a second required binary,
    and CI provisions FFmpeg from the ``imageio-ffmpeg`` wheel, which has no
    ``ffprobe``.

    Returns ``None`` for anything unrecognised — the caller keeps failing closed,
    because a guessed channel count would corrupt the loudness channel sum.
    """
    token = (layout_token or "").strip()
    if not token:
        return None
    lowered = token.lower()
    # ``mono`` / ``stereo`` / ``quad`` and friends, optionally with a tag.
    name = lowered.split("(", 1)[0].strip()
    if name in _NAMED_LAYOUTS:
        return _NAMED_LAYOUTS[name]
    # ``5.1`` / ``5.1(side)`` / ``7.1.4`` — the components sum to the count.
    surround = _SURROUND_LAYOUT_RE.match(lowered)
    if surround is not None:
        parts = [int(part) for part in surround.groups() if part is not None]
        if all(part >= 0 for part in parts) and sum(parts) >= 1:
            return sum(parts)
        return None
    # ``1 channels`` / ``2 channel`` / ``6 ch`` (FFmpeg 5.x and 6.x).
    counted = _COUNT_LAYOUT_RE.match(lowered)
    if counted is not None:
        count = int(counted.group(1))
        return count if count >= 1 else None
    return None


class AudioEngineError(RuntimeError):
    """The native audio engine could not produce a verifiable result."""


class AudioProbeError(AudioEngineError):
    """A machine probe could not establish the stream's shape (fail closed).

    Deliberately distinct from :class:`AudioEngineError`: "I could not read the
    stream" is a different fact from "the stream is wrong", and callers act on
    them differently (retry/repair vs refuse).
    """


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
        probe_binary: str | None = None,
        governor: ProcessGovernor | None = None,
        deadline_seconds: float = 900.0,
        max_captured_bytes: int = 256 * 1024,
    ) -> None:
        self._explicit_binary = binary
        self._binary: str | None = None
        self._explicit_probe_binary = probe_binary or os.environ.get("NEXUS_FFPROBE_BIN")
        self._probe_binary: str | None = None
        self.governor = governor or ProcessGovernor()
        self.deadline_seconds = deadline_seconds
        self.max_captured_bytes = max_captured_bytes
        #: CPU threads for every child (``-threads``).  ``None`` = FFmpeg's own
        #: default.  Set by the engine from ``AudioExecutionSettings.threads``;
        #: kept here so the *transcoder* is the one place that emits the flag.
        self.threads: int | None = None

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

    @property
    def probe_binary(self) -> str | None:
        """The ``ffprobe`` binary, or ``None`` when this environment has none.

        ``ffprobe`` is a *separate executable* from ``ffmpeg``; a static build
        can ship one without the other (the bundled imageio-ffmpeg wheel does
        exactly that).  Resolution order is explicit argument, then
        ``NEXUS_FFPROBE_BIN``, then a sibling of the resolved ``ffmpeg``, then
        ``PATH``.  ``None`` is an honest "unavailable" — the caller must decide
        whether a banner parse is an acceptable substitute, and
        :meth:`probe` records which one it used.
        """
        if self._probe_binary is None:
            candidate: str | None = self._explicit_probe_binary
            if not candidate:
                sibling = (
                    Path(f"{self.binary[: -len('ffmpeg')]}ffprobe")
                    if (self.binary.endswith("ffmpeg"))
                    else None
                )
                if sibling is not None and sibling.is_file() and os.access(sibling, os.X_OK):
                    candidate = str(sibling)
            if not candidate:
                candidate = shutil.which("ffprobe")
            self._probe_binary = candidate or ""
        return self._probe_binary or None

    # -- probe ------------------------------------------------------------ #
    #: Fields ffprobe must report for this engine to make a verdict.  Not in
    #: this tuple, on purpose: ``channel_layout`` (ffprobe omits it for a plain
    #: mono/stereo WAV, where the count already determines the layout) and
    #: ``start_pts`` (absent on formats without an explicit start).  Their
    #: absence is recorded as ``None``/a derived value, never guessed into a
    #: different fact.
    _REQUIRED_STREAM_KEYS = (
        "codec_name",
        "sample_rate",
        "channels",
        "sample_fmt",
        "time_base",
        "duration_ts",
    )

    def probe_json(self, path: Path | str, *, cancel: CancelToken | None = None) -> MediaProbe:
        """Probe with ``ffprobe -of json`` under a **fail-closed** contract.

        Every field the engine reasons about is required: a missing key, a
        non-integer rate, a zero channel count or a ``pix_fmt`` on an audio
        stream all raise :class:`AudioProbeError` instead of yielding a
        partially-populated record.  A half-read stream shape is exactly what
        produces a confident wrong verdict downstream, so "incomplete" must be
        a refusal, not a default.

        Raises :class:`AudioEngineUnavailableError` when no ``ffprobe`` exists
        in this environment — the engine must never pretend it asked.
        """
        source = Path(path)
        if not source.is_file():
            raise AudioEngineError(f"media file not found: {source}")
        binary = self.probe_binary
        if binary is None:
            raise AudioEngineUnavailableError(
                "ffprobe is not installed in this environment; the typed probe "
                "contract needs `ffprobe -of json` (set NEXUS_FFPROBE_BIN to a "
                "real binary). A banner parse is not a substitute for a verdict."
            )
        outcome = self._run(
            [
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(source),
            ],
            cancel=cancel,
            binary_override=binary,
        )
        if not outcome.ok:
            raise AudioProbeError(
                f"ffprobe failed ({outcome.termination}): "
                f"{(outcome.stderr_tail or '').strip()[-400:]}"
            )
        payload = _json_object(outcome.stdout_tail or outcome.stderr_tail)
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise AudioProbeError("ffprobe JSON has no `streams` array")
        audio = [entry for entry in streams if entry.get("codec_type") == "audio"]
        if not audio:
            raise AudioProbeError("ffprobe found no audio stream")
        stream = audio[0]
        missing = [key for key in self._REQUIRED_STREAM_KEYS if key not in stream]
        if missing:
            raise AudioProbeError(
                f"ffprobe stream is missing required key(s): {', '.join(missing)}"
            )
        if stream.get("pix_fmt") is not None:
            raise AudioProbeError(
                f"audio stream declares pix_fmt={stream['pix_fmt']!r}; a video-shaped "
                "stream is not a valid audio program"
            )
        sample_rate = _as_int(stream["sample_rate"], "sample_rate")
        channels = _as_int(stream["channels"], "channels")
        duration_ts = _as_int(stream["duration_ts"], "duration_ts")
        start_pts = _as_int(stream["start_pts"], "start_pts") if "start_pts" in stream else None
        time_base = str(stream["time_base"])
        if sample_rate <= 0 or channels <= 0:
            raise AudioProbeError(
                f"ffprobe reported a non-physical stream (rate {sample_rate}, channels {channels})"
            )
        if duration_ts < 0:
            raise AudioProbeError(f"ffprobe reported a negative duration_ts ({duration_ts})")
        numerator, _, denominator = time_base.partition("/")
        try:
            seconds_per_tick = int(numerator) / int(denominator)
        except (ValueError, ZeroDivisionError) as error:
            raise AudioProbeError(f"ffprobe time_base is not a rational: {time_base!r}") from error
        if seconds_per_tick <= 0:
            raise AudioProbeError(f"ffprobe time_base is not positive: {time_base!r}")
        raw_format = payload.get("format")
        format_block: dict[str, object] = raw_format if isinstance(raw_format, dict) else {}
        format_duration = format_block.get("duration")
        duration_us = int(round(duration_ts * seconds_per_tick * _MICROSECONDS_PER_SECOND))
        if duration_us == 0 and isinstance(format_duration, (str, int, float)):
            if format_duration != "":
                try:
                    duration_us = int(round(float(format_duration) * _MICROSECONDS_PER_SECOND))
                except (TypeError, ValueError) as error:
                    raise AudioProbeError(
                        f"ffprobe format duration is not a number: {format_duration!r}"
                    ) from error
        raw_layout = stream.get("channel_layout")
        if isinstance(raw_layout, str) and raw_layout.strip():
            layout: str = raw_layout.strip()
        else:
            # Derived from the count, and labelled as derived: this is the only
            # honest reading of a document that states "1 channel" and no name.
            layout = _LAYOUT_BY_COUNT.get(channels, f"unknown({channels}ch)")
        return MediaProbe(
            container_duration_us=duration_us,
            sample_rate=sample_rate,
            channels=channels,
            channel_layout=layout,
            codec=str(stream["codec_name"]),
            sample_format=str(stream["sample_fmt"]),
            size_bytes=source.stat().st_size,
            sha256=hash_file(source),
            profile=(stream.get("profile") if isinstance(stream.get("profile"), str) else None),
            pix_fmt=None,
            time_base=time_base,
            start_pts=start_pts,
            duration_ts=duration_ts,
            extradata_hash=_extradata_hash(stream.get("extradata")),
            source="ffprobe_json",
        )

    def _run(
        self,
        args: Sequence[str],
        *,
        deadline_seconds: float | None = None,
        cancel: CancelToken | None = None,
        stdout_path: str | None = None,
        binary_override: str | None = None,
    ) -> ProcessOutcome:
        executable = binary_override or self.binary
        # ``-hide_banner``/``-nostdin`` are ffmpeg options; a different tool is
        # invoked with its own arguments only.
        prefix = ["-hide_banner", "-nostdin"] if executable == self.binary else []
        if executable == self.binary and self.threads is not None:
            # Placed before the inputs so it applies globally; FFmpeg accepts it
            # as a global option and an explicit per-stream value would win.
            prefix += ["-threads", str(self.threads)]
        argv = [executable, *prefix, *args]
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
            # The banner's layout token changed shape between FFmpeg versions
            # (9.x prints ``mono``/``stereo``, 6.1.1 prints ``1 channels``).
            # See :func:`parse_audio_stream_channels`.
            channels = parse_audio_stream_channels(layout)
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


# -- probe helpers ----------------------------------------------------------- #


#: Channel count -> layout name, for documents that omit ``channel_layout``.
_LAYOUT_BY_COUNT = {1: "mono", 2: "stereo"}


def _json_object(text: str) -> dict[str, object]:
    """The last JSON object in ``text``, or a refusal.

    ffprobe writes its document to stdout; some wrappers interleave a warning,
    so the object is located rather than assumed to start at offset 0.  A
    document that is not an object is a probe failure, never an empty probe.
    """
    start = text.find("{")
    if start < 0:
        raise AudioProbeError(f"ffprobe produced no JSON document: {text.strip()[-300:]}")
    # ``raw_decode`` from the first ``{`` parses exactly one object and ignores
    # whatever follows it.  Slicing between the outermost braces would instead
    # span nested objects (``disposition``, ``metadata``) and be unparseable —
    # and a truncated capture must fail here rather than half-parse.
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as error:
        raise AudioProbeError(f"ffprobe JSON is malformed: {error}") from error
    if not isinstance(payload, dict):
        raise AudioProbeError("ffprobe JSON is not an object")
    return payload


def _as_int(value: object, field: str) -> int:
    """A required integer field, refusing a stringly-typed or empty value."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AudioProbeError(f"ffprobe {field} is not an integer: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise AudioProbeError(f"ffprobe {field} is not an integer: {value!r}") from error


def _extradata_hash(extradata: object) -> str | None:
    """A stable digest of a stream's codec extradata, when ffprobe reports it.

    Extradata carries the codec-specific headers (for FLAC the STREAMINFO
    block), so two streams with identical names but different extradata are not
    the same stream.  ``None`` means "not reported", never "empty".
    """
    if not isinstance(extradata, str) or not extradata.strip():
        return None
    return "sha256:" + hashlib.sha256(extradata.encode("utf-8")).hexdigest()
