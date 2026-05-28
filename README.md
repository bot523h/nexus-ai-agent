# nexus-ai-agent

NEXUS AI Agent is an offline-first, mobile-optimized AI orchestration runtime with a Telegram bot interface.
It is **not** “just a chatbot”: it provides routing, planning, tool execution gates, and memory primitives built
around **LangGraph** and local LLM backends.

## Architecture (high-level)

```
Telegram
  |
  v
python-telegram-bot handlers (auth + rate limiting)
  |
  v
LangGraph StateGraph (router -> agent -> memory)
  |                 |               |
  |                 |               +--> Long-term memory (sqlite-vec) + short-term summarization
  |                 |
  |                 +--> Tools subsystem (sandboxed file tools + guarded shell tool)
  |
  +--> LLMProvider abstraction
        |--> llama.cpp local GGUF (LocalLlamaCppProvider)
        +--> FakeLLMProvider (tests / missing model)

Storage:
  - SQLModel async SQLite (WAL enabled) for users/chats/messages/tasks/tool runs
  - LangGraph checkpointing in SQLite
```

## Quick start

### 1) Clone + install

```bash
git clone https://github.com/bot523h/nexus-ai-agent.git
cd nexus-ai-agent
make setup
```

### 2) Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set:

- `TELEGRAM_BOT_TOKEN=...`

### 3) Initialize DB

```bash
make migrate
```

### 4) Run bot (polling)

```bash
make run
```

## Downloading a local model (GGUF)

This project expects a GGUF model file path via `NEXUS_MODEL_PATH` (default: `models/model.gguf`).
You can download GGUF models from Hugging Face:

- https://huggingface.co/models?search=gguf

If no model is present, the runtime falls back to `FakeLLMProvider` (useful for smoke tests and CI).

## CLI commands

```bash
python -m nexus_ai_agent.cli migrate
python -m nexus_ai_agent.cli smoke --input "Hello NEXUS"
python -m nexus_ai_agent.cli run-bot --mode polling
```

## Testing

```bash
make test
make lint
make types
```
