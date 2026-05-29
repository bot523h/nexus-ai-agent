# ── NEXUS AI Agent v3.0.0 — Multi-stage Docker Image ──
# Lightweight production image for cloud deployment (Koyeb, Fly.io, Railway)

# ── Stage 1: Build ──
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc git && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY VERSION ./

RUN pip install --no-cache-dir -e .

# ── Stage 2: Runtime ──
FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg sqlite3 && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --from=builder /app /app

# Create data directory
RUN mkdir -p /app/data

# Non-root user for security
RUN useradd -m -d /app nexus
RUN chown -R nexus:nexus /app
USER nexus

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('https://api.telegram.org/bot' + __import__('os').environ['TELEGRAM_BOT_TOKEN'] + '/getMe'); assert r.status_code == 200" || exit 1

# Default environment
ENV PYTHONUNBUFFERED=1
ENV NEXUS_DB_PATH=/app/data/app.sqlite
ENV NEXUS_CHECKPOINT_PATH=/app/data/langgraph.sqlite
ENV NEXUS_VECTOR_PATH=/app/data/vector.sqlite
ENV NEXUS_CACHE_DIR=/app/data/cache
ENV NEXUS_LOG_LEVEL=INFO

ENTRYPOINT ["python", "-m", "nexus_ai_agent.bot.main"]
