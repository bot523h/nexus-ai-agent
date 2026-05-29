# NEXUS AI Agent

**Global AI Platform** — the world's most feature-rich free Telegram AI bot, combining Google Gemini 2.0 Flash, 57GB+ unified cloud storage, viral referral growth, 15-language support, image generation, speech synthesis, and smart summarization — all powered by 100% free APIs.

> **Current version: v2.0.0** — Global Expansion Release 🌍🚀

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

### 🌍 Global Expansion (v2.0.0)

#### 🤖 Google Gemini AI Integration
- **Gemini 2.0 Flash** — Free tier: 15 RPM, 1M TPM, 1500 requests/day
- `/ai <text>` — Conversational AI with context memory (20 messages per session)
- `/ask <question>` — Single-turn factual Q&A
- `/vision` — Image analysis via Gemini Vision (reply to any photo)
- `/code <prompt>` — AI code generation
- `/translate <text>` — AI-powered translation with auto-detect
- `/summarize <text|URL>` — Smart summarization with 5 modes

#### 🎨 Free Image Generation (Pollinations.ai)
- **10 style presets**: realistic, anime, digital, oil, watercolor, pixel, 3d, comic, minimal, fantasy
- **5 size options**: 1024×1024, 1792×1024, 1024×1792, 512×512, 1280×720
- `/image <description>` — e.g., `/image style:anime a cat samurai`
- Zero API key required — completely free

#### 🎤 Speech-to-Text & Text-to-Speech
- `/tts <text>` — Convert text to voice message (100+ languages via gTTS)
- `/stt` — Transcribe voice/audio messages (reply to voice, powered by Gemini)
- Automatic MIME type detection for all audio formats

#### ☁️ Unified Cloud Storage (57GB+ Free)
- **5+ free providers**: Dropbox (2GB), pCloud (10GB), Internxt (10GB), MEGA (20GB), GitHub Releases
- Round-robin distribution with capacity-aware routing and automatic failover
- `/cloud` — Upload file to unified cloud (reply to document)
- `/myfiles` — List all your cloud files
- `/download <filename>` — Download from cloud
- `/cloud_status` — View storage status across all providers

#### 🔗 Referral Viral Loop System
- **6 exponential growth tiers**: 🥉 Inviter → 🥈 Networker → 🥇 Star → 💎 Diamond → 👑 Legendary → 🚀 Viral Master
- Dual-reward system: both referrer and referee get prizes
- `/referral` — View your code, link, and tier progress
- `/referral_board` — Global referral leaderboard
- Deep-link support: `t.me/bot?start=ref_CODE`

#### 🌐 i18n Multi-Language (15 Languages)
- English, Persian, Arabic, Spanish, French, German, Russian, Chinese, Japanese, Korean, Portuguese, Hindi, Turkish, Indonesian, Italian
- Per-user language preference persistence
- `/language` — Interactive inline keyboard for language selection

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                       Telegram Bot API                           │
└──────────────────────────────────┬───────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│              python-telegram-bot Handlers                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────────┐  │
│  │  Auth    │ │  Rate    │ │  Force   │ │  Moderation        │  │
│  │Middleware│ │ Limiter  │ │  Join    │ │  Pipeline          │  │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────────┘  │
└──────────────────────────────────┬───────────────────────────────┘
                                   │
           ┌───────────────────────┼───────────────────────┐
           │                       │                       │
           ▼                       ▼                       ▼
┌──────────────────────┐ ┌──────────────────┐ ┌─────────────────────┐
│  LangGraph           │ │  Feature         │ │  Inline Keyboard UI │
│  StateGraph          │ │  Engines         │ │  ┌────────┐         │
│  (router→agent       │ │                  │ │  │ Main   │ v2.0.0  │
│   →memory→tools)     │ │  v1.x:           │ │  │ Menu   │ Menu    │
│                      │ │  • Owner         │ │  └───┬────┘ System  │
│  LLMProvider:        │ │  • Personality   │ │      │              │
│  • llama.cpp         │ │  • Engagement    │ │  ┌───┴──────────┐   │
│  • FakeLLM           │ │  • Viral/Ads     │ │  │6 Sections:   │   │
│                      │ │  • Moderation    │ │  │🤖AI 🎨Image  │   │
│  v2.0.0:             │ │  • Gamification  │ │  │🎤Speech ☁️Cloud│   │
│  • Gemini 2.0 Flash  │ │  • Analytics     │ │  │🔗Referral    │   │
│                      │ │                  │ │  │🌐Language     │   │
│                      │ │  v2.0.0:         │ │  └──────────────┘   │
│                      │ │  • GeminiEngine  │ │                     │
│                      │ │  • ImageGen      │ └─────────────────────┘
│                      │ │  • SpeechEngine  │
│                      │ │  • Summarizer    │
│                      │ │  • ReferralEngine│
│                      │ │  • I18n          │
└──────────────────────┘ └──────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│              Unified Cloud Storage (57GB+ Free)                  │
│  ┌─────────┐ ┌────────┐ ┌─────────┐ ┌──────┐ ┌──────────────┐  │
│  │ Dropbox │ │ pCloud │ │ Internxt│ │ MEGA │ │ GitHub Rel.  │  │
│  │  2GB    │ │ 10GB   │ │  10GB   │ │ 20GB │ │  Unlimited   │  │
│  └─────────┘ └────────┘ └─────────┘ └──────┘ └──────────────┘  │
└──────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    SQLModel + SQLite                             │
│  ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌─────┐ ┌──────┐ ┌──────┐        │
│  │User│ │Chat│ │Msg │ │XP  │ │Event│ │Referr│ │Cloud │ ...     │
│  └────┘ └────┘ └────┘ └────┘ └─────┘ └──────┘ └──────┘        │
└──────────────────────────────────────────────────────────────────┘
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

```env
# Required
TELEGRAM_BOT_TOKEN=your_bot_token

# v2.0.0: Google Gemini AI (free — get key at https://aistudio.google.com/apikey)
GEMINI_API_KEY=your_gemini_api_key

# v2.0.0: Bot username for referral links
BOT_USERNAME=your_bot_username

# v2.0.0: Cloud storage (optional — add providers you want)
DROPBOX_TOKEN=           # https://www.dropbox.com/developers
PCLOUD_TOKEN=            # https://www.pcloud.com/developers
INTERNXT_TOKEN=          # https://developer.internxt.com
MEGA_EMAIL=              # MEGA account email
MEGA_PASSWORD=           # MEGA account password
```

### 3) Initialize DB

```bash
make migrate
```

### 4) Run bot (polling)

```bash
make run
```

---

## Commands Reference

### 🤖 AI (v2.0.0)
| Command | Description |
|---------|-------------|
| `/ai <text>` | Chat with Gemini AI (with memory) |
| `/ask <question>` | Single-turn Q&A |
| `/vision` | Analyze image (reply to photo) |
| `/code <prompt>` | Generate code |
| `/translate <text>` | Translate text |
| `/summarize <text\|URL>` | Summarize content |

### 🎨 Image (v2.0.0)
| Command | Description |
|---------|-------------|
| `/image <description>` | Generate AI image |
| `/image style:anime a cat` | Generate with style preset |

### 🎤 Speech (v2.0.0)
| Command | Description |
|---------|-------------|
| `/tts <text>` | Text to speech |
| `/stt` | Speech to text (reply to voice) |

### ☁️ Cloud Storage (v2.0.0)
| Command | Description |
|---------|-------------|
| `/cloud` | Upload file to cloud (reply to file) |
| `/myfiles` | List your cloud files |
| `/download <name>` | Download file from cloud |
| `/cloud_status` | Cloud storage status |

### 🔗 Referral (v2.0.0)
| Command | Description |
|---------|-------------|
| `/referral` | Your referral code & stats |
| `/referral_board` | Global referral leaderboard |

### 🌐 Language (v2.0.0)
| Command | Description |
|---------|-------------|
| `/language` | Change bot language |

### 💬 Chat (v1.x)
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

## Free API Stack (v2.0.0)

| Service | Free Tier | Purpose |
|---------|-----------|---------|
| Google Gemini 2.0 Flash | 15 RPM, 1500/day | AI chat, vision, code, translation |
| Pollinations.ai | Unlimited | Image generation (10 styles) |
| gTTS | Unlimited | Text-to-speech (100+ languages) |
| Dropbox | 2GB | Cloud storage |
| pCloud | 10GB | Cloud storage |
| Internxt | 10GB | Cloud storage |
| MEGA | 20GB | Cloud storage |
| GitHub Releases | Unlimited | Cloud storage overflow |

**Total free storage: 57GB+**

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
- **AI (v2.0.0):** Google Gemini 2.0 Flash API
- **AI Orchestration:** LangGraph StateGraph
- **LLM (local):** llama.cpp GGUF with FakeLLMProvider fallback
- **Image Gen:** Pollinations.ai (free, no API key)
- **Speech:** gTTS (free TTS) + Gemini (STT)
- **Cloud Storage:** Unified 5+ provider orchestrator
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
│   ├── ai_chat.py           # v2.0.0: Gemini 2.0 Flash integration
│   ├── channel_manager.py   # Channel/group management
│   ├── engagement.py        # Community engagement
│   ├── force_join.py        # Force join verification
│   ├── games.py             # Quiz, Wordle, polls
│   ├── gamification.py      # XP, levels, achievements
│   ├── image_gen.py         # v2.0.0: Pollinations.ai image generation
│   ├── moderation.py        # Smart moderation
│   ├── owner_control.py     # Owner control system
│   ├── personality.py       # AI personality engine
│   ├── referral.py          # v2.0.0: Referral viral loop system
│   ├── speech.py            # v2.0.0: gTTS + Gemini STT
│   ├── summarizer.py        # v2.0.0: Smart content summarizer
│   ├── tools.py             # Calculator, translator, etc.
│   └── viral_engine.py      # Viral content engine
├── i18n/
│   ├── __init__.py          # v2.0.0: I18n manager (15 languages)
│   └── loader.py            # v2.0.0: Language loader
├── orchestration/
│   ├── graph.py             # LangGraph StateGraph
│   └── state.py             # NexusState definition
├── storage/
│   ├── models.py            # All SQLModel tables (including v2.0.0 models)
│   ├── unified_cloud.py     # v2.0.0: Unified cloud storage orchestrator
│   └── providers/           # Storage backends
├── observability/
│   └── logging.py           # structlog setup
└── presence.py              # Online presence tracking
```

## License

MIT
