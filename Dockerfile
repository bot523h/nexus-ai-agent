FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libmagic1 \
    libgl1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy assets and code
COPY . .

# Install dependencies (+ the optional [pdf] extra: PDF uploads work out of the box)
RUN pip install --no-cache-dir ".[pdf]"

# Create necessary directories
RUN mkdir -p data/chroma data/cache assets/fonts

# Initialize database (optional during build, better at runtime)
# RUN export PYTHONPATH=$PYTHONPATH:$(pwd)/src && python -m nexus_ai_agent.cli migrate

EXPOSE 8000

# Default command runs the bot.
# NEXUS_RUN_MODE selects the run mode (see bot/webhook.py); "polling" is the
# default so existing always-on (worker-type) deployments keep working
# unchanged. Set NEXUS_RUN_MODE=webhook for scale-to-zero web deployments.
CMD ["sh", "-c", "python -m nexus_ai_agent.cli run-bot --mode ${NEXUS_RUN_MODE:-polling}"]
