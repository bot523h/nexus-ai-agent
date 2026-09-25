# NEXUS AI Agent

**Telegram AI platform** — multi-provider conversations, cloud storage, 15-language support, image generation, speech synthesis, and the Nagar creative studio. Local/free paths are available; optional hosted services may require credentials and incur charges.

> **Version: v3.13.0** (see `VERSION` and [the changelog](CHANGELOG.md)). Nagar Wave 2.5 and Wave 3 image generation; local upscaling remains deferred.

Repository version is not a published-release claim. See [release truth and policy](README_RELEASE_TRUTH.md) for tag/release gaps and the dependency/container audit.

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

## Nagar image generation and slideshow (v3.12.0)

### `/imagine`: text to image

```text
/imagine a watercolor sunrise over the Caspian Sea
```

The new command uses `ImageGenProvider`, not the legacy `/image` engine. It sends
only your text prompt to the configured service and replies with image bytes.
Prompts must be non-empty and at most 2,000 characters. `/imagine` and slideshow
`--fill` require the owner or an entry in `NEXUS_ALLOWED_USER_IDS`; `/imagine`
also uses the bot's request limiter. Existing `/image` style syntax is unchanged.

**Default provider:** Pollinations, with no automatic paid fallback. External
availability is not guaranteed; HTTP failures surface as errors, not fake images.

**Optional paid Gemini:** configure all of the following and restart the process:

| Variable | Default / meaning |
|---|---|
| `NEXUS_IMAGE_GEN_PROVIDER` | `pollinations`; set to `gemini` to select Gemini |
| `NEXUS_IMAGE_GEN_PAID_TIER` | `false`; must be `true` to authorize paid generation |
| `NEXUS_CREATIVE_GEMINI_API_KEY` | Required for Gemini; use your secret environment, not Git |
| `NEXUS_IMAGE_GEN_MODEL` | `gemini-2.5-flash-image` (Gemini only) |
| `NEXUS_IMAGE_GEN_ESTIMATED_COST_USD` | `0`; Gemini requires a **positive operator-supplied estimate per response** |

The estimate is not a live price lookup. Confirm current model availability,
provider pricing and account billing before enabling Gemini. Merely supplying an
API key never authorizes paid generation. There were no live image-provider calls
in the offline verification suite.

### `/slideshow`: uploads plus optional generated images

1. Send `/slideshow` to open a photo collection session.
2. Upload one to five photos in that chat (sessions are per chat/user).
3. Send `/slideshow Ocean holiday` for the existing upload-only behavior.
4. To fill a deficit, instead send:

   ```text
   /slideshow --slides 5 --fill Ocean holiday
   ```

With **three uploaded photos**, that final command generates **exactly two**
complementary images. `--slides N` specifies the total number of input images,
not additional images; `N` is 1–5 and cannot discard already collected uploads.
Flags precede the title. A title is at most 60 characters, using letters, digits,
spaces, dots, colons or hyphens. The same syntax works in a photo caption.

`--fill` is opt-in consent to send the title as a generation prompt and, if the
operator enabled Gemini, incur its generation cost. A count alone is **not**
consent: the bot asks for `--fill` and retains the collected photos. No extra
confirmation round-trip is needed once the flag is present. Existing photographs
are **never uploaded to the image generator**. The separate slideshow-analysis
upload setting still controls optional hosted analysis. Without `--fill`, no
image generation occurs; when there is no deficit, no provider is contacted.

Generation runs in the existing `slideshow_render` queue before the same planner
and FFmpeg renderer. The Telegram flow remains capped at **five images / 30
seconds**, at 1280×720. The worker removes inputs and generated intermediates;
on success the notifier delivers the MP4 and then deletes it. On generation
failure or cancellation, partial generated images are removed. Session photo IDs
expire after 30 minutes; queued jobs follow the existing queue lifecycle.

### Reliability, caching and cost events

- Up to three HTTP attempts for transport errors, 429 and 5xx, with jittered
  exponential backoff and a numeric `Retry-After` capped at eight seconds.
  Other 4xx, redirects, invalid images and safety refusals are not retried.
- Each process/provider owns an in-memory SHA-256 cache over provider, model,
  prompt, dimensions and seed. It is limited to **16 entries / 32 MiB / one hour**;
  LRU eviction, restart or expiration permits a new request. It is not durable
  across queue restarts and is not a disk cache.
- Concurrent requests are serialized per adapter, so identical requests share
  their result. Failed or cancelled requests are never cached. Gemini's paid-tier
  guard runs **before** cache access as well as before waiting requests proceed.
- `image_generation_cost` records provider, model, attempt and
  `estimated_cost_usd` once per successful HTTP response, including malformed
  responses that might still be billable. Cache hits and rejected guards emit no
  cost event. Adapter cost/retry events contain neither API keys nor prompt text.
- These events are **estimates, not an invoice or spending cap**. A timeout can
  occur after the provider has billed a request; retries and resumed jobs may
  incur additional charges. Reconcile actual charges with the provider.

See [the decision log](docs/DECISION_LOG.md#2026-09-20--wave-3-image-generation-refinement)
for filenames, ownership boundaries and the cleanup policy.

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

# v3.13.0: Security — the bot is DENY-BY-DEFAULT. Only the owner
# (NEXUS_OWNER_TELEGRAM_ID) and explicitly allowed user ids can use it.
NEXUS_OWNER_TELEGRAM_ID=your_telegram_id
NEXUS_ALLOWED_USER_IDS=12345,67890     # optional extra users

# v3.13.0: Dashboard API bearer token. Set this whenever the dashboard
# port is reachable beyond localhost. Responses are PII-free; the token
# gates access.
NEXUS_DASHBOARD_TOKEN=                 # python -c "import secrets; print(secrets.token_urlsafe(32))"
```

> **Security (v3.13.0):** a global deny-by-default access guard now
> runs before every command, callback and free message. Unlisted users
> get one rate-limited denial and nothing else — this closes the hole
> where only `/ai`-family and `/imagine` were checked. The dashboard
> API no longer returns `telegram_id`/`username`, is bearer-token gated
> when `NEXUS_DASHBOARD_TOKEN` is set, and docker-compose binds port
> 8000 to `127.0.0.1` by default. `/cloud` and `/download` sanitize
> file names (no path traversal). **AIMemory egress (P0-7)** is
> default-deny: `/memory` extraction only sends text to the external
> Gemini model after an explicit per-user consent vote via inline keyboard
> (`aimem:grant`/`aimem:deny`); see `NEXUS_AI_MEMORY_ENABLED` and
> `NEXUS_AI_MEMORY_MIN_EGRESS_SECONDS` below. **Event-loop non-blocking
> (P1-2):** all sync-DB feature engines (reminders, referrals, force-join,
> anon chat) are offloaded to worker threads via `asyncio.to_thread`.

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

### ✅ Real vs. Simulated — status honesty (v3.13.0)

This bot has been caught overselling before; this table is the
source of truth. **Real** = wired to a working engine with tests.
**Simulated** = replies with a canned message; no backend effect.

| Status | Commands |
|---|---|
| ✅ Real | `/ai`, `/ask`, `/code`, `/translate`, `/summarize`, `/image`, `/imagine`, `/slideshow`, `/tts`, `/stt`, `/cloud`, `/myfiles`, `/download`, `/cloud_status`, `/referral`, `/referral_board`, `/start` (referral deep-link), `/calc`, `/remind`, `/cancel_remind`, `/reminds`, `/tr`, `/convert`, `/quiz`, `/guess_start`, `/guess`, `/guess_stop`, `/wordle`, `/wordle_stop`, `/poll`, `/anon_start`, `/anon_stop`, `/anon_report` (plus live anonymous delivery), force-join (`/forcejoin_on` + verify gate), `/owner`, `/system`, `/broadcast`, `/admin_logs`, `/personality`, `/engagement_*`, `/joke`, `/challenge`, `/analytics*`, `/track`, `/viral_now`, `/health`, `/agents`, `/myagent`, `/memory`, `/forget_me`, `/story` |
| ⚠️ Simulated | `/vision` (canned image description), `/post`, `/schedule`, `/ban`, `/unban`, `/stats`, `/welcome`, `/pin`, `/leaderboard`, `/daily`, `/xp_leaderboard`, `/achievements`, `/docs`, `/doc_delete`, `/chat_with_doc`, `/newchat` (claims to clear history but does not), `/ad_*`, `/mod_config`, `/warn`, `/mute`, `/unmute`, `/reputation`, `/viral_preview`, `/viral_stats`, `/viral_post`, `/companion`, `/analyze` |

Simulated commands reply with "(simulated)" or a canned string; they
are on the roadmap (see `AUDIT_REPORT_2026-09-21.md` §12) but should
not be treated as working features.

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
| `/imagine <description>` | Generate via retry/cache-aware provider (authorized users) |
| `/slideshow` | Start collecting one to five photos |
| `/slideshow <title>` | Queue an upload-only slideshow |
| `/slideshow --slides 5 --fill <title>` | Opt in to generating only missing images |

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
| `/start ref_<code>` | Deep-link: records the referral + rewards on first start |

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
| `/quiz` | Start quiz (real engine, inline answers) |
| `/guess_start` | Number guessing game |
| `/guess <n>` | Submit a guess |
| `/guess_stop` | Stop number guessing |
| `/wordle` | Persian Wordle (start) |
| `/wordle <5-letter>` | Submit a Wordle guess |
| `/wordle_stop` | Stop Wordle |
| `/poll Q \| A \| B` | Quick poll with inline votes + results |

### 🛠️ Tools
| Command | Description |
|---------|-------------|
| `/remind 30m text` | Set reminder (30m / 2h / 1d; fires into this chat) |
| `/reminds` | List your pending reminders |
| `/cancel_remind <id>` | Cancel one of your reminders |
| `/tr text` | Translate (fa→en) |
| `/tr <from> <to> text` | Translate between any pair, e.g. `/tr en fa سلام` |
| `/convert 100 usd irt` | Unit conversion (currency / length / weight / temp) |
| `/calc expression` | Real safe calculator (e.g. `/calc 2+2*3`, `/calc sqrt(144)`) |

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
├── adapters/                # Out-of-band adapters (caption unavailable shim, in-process job queue)
├── agent/                   # Governance: approval, feedback, self-monitor, updater
├── agents/                  # Persona agents (planner/executor/chat + model-tuned variants) & agent store
├── api/                     # FastAPI dashboard app
├── application/             # Hexagon core: image-generation app service + ports/ (typed seams)
├── bot/                     # Telegram surface: handlers, middleware, access guard, rate limiter
├── config/                  # pydantic-settings configuration (env aliases, fail-closed defaults)
├── continuum/               # Continuum snapshot state (.nexus/continuum.json contract)
├── core/                    # Hardened primitives: async DB, HTTP client w/ SSRF guard, instrumentation
├── creative/                # Nagar creative studio: capability packs (data-only manifests), slideshow
│   │                        #   render lane, image generation, caption/audio/edit/motion/delivery packs
├── domain/                  # Pure domain: glossary, retention/lifecycle/reconciler policies
├── features/                # Product feature engines (chat, games, referral, moderation, RAG, ...)
├── i18n/                    # 15-locale message catalogs
├── infrastructure/          # Observability: metrics, structured events, log redaction
├── integrations/            # Free third-party tool integrations
├── knowledge/               # Knowledge manager + web/Wikipedia trainers
├── llm/                     # LLM providers: litellm router, Gemini, local llama.cpp, fallback, fake
├── maintenance/             # R2 backups + scheduled housekeeping
├── memory/                  # Short-term + long-term memory stores
├── observability/           # structlog setup
├── orchestration/           # LangGraph StateGraph, router, NexusState
├── personality/             # Personality engine
├── storage/                 # SQLModel tables, checkpoint lifecycle, cloud providers (R2, HF, MEGA)
├── tools/                   # Sandboxed tool registry (files, shell)
├── cli.py                   # Single CLI entrypoint (`nexus ...`)
└── worker.py                # Job-queue worker entrypoint
```

## Documentation Map

Everything lives under `docs/` (index: [`docs/README.md`](docs/README.md)):

- `docs/DECISION_LOG.md` — the authoritative architecture decision log
- `docs/architecture.md` + `docs/architecture/` — current architecture & data lifecycle
- `docs/audits/` — dated audits and handoff analyses
- `docs/history/` — archived plans from the v1/v2 era (kept for traceability only)
- `docs/ops/` — deployment & operations runbooks (Koyeb, Neon, R2)

## License

MIT
