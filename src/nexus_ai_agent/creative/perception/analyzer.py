"""The perception engine: free, offline media measurement through FFmpeg.

Why FFmpeg.  It is already a hard dependency of the render lane
(:mod:`nexus_ai_agent.creative.slideshow.ffmpeg`), costs nothing, needs no API
key and no network, and ships real, standardised measurement filters
(``silencedetect``, ``select='gt(scene,T)'``, EBU R128 ``ebur128``) -- so the
studio can *measure* media instead of assuming it.  No new dependency is added.

Safety properties, each pinned by a test:

* **argv only, no shell.**  The binary is resolved once (override, env, PATH,
  bundled wheel); every argument is a list element.
* **a path can never become an option or a protocol.**  The input is resolved to
  an absolute path of an existing regular file, and ``-protocol_whitelist file``
  makes FFmpeg refuse network/playlist indirection (``http:``, HLS entries,
  ``concat:``) even if a container tries to name one.
* **bounded memory and time.**  stderr is *streamed*; only the lines a parser
  needs are kept (capped), and a watchdog kills the process at ``timeout_s``.
  Input reading is capped at ``max_media_s`` seconds.
* **evidence is content-bound.**  The file is hashed before analysis and its
  ``(size, mtime_ns)`` is re-checked afterwards; a file that changes while it is
  analysed yields an error, not evidence about bytes nobody can name.
* **fail loud.**  A missing stream, a non-zero exit, an unparsable summary and a
  timeout are four distinct typed errors; none degrades into empty evidence.

Times are on the container's normalised timeline (what a player shows): FFmpeg
removes the stream start offset before reporting, so evidence lines up with
``Clip.source_range``.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from nexus_ai_agent.creative.packs.perception.models import (
    LoudnessEvidence,
    SceneCutEvidence,
    SilenceEvidence,
    SilenceSpan,
)
from nexus_ai_agent.creative.perception import parsers
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin

DEFAULT_TIMEOUT_S = 300.0
DEFAULT_MAX_MEDIA_S = 4 * 3600
MAX_KEPT_LINES = 200_000
_TAIL_LINES = 6
_TAIL_CHARS = 240
_HASH_CHUNK = 1024 * 1024


class AnalysisError(RuntimeError):
    """Base class: the engine could not produce trustworthy evidence."""


class AnalysisUnavailableError(AnalysisError):
    """No FFmpeg binary could be resolved."""


class AnalysisInputError(AnalysisError):
    """The input file is missing, not a regular file, or lacks the needed stream."""


class AnalysisTimeoutError(AnalysisError):
    """The engine exceeded its wall-clock budget and was killed."""


class AnalysisFailedError(AnalysisError):
    """The engine exited non-zero, or its output did not contain a coherent result."""


@dataclass(frozen=True)
class _Source:
    path: Path
    sha256: str
    size: int
    mtime_ns: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _fixed(value: float, digits: int) -> str:
    """Fixed-point text: never an exponent form FFmpeg's option parser could misread."""
    return f"{value:.{digits}f}"


@dataclass
class FfmpegAnalyzer:
    """Run FFmpeg's measurement filters and return sealed, content-bound evidence."""

    binary: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_media_s: int = DEFAULT_MAX_MEDIA_S
    _resolved: str | None = field(default=None, init=False, repr=False)
    _engine: str | None = field(default=None, init=False, repr=False)

    # -- plumbing -------------------------------------------------------

    def _binary(self) -> str:
        if self._resolved is None:
            try:
                self._resolved = resolve_ffmpeg_bin(self.binary)
            except FfmpegUnavailableError as error:
                raise AnalysisUnavailableError(str(error)) from error
        return self._resolved

    def _source(self, path: Path | str) -> _Source:
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise AnalysisInputError(f"media not found: {candidate}") from error
        if not resolved.is_file():
            raise AnalysisInputError(f"not a regular file: {resolved}")
        stat = resolved.stat()
        return _Source(resolved, _sha256(resolved), stat.st_size, stat.st_mtime_ns)

    def _assert_unchanged(self, source: _Source) -> None:
        stat = source.path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (source.size, source.mtime_ns):
            raise AnalysisFailedError(
                f"{source.path} changed while it was being analysed; refusing evidence"
            )

    def _run(
        self,
        arguments: Sequence[str],
        keep: Callable[[str], object],
    ) -> list[str]:
        """Run FFmpeg, stream stderr, keep only relevant lines, enforce the time budget."""
        argv = [self._binary(), "-hide_banner", "-nostdin", "-nostats", *arguments]
        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        kept: list[str] = []
        try:
            process = subprocess.Popen(  # noqa: S603 - argv list, shell=False, resolved binary
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,  # own process group: a kill reaches every child
            )
        except OSError as error:
            raise AnalysisUnavailableError(f"cannot execute FFmpeg: {error}") from error

        def _terminate() -> None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - non-POSIX
                    process.kill()
            except (ProcessLookupError, PermissionError):
                pass

        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            _terminate()

        watchdog = threading.Timer(self.timeout_s, _kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            assert process.stderr is not None  # noqa: S101 - PIPE was requested
            for raw in process.stderr:
                line = raw.rstrip("\r\n")
                tail.append(line[:_TAIL_CHARS])
                if keep(line):  # a regex match object or None is enough
                    if len(kept) >= MAX_KEPT_LINES:
                        _terminate()
                        raise AnalysisFailedError(
                            f"FFmpeg produced more than {MAX_KEPT_LINES} relevant lines; "
                            "raise the detection threshold"
                        )
                    kept.append(line)
            returncode = process.wait()
        finally:
            watchdog.cancel()
            _terminate()  # also reaps stragglers that outlive the parent
            process.wait()
            if process.stderr is not None:
                process.stderr.close()
        if timed_out.is_set():
            raise AnalysisTimeoutError(f"FFmpeg exceeded {self.timeout_s:g}s and was killed")
        if returncode != 0:
            raise AnalysisFailedError(
                f"FFmpeg exited with status {returncode}: " + " | ".join(tail)
            )
        return kept

    def _require_within_limit(self, source: _Source, duration_us: int) -> None:
        if duration_us > self.max_media_s * 1_000_000:
            raise AnalysisInputError(
                f"{source.path} is longer than the {self.max_media_s}s analysis limit; "
                "refusing partial evidence"
            )

    def _common_input(self, source: _Source, *, limit: bool = True) -> list[str]:
        arguments = ["-protocol_whitelist", "file"]
        if limit:
            arguments += ["-t", str(self.max_media_s)]
        return [*arguments, "-i", str(source.path)]

    # -- public API -----------------------------------------------------

    def engine_id(self) -> str:
        """``ffmpeg <version>``; one cheap process, cached for the analyzer's lifetime."""
        if self._engine is None:
            try:
                completed = subprocess.run(  # noqa: S603 - argv list, shell=False
                    [self._binary(), "-version"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=min(self.timeout_s, 30.0),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AnalysisUnavailableError(f"cannot query FFmpeg version: {error}") from error
            version = parsers.parse_version(completed.stdout.splitlines())
            self._engine = f"ffmpeg {version}"[:120]
        return self._engine

    def probe(self, path: Path | str) -> parsers.MediaProbe:
        """Duration and stream presence of ``path`` (no evidence, no hashing)."""
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise AnalysisInputError(f"media not found: {candidate}") from error
        if not resolved.is_file():
            raise AnalysisInputError(f"not a regular file: {resolved}")
        return self._probe_resolved(resolved)

    def _probe_resolved(self, resolved: Path) -> parsers.MediaProbe:
        lines = self._run(
            [
                "-protocol_whitelist",
                "file",
                "-i",
                str(resolved),
                "-t",
                "0.05",
                "-f",
                "null",
                "-",
            ],
            parsers.PROBE_LINE.match,
        )
        try:
            return parsers.parse_probe(lines)
        except parsers.ParseError as error:
            raise AnalysisFailedError(f"cannot probe {resolved}: {error}") from error

    def silences(
        self,
        path: Path | str,
        *,
        noise_floor_db: float = -40.0,
        min_silence_s: float = 0.5,
    ) -> SilenceEvidence:
        """Silent intervals of the audio stream (``silencedetect``)."""
        if not -100.0 < noise_floor_db <= -10.0:
            raise AnalysisInputError("noise_floor_db must be in (-100, -10]")
        if not 0.01 <= min_silence_s <= 600.0:
            raise AnalysisInputError("min_silence_s must be in [0.01, 600]")
        source = self._source(path)
        probe = self._probe_resolved(source.path)
        if not probe.has_audio:
            raise AnalysisInputError(f"{source.path} has no audio stream")
        self._require_within_limit(source, probe.duration_us)
        lines = self._run(
            [
                *self._common_input(source),
                "-vn",
                "-af",
                f"silencedetect=noise={_fixed(noise_floor_db, 1)}dB:d={_fixed(min_silence_s, 3)}",
                "-f",
                "null",
                "-",
            ],
            parsers.SILENCE_LINE.search,
        )
        try:
            spans = parsers.parse_silences(lines, probe.duration_us)
        except parsers.ParseError as error:
            raise AnalysisFailedError(f"unreadable silencedetect output: {error}") from error
        self._assert_unchanged(source)
        return SilenceEvidence.seal(
            engine=self.engine_id(),
            asset_sha256=source.sha256,
            duration_us=probe.duration_us,
            noise_floor_db=noise_floor_db,
            min_silence_us=parsers.to_us(min_silence_s),
            spans=tuple(SilenceSpan(start_us=a, end_us=b) for a, b in spans),
        )

    def scene_cuts(self, path: Path | str, *, threshold: float = 0.3) -> SceneCutEvidence:
        """Scene-change instants of the video stream (``select='gt(scene,T)'``)."""
        if not 0.0 < threshold < 1.0:
            raise AnalysisInputError("threshold must be strictly between 0 and 1")
        source = self._source(path)
        probe = self._probe_resolved(source.path)
        if not probe.has_video:
            raise AnalysisInputError(f"{source.path} has no video stream")
        self._require_within_limit(source, probe.duration_us)
        lines = self._run(
            [
                *self._common_input(source),
                "-an",
                "-vf",
                f"select='gt(scene,{_fixed(threshold, 3)})',showinfo",
                "-f",
                "null",
                "-",
            ],
            parsers.SCENE_LINE.search,
        )
        try:
            cuts = parsers.parse_scene_cuts(lines, probe.duration_us)
        except parsers.ParseError as error:
            raise AnalysisFailedError(f"unreadable showinfo output: {error}") from error
        self._assert_unchanged(source)
        return SceneCutEvidence.seal(
            engine=self.engine_id(),
            asset_sha256=source.sha256,
            duration_us=probe.duration_us,
            threshold=threshold,
            cuts_us=tuple(cuts),
        )

    def loudness(self, path: Path | str) -> LoudnessEvidence:
        """EBU R128 integrated loudness, loudness range and true peak (``ebur128``)."""
        source = self._source(path)
        probe = self._probe_resolved(source.path)
        if not probe.has_audio:
            raise AnalysisInputError(f"{source.path} has no audio stream")
        self._require_within_limit(source, probe.duration_us)
        lines = self._run(
            [
                *self._common_input(source),
                "-vn",
                "-af",
                "ebur128=peak=true:framelog=quiet",
                "-f",
                "null",
                "-",
            ],
            parsers.LOUDNESS_SUMMARY_LINE.search,
        )
        try:
            result = parsers.parse_loudness("\n".join(lines))
        except parsers.ParseError as error:
            raise AnalysisFailedError(f"unreadable ebur128 output: {error}") from error
        self._assert_unchanged(source)
        return LoudnessEvidence.seal(
            engine=self.engine_id(),
            asset_sha256=source.sha256,
            duration_us=probe.duration_us,
            integrated_lufs=result.integrated_lufs,
            loudness_range_lu=result.loudness_range_lu,
            true_peak_dbfs=result.true_peak_dbfs,
        )


__all__ = [
    "AnalysisError",
    "AnalysisFailedError",
    "AnalysisInputError",
    "AnalysisTimeoutError",
    "AnalysisUnavailableError",
    "FfmpegAnalyzer",
]
