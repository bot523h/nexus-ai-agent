# NEXUS AI v2.0.0 — Global Expansion Release

## Phase 1: Google Gemini AI Integration (Critical)
- [ ] Create `features/ai_chat.py` — Gemini 2.0 Flash API integration
- [ ] Create `features/ai_vision.py` — Gemini Vision (image analysis)
- [ ] Add Gemini config to `config.py` (API key, model, rate limits)
- [ ] Add `/ai` command handler — AI chat with context
- [ ] Add `/vision` command handler — Image analysis
- [ ] Add `/ask` command — Quick one-shot AI question
- [ ] Add `/translate` command — AI-powered translation
- [ ] Add `/summarize` command — AI text summarization
- [ ] Add `/code` command — AI code generation
- [ ] Conversation memory with context window management
- [ ] Rate limiting (15 RPM, 1500/day free tier)

## Phase 2: Free Cloud Storage Unified System (Critical)
- [ ] Create `storage/unified_cloud.py` — Unified cloud storage manager
- [ ] Integrate Google Drive (free 15GB) via OAuth
- [ ] Integrate MEGA (free 20GB) 
- [ ] Integrate Dropbox (free 2GB)
- [ ] Integrate pCloud (free 10GB)
- [ ] Integrate Internxt (free 10GB)
- [ ] Total: ~57GB unified virtual storage
- [ ] Auto-distribute files across providers (round-robin + capacity check)
- [ ] Seamless download via bot — user never sees which provider
- [ ] `/cloud` command — Upload files to unified cloud
- [ ] `/myfiles` command — List all files across providers
- [ ] `/download` command — Download from unified cloud
- [ ] `/cloud_status` command — Show storage usage per provider

## Phase 3: Referral Viral Loop System (Critical)
- [ ] Create `features/referral.py` — Referral engine
- [ ] SQLModel: `Referral` table (referrer, referee, code, status, reward)
- [ ] `/referral` command — Get unique referral link
- [ ] `/join_referral` callback — Track referral signups
- [ ] Tiered rewards: 1→badge, 5→premium 3d, 10→AI credits, 25→VIP
- [ ] Referral leaderboard
- [ ] Welcome message with referrer info
- [ ] Auto-XP reward for successful referrals

## Phase 4: i18n Multi-Language System (Critical)
- [ ] Create `i18n/` directory with JSON language files
- [ ] Languages: en, fa, ar, es, fr, de, ru, zh, ja, pt, hi, tr, id, ko
- [ ] Create `i18n/loader.py` — Language loader & formatter
- [ ] Add user language preference to DB
- [ ] `/language` command — Set language
- [ ] All bot responses use i18n keys
- [ ] Auto-detect language from Telegram user settings
- [ ] Fallback to English for missing keys

## Phase 5: Free Image Generation via Pollinations.ai (High)
- [ ] Create `features/image_gen.py` — Pollinations.ai integration
- [ ] `/image` command — Generate AI image from text
- [ ] `/imagine` command — Advanced image with style options
- [ ] Style presets: realistic, anime, digital art, oil painting, etc.
- [ ] Image size options
- [ ] Auto-upload to unified cloud storage
- [ ] Rate limiting for fairness

## Phase 6: Speech-to-Text & Text-to-Speech (High)
- [ ] Create `features/speech.py` — STT/TTS engine
- [ ] gTTS integration — 100+ languages TTS
- [ ] Whisper API (free via Gemini) for STT
- [ ] `/tts` command — Text to speech
- [ ] `/stt` command — Voice message transcription
- [ ] Auto-transcribe voice messages in groups

## Phase 7: Smart Summarizer (Medium)
- [ ] Create `features/summarizer.py` — Content summarizer
- [ ] `/summary` command — Summarize chat history
- [ ] `/url_summary` command — Summarize web articles
- [ ] Multi-language summary output

## Phase 8: Final QA & Release
- [ ] ruff check + format
- [ ] mypy type check
- [ ] pytest all green
- [ ] Update CHANGELOG.md for v2.0.0
- [ ] Update README.md for v2.0.0
- [ ] Update architecture.md
- [ ] Git commit + tag v2.0.0
- [ ] GitHub Release v2.0.0
