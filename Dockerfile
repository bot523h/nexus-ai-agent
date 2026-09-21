# syntax=docker/dockerfile:1
#
# Multi-stage image with two runtime targets (task-107):
#
#   docker build -t nexus-slim .                    → core only (DEFAULT target)
#   docker build --target full -t nexus-full .      → every capability extra
#   docker build --build-arg NEXUS_EXTRAS=rag,media → exactly the extras you need
#
# Dependency resolution runs in a builder stage with uv (free, Rust-based);
# nothing is compiled in the runtime stages. Measured core install: 520 MB of
# site-packages versus 6.5 GB before the extras split (no torch / ChromaDB /
# llama.cpp in the default image). See README.md → "Install & extras".

ARG PYTHON_VERSION=3.12
# Comma-separated extras for the `slim` target; empty means core only.
ARG NEXUS_EXTRAS=

# ── builder: resolve and install the core dependencies into a prefix ─────────
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir --upgrade pip uv

WORKDIR /app

# Only the metadata + package source is needed to install; copying the rest of
# the repo later keeps this layer cached across documentation/test changes.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

ARG NEXUS_EXTRAS
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "$NEXUS_EXTRAS" ]; then SPEC=".[$NEXUS_EXTRAS]"; else SPEC="."; fi \
    && echo "installing $SPEC" \
    && uv pip install --prefix=/install "$SPEC"

# ── builder-full: the same, plus every capability extra ──────────────────────
FROM builder AS builder-full

# llama-cpp-python needs a C++ toolchain; it is installed here and only here, so
# the compiler never reaches a runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --prefix=/install ".[rag,local-llm,speech,r2,pdf,postgres,otio,media]"

# ── full: every extra ────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS full

WORKDIR /app

# ffmpeg: the render lane resolves NEXUS_FFMPEG_BIN → PATH → the imageio-ffmpeg
# wheel, so shipping the real binary keeps /slideshow and the Nagar lane working.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder-full /install /usr/local
COPY . .

RUN mkdir -p data/chroma data/cache assets/fonts

EXPOSE 8000

# NEXUS_RUN_MODE selects the run mode (see bot/webhook.py); "polling" is the
# default so existing always-on (worker-type) deployments keep working unchanged.
CMD ["sh", "-c", "python -m nexus_ai_agent.cli run-bot --mode ${NEXUS_RUN_MODE:-polling}"]

# ── slim: core only (DEFAULT target) ─────────────────────────────────────────
# No apt packages: the code needs no libgl (no OpenCV), no libmagic (unused) and
# no system fonts (assets/fonts/Vazirmatn.ttf ships in the repo).
FROM python:${PYTHON_VERSION}-slim AS slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

RUN mkdir -p data/cache assets/fonts

EXPOSE 8000

CMD ["sh", "-c", "python -m nexus_ai_agent.cli run-bot --mode ${NEXUS_RUN_MODE:-polling}"]
