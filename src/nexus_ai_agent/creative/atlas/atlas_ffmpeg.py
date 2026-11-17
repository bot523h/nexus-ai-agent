"""FFmpeg binary discovery for the ATLAS render/verify plane.

The ATLAS operators encode and re-decode real media; a machine that cannot find
FFmpeg must fail *closed* (report the media as blocked) rather than fabricate a
file. This module only resolves the binary paths, in the same explicit order the
rest of the project already uses (``creative/slideshow/ffmpeg.py``):

1. ``NEXUS_FFMPEG_BIN`` (an operator override, e.g. a managed static build);
2. ``PATH`` (the production image installs FFmpeg properly);
3. the ``imageio-ffmpeg`` wheel (a dev/test convenience, already declared as a
   dev dependency and allow-listed for this exact purpose in ``pyproject.toml``).

``ffprobe`` has no wheel fallback: the ``imageio-ffmpeg`` wheel ships ``ffmpeg``
only, so :func:`ffprobe_binary` reports ``None`` when the tool is absent. The
verifier derives frame counts from a raw ``ffmpeg`` decode, so it never depends
on ``ffprobe`` being present.

This module performs no I/O beyond ``shutil.which`` and importing the wheel: it
never executes the resolved binary.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache

__all__ = ["ffmpeg_binary", "ffprobe_binary", "clear_cache"]


@lru_cache(maxsize=1)
def _bundled_ffmpeg() -> str | None:
    """The ``imageio-ffmpeg`` wheel's static build, or ``None`` when absent."""
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]
    except Exception:  # pragma: no cover - depends on the environment
        return None
    try:
        path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - depends on the environment
        return None
    return path or None


def ffmpeg_binary() -> str | None:
    """Resolve the ``ffmpeg`` executable, or ``None`` when none is available."""
    override = os.environ.get("NEXUS_FFMPEG_BIN")
    if override:
        return override if os.path.isfile(override) else None
    found = shutil.which("ffmpeg")
    if found:
        return found
    return _bundled_ffmpeg()


def ffprobe_binary() -> str | None:
    """Resolve the ``ffprobe`` executable from ``PATH`` (no wheel fallback)."""
    return shutil.which("ffprobe")


def clear_cache() -> None:
    """Drop the cached wheel probe (test seam for environment changes)."""
    _bundled_ffmpeg.cache_clear()
