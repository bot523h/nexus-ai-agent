# NEXUS AI Agent

**Telegram AI platform** — multi-provider conversations, cloud storage, 15-language support, image generation, speech synthesis, and the Nagar creative studio. Local/free paths are available; optional hosted services may require credentials and incur charges.

> **Version: v3.13.0** (see `VERSION` and [the changelog](CHANGELOG.md)). Nagar Wave 2.5 and Wave 3 image generation; local upscaling remains deferred.

---

## Features

### Core (v1.0–v1.2)
- 💬 **AI Chat** — Multi-persona conversations with auto-routing (Qwen, Gemma, Phi)
- 👤 **Anonymous Chat** — Random queue-based pairing, live message delivery, report system
- 🎮 **Games** — Quiz, number guessing, Persian Wordle, quick polls (all real, stateful engines)
- 🛠️ **Tools** — Reminders (persistent, cancellable, restart-safe), translation, unit conversion, safe calculator (no `eval`)
- 📢 **Channel Management** — ⚠️ *simulated replies; see "Real vs. Simulated" table below*
- 📋 **Inline Menu System** — Full interactive keyboard navigation

### Community OS (v1.3.0)
- 👑 **Owner Control** — Admin dashboard, broadcast, system status, admin logs
- 📢 **Force Join** — Real channel membership verification (bot-injected `get_chat_member`), 5-minute cache, verify button, and a message-flow gate while enabled
- 🎭 **AI Personalities** — 10 distinct personalities with per-group config and persistence
- 💬 **Auto Engagement** — Ice breakers, jokes, challenges, daily questions, events with rate limiting
- 🔥 **Viral Engine** — Real post generation/scoring/storage via `/viral_now`; preview/stats/post views are ⚠️ *simulated*
- 📢 **Ad System** — ⚠️ *simulated replies; no persistence yet*
- 🛡️ **Smart Moderation** — Config on/off is real; warn/mute/unmute/reputation are ⚠️ *simulated*
- 🏆 **Gamification** — ⚠️ *simulated at the command level* (quiz scoring is real)
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
- Legacy Pollinations integration; provider availability, terms and quotas may change.

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

## Nagar image generation and slideshow (v3.13.0)

See CHANGELOG and docs/DECISION_LOG for the full `/imagine` and `/slideshow` operator contract, paid-tier guards, and reliability notes. The canonical Version label above is **v3.13.0** (aligned with `VERSION`, `pyproject.toml`, and the newest released CHANGELOG heading).

---

## Architecture

Telegram → handlers → LangGraph / feature engines / Nagar creative studio → SQLite (SQLModel) and optional cloud providers. Authority boundaries: CommandBus executes; Cognition proposes; Passport/provenance observe.

---

## Quick Start

```bash
git clone https://github.com/bot523h/nexus-ai-agent.git
cd nexus-ai-agent
make setup
cp .env.example .env
# set TELEGRAM_BOT_TOKEN, NEXUS_OWNER_TELEGRAM_ID, optional GEMINI_API_KEY
make migrate
make run
```

Security (v3.13.0): deny-by-default access guard; set `NEXUS_OWNER_TELEGRAM_ID` and optional `NEXUS_ALLOWED_USER_IDS`.

---

## Commands Reference

See the Real vs Simulated table and command list in CHANGELOG / prior main README revisions for the full matrix. Simulated commands must never be claimed as production-complete without a backend.

---

## Development

```bash
make lint
make test
```

CI enforces ruff, mypy, version lockstep (VERSION / pyproject / CHANGELOG / README Version label), pytest, continuum evidence, and python-parity 3.10–3.12.

---

## License

MIT
