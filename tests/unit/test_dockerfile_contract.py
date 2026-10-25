"""The production Dockerfile must ship the FFmpeg encoder (task-163).

``/slideshow`` (Wave 2.5, v3.12.0) and ``nexus slideshow render`` are released
features, and both end in one FFmpeg encode.  The encoder is resolved by
``resolve_ffmpeg_bin`` (``creative/slideshow/ffmpeg.py``) as
``NEXUS_FFMPEG_BIN`` -> ``PATH`` -> ``imageio-ffmpeg`` wheel.  The Dockerfile is
the canonical production packaging (``docs/ops/DEPLOY_RUNBOOK.md``: "Koyeb
builds from Dockerfile") and installs the core distribution only — the
``imageio-ffmpeg`` fallback is a ``[dev]`` extra, absent from the image.  So
the only in-container source of the binary is ``PATH``, i.e. an apt package.

Before this guard the Dockerfile (last updated v3.8.0, before the slideshow
shipped) installed no FFmpeg, so every encode in the deployed container failed
with ``FfmpegUnavailableError``.  CI never saw the gap: the test job installs
``.[dev]`` and therefore carries the ``imageio-ffmpeg`` wheel.

This guard is a text ratchet, in the style of ``test_ci_lint_parity.py``: no
YAML/Docker parser is imported, the check is a pure function, and the red path
is proven on a fixture.  It cannot prove the image builds (no Docker daemon in
most sandboxes); it proves the explicit install line is never silently dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"

# A bare package token: optional :release, optional =version pin.  The
# lookarounds keep binary paths (/usr/bin/ffmpeg) and longer names
# (libavffmpeg, ffmpeg-tools) from matching.
_FFMPEG_TOKEN = re.compile(r"(?<![\w/.])ffmpeg(?::[A-Za-z0-9.+-]+)?(=[\w.+~-]+)?(?![\w-])")


def _apt_install_blocks(dockerfile_text: str) -> list[str]:
    """One block per ``apt-get install`` invocation.

    A block is the invocation line plus every following line while the
    previous line ends with a backslash (shell continuation) — the shape
    this Dockerfile uses for its package list.
    """
    lines = dockerfile_text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if re.search(r"\bapt-get\s+install\b", lines[i]):
            block = [lines[i]]
            j = i
            while lines[j].rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
                block.append(lines[j])
            blocks.append("\n".join(block))
            i = j + 1
        else:
            i += 1
    return blocks


def dockerfile_installs_ffmpeg(dockerfile_text: str) -> bool:
    """True when some apt-get install block lists the ``ffmpeg`` package.

    The token must stand on its own as a package name (``ffmpeg`` or a
    ``ffmpeg=version`` pin) — a binary path such as ``/usr/bin/ffmpeg`` or a
    different package name does not count as the package being installed.
    """
    for block in _apt_install_blocks(dockerfile_text):
        for line in block.splitlines():
            stripped = line.strip().rstrip("\\").strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _FFMPEG_TOKEN.search(stripped):
                return True
    return False


def test_repository_dockerfile_installs_ffmpeg() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert _apt_install_blocks(text), "Dockerfile has no apt-get install block to check"
    assert dockerfile_installs_ffmpeg(text), (
        "the production Dockerfile must install the `ffmpeg` apt package: the "
        "shipped /slideshow surface and `nexus slideshow render` need an FFmpeg "
        "binary at encode time, and the imageio-ffmpeg wheel fallback is a [dev] "
        "extra this core-only image does not install (task-163)"
    )


def test_missing_ffmpeg_package_fails_the_contract() -> None:
    """Red-proof: the pre-fix Dockerfile shape must be rejected."""
    fixture = """FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y \\
    build-essential \\
    libmagic1 \\
    libgl1 \\
    fonts-liberation \\
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN pip install --no-cache-dir .
"""
    assert not dockerfile_installs_ffmpeg(fixture)


def test_version_pinned_ffmpeg_passes_the_contract() -> None:
    fixture = """FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg=7:6.1.1-3
"""
    assert dockerfile_installs_ffmpeg(fixture)


def test_ffmpeg_binary_path_is_not_a_package_install() -> None:
    """Referencing a binary path is not the same as installing the package."""
    fixture = """FROM python:3.12-slim
RUN apt-get update && apt-get install -y libavcodec60
COPY ffmpeg-bin /usr/local/bin/ffmpeg
"""
    assert not dockerfile_installs_ffmpeg(fixture)
