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

# Install dependencies
RUN pip install --no-cache-dir .

# Create necessary directories
RUN mkdir -p data/chroma data/cache assets/fonts

# Initialize database (optional during build, better at runtime)
# RUN export PYTHONPATH=$PYTHONPATH:$(pwd)/src && python -m nexus_ai_agent.cli migrate

EXPOSE 8000

# Default command runs the bot
CMD ["python", "-m", "nexus_ai_agent.cli", "run-bot", "--mode", "polling"]
