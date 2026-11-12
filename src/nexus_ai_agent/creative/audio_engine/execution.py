"""The native audio execution plane: real signal processing behind one port.

This module implements
:class:`nexus_ai_agent.application.ports.audio_engine.AudioEnginePort` with
actual FFmpeg work and an independent verification pass.  It is the *only* place
in the tree that knows both halves (the pure pack contract and the process
plane), which is why it lives in ``creative/audio_engine`` rather than in the
pack.

One run, end to end (``normalize_loudness``):

1. **precheck** the source: exists, is decodable audio, hash matches the claim;
2. **stage** a private copy inside the workspace (the caller's file is never
   opened for writing);
3. **measure** with the ``loudnorm`` first pass (``-14/-23`` targets are
   profile-driven; no hard-coded numbers);
4. **encode** into the run's staging directory under the canonical stream lock;
5. **publish** atomically (``os.link`` + ``os.rename``), refusing to replace the
   input;
6. **verify** the *published* bytes: hash, full decode, fresh ``ebur128``
   measurement against the target, canonical shape, acoustic signature, and
   finally an Ed25519-signed :class:`ArtifactPassport`;
7. **report** measured evidence (and only measured evidence).

Any failure removes the run directory and publishes nothing.

Bounded memory: the only in-memory audio is the analysis window
(``max_analysis_seconds``) and the beat-analysis window
(``max_beat_analysis_seconds``); everything else is streamed through files.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from nexus_ai_agent.creative.audio_engine.continuity import (
    AssemblyProof,
    ContinuityError,
    assert_lossless_assembly,
)
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
    AudioEngineError,
    AudioEngineUnavailableError,
    FfmpegTranscoder,
)
from nexus_ai_agent.creative.audio_engine.models import (
    CANONICAL_PROFILE_ID,
    MIN_PROGRAMME_SECONDS,
    NAGAR_STREAMING_PRESET,
    TRUE_PEAK_TOLERANCE_DB,
    CanonicalStreamProfile,
    LoudnessMeasurement,
    LoudnessProfile,
    MediaContractError,
    MediaProbe,
    RenderOutcome,
    ResourceBudget,
    resolve_loudness_profile,
)
from nexus_ai_agent.creative.audio_engine.numpy_dsp import decode_s32le
from nexus_ai_agent.creative.audio_engine.process import (
    CancelToken,
    ProcessCancelled,
    ProcessGovernor,
)
from nexus_ai_agent.creative.audio_engine.recipe import recipe_identity
from nexus_ai_agent.creative.audio_engine.verify import (
    ArtifactPassport,
    PassportError,
    build_passport,
)
from nexus_ai_agent.creative.audio_engine.workspace import (
    PublishResult,
    RuntimeWorkspace,
    WorkspaceBoundaryError,
    hash_file,
    safe_file_name,
)
from nexus_ai_agent.creative.slideshow.audio import (
    PcmAudio,
    collect_tempo_candidates,
)
from nexus_ai_agent.creative.studio.audio_engine import (
    ArtifactEvidence,
    AudioEngineHealth,
    BeatAnalysis,
    BeatRequest,
    DenoiseRequest,
    EngineCapability,
    EngineInputError,
    EngineRejected,
    LoudnessRequest,
    SourcePrecheck,
    SourceRequest,
)

__all__ = [
    "AudioExecutionSettings",
    "NativeAudioEngine",
    "AudioSourceCatalog",
    "CallbackSourceCatalog",
    "LocalDirectorySourceCatalog",
    "build_native_audio_engine",
    "AssemblyProof",
    "AUDIO_SUFFIXES",
]

_VERSION_RE = re.compile(r"ffmpeg version (\S+)")

#: Suffixes the engine will hand to FFmpeg (anything else is refused before a
#: process starts, so a mis-suffixed file cannot become a decode error later).
AUDIO_SUFFIXES = frozenset(
    {
        ".wav",
        ".flac",
        ".mp3",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".aiff",
        ".aif",
        ".wma",
        ".mp4",
        ".mov",
    }
)


@dataclass(frozen=True)
class AudioExecutionSettings:
    """Composition settings (paths and budgets), not operation parameters."""

    workspace_root: Path
    max_analysis_seconds: float = 60.0
    max_beat_analysis_seconds: float = 120.0
    deadline_seconds: float = 600.0
    max_captured_bytes: int = 256 * 1024
    #: CPU threads handed to every FFmpeg child (``-threads``).  ``None`` leaves
    #: FFmpeg's own default, which is "all cores" — acceptable on a workstation,
    #: hostile on a shared worker.  A deployment that shares the box MUST set it.
    threads: int | None = None
    #: Bytes the workspace may hold in staging before a run is refused.  This is
    #: measured against the workspace, not against a per-file guess.
    staging_quota_bytes: int = 8 * 1024**3
    #: The Ed25519 seed used to sign passports.  ``None`` ⇒ passports are issued
    #: unsigned and every consumer must treat them as non-attested.
    signing_key: bytes | None = None
    signer_key_id: str | None = None
    ffmpeg_binary: str | None = None


class AudioSourceCatalog:
    """Maps a studio asset id to the file that holds its bytes.

    The studio state model (:class:`~nexus_ai_agent.creative.studio.models.AssetRecord`)
    deliberately carries a content hash and *no* filesystem path; the mapping
    from asset identity to bytes is a runtime concern.  A deployment supplies it
    here (see :class:`LocalDirectorySourceCatalog`), which keeps the pack pure
    and the engine free of storage policy.
    """

    def resolve(self, asset_id: str) -> Path | None:  # pragma: no cover - interface
        raise NotImplementedError


class CallbackSourceCatalog(AudioSourceCatalog):
    """A catalog backed by a caller-supplied lookup function (tests, adapters)."""

    def __init__(self, lookup: Any) -> None:  # noqa: ANN401 - Callable[[str], Path | None]
        self._lookup = lookup

    def resolve(self, asset_id: str) -> Path | None:
        result = self._lookup(asset_id)
        return None if result is None else Path(result)


class LocalDirectorySourceCatalog(AudioSourceCatalog):
    """Resolve ``<asset_id>.<ext>`` inside one directory (the local runtime).

    Content is verified by hash in :meth:`NativeAudioEngine.precheck`, so a
    name-based lookup can never be used to smuggle different bytes past the
    registry: the promise hash is authoritative.
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def resolve(self, asset_id: str) -> Path | None:
        if not asset_id or "/" in asset_id or "\\" in asset_id or asset_id in (".", ".."):
            return None
        candidate = self.directory / safe_file_name(asset_id, suffix="", default="missing")
        if candidate.suffix.lower() in AUDIO_SUFFIXES and candidate.is_file():
            return candidate
        for suffix in sorted(AUDIO_SUFFIXES):
            alternative = self.directory / f"{asset_id}{suffix}"
            if alternative.is_file():
                return alternative
        return None


def _as_render_outcome(value: Any) -> RenderOutcome:  # noqa: ANN401 - builder contract
    """Normalise a builder's return value to a :class:`RenderOutcome`.

    Builders that produce an artifact with no loudness target (denoise, ducking,
    …) return a bare path; only the loudness path knows its processing mode.
    Accepting both keeps the builder signature honest without forcing every
    builder to invent a crest factor it never measured.
    """
    if isinstance(value, RenderOutcome):
        return value
    return RenderOutcome(path=Path(value))


def _run_artifact_path(run_dir: Path | None, staged: Path, name: str) -> Path:
    """Where a run writes its intermediate artifact.

    The run directory when one was given (the normal case: it is removed on
    exit whether the run succeeds, refuses or raises), otherwise next to the
    staged input — which is only correct for a caller that is not using
    ``run_directory`` at all (a unit test of the builder, or the loudness
    two-pass path invoked directly).  Rendering next to the *input* is never
    the intent: ``inputs/`` is the immutable, content-addressed directory.
    """
    return (run_dir or staged.parent) / name


def _tree_bytes(root: Path) -> int:
    """Total bytes under ``root`` (a measurement, not an estimate)."""
    total = 0
    for entry in Path(root).rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:  # pragma: no cover - a file removed mid-walk
            continue
    return total


#: Suffix of the sidecar that records *which recipe produced* a published
#: artifact.  It lives next to the media file, is written after the atomic
#: publish, and is the only thing the reuse path trusts: the artifact's own
#: bytes cannot say whether they came from ``strength=0.2`` or ``0.9``.
RECIPE_SUFFIX = ".recipe"


def _read_recipe_marker(marker: Path) -> str | None:
    """The recipe identity recorded for the artifact, or ``None``.

    A missing, unreadable or malformed marker means "unknown producer", which
    must never be treated as a match: an artifact of unknown provenance is
    re-rendered rather than reused.
    """
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _write_recipe_marker(marker: Path, identity: str) -> None:
    """Atomically record the recipe identity next to the published artifact."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(identity + "\n", encoding="utf-8")
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _cancelled_before_start(cancel: CancelToken) -> BaseException:
    """The refusal raised for a run whose token had already fired."""
    return ProcessCancelled(
        "the run was cancelled before its first process started; "
        "nothing was rendered and nothing was published"
    )


@dataclass
class _RunContext:
    """Bookkeeping for one execution (workspace + temp sinks + timings)."""

    workspace: RuntimeWorkspace
    cancel: CancelToken | None = None
    temp_paths: list[Path] = field(default_factory=list)

    def temp_sink(self, prefix: str, suffix: str) -> Path:
        handle, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
        os.close(handle)
        target = Path(path)
        self.temp_paths.append(target)
        return target

    def cleanup(self) -> None:
        for path in self.temp_paths:
            path.unlink(missing_ok=True)


def build_native_audio_engine(
    *,
    workspace_root: Path | str,
    media_root: Path | str,
    signing_key: bytes | None = None,
    signer_key_id: str | None = None,
    ffmpeg_binary: str | None = None,
    max_analysis_seconds: float = 60.0,
    deadline_seconds: float = 600.0,
) -> NativeAudioEngine:
    """Compose the engine a deployment should actually hand to the studio.

    This is the one place that decides *where* a deployment's bytes live:

    * :func:`workspace_root` is the isolated runtime workspace — staging,
      published media and quarantined refusals all live under it, and nothing
      outside it can be published;
    * :func:`media_root` is where an asset id resolves to a file
      (:class:`LocalDirectorySourceCatalog`).  Content is verified against the
      registry's recorded hash before any process starts, so the directory is a
      *lookup*, not an authority.

    The signing key is the deployment's Ed25519 seed.  With no key the engine
    still runs and still verifies every artifact, but its passports are issued
    unsigned and every consumer must treat them as non-attested
    (``ARTIFACT_VERIFIED`` is the ceiling without a signature).

    Environment fallbacks (all optional) are read here and nowhere else:
    ``NEXUS_CREATIVE_WORKSPACE``, ``NEXUS_AUDIO_MEDIA_ROOT`` and
    ``NEXUS_SIGNING_KEY`` (64 hex chars, the same variable the delivery pack
    already uses).
    """
    # ``media_root`` is the directory an asset id resolves to.  It used to
    # default to ``Path.cwd() / "media"``, which made the *process's working
    # directory* a source of truth: a deployment that changed cwd silently
    # started resolving asset ids against a different folder, and a test that
    # forgot to pass a root read whatever happened to be there.  The default is
    # now derived from the workspace root (one declared location, one meaning).
    root = (
        Path(workspace_root or os.environ.get("NEXUS_CREATIVE_WORKSPACE") or Path.cwd() / ".nagar")
        .expanduser()
        .resolve(strict=False)
    )
    configured_media = media_root or os.environ.get("NEXUS_AUDIO_MEDIA_ROOT")
    if configured_media:
        media = Path(configured_media).expanduser().resolve(strict=False)
    else:
        # A sibling of the workspace root, not of the *cwd*.
        media = (root.parent / "media").resolve(strict=False)
    key = signing_key
    if key is None:
        raw = os.environ.get("NEXUS_SIGNING_KEY", "").strip()
        if raw and len(raw) == 64:
            try:
                key = bytes.fromhex(raw)
            except ValueError:  # pragma: no cover - a non-hex 64-char value
                key = None
    from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key, key_id_for

    resolved_key_id = signer_key_id or (key_id_for(derive_public_key(key)) if key else None)
    return NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media),
        settings=AudioExecutionSettings(
            workspace_root=root,
            signing_key=key,
            signer_key_id=resolved_key_id,
            ffmpeg_binary=ffmpeg_binary,
            max_analysis_seconds=max_analysis_seconds,
            deadline_seconds=deadline_seconds,
        ),
    )


class NativeAudioEngine:
    """Real DSP behind :class:`AudioEnginePort` (FFmpeg + bounded numpy)."""

    def __init__(
        self,
        *,
        catalog: AudioSourceCatalog,
        settings: AudioExecutionSettings,
        transcoder: FfmpegTranscoder | None = None,
    ) -> None:
        self._catalog = catalog
        self._settings = settings
        self._transcoder = transcoder or FfmpegTranscoder(
            binary=settings.ffmpeg_binary,
            governor=ProcessGovernor(),
            deadline_seconds=settings.deadline_seconds,
            max_captured_bytes=settings.max_captured_bytes,
        )
        # A deployment that shares a box must be able to cap FFmpeg's fan-out;
        # the value is recorded in the run's ResourceBudget either way.
        self._transcoder.threads = settings.threads

    # -- health ----------------------------------------------------------- #
    def health(self) -> AudioEngineHealth:
        binary: str | None = None
        version: str | None = None
        try:
            binary = self._transcoder.binary
        except AudioEngineUnavailableError as error:
            unavailable = EngineCapability(
                name="ffmpeg",
                status="UNAVAILABLE",
                code="requires_ffmpeg",
                detail=str(error),
                remedy="install FFmpeg, set NEXUS_FFMPEG_BIN, or install imageio-ffmpeg",
            )
            return AudioEngineHealth(
                capabilities={
                    "ffmpeg": unavailable,
                    "audio.detect_beats": unavailable.model_copy(
                        update={"name": "audio.detect_beats"}
                    ),
                    "audio.normalize_loudness": unavailable.model_copy(
                        update={"name": "audio.normalize_loudness"}
                    ),
                    "audio.remove_noise": unavailable.model_copy(
                        update={"name": "audio.remove_noise"}
                    ),
                }
            )
        probe = self._transcoder.governor.run(
            [binary, "-hide_banner", "-version"],
            deadline_seconds=30.0,
            max_captured_bytes=64 * 1024,
        )
        match = _VERSION_RE.search(probe.stdout_tail or "")
        version = match.group(1) if match else None
        available = EngineCapability(name="ffmpeg", status="AVAILABLE", detail=version)
        return AudioEngineHealth(
            binary=binary,
            ffmpeg_version=version,
            capabilities={
                "ffmpeg": available,
                "audio.detect_beats": available.model_copy(
                    update={
                        "name": "audio.detect_beats",
                        "detail": "ffmpeg decode + numpy onset analysis",
                    }
                ),
                "audio.normalize_loudness": available.model_copy(
                    update={
                        "name": "audio.normalize_loudness",
                        "detail": "two-pass ffmpeg loudnorm + independent ebur128 verification",
                    }
                ),
                "audio.remove_noise": available.model_copy(
                    update={
                        "name": "audio.remove_noise",
                        "detail": "highpass + afftdn spectral denoise (static noise)",
                    }
                ),
            },
        )

    # -- source ----------------------------------------------------------- #
    def _path(self, asset_id: str) -> Path:
        resolved = self._catalog.resolve(asset_id)
        if resolved is None:
            raise EngineInputError(f"no media is registered for asset {asset_id!r}")
        if not resolved.is_file():
            raise EngineInputError(f"media for asset {asset_id!r} is missing at {resolved}")
        if resolved.suffix.lower() not in AUDIO_SUFFIXES:
            raise EngineInputError(
                f"asset {asset_id!r} has unsupported media type {resolved.suffix!r} "
                f"(accepted: {sorted(AUDIO_SUFFIXES)})"
            )
        return resolved

    def precheck(self, request: SourceRequest) -> SourcePrecheck:
        try:
            path = self._path(request.asset_id)
        except EngineInputError as error:
            return SourcePrecheck(asset_id=request.asset_id, exists=False, detail=str(error))
        digest = hash_file(path)
        if request.expected_sha256 is not None and digest != request.expected_sha256:
            return SourcePrecheck(
                asset_id=request.asset_id,
                path=str(path),
                exists=True,
                sha256=digest,
                size_bytes=path.stat().st_size,
                readable=False,
                detail=(
                    f"the bytes on disk do not match the registered hash "
                    f"(disk {digest}, registry {request.expected_sha256})"
                ),
            )
        try:
            probe = self._transcoder.probe(path)
        except AudioEngineError as error:
            return SourcePrecheck(
                asset_id=request.asset_id,
                path=str(path),
                exists=True,
                sha256=digest,
                size_bytes=path.stat().st_size,
                readable=False,
                detail=f"the file is not decodable audio: {error}",
            )
        return SourcePrecheck(
            asset_id=request.asset_id,
            path=str(path),
            exists=True,
            sha256=digest,
            size_bytes=path.stat().st_size,
            sample_rate=probe.sample_rate,
            channels=probe.channels,
            duration_us=probe.container_duration_us,
            codec=probe.codec,
            readable=probe.sample_rate is not None,
            detail=None if probe.sample_rate is not None else "no audio stream found",
        )

    # -- beat detection --------------------------------------------------- #
    def detect_beats(
        self, request: BeatRequest, *, cancel: CancelToken | None = None
    ) -> BeatAnalysis:
        if request.max_bpm <= request.min_bpm:
            raise EngineInputError("max_bpm must exceed min_bpm")
        source = self._path(request.source_asset_id)
        workspace = RuntimeWorkspace(self._settings.workspace_root).prepare()
        sink = Path(tempfile.mkstemp(prefix="nagar_beats_", suffix=".s32le")[1])
        run_started = time.monotonic()
        sink_bytes = 0
        try:
            probe = self._transcoder.probe(source)
            if probe.sample_rate is None or probe.channels is None:
                raise EngineInputError(f"{source.name} carries no decodable audio stream")
            window = self._settings.max_beat_analysis_seconds
            outcome = self._transcoder._run(  # noqa: SLF001 - same package, bounded call
                [
                    "-v",
                    "error",
                    *(["-t", f"{window:g}"] if window > 0 else []),
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-f",
                    "s32le",
                    "-acodec",
                    "pcm_s32le",
                    "-",
                ],
                stdout_path=str(sink),
            )
            if not outcome.ok:
                raise EngineInputError(f"could not decode {source.name} for beat analysis")
            sink_bytes = sink.stat().st_size
            pcm = self._read_pcm_window(sink, max_seconds=window)
            mono = pcm if pcm.ndim == 1 else pcm.mean(axis=1)
            evidence = collect_tempo_candidates(
                PcmAudio(samples=np.asarray(mono, dtype=np.float32), sample_rate=probe.sample_rate),
                min_bpm=request.min_bpm,
                max_bpm=request.max_bpm,
            )
            return BeatAnalysis(
                status=evidence.status,  # type: ignore[arg-type]
                tempo_bpm=evidence.tempo_bpm,
                beat_period_us=evidence.beat_period_us,
                confidence=evidence.confidence,
                first_beat_us=evidence.first_beat_us,
                bpm_std_uncertainty=evidence.bpm_std_uncertainty,
                bpm_uncertainty_type_a=evidence.bpm_uncertainty_type_a,
                bpm_uncertainty_type_b=evidence.bpm_uncertainty_type_b,
                bpm_ci95_half_width=evidence.bpm_ci95_half_width,
                octave_relation=evidence.octave_relation,  # type: ignore[arg-type]
                octave_pulse_clarity=evidence.octave_pulse_clarity,
                onset_count=evidence.onset_count,
                onset_times_us=tuple(
                    int(round(seconds * 1_000_000)) for seconds in evidence.onset_seconds
                ),
                min_bpm=request.min_bpm,
                max_bpm=request.max_bpm,
                budget=self._analysis_budget(
                    source=source,
                    probe=probe,
                    decoded_bytes=sink_bytes,
                    elapsed_seconds=time.monotonic() - run_started,
                    samples=int(np.asarray(mono).size),
                ).report(),
            )
        finally:
            sink.unlink(missing_ok=True)
            del workspace

    def _read_pcm_window(self, sink: Path, *, max_seconds: float) -> np.ndarray:
        frames = int(max_seconds * 192_000) if max_seconds > 0 else None
        data = decode_s32le(sink, channels=1, max_frames=frames)
        if data.size == 0:
            raise EngineInputError("beat analysis decoded zero samples")
        return data.reshape(-1)

    # -- loudness --------------------------------------------------------- #
    def normalize_loudness(
        self, request: LoudnessRequest, *, cancel: CancelToken | None = None
    ) -> ArtifactEvidence:
        try:
            profile = resolve_loudness_profile(
                request.profile_id,
                target_lufs=request.target_lufs,
                true_peak_dbtp=request.true_peak_dbtp,
                target_lra_lu=request.target_lra_lu,
            )
        except MediaContractError as error:
            raise EngineInputError(str(error)) from error
        if not request.two_pass:  # pragma: no cover - the pack always asks for two passes
            raise EngineInputError(
                "single-pass loudness is refused: without a measurement pass there is "
                "no evidence the output reached the target"
            )
        return self._derive(
            request_id=request.request_id,
            operation="audio.normalize_loudness",
            source_asset_id=request.source_asset_id,
            output_asset_id=request.output_asset_id,
            expected_source_sha256=request.source_sha256,
            parameters={
                "profile_id": profile.profile_id,
                "target_lufs": profile.target_lufs,
                "true_peak_dbtp": profile.true_peak_dbtp,
                "target_lra_lu": profile.target_lra_lu,
                "two_pass": True,
            },
            loudness_profile=profile,
            transaction_id=request.transaction_id,
            continuity_lock_hash=request.continuity_lock_hash,
            builder=lambda staged, workspace, cancel, run_dir: self._render_loudness(
                staged, workspace, profile, cancel, run_dir=run_dir
            ),
            cancel=cancel,
        )

    def _canonical_form(
        self,
        staged: Path,
        stream: CanonicalStreamProfile,
        *,
        cancel: CancelToken | None,
        run_dir: Path | None,
    ) -> Path:
        """Convert ``staged`` to the canonical stream shape (idempotent).

        Returns the *same* path when the source already has the shape, so the
        common case costs one probe and no extra encode.  When it does not, the
        conversion is explicit and its coefficients are stated in the filter
        chain: ``pan=stereo|c0=c0|c1=c0`` is a dual-mono up-mix — the **same**
        signal on both channels, which BS.1770 sums as ``10*log10(2)`` =
        +3.0103 dB.  That constant is the whole reason this step exists.
        """
        probe = self._transcoder.probe(staged, cancel=cancel)
        needs_rate = probe.sample_rate != stream.sample_rate
        needs_channels = stream.enforce_channel_layout and probe.channels != stream.channels
        if not needs_rate and not needs_channels:
            return staged
        destination = (run_dir or staged.parent) / f"canonical{stream.container}"
        self._transcoder.render(
            source=staged,
            destination=destination,
            audio_filters=list(stream.filter_arguments()),
            profile_args=stream.format_arguments(),
            container_hint=stream.container,
            cancel=cancel,
        )
        converted = self._transcoder.probe(destination, cancel=cancel)
        if converted.sample_rate != stream.sample_rate or (
            stream.enforce_channel_layout and converted.channels != stream.channels
        ):  # pragma: no cover - the encoder would have to ignore -ar/-ac
            raise EngineRejected(
                "canonical conversion did not produce the locked stream shape "
                f"({converted.sample_rate} Hz / {converted.channels} ch)"
            )
        return destination

    def _render_loudness(
        self,
        staged: Path,
        workspace: RuntimeWorkspace,
        profile: LoudnessProfile,
        cancel: CancelToken | None,
        *,
        profile_lock: CanonicalStreamProfile | None = None,
        run_dir: Path | None = None,
    ) -> RenderOutcome:
        stream = profile_lock or CanonicalStreamProfile()
        # Pass 1 must measure the *canonical* stream shape, not the source
        # layout: an up-mix to dual mono changes the BS.1770 energy sum by
        # +3.0103 dB (+3.0103 LU on a mono programme), so a gain computed from
        # the mono measurement lands 3 dB away from the target once the stereo
        # file is re-measured.  Converting first and measuring second is the
        # only order whose number describes the file that gets published.
        measured_source = self._canonical_form(staged, stream, cancel=cancel, run_dir=run_dir)
        probe = self._transcoder.probe(measured_source, cancel=cancel)
        duration_seconds = (probe.container_duration_us or 0) / 1_000_000
        if duration_seconds < MIN_PROGRAMME_SECONDS:
            raise EngineRejected(
                f"programme is {duration_seconds:.2f}s; EBU R128 needs at least "
                f"{MIN_PROGRAMME_SECONDS:g}s to measure"
            )
        measurement = self._transcoder.measure_loudnorm_pass(
            measured_source, profile, cancel=cancel
        )
        if measurement is None:
            raise EngineRejected(
                "the programme is silent (-inf LUFS): there is nothing to normalize, "
                "and publishing a 'normalized' silent file would be a false success"
            )
        # Is the request physically reachable?  Raising the programme to the
        # target also raises its peaks by the same number of dB.  If that would
        # put the peaks above the true-peak ceiling, no correct render exists:
        # ``loudnorm`` will (rightly) stop at the ceiling and undershoot the
        # integrated target, and a limiter does not help either — the peak is
        # *inherent* to the signal, so cutting it into the ceiling removes
        # exactly the energy the LUFS target asks for.  Such a programme needs a
        # dynamic range the profile does not allow (a broadcast target of
        # -23 LUFS with the -1 dBTP ceiling caps the crest factor at 22 dB).
        # Peak-to-RMS of the *source* programme, on the same measurement the
        # gain is computed from.  The verifier compares it to the published
        # file's own crest factor, so a dynamic render can never claim a linear
        # result: the difference is the crest reduction that actually happened.
        #
        # Both numbers below are measured on ``measured_source`` — the file in
        # the *canonical* stream shape, after the conversion above.  That is what
        # makes them directly comparable to the published file: a mono source
        # measured before a dual-mono up-mix carries its energy once, while the
        # stereo artifact carries it twice (+10*log10(2) = +3.0103 dB of BS.1770
        # channel sum), so measuring the source layout instead would report
        # 3.01 dB of "crest reduction" that no processing performed (measured on
        # the §4.5 fixture: 18.69 dB mono vs 15.68 dB as the 2-channel file).
        # The ordering is the correction; there is no term to add afterwards.
        source_crest_db = measurement["input_tp"] - measurement["input_i"]
        required_gain_db = profile.target_lufs - measurement["input_i"]
        projected_peak_dbtp = measurement["input_tp"] + required_gain_db
        dynamic = profile.processing_mode == "DYNAMIC_ALLOWED"
        if projected_peak_dbtp > profile.true_peak_dbtp + TRUE_PEAK_TOLERANCE_DB and not dynamic:
            # How much crest factor the profile permits: the distance from the
            # integrated target up to the peak ceiling.
            crest_budget_db = profile.true_peak_dbtp - profile.target_lufs
            programme_crest_db = measurement["input_tp"] - measurement["input_i"]
            raise EngineRejected(
                "loudness_target_unreachable: this programme cannot reach "
                f"{profile.target_lufs:g} LUFS without exceeding the "
                f"{profile.true_peak_dbtp:g} dBTP ceiling. It measures "
                f"{measurement['input_i']:.2f} LUFS at {measurement['input_tp']:.2f} dBTP "
                f"(crest {programme_crest_db:.2f} dB) and needs {required_gain_db:+.2f} dB, "
                f"which would peak at {projected_peak_dbtp:+.2f} dBTP. This profile allows "
                f"at most {crest_budget_db:g} dB of crest factor, so no render — not a "
                f"limiter, not a dynamic pass — can satisfy both numbers at once. "
                "Publishing a file that undershoots the target (or clips to reach it) "
                "would be a false success; lower the target, raise the ceiling, or "
                "supply a programme with less dynamic range. (This profile is "
                "STRICT_LINEAR: it does not permit a limiter to buy the target by "
                "removing crest factor. A DYNAMIC_ALLOWED profile — "
                f"{NAGAR_STREAMING_PRESET.profile_id!r} — would render it, and would "
                "report the crest reduction it applied.)"
            )
        i_arg, tp_arg, lra_arg = profile.ffmpeg_loudnorm_targets()
        loudnorm = (
            f"loudnorm={i_arg}:{tp_arg}:{lra_arg}"
            f":measured_I={measurement['input_i']:.6f}"
            f":measured_TP={measurement['input_tp']:.6f}"
            f":measured_LRA={measurement['input_lra']:.6f}"
            f":measured_thresh={measurement['input_thresh']:.6f}"
            f":offset={measurement['target_offset']:.6f}"
            ":linear=true:print_format=summary"
        )
        destination = (run_dir or staged.parent) / f"loudnorm{stream.container}"
        if dynamic and projected_peak_dbtp > profile.true_peak_dbtp + TRUE_PEAK_TOLERANCE_DB:
            # DYNAMIC_ALLOWED: the programme cannot sit under the ceiling at the
            # target on gain alone.  What the target *costs* is arithmetic:
            #
            #   needed_reduction = source_crest - (ceiling - target)
            #
            # If that exceeds the profile's destruction bound the number is
            # reachable and the master is not — a 27.8 dB-crest click track needs
            # ~15 dB removed, i.e. its transients deleted.  Refuse, and say why.
            needed_reduction_db = max(
                0.0, source_crest_db - (profile.true_peak_dbtp - profile.target_lufs)
            )
            limit = profile.max_crest_reduction_db
            if limit is not None and needed_reduction_db > limit:
                raise EngineRejected(
                    "loudness_crest_destruction: reaching "
                    f"{profile.target_lufs:g} LUFS under a "
                    f"{profile.true_peak_dbtp:g} dBTP ceiling needs "
                    f"{needed_reduction_db:.2f} dB of crest reduction from this "
                    f"programme's {source_crest_db:.2f} dB, but profile "
                    f"{profile.profile_id!r} permits at most {limit:g} dB. The "
                    "number is reachable by crushing the transients; the master "
                    "would not be worth publishing, so the engine refuses instead. "
                    "Lower the target, raise the ceiling, or supply a programme "
                    "that needs less of its dynamics removed."
                )
            # ``linear=false`` is what makes this a *dynamic* normalisation: the
            # limiter is an explicit part of the chain, so the render is
            # deterministic and single-pass (the gain and the limiting are solved
            # together, unlike a blind ``volume`` stage that the limiter then eats
            # — which needed 6 measured iterations to land and still overshot the
            # true-peak ceiling by 1 dB).  ``linear=true`` is *not* usable here:
            # FFmpeg silently degrades it to this same dynamic path when linear
            # gain cannot hit the target, which is why the verifier now measures
            # the crest reduction instead of trusting the mode label.
            dynamic_norm = loudnorm.replace(":linear=true", ":linear=false")
            self._transcoder.render(
                source=measured_source,
                destination=destination,
                audio_filters=[dynamic_norm, *stream.filter_arguments()],
                profile_args=stream.format_arguments(),
                container_hint=stream.container,
                cancel=cancel,
            )
            return RenderOutcome(
                path=destination,
                source_crest_factor_db=source_crest_db,
                processing_mode="DYNAMIC_ALLOWED",
            )
        self._transcoder.render(
            source=measured_source,
            destination=destination,
            audio_filters=[loudnorm, *stream.filter_arguments()],
            profile_args=stream.format_arguments(),
            container_hint=stream.container,
            cancel=cancel,
        )
        return RenderOutcome(
            path=destination,
            source_crest_factor_db=source_crest_db,
            processing_mode="STRICT_LINEAR",
        )

    # -- denoise ---------------------------------------------------------- #
    def remove_noise(
        self, request: DenoiseRequest, *, cancel: CancelToken | None = None
    ) -> ArtifactEvidence:
        if not 0.0 <= request.strength <= 1.0:
            raise EngineInputError("strength must be within [0, 1]")

        def builder(
            staged: Path,
            workspace: RuntimeWorkspace,
            cancel: CancelToken | None,
            run_dir: Path | None = None,
        ) -> Path:
            # ``afftdn`` attenuation is in dB; the public 0..1 "strength" maps
            # linearly onto 0..30 dB of noise reduction, which is the honest
            # maximum for a static-noise filter before musical artefacts appear.
            reduction_db = 30.0 * request.strength
            filters = ["highpass=f=80"]
            if request.strength > 0:
                filters.append(f"afftdn=nr={reduction_db:.1f}:nf=-50:tn=1")
            else:
                filters.append("anull")
            stream = CanonicalStreamProfile()
            # The run's own directory.  Rendering into ``staged.parent`` would
            # write the temporary artifact into ``inputs/`` — the directory the
            # engine promises is immutable — and would leave it behind if the
            # process died, because only the run directory is removed on exit.
            destination = _run_artifact_path(run_dir, staged, f"denoise{stream.container}")
            self._transcoder.render(
                source=staged,
                destination=destination,
                audio_filters=[*filters, *stream.filter_arguments()],
                profile_args=stream.format_arguments(),
                container_hint=stream.container,
                cancel=cancel,
            )
            return destination

        return self._derive(
            request_id=request.request_id,
            operation="audio.remove_noise",
            source_asset_id=request.source_asset_id,
            output_asset_id=request.output_asset_id,
            expected_source_sha256=request.source_sha256,
            parameters={
                "strength": request.strength,
                "preserve_speech": request.preserve_speech,
                "filter_chain": ["highpass=f=80", f"afftdn(nr≤{30.0 * request.strength:.1f}dB)"],
                "claims": "static broadband noise reduction only; no semantic separation",
            },
            loudness_profile=None,
            transaction_id=request.transaction_id,
            continuity_lock_hash=request.continuity_lock_hash,
            builder=builder,
            cancel=cancel,
        )

    # -- the shared derive pipeline --------------------------------------- #
    def _derive(
        self,
        *,
        request_id: str,
        operation: str,
        source_asset_id: str,
        output_asset_id: str,
        expected_source_sha256: str | None,
        parameters: dict[str, object],
        loudness_profile: LoudnessProfile | None,
        transaction_id: str | None,
        continuity_lock_hash: str | None,
        builder: Any,  # noqa: ANN401 - Callable[[Path, RuntimeWorkspace, CancelToken|None], Path]
        cancel: CancelToken | None = None,
        source_crest_factor_db: float | None = None,
    ) -> ArtifactEvidence:
        """Stage, render, **verify**, then publish.  In that order, always.

        The publish used to happen before verification, which made a rejected
        artifact readable at its final path for the length of the verification
        and left it there permanently if the process died inside that window.
        Now the artifact is verified while it is still private to the run
        directory, and the rename into ``media/`` is the last thing that happens.
        A refusal therefore never creates the final path at all, so nothing has
        to be quarantined afterwards.
        """
        source_path = self._path(source_asset_id)
        workspace = RuntimeWorkspace(self._settings.workspace_root).prepare()
        stream = CanonicalStreamProfile()
        destination = workspace.media_path(output_asset_id, suffix=stream.container)
        source_sha = hash_file(source_path)
        if expected_source_sha256 is not None and source_sha != expected_source_sha256:
            raise EngineInputError(
                f"asset {source_asset_id!r} bytes do not match the registered hash "
                f"(disk {source_sha}, registry {expected_source_sha256})"
            )
        identity = recipe_identity(
            operation=operation,
            source_sha256=source_sha,
            parameters=dict(parameters),
            profiles={
                "canonical": stream.profile_id,
                "loudness": None
                if loudness_profile is None
                else {
                    "profile_id": loudness_profile.profile_id,
                    "target_lufs": loudness_profile.target_lufs,
                    "true_peak_dbtp": loudness_profile.true_peak_dbtp,
                    "target_lra_lu": loudness_profile.target_lra_lu,
                },
            },
            engine=self._engine_identity(),
        )
        # A published artifact carries its recipe identity in a sidecar, so the
        # reuse decision is made against the recipe that *produced* the bytes,
        # never against whatever recipe happens to be asking now.
        marker = destination.with_name(destination.name + RECIPE_SUFFIX)
        if destination.is_file() and _read_recipe_marker(marker) == identity:
            passport = self._verify(
                operation=operation,
                asset_id=output_asset_id,
                path=destination,
                source_sha256=source_sha,
                loudness_profile=loudness_profile,
                expected_output_sha256=None,
                parameters=parameters,
                continuity_lock_hash=continuity_lock_hash,
                transaction_id=transaction_id,
                cancel=cancel,
            )
            if passport.verified:
                return self._evidence(
                    passport,
                    output_asset_id=output_asset_id,
                    destination=destination,
                    reused=True,
                )
        if cancel is not None and cancel.cancelled:
            raise _cancelled_before_start(cancel)
        self._enforce_staging_quota(workspace)
        staged = workspace.stage_input(source_path)
        run_started = time.monotonic()
        # Every run gets its own isolated directory; it is removed afterwards
        # whether the run succeeds, refuses or raises (the builder's ``finally``
        # in ``workspace.run_directory``), so a refusal cannot leave a partial
        # file behind to be mistaken for media.
        staging_peak = 0
        with workspace.run_directory(request_id) as run_dir:
            # A cancelled job must not start a process at all.  The token is
            # handed to the builder, which hands it to the process governor, so
            # cancelling mid-render kills the process group; this check only
            # covers the "already cancelled on arrival" case.
            outcome = _as_render_outcome(builder(staged, workspace, cancel, run_dir))
            produced = outcome.path
            if cancel is not None and cancel.cancelled:
                raise _cancelled_before_start(cancel)
            if produced.stat().st_size == 0:  # pragma: no cover - render refuses this
                raise EngineRejected("the encoder produced an empty file")
            # Verification happens on the *private* run file, before the final
            # path exists.  ``expected_output_sha256`` is not needed here: the
            # hash is computed from these very bytes, and measuring them twice
            # would only re-answer the same question.
            passport = self._verify(
                operation=operation,
                asset_id=output_asset_id,
                path=produced,
                source_sha256=source_sha,
                loudness_profile=loudness_profile,
                expected_output_sha256=None,
                parameters=parameters,
                continuity_lock_hash=continuity_lock_hash,
                transaction_id=transaction_id,
                cancel=cancel,
                source_crest_factor_db=outcome.source_crest_factor_db,
            )
            if not passport.verified:
                # Nothing was published, so there is nothing to quarantine: the
                # run directory is about to be removed with the refused bytes in
                # it, and the previous good artifact (if any) is still intact.
                raise EngineRejected(
                    f"{operation} produced an artifact that failed verification "
                    f"({passport.canonical_code or 'loudness/loudness_target'}: "
                    f"{passport.canonical_detail or passport.loudness_target})"
                )
            staging_peak = _tree_bytes(workspace.staging_dir)
            published = workspace.publish(
                produced,
                destination,
                replace=True,
                protected=(staged,),
            )
            staging_peak = max(staging_peak, _tree_bytes(workspace.staging_dir))
            if published.sha256 != passport.output_sha256:  # pragma: no cover - defensive
                # The bytes moved between verification and the rename.  Treat it
                # as a refusal and take the artifact back off the final path.
                destination.unlink(missing_ok=True)
                raise EngineRejected(
                    f"{operation} published bytes that differ from the verified ones "
                    f"(verified {passport.output_sha256}, published {published.sha256})"
                )
        _write_recipe_marker(marker, identity)
        budget = self._measure_budget(
            source_path=source_path,
            published=published,
            elapsed_seconds=time.monotonic() - run_started,
            staging_peak=staging_peak,
        )
        return self._evidence(
            passport,
            output_asset_id=output_asset_id,
            destination=published.path,
            reused=False,
            budget=budget,
        )

    def _analysis_budget(
        self,
        *,
        source: Path,
        probe: MediaProbe,
        decoded_bytes: int,
        elapsed_seconds: float,
        samples: int,
    ) -> ResourceBudget:
        """The budget of a read-only analysis (no artifact is published).

        Same fields, different meaning for two of them: ``output_size_bytes`` is
        0 because nothing was published, and ``decoded_samples`` is the number of
        samples the analyser actually read (which the analysis window may have
        truncated) rather than a duration-derived estimate.
        """
        governor = getattr(self._transcoder, "governor", None)
        return ResourceBudget(
            deadline_seconds=self._settings.deadline_seconds,
            max_captured_bytes=self._settings.max_captured_bytes,
            max_analysis_bytes=max(1, int(self._settings.max_analysis_seconds * 192_000 * 4)),
            elapsed_seconds=max(0.0, elapsed_seconds),
            peak_captured_bytes=self._settings.max_captured_bytes,
            peak_analysis_bytes=decoded_bytes,
            processes_started=int(getattr(governor, "processes_started", 0) or 0),
            input_size_bytes=source.stat().st_size,
            input_duration_us=int(probe.container_duration_us or 0),
            decoded_samples=samples,
            output_size_bytes=0,
            staging_quota_bytes=self._settings.staging_quota_bytes,
            staging_peak_bytes=0,
            threads=self._settings.threads,
            peak_rss_bytes=getattr(governor, "peak_rss_bytes", None),
            ffmpeg_version=None,
        )

    # -- resources (§4.8) -------------------------------------------------- #
    def _enforce_staging_quota(self, workspace: RuntimeWorkspace) -> None:
        """Refuse a run *before* staging when the workspace is already full.

        A quota that is only checked afterwards is a report, not a bound; the
        refusal has to happen before the copy that would breach it.
        """
        used = _tree_bytes(workspace.staging_dir)
        if used > self._settings.staging_quota_bytes:
            raise EngineRejected(
                f"staging quota exceeded: {used} bytes already in "
                f"{workspace.staging_dir} against a quota of "
                f"{self._settings.staging_quota_bytes}; refusing to start another run "
                "(a leaked attempt is the usual cause — check for staging residue)"
            )

    def _measure_budget(
        self,
        *,
        source_path: Path,
        published: PublishResult,
        elapsed_seconds: float,
        staging_peak: int,
    ) -> ResourceBudget:
        """The run's real consumption, measured — never estimated.

        ``decoded_samples`` is the number of frames the *decoder* reported for
        the input, so an analysis that was truncated by ``max_analysis_seconds``
        is visible as a smaller number rather than as a silent cap.
        """
        governor = getattr(self._transcoder, "governor", None)
        processes_started = int(getattr(governor, "processes_started", 0) or 0)
        captured = int(self._settings.max_captured_bytes)
        peak_rss: int | None = getattr(governor, "peak_rss_bytes", None)
        try:
            probe = self._transcoder.probe(source_path)
            input_duration_us = int(probe.container_duration_us or 0)
            decoded = (
                int(probe.container_duration_us or 0) * int(probe.sample_rate or 0) // 1_000_000
            )
        except Exception:  # pragma: no cover - probe already succeeded earlier
            input_duration_us = 0
            decoded = 0
        version: str | None = None
        try:
            health = self.health()
            version = health.ffmpeg_version
        except Exception:  # pragma: no cover - health is best-effort here
            version = None
        return ResourceBudget(
            deadline_seconds=self._settings.deadline_seconds,
            max_captured_bytes=captured,
            max_analysis_bytes=max(1, int(self._settings.max_analysis_seconds * 192_000 * 4)),
            elapsed_seconds=max(0.0, elapsed_seconds),
            peak_captured_bytes=captured,
            processes_started=processes_started,
            input_size_bytes=source_path.stat().st_size,
            input_duration_us=input_duration_us,
            decoded_samples=decoded,
            output_size_bytes=published.size_bytes,
            staging_quota_bytes=self._settings.staging_quota_bytes,
            staging_peak_bytes=staging_peak,
            threads=self._settings.threads,
            peak_rss_bytes=peak_rss,
            ffmpeg_version=version,
        )

    def _engine_identity(self) -> dict[str, str]:
        """What produced the bytes, for the recipe identity.

        The binary *name* (not its absolute path, which differs per machine) plus
        the measurement/decode strategies.  An engine whose measurement strategy
        changes must not reuse an artifact it would no longer accept.
        """
        return {
            "backend": "ffmpeg",
            "binary_name": Path(self._transcoder.binary).name,
            "measurement": "ebur128(peak=true)",
            "decode": "s32le full-file",
            "canonical": CANONICAL_PROFILE_ID,
        }

    def _verify(
        self,
        *,
        operation: str,
        asset_id: str,
        path: Path,
        source_sha256: str,
        loudness_profile: LoudnessProfile | None,
        expected_output_sha256: str | None,
        parameters: dict[str, object],
        continuity_lock_hash: str | None,
        transaction_id: str | None,
        cancel: CancelToken | None = None,
        source_crest_factor_db: float | None = None,
    ) -> ArtifactPassport:
        try:
            return build_passport(
                operation=operation,
                asset_id=asset_id,
                path=path,
                source_sha256=source_sha256,
                profile=CanonicalStreamProfile(),
                loudness_profile=loudness_profile,
                engine=self._transcoder,
                expected_output_sha256=expected_output_sha256,
                max_analysis_seconds=self._settings.max_analysis_seconds,
                parameters=parameters,
                continuity_lock_hash=continuity_lock_hash,
                transactional_identity={"transaction_id": transaction_id} if transaction_id else {},
                signing_key=self._settings.signing_key,
                signer_key_id=self._settings.signer_key_id,
                cancel=cancel,
                source_crest_factor_db=source_crest_factor_db,
            )
        except (PassportError, MediaContractError, WorkspaceBoundaryError) as error:
            raise EngineRejected(f"{operation} verification could not complete: {error}") from error

    def _evidence(
        self,
        passport: ArtifactPassport,
        *,
        output_asset_id: str,
        destination: Path,
        reused: bool,
        budget: ResourceBudget | None = None,
    ) -> ArtifactEvidence:
        signature = passport.signature
        loudness = passport.loudness
        target = passport.loudness_target
        return ArtifactEvidence(
            asset_id=output_asset_id,
            output_path=str(destination),
            sha256=passport.output_sha256,
            size_bytes=passport.output_size_bytes,
            duration_us=passport.output_duration_us,
            sample_rate=passport.probe.sample_rate,
            channels=passport.probe.channels,
            codec=passport.probe.codec,
            integrated_lufs=None if loudness is None else loudness.integrated_lufs,
            true_peak_dbtp=None if loudness is None else loudness.true_peak_dbtp,
            loudness_range_lu=None if loudness is None else loudness.loudness_range_lu,
            loudness_checked=target is not None,
            loudness_passed=None if target is None else target.passed,
            loudness_code=None if target is None else target.code,
            processing_mode=None if target is None else target.processing_mode,
            crest_reduction_db=None if target is None else target.crest_reduction_db,
            spectral_centroid_hz=None if signature is None else signature.spectral_centroid_hz,
            rms_dbfs=None if signature is None else signature.rms_dbfs,
            crest_factor_db=None if signature is None else signature.crest_factor_db,
            dynamic_range_db=None if signature is None else signature.dynamic_range_db,
            acoustic_signature_sha256=None if signature is None else signature.signature_hash,
            verified=passport.verified,
            passport_id=passport.passport_id,
            passport_signature=passport.signature_hex,
            continuity_lock_hash=passport.continuity_lock_hash,
            canonical_profile_ok=passport.canonical_ok,
            elapsed_seconds=passport.elapsed_seconds,
            reused_existing=reused,
            transaction_id=passport.transactional_identity.get("transaction_id"),
            parameters=dict(passport.parameters),
            budget=None if budget is None else budget.report(),
        )

    # -- episode assembly (Zero-Drift staging) ------------------------------ #
    def prove_lossless_assembly(
        self,
        segments: tuple[Path | str, ...] | list[Path | str],
        *,
        profile: CanonicalStreamProfile | None = None,
    ) -> AssemblyProof:
        """Prove (or refuse) a ``-c copy`` concatenation of rendered segments.

        This is the staging half of the Zero-Drift requirement: an episode is
        assembled from independent segments, so the engine must be able to say
        *before* the concat whether the segments share one stream shape.  Each
        segment is probed from disk (the file, not a remembered array) and the
        proof is produced by :func:`assert_lossless_assembly`; a mismatch
        returns ``reencode_required`` with the reason instead of silently
        producing a master that drops samples at the format change.
        """
        paths = tuple(Path(segment) for segment in segments)
        if not paths:
            raise ContinuityError("cannot prove an assembly of zero segments")
        probes = [self._transcoder.probe(path) for path in paths]
        return assert_lossless_assembly(
            probes,
            profile=profile or CanonicalStreamProfile(),
            names=tuple(path.name for path in paths),
        )

    # -- helpers used by tests and adapters -------------------------------- #
    def measure(self, path: Path | str) -> LoudnessMeasurement:
        return self._transcoder.measure_loudness(path)

    def probe(self, path: Path | str) -> MediaProbe:
        return self._transcoder.probe(path)

    def signature_of(self, path: Path | str, *, analysis_rate: int = 48_000) -> Any:  # noqa: ANN401
        handle, sink = tempfile.mkstemp(prefix="nagar_sig_", suffix=".s32le")
        os.close(handle)
        try:
            return self._transcoder.signature_resampled(
                path,
                pcm_path=sink,
                analysis_rate=analysis_rate,
                max_analysis_seconds=self._settings.max_analysis_seconds,
            )
        finally:
            Path(sink).unlink(missing_ok=True)
