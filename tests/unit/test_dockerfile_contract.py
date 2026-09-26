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


# ── the container must not run as root (owner audit, v3.13.0) ───────────────


def _last_user_directive(dockerfile_text: str) -> str | None:
    """The effective USER for the CMD: the last USER instruction in the file."""
    found: str | None = None
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = re.match(r"USER\s+(\S+)", stripped, re.IGNORECASE)
        if match:
            found = match.group(1)
    return found


def test_the_image_does_not_run_as_root() -> None:
    """A Telegram bot that shells out to ffmpeg must not be uid 0.

    The process terminates untrusted input from any user and hands
    attacker-supplied media to ffmpeg, a large C codebase with a long CVE
    history. Running that as root means a parsing bug starts from the most
    privileged account in the container instead of an unprivileged one.
    """
    user = _last_user_directive(DOCKERFILE.read_text(encoding="utf-8"))
    assert user is not None, "Dockerfile never drops root: no USER instruction"
    assert user not in {"root", "0"}, f"Dockerfile runs as {user!r}"


def test_the_runtime_user_is_created_before_it_is_selected() -> None:
    """`USER nexus` against a non-existent account fails at container start."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    user = _last_user_directive(text)
    assert user is not None
    assert re.search(rf"useradd[^\n]*\b{re.escape(user)}\b", text), (
        f"USER {user} is selected but never created with useradd"
    )
    assert text.index("useradd") < text.rindex(f"USER {user}")


def test_app_is_owned_by_the_runtime_user() -> None:
    """Dropping root without chown leaves the writable paths unwritable."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    user = _last_user_directive(text)
    assert user is not None
    assert re.search(rf"chown\s+-R\s+{re.escape(user)}", text), (
        "the runtime user must own /app or data/ and cache writes fail at runtime"
    )


def test_creative_scratch_is_not_under_shared_tmp() -> None:
    """A predictable path in a world-writable /tmp is a symlink-swap target."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"NEXUS_CREATIVE_TEMP_DIR=(\S+)", text)
    assert match, "the image must pin NEXUS_CREATIVE_TEMP_DIR"
    assert not match.group(1).startswith("/tmp"), match.group(1)


def test_a_rootful_dockerfile_fails_the_contract() -> None:
    """Red-proof: the pre-fix shape must be rejected."""
    fixture = """FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
CMD ["python", "-m", "nexus_ai_agent.cli", "run-bot"]
"""
    assert _last_user_directive(fixture) is None


def test_an_explicit_root_user_fails_the_contract() -> None:
    fixture = """FROM python:3.12-slim
USER nexus
USER root
"""
    assert _last_user_directive(fixture) == "root"
