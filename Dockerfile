FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m nexus_ai_agent.cli migrate
CMD ["python", "-m", "nexus_ai_agent.cli", "run-bot", "--mode", "polling"]
