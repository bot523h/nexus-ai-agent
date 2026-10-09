FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        ffmpeg \
        fonts-liberation \
        git \
        libgl1 \
        libmagic1 \
        python3.14 \
        python3.14-venv \
        python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.14 /usr/local/bin/python \
    && python -m venv /opt/venv \
    && python --version

COPY pyproject.toml README.md LICENSE VERSION CHANGELOG.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

RUN python -m pip install --no-cache-dir --no-compile . \
    && python -c "import nexus_ai_agent; print('Nexus import OK')"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin nexus \
    && mkdir -p data/chroma data/cache assets/fonts \
    && chown -R nexus:nexus /app

USER nexus

EXPOSE 8000

CMD ["sh", "-c", "python -m nexus_ai_agent.cli run-bot --mode ${NEXUS_RUN_MODE:-polling}"]
