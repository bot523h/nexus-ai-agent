"""Canonical integration bridge for the composition layer.

The composition layer must not become a second authority. This module is the
single seam through which the layer touches the canonical NEXUS substrate:

* **Live registry validation** -- :func:`live_operation_ids` and
  :func:`live_registry_fingerprint` read the *live* registry the runtime
  actually ships (:func:`nexus_ai_agent.creative.packs.runtime.build_runtime_registry`),
  never a private copy. A capability that claims a ``canonical_operation`` the
  live registry does not provide is refused
  (:func:`validate_capability`), so the composition layer can never authorize
  an operation the authoritative registry does not offer.

* **Frozen adapter allow-list** -- :data:`BUILTIN_ADAPTERS` names the worker
  backends the composition layer is allowed to bind. A capability whose
  ``runtime_adapter_id`` is outside the list is refused. This is the layer's
  equivalent of the codebase's binary allow-list: registration is declarative,
  but *execution* is restricted to reviewed backends.

* **Canonical tool resolution** -- :func:`resolve_ffmpeg` wraps the codebase's
  single FFmpeg resolver (:func:`nexus_ai_agent.creative.slideshow.ffmpeg.resolve_ffmpeg_bin`),
  so the composition workers spawn the *allow-listed* binary rather than a bare
  ``"ffmpeg"`` from ``PATH``. :func:`probe_media` and :func:`sha256_of` reuse the
  same canonical primitives for measurement.

Trust model: a ``CapabilitySpec`` with an unrecognised ``runtime_adapter_id`` or
an unbacked ``canonical_operation`` is a *contract error* (refused at build
time); a ``trust_level`` of ``untrusted`` is refused at runtime unless the
policy explicitly opts in.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .spec import CapabilitySpec

#: Worker backends the composition layer is permitted to bind. Adding an entry
#: here is the reviewed operator act that widens execution; a capability naming
#: any other adapter is refused at registry-build time.
BUILTIN_ADAPTERS: frozenset[str] = frozenset({"imagemagick", "ffmpeg"})


class CanonicalRegistryError(ValueError):
    """A capability is not backed by the canonical substrate."""


# ---------------------------------------------------------------------------
# Live NEXUS registry (never a private copy)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _live_operation_ids() -> frozenset[str]:
    """The operation ids the live NEXUS registry actually provides.

    Read once from :func:`build_runtime_registry` and cached: the registry is
    deterministic for a given install, so caching is safe and keeps the
    composition layer's import cheap.
    """
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    registry = build_runtime_registry()
    return frozenset(registry.list_operations())


def live_operation_ids() -> frozenset[str]:
    """Public accessor for the live registry's operation ids."""
    return _live_operation_ids()


def live_registry_fingerprint() -> str:
    """A stable fingerprint of the live registry's operation-id set.

    Recorded in the derived spec and the proof-carrying artifact so evidence
    names *which* registry produced it.
    """
    ids = sorted(live_operation_ids())
    digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Capability validation
# ---------------------------------------------------------------------------


def validate_capability(spec: CapabilitySpec) -> None:
    """Refuse a capability that is not backed by the canonical substrate.

    Checks, in order (first fault raises):

    1. ``runtime_adapter_id`` must be in :data:`BUILTIN_ADAPTERS` (unless the
       capability is a pure composition with no adapter -- see below).
    2. ``canonical_operation`` (when set) must exist in the live registry.
    3. ``dependencies`` must name non-blank ids.
    """
    adapter = spec.runtime_adapter_id
    if adapter != "unbound" and adapter not in BUILTIN_ADAPTERS:
        raise CanonicalRegistryError(
            f"capability {spec.capability_id!r}: runtime_adapter_id {adapter!r} is "
            f"not in the composition adapter allow-list {sorted(BUILTIN_ADAPTERS)}"
        )
    if spec.canonical_operation is not None:
        if spec.canonical_operation not in live_operation_ids():
            raise CanonicalRegistryError(
                f"capability {spec.capability_id!r} claims canonical operation "
                f"{spec.canonical_operation!r} which the live NEXUS registry does "
                "not provide"
            )
    for dep in spec.dependencies:
        if not dep.strip():
            raise CanonicalRegistryError(
                f"capability {spec.capability_id!r}: blank dependency id"
            )


@dataclass(frozen=True)
class RegistrySnapshot:
    """A validated, fingerprinted view of the composition capability set.

    This is what the runtime is allowed to execute against: every capability in
    it has passed :func:`validate_capability`, and it records the live registry
    fingerprint so evidence can name the substrate.
    """

    capabilities: dict[str, CapabilitySpec]
    live_fingerprint: str
    operation_ids: frozenset[str]

    @property
    def capability_ids(self) -> frozenset[str]:
        return frozenset(self.capabilities)

    def to_dict(self) -> dict[str, object]:
        return {
            "live_registry_fingerprint": self.live_fingerprint,
            "live_operation_count": len(self.operation_ids),
            "capability_ids": sorted(self.capabilities),
        }


def build_snapshot(capabilities: dict[str, CapabilitySpec]) -> RegistrySnapshot:
    """Validate every capability and return a fingerprinted snapshot.

    Raises :class:`CanonicalRegistryError` on the first capability that is not
    backed by the canonical substrate. ``untrusted`` capabilities are *not*
    refused here -- that is a runtime policy decision -- but they must still
    name a valid adapter.
    """
    if not capabilities:
        raise CanonicalRegistryError("cannot build an empty composition registry")
    for spec in capabilities.values():
        validate_capability(spec)
    return RegistrySnapshot(
        capabilities=dict(capabilities),
        live_fingerprint=live_registry_fingerprint(),
        operation_ids=live_operation_ids(),
    )


# ---------------------------------------------------------------------------
# Canonical tool resolution (single allow-listed binary)
# ---------------------------------------------------------------------------


def resolve_ffmpeg(explicit: str | None = None) -> str:
    """Resolve the allow-listed FFmpeg binary (override / env / PATH / wheel).

    Delegates to the codebase's single resolver so the composition layer shares
    one binary policy with the render lane.
    """
    from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin

    return resolve_ffmpeg_bin(explicit)


def sha256_of(path: str) -> str:
    """The codebase's canonical file digest (``sha256:<hex>``)."""
    from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file
    from pathlib import Path

    return sha256_file(Path(path))


@dataclass(frozen=True)
class MediaProbe:
    duration_us: int
    width: int | None
    height: int | None
    has_audio: bool


def probe_media(path: str, *, binary: str | None = None) -> MediaProbe:
    """Decode-measure a produced media file with the allow-listed binary.

    Uses the codebase's ``probe_video`` (which reads the stream table back out
    of ``ffmpeg -i``) so measurement shares the render lane's primitives.
    """
    from nexus_ai_agent.creative.slideshow.ffmpeg import probe_video

    resolved = binary or resolve_ffmpeg()
    info = probe_video(Path(path), binary=resolved)
    return MediaProbe(
        duration_us=info.duration_us,
        width=info.width,
        height=info.height,
        has_audio=info.has_audio,
    )


# ---------------------------------------------------------------------------
# Independent decode primitives (used by the semantic verifier)
# ---------------------------------------------------------------------------

#: ``ffmpeg -i`` prints the stream table on stderr; the duration line looks
#: like ``Duration: 00:00:02.00``.
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
#: ``Video: h264 ... 1280x720`` in the same table.
_DIMENSIONS_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
#: Coarse codec token right after ``Video:`` (e.g. ``h264``, ``png``).
_VIDEO_CODEC_RE = re.compile(r"Video:\s*([A-Za-z0-9_]+)")
#: The number of decoded frames ``ffmpeg -f null -`` reports.
_FRAME_RE = re.compile(r"frame=\s*(\d+)")


def probe_raw(path: str, *, binary: str | None = None, timeout: int = 120) -> str:
    """Raw ``ffmpeg -i`` stream table (allow-listed binary, argv-only).

    The semantic verifier parses this itself rather than trusting the worker's
    own summary, so the measurement is independent of the encode path.
    """
    resolved = binary or resolve_ffmpeg()
    proc = subprocess.run(  # noqa: S603 - argv list, shell=False, allow-listed binary
        [resolved, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return f"{proc.stderr or ''}\n{proc.stdout or ''}"


def parse_probe(raw: str) -> dict[str, object]:
    """Decode-measure a raw stream table into structural facts."""
    facts: dict[str, object] = {}
    duration = _DURATION_RE.search(raw)
    if duration is not None:
        hours, minutes, seconds = duration.groups()
        facts["duration_us"] = int(
            round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1_000_000)
        )
    dims = _DIMENSIONS_RE.search(raw)
    if dims is not None:
        facts["width"] = int(dims.group(1))
        facts["height"] = int(dims.group(2))
    codec = _VIDEO_CODEC_RE.search(raw)
    if codec is not None:
        facts["codec"] = codec.group(1).lower()
    return facts


def decode_video_frames(path: str, *, binary: str | None = None, timeout: int = 180) -> tuple[int, str]:
    """Independently decode every video frame; return ``(frame_count, errors)``.

    ``ffmpeg -map 0:v:0 -f null -`` decodes the elementary video stream to the
    null muxer; a non-zero exit or a decoder error means the file does not
    actually decode even if its bytes hash stably. ``frame_count`` is ``-1``
    when the count could not be read.
    """
    resolved = binary or resolve_ffmpeg()
    proc = subprocess.run(  # noqa: S603 - argv list, shell=False, allow-listed binary
        [resolved, "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    text = proc.stderr or ""
    # FFmpeg writes the progress line beginning ``frame=    0`` and then
    # overwrites it with ``\r`` as decoding proceeds; capture-output keeps every
    # intermediate line, so the *last* (i.e. largest) count is the true one.
    matches = _FRAME_RE.findall(text)
    count = max((int(m) for m in matches), default=-1)
    errors = "" if proc.returncode == 0 else text.strip()[-400:]
    return count, errors


__all__ = [
    "BUILTIN_ADAPTERS",
    "CanonicalRegistryError",
    "MediaProbe",
    "RegistrySnapshot",
    "build_snapshot",
    "decode_video_frames",
    "live_operation_ids",
    "live_registry_fingerprint",
    "parse_probe",
    "probe_media",
    "probe_raw",
    "resolve_ffmpeg",
    "sha256_of",
    "validate_capability",
]
