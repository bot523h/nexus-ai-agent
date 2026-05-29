# NEXUS AI Agent

**AI Community Operating System** — an offline-first, mobile-optimized AI orchestration runtime with a Telegram bot interface.

NEXUS AI is not just a chatbot: it provides routing, planning, tool execution gates, memory primitives, community management, gamification, analytics, and content automation — all built around **LangGraph** and local LLM backends.

> **Current version: v1.3.0** — AI Community Operating System

---

## Features

### Core (v1.0–v1.2)
- 💬 **AI Chat** — Multi-persona conversations with auto-routing (Qwen, Gemma, Phi)
- 👤 **Anonymous Chat** — Random queue-based pairing with report system
- 🎮 **Games** — Quiz, number guessing, Persian Wordle, quick polls
- 🛠️ **Tools** — Reminders, translation, unit conversion, calculator
- 📢 **Channel Management** — Post, schedule, ban, welcome, pin, stats
- 📋 **Inline Menu System** — Full interactive keyboard navigation

### Community OS (v1.3.0)
- 👑 **Owner Control** — Admin dashboard, broadcast, system status, admin logs
- 📢 **Force Join** — Channel membership verification with cached checks and anti-bypass
- 🎭 **AI Personalities** — 10 distinct personalities with per-group config and persistence
- 💬 **Auto Engagement** — Ice breakers, jokes, challenges, daily questions, events with rate limiting
- 🔥 **Viral Engine** — Auto viral post generation, scoring, hashtags, scheduling, duplicate prevention
- 📢 **Ad System** — Scheduled ads with repeat intervals, campaigns, pause/resume/delete lifecycle
- 🛡️ **Smart Moderation** — Anti-spam, flood, link filter, Persian profanity filter, warnings, reputation
- 🏆 **Gamification** — XP, 16 levels with Persian titles, daily rewards, streaks, 8 achievements, leaderboard
- 📊 **Analytics** — Active users, engagement rate, peak hours, cohort retention, command usage, dashboard
- 🎨 **Advanced UI** — 6-row main menu, nested submenus, admin dashboard panel

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       Telegram Bot API                       │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│              python-telegram-bot Handlers                     │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │  Auth   │ │  Rate    │ │  Force   │ │  Moderation     │ │
│  │Middleware│ │ Limiter  │ │  Join    │ │  Pipeline       │ │
│  └─────────┘ └──────────┘ └──────────┘ └──────────────────┘ │
└──────────────────────────┬───────────────────────────────────┘
                           │
           ┌───────────────┼───────────────┐
           │               │               │
           ▼               ▼               ▼
┌─────────────────┐ ┌──────────────┐ ┌─────────────────────────┐
│  LangGraph      │ │  Feature     │ │  Inline Keyboard UI     │
│  StateGraph     │ │  Engines     │ │  ┌───────┐ ┌─────────┐  │
│  (router→agent  │ │              │ │  │ Main  │ │ Admin   │  │
│   →memory→tools)│ │ • Owner      │ │  │ Menu  │ │ Dashboard│ │
│                 │ │ • Personality│ │  └───┬───┘ └────┬────┘  │
│  LLMProvider:   │ │ • Engagement │ │      │          │       │
│  • llama.cpp    │ │ • Viral      │ │  ┌───┴───┐  ┌───┴───┐  │
│  • FakeLLM      │ │ • Ads        │ │  │Nested │  │Nested │  │
│                 │ │ • Moderation │ │  │Subs   │  │Subs   │  │
│                 │ │ • Gamification│ │  └───────┘  └───────┘  │
│                 │ │ • Analytics  │ │                          │
└─────────────────┘ └──────┬───────┘ └─────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    SQLModel + SQLite                          │
│  ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐  │
│  │User│ │Chat│ │Ad  │ │Viral│ │Mod │ │XP  │ │Event│ │Conf │  │
│  │    │ │    │ │Camp│ │Post │ │Conf│ │    │ │     │ │igs  │  │
│  └────┘ └────┘ └────┘ └────┘ └────┘ └────┘ └────┘ └────┘  │
└──────────────────────────────────────────────────────────────┘
```

---

## Quick Start

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

---

## Commands Reference

### 💬 Chat
| Command | Description |
|---------|-------------|
| (any message) | Chat with AI |
| `/story` | Story mode (Qwen) |
| `/companion` | Social mode (Gemma) |
| `/analyze` | Analysis mode (Phi) |
| `/personality list` | List AI personalities |
| `/personality set <name>` | Set group personality |

### 👤 Anonymous Chat
| Command | Description |
|---------|-------------|
| `/anon_start` | Join anonymous queue |
| `/anon_stop` | Leave anonymous chat |
| `/anon_report` | Report partner |

### 🎮 Games
| Command | Description |
|---------|-------------|
| `/quiz` | Start quiz |
| `/guess_start` | Number guessing game |
| `/wordle` | Persian Wordle |
| `/poll Q \| A \| B` | Quick poll |

### 🛠️ Tools
| Command | Description |
|---------|-------------|
| `/remind 30m text` | Set reminder |
| `/tr text` | Translate (fa→en) |
| `/convert 100 usd to irt` | Unit conversion |
| `/calc expression` | Calculator |

### 👑 Owner (admin only)
| Command | Description |
|---------|-------------|
| `/owner` | Owner dashboard |
| `/system` | System status |
| `/broadcast <text>` | Broadcast to all chats |
| `/admin_logs` | Recent admin logs |

### 📢 Channel & Force Join
| Command | Description |
|---------|-------------|
| `/post <text>` | Post to channel |
| `/schedule <date> <time> <text>` | Schedule post |
| `/forcejoin_on` | Enable force join |
| `/forcejoin_off` | Disable force join |

### 🔥 Viral & Ads (owner only)
| Command | Description |
|---------|-------------|
| `/viral_now` | Generate viral post |
| `/viral_preview` | Preview viral post |
| `/viral_stats` | Viral engine stats |
| `/ad_create <hours> <text>` | Create ad campaign |
| `/ad_list` | List ad campaigns |
| `/ad_pause <id>` | Pause campaign |
| `/ad_resume <id>` | Resume campaign |

### 🛡️ Moderation
| Command | Description |
|---------|-------------|
| `/mod_on` | Enable smart moderation |
| `/mod_off` | Disable moderation |
| `/mod_config` | Show moderation settings |
| `/warn <user_id>` | Warn user |
| `/mute <user_id> [min]` | Mute user |
| `/unmute <user_id>` | Unmute user |
| `/reputation [user_id]` | Show user reputation |

### 🏆 Gamification
| Command | Description |
|---------|-------------|
| `/profile` | Your gamification profile |
| `/daily` | Claim daily XP reward |
| `/xp_leaderboard` | XP leaderboard |
| `/achievements` | View achievements |

### 📊 Analytics (owner only)
| Command | Description |
|---------|-------------|
| `/analytics` | Analytics dashboard |
| `/analytics_active [h]` | Active users |
| `/analytics_retention [d]` | User retention |
| `/track <event>` | Track custom event |

---

## Testing

```bash
make test        # unit tests
make lint        # ruff check
make types       # mypy
```

## Tech Stack

- **Runtime:** Python 3.11+ with asyncio
- **Bot Framework:** python-telegram-bot v21+
- **Database:** SQLModel + SQLAlchemy async/sync SQLite
- **AI Orchestration:** LangGraph StateGraph
- **LLM:** llama.cpp GGUF (local) with FakeLLMProvider fallback
- **Linting:** ruff + mypy
- **Testing:** pytest

## Project Structure

```
src/nexus_ai_agent/
├── bot/
│   ├── handlers.py          # All Telegram command/callback handlers
│   └── middleware.py        # Auth + rate limiting
├── config/
│   └── settings.py          # pydantic-settings configuration
├── features/
│   ├── ads.py               # Advertisement system
│   ├── analytics.py         # Analytics engine
│   ├── anonymous_chat.py    # Anonymous chat pairing
│   ├── channel_manager.py   # Channel/group management
│   ├── engagement.py        # Community engagement
│   ├── force_join.py        # Force join verification
│   ├── games.py             # Quiz, Wordle, polls
│   ├── gamification.py      # XP, levels, achievements
│   ├── moderation.py        # Smart moderation
│   ├── owner_control.py     # Owner control system
│   ├── personality.py       # AI personality engine
│   ├── tools.py             # Calculator, translator, etc.
│   └── viral_engine.py      # Viral content engine
├── orchestration/
│   ├── graph.py             # LangGraph StateGraph
│   └── state.py             # NexusState definition
├── storage/
│   ├── models.py            # All SQLModel tables
│   └── providers/           # Storage backends
├── observability/
│   └── logging.py           # structlog setup
└── presence.py              # Online presence tracking
```

## License

MIT
