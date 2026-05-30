FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install --no-cache-dir .

# Initialize database
RUN export PYTHONPATH=$PYTHONPATH:$(pwd)/src && python -m nexus_ai_agent.cli migrate

EXPOSE 8000

CMD ["python", "-m", "nexus_ai_agent.cli", "run-bot", "--mode", "polling"]
