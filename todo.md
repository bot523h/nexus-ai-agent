# NEXUS AI v1.2.0 — Full Platform Upgrade

## Phase 1 — Channel & Group Management
- [x] Create `src/nexus_ai_agent/features/__init__.py`
- [x] Create `src/nexus_ai_agent/features/channel_manager.py`
- [x] Add channel/group command handlers to `handlers.py`
- [x] Lint + test + commit Phase 1

## Phase 2 — Anonymous Chat
- [x] Create `src/nexus_ai_agent/features/anonymous_chat.py`
- [x] Add anon chat command handlers to `handlers.py`
- [x] Lint + test + commit Phase 2

## Phase 3 — Games & Entertainment
- [x] Create `src/nexus_ai_agent/features/games.py`
- [x] Create quiz questions JSON and Persian words list
- [x] Add game command handlers to `handlers.py`
- [x] Lint + test + commit Phase 3

## Phase 4 — Utility Tools
- [x] Create `src/nexus_ai_agent/features/tools.py`
- [x] Add utility command handlers to `handlers.py`
- [x] Lint + test + commit Phase 4

## Phase 5 — Main Menu with Inline Keyboard
- [x] Update `/start` to show inline keyboard menu
- [x] Add callback query handlers for all submenus
- [x] Lint + test + commit Phase 5

## Phase 6 — Database Models & Migration
- [ ] Add new SQLModel tables to `models.py`
- [ ] Update `settings.py` with OWNER_TELEGRAM_ID
- [ ] Run `make migrate`
- [ ] Lint + test + commit Phase 6

## Final
- [ ] mypy + ruff + pytest all green
- [ ] git commit -m "feat: full platform - games, anon, channel mgmt v1.2.0"
- [ ] git push
- [ ] GitHub Release v1.2.0
