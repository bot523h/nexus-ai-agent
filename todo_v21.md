# NEXUS AI v2.1 — Architecture & Quality Improvements

## Critical Fixes & Architecture

- [x] **1. GeminiProvider in llm/** — Created `llm/gemini_provider.py` implementing `LLMProvider` abstract base
- [x] **2. Smart Request Queue for Gemini** — Created `features/request_queue.py` with async priority queue, per-user fairness
- [x] **3. Conversation History Persistence** — Created `features/conversation_store.py` with SQLite-backed storage
- [x] **4. Proper Engine Lifecycle in app.py** — Rewrote `app.py` with centralized `_init_v2_engines()`, engines in `bot_data`
- [x] **5. /newchat Command** — Added handler + registration in `handlers.py`
- [x] **6. i18n for start/help/status** — All three commands now use i18n.t() with user language detection

## Creative Improvements

- [x] **7. Auto-detect User Language** — In start command, uses `i18n.detect_language()` + stores in UserLanguage table
- [x] **8. Image Generation Cache** — Rewrote `image_gen.py` with 1-hour TTL cache
- [x] **9. Smart Fallback Provider** — Created `llm/fallback_provider.py` with automatic degradation + disclaimer
- [x] **10. User Onboarding Flow** — Created `features/onboarding.py` with interactive welcome + i18n for 15 languages

## DevOps & Quality

- [x] **11. Update .env.example** — Added GEMINI_API_KEY, BOT_USERNAME, DROPBOX_TOKEN, PCLOUD_TOKEN, INTERNXT_TOKEN
- [x] **12. Update mypy config** — Added ignore_missing_imports for llama_cpp, gtts, sentence_transformers, langgraph, mega, sqlite_vec, telegram
- [x] **13. Run all quality gates** — ruff ✅ mypy ✅ pytest 23/23 ✅
- [ ] **14. Commit & push** — All changes pushed to GitHub
