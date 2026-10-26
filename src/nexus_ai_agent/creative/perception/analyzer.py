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
* **one descriptor, one inode.**  The file is opened once with ``O_NOFOLLOW`` and
  its type, size, text sniff and hash are read from that descriptor; FFmpeg is
  handed the same descriptor (``/dev/fd/N`` via ``pass_fds``), never the pathname.
  A concurrent swap of the pathname therefore cannot make the engine validate one
  file and analyse another.
* **bounded memory and time.**  stderr is *streamed*; only the lines a parser
  needs are kept (capped), and a watchdog kills the process at ``timeout_s``.
  Input reading is capped at ``max_media_s`` seconds.
* **only known media demuxers.**  ``-format_whitelist`` limits input probing to
  real media containers (no HLS, concat, SDP, DASH, image2, data), and a text
  sniff refuses playlists before any process starts.
* **evidence is content-bound.**  The file is hashed before analysis; afterwards
  its identity (device, inode), size, mtime *and content hash* are re-checked, so
  a swap (symlink/rename/in-place write) during the run yields an error, not
  evidence about bytes FFmpeg may never have read.  (A writer that restores the
  exact original bytes before the re-hash is indistinguishable from no write.)
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
import stat as stat_module
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
    device: int = 0
    inode: int = 0
    fd: int = -1


def _sha256_fd(fd: int) -> str:
    """Hash an already-open descriptor, restoring its offset afterwards."""
    digest = hashlib.sha256()
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, _HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.lseek(fd, offset, os.SEEK_SET)
    return f"sha256:{digest.hexdigest()}"


def _read_head_fd(fd: int, size: int) -> bytes:
    """Read up to ``_SNIFF_BYTES`` from a descriptor, restoring its offset."""
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, min(_SNIFF_BYTES, size))
    finally:
        os.lseek(fd, offset, os.SEEK_SET)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


#: Demuxers a measurement may open.  Matching is per name token, so ``mov`` admits
#: the whole ``mov,mp4,m4a,3gp,3g2,mj2`` family.  HLS/concat/SDP/DASH/image2/data
#: are deliberately absent (verified on FFmpeg 6.1.1 and 7.0.2).
FORMAT_WHITELIST = ",".join(
    (
        "wav", "w64", "aiff", "flac", "mp3", "ogg", "matroska", "webm", "mov", "avi",
        "mpegts", "mpeg", "aac", "ac3", "eac3", "flv", "asf", "caf", "au", "amr",
    )
)  # fmt: skip

_SNIFF_BYTES = 1024
_TEXT_RATIO = 0.95


def _looks_textual(head: bytes) -> bool:
    """True for text-based inputs (HLS/DASH playlists, ffconcat, SDP, XML, scripts).

    Real media containers are binary (RIFF, ISO-BMFF, Matroska, Ogg, ID3/MPEG frames
    all carry NUL or high bytes in their first kilobyte).  Every FFmpeg *indirection*
    format is text: it can name other files or URLs, which is the HLS+concat
    local-file-read class (CVE-2016-1897/1898).  Refusing text outright removes the
    class instead of relying on each FFmpeg version's own mitigations.
    """
    if not head:
        return True
    if b"\x00" in head:
        return False
    printable = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(head) >= _TEXT_RATIO


def _refuse_indirection_bytes(path: Path) -> bool:
    with path.open("rb") as handle:
        return _looks_textual(handle.read(_SNIFF_BYTES))


def _fixed(value: float, digits: int) -> str:
    """Fixed-point text: never an exponent form FFmpeg's option parser could misread."""
    return f"{value:.{digits}f}"


def _seal(model: Any, **fields: Any) -> Any:
    """``model.seal`` with validation failures surfaced as typed engine errors."""
    try:
        return model.seal(**fields)
    except ValidationError as error:
        raise AnalysisFailedError(
            "measurement is outside the evidence limits (too many spans/cuts or an "
            "out-of-range value); raise the threshold or shorten the media"
        ) from error


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

    def _open_checked(self, path: Path | str) -> tuple[Path, int, os.stat_result]:
        """Resolve, open (no-follow) and validate one inode; return its descriptor.

        The caller owns the descriptor.  Type, size and the text sniff are all read
        from the descriptor, so a concurrent swap of the pathname cannot make us
        validate one file and later analyse another.
        """
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise AnalysisInputError(f"media not found: {candidate}") from error
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(resolved, flags)
        except OSError as error:
            raise AnalysisInputError(f"cannot open {resolved}: {error}") from error
        try:
            stat = os.fstat(fd)
            if not stat_module.S_ISREG(stat.st_mode):
                raise AnalysisInputError(f"not a regular file: {resolved}")
            if _looks_textual(_read_head_fd(fd, stat.st_size)):
                raise AnalysisInputError(
                    f"{resolved} is text, not a media container (playlists, concat lists "
                    "and SDP can reference other files or URLs and are refused)"
                )
        except BaseException:
            os.close(fd)
            raise
        return resolved, fd, stat

    def _source(self, path: Path | str) -> _Source:
        resolved, fd, stat = self._open_checked(path)
        try:
            return _Source(
                resolved,
                _sha256_fd(fd),
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_dev,
                stat.st_ino,
                fd,
            )
        except BaseException:
            os.close(fd)
            raise

    def _assert_unchanged(self, source: _Source) -> None:
        changed = AnalysisFailedError(
            f"{source.path} changed while it was being analysed; refusing evidence"
        )
        try:
            now = source.path.resolve(strict=True)
            stat = now.stat()
            same = (now, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) == (
                source.path,
                source.device,
                source.inode,
                source.size,
                source.mtime_ns,
            )
            if not same or _refuse_indirection_bytes(now) or _sha256(now) != source.sha256:
                raise changed
        except OSError as error:
            raise changed from error

    def _run(
        self,
        arguments: Sequence[str],
        keep: Callable[[str], object],
        *,
        pass_fds: tuple[int, ...] = (),
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
                pass_fds=pass_fds,
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
        arguments = ["-protocol_whitelist", "file", "-format_whitelist", FORMAT_WHITELIST]
        if limit:
            arguments += ["-t", str(self.max_media_s)]
        # Hand FFmpeg the *descriptor* we validated, not the pathname, so it reads
        # the same inode whose type, sniff and hash are recorded in the evidence.
        if source.fd < 0:
            # Fail closed: a pathname here would silently reopen the TOCTOU window.
            raise AnalysisFailedError(
                "no validated descriptor to hand FFmpeg; refusing to fall back to a pathname"
            )
        return [*arguments, "-i", f"/dev/fd/{source.fd}"]

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
        resolved, fd, _stat = self._open_checked(path)
        try:
            return self._probe_source(_Source(resolved, "", 0, 0, fd=fd))
        finally:
            os.close(fd)

    def _probe_source(self, source: _Source) -> parsers.MediaProbe:
        lines = self._run(
            [
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                FORMAT_WHITELIST,
                "-i",
                f"/dev/fd/{source.fd}",
                "-t",
                "0.05",
                "-f",
                "null",
                "-",
            ],
            parsers.PROBE_LINE.match,
            pass_fds=(source.fd,),
        )
        try:
            return parsers.parse_probe(lines)
        except parsers.ParseError as error:
            raise AnalysisFailedError(f"cannot probe {source.path}: {error}") from error

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
        # Round once; the filter, the evidence and the range check all use the rounded
        # values, so evidence never names a parameter FFmpeg did not receive.
        noise_floor_db = round(noise_floor_db, 1)
        min_silence_s = round(min_silence_s, 3)
        if not -100.0 < noise_floor_db <= -10.0 or not 0.01 <= min_silence_s <= 600.0:
            raise AnalysisInputError("parameters are out of range after rounding")
        source = self._source(path)
        try:
            probe = self._probe_source(source)
            if not probe.has_audio:
                raise AnalysisInputError(f"{source.path} has no audio stream")
            self._require_within_limit(source, probe.duration_us)
            lines = self._run(
                [
                    *self._common_input(source),
                    "-vn",
                    "-af",
                    (
                        f"silencedetect=noise={_fixed(noise_floor_db, 1)}dB:"
                        f"d={_fixed(min_silence_s, 3)}"
                    ),
                    "-f",
                    "null",
                    "-",
                ],
                parsers.SILENCE_LINE.search,
                pass_fds=(source.fd,),
            )
            try:
                spans = parsers.parse_silences(lines, probe.duration_us)
            except parsers.ParseError as error:
                raise AnalysisFailedError(f"unreadable silencedetect output: {error}") from error
            self._assert_unchanged(source)
            return _seal(
                SilenceEvidence,
                engine=self.engine_id(),
                asset_sha256=source.sha256,
                duration_us=probe.duration_us,
                noise_floor_db=noise_floor_db,
                min_silence_us=parsers.to_us(min_silence_s),
                spans=tuple(SilenceSpan(start_us=a, end_us=b) for a, b in spans),
            )
        finally:
            os.close(source.fd)

    def scene_cuts(self, path: Path | str, *, threshold: float = 0.3) -> SceneCutEvidence:
        """Scene-change instants of the video stream (``select='gt(scene,T)'``)."""
        if not 0.0 < threshold < 1.0:
            raise AnalysisInputError("threshold must be strictly between 0 and 1")
        threshold = round(threshold, 3)
        if not 0.0 < threshold < 1.0:
            raise AnalysisInputError("threshold is out of range after rounding to 3 digits")
        source = self._source(path)
        try:
            probe = self._probe_source(source)
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
                pass_fds=(source.fd,),
            )
            try:
                cuts = parsers.parse_scene_cuts(lines, probe.duration_us)
            except parsers.ParseError as error:
                raise AnalysisFailedError(f"unreadable showinfo output: {error}") from error
            self._assert_unchanged(source)
            return _seal(
                SceneCutEvidence,
                engine=self.engine_id(),
                asset_sha256=source.sha256,
                duration_us=probe.duration_us,
                threshold=threshold,
                cuts_us=tuple(cuts),
            )
        finally:
            os.close(source.fd)

    def loudness(self, path: Path | str) -> LoudnessEvidence:
        """EBU R128 integrated loudness, loudness range and true peak (``ebur128``)."""
        source = self._source(path)
        try:
            probe = self._probe_source(source)
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
                pass_fds=(source.fd,),
            )
            try:
                result = parsers.parse_loudness("\n".join(lines))
            except parsers.ParseError as error:
                raise AnalysisFailedError(f"unreadable ebur128 output: {error}") from error
            self._assert_unchanged(source)
            return _seal(
                LoudnessEvidence,
                engine=self.engine_id(),
                asset_sha256=source.sha256,
                duration_us=probe.duration_us,
                integrated_lufs=result.integrated_lufs,
                loudness_range_lu=result.loudness_range_lu,
                true_peak_dbfs=result.true_peak_dbfs,
            )
        finally:
            os.close(source.fd)


__all__ = [
    "AnalysisError",
    "AnalysisFailedError",
    "AnalysisInputError",
    "AnalysisTimeoutError",
    "AnalysisUnavailableError",
    "FfmpegAnalyzer",
]
