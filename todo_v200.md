# NEXUS AI v2.0.0 — Global Expansion Release

## Phase 1: Google Gemini AI Integration (Critical)
- [x] Create `features/ai_chat.py` — Gemini 2.0 Flash API integration
- [x] Add Gemini config to `config/settings.py` (API key, model, rate limits)
- [ ] Add `/ai`, `/ask`, `/vision`, `/code`, `/translate`, `/summarize` command handlers to handlers.py

## Phase 2: Free Cloud Storage Unified System (Critical)
- [x] Create `storage/unified_cloud.py` — Unified cloud storage manager
- [x] Integrate Dropbox (free 2GB)
- [x] Integrate pCloud (free 10GB)
- [x] Integrate Internxt (free 10GB)
- [x] Auto-distribute files across providers (round-robin + capacity check)
- [ ] Add `/cloud`, `/myfiles`, `/download`, `/cloud_status` command handlers to handlers.py

## Phase 3: Referral Viral Loop System (Critical)
- [x] Create `features/referral.py` — Referral engine with 6 reward tiers
- [ ] Add Referral/ReferralCode tables to `storage/models.py`
- [ ] Add `/referral`, `/referral_board` command handlers to handlers.py

## Phase 4: i18n Multi-Language System (Critical)
- [x] Create `i18n/` directory with JSON language files (15 languages)
- [x] Create `i18n/loader.py` — Language loader & formatter
- [ ] Add user language preference to DB (UserLanguage model in models.py)
- [ ] Add `/language` command handler to handlers.py

## Phase 5: Free Image Generation via Pollinations.ai (High)
- [x] Create `features/image_gen.py` — Pollinations.ai integration with 10 styles
- [ ] Add `/image` command handler with style/size selection to handlers.py

## Phase 6: Speech-to-Text & Text-to-Speech (High)
- [x] Create `features/speech.py` — gTTS + Gemini STT engine
- [ ] Add `/tts`, `/stt` command handlers to handlers.py

## Phase 7: Smart Summarizer (Medium)
- [ ] Create `features/summarizer.py` — Content summarizer
- [ ] Add `/summary` command handler to handlers.py

## Phase 8: Final QA & Release
- [ ] ruff check + format
- [ ] mypy type check
- [ ] pytest all green
- [ ] Update CHANGELOG.md for v2.0.0
- [ ] Update README.md for v2.0.0
- [ ] Update architecture.md
- [ ] Git commit + tag v2.0.0
- [ ] GitHub Release v2.0.0
