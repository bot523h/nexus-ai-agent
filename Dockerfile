FROM python:3.12-slim

WORKDIR /app

# Install system dependencies.
# `ffmpeg` is the single declared external binary of the slideshow pack
# (ROADMAP_STATUS.md, "Dependencies"): the shipped /slideshow surface (Wave 2.5)
# and `nexus slideshow render` resolve the encoder as NEXUS_FFMPEG_BIN -> PATH
# -> imageio-ffmpeg wheel, and the imageio-ffmpeg fallback is a [dev] extra
# that this core-only image does not install — without this package every
# encode fails with FfmpegUnavailableError (task-163).
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    libmagic1 \
    libgl1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy assets and code
COPY . .

# Install dependencies
RUN pip install --no-cache-dir .

# Create necessary directories
RUN mkdir -p data/chroma data/cache assets/fonts

# Drop root.
#
# The image ran as uid 0 with a process that (a) terminates untrusted input
# from any Telegram user, (b) shells out to ffmpeg — a large C codebase with a
# long CVE history — on attacker-supplied media, and (c) can self-update via
# `git pull` + `pip install .`. A single container escape or an ffmpeg parsing
# bug therefore started from root rather than from an unprivileged account.
#
# `nexus` owns /app so the writable runtime paths created above (data/chroma,
# data/cache) and NEXUS_CREATIVE_TEMP_DIR stay writable after the switch. The
# chown is a separate layer on purpose: it runs after every COPY/RUN that
# writes into /app.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin nexus \
    && chown -R nexus:nexus /app
USER nexus

# Creative scratch space lives under /app, not /tmp: in this image /tmp is
# world-writable and shared with every other process in the container, so a
# predictable path there is a symlink-swap target. See config/settings.py.
ENV NEXUS_CREATIVE_TEMP_DIR=/app/data/creative_tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Default command runs the bot.
# NEXUS_RUN_MODE selects the run mode (see bot/webhook.py); "polling" is the
# default so existing always-on (worker-type) deployments keep working
# unchanged. Set NEXUS_RUN_MODE=webhook for scale-to-zero web deployments.
CMD ["sh", "-c", "python -m nexus_ai_agent.cli run-bot --mode ${NEXUS_RUN_MODE:-polling}"]
