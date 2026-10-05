# Bot surface contract — reference

Sources: `src/nexus_ai_agent/bot/surface/__init__.py`, `tests/unit/test_surface_registration.py`,
`tests/unit/test_surface_ptb.py`, `tests/unit/test_surface_onboarding.py`,
`docs/architecture/MODULE_MAP.md` (R12, "Add a Telegram command").

## Surface modules

| Module | Commands / callbacks |
|---|---|
| `gamification.py` | `/daily` `/profile` `/achievements` `/xp_leaderboard` |
| `docs.py` | `/docs` `/doc_delete` `/chat_with_doc` (+ free-text routing) |
| `ads.py` | `/ad_create` `/ad_list` `/ad_pause` `/ad_resume` `/ad_delete` `/ad_stats` |
| `channel_management.py` | `/post` `/schedule` `/pin` `/ban` `/unban` `/stats` `/welcome` |
| `onboarding.py` | the `onboarding_*` inline-keyboard callbacks |
| `_ptb.py` | duck-typed accessors; never imports `telegram` |

## `_ptb` accessors

`args`, `bot_data`, `bot_of`, `callback_data`, and peers. Every accessor tolerates a malformed update
(missing message, no `effective_user`, no `args`) and degrades to an empty value or a silent no-op —
an exception at dispatcher level would drop the update instead of answering it.

## Static registration contract (`test_surface_registration.py`)

- `EXPECTED: dict[command → surface symbol]` — 20 commands (e.g. `daily → daily_cmd`,
  `ad_create → ad_create_cmd`, `welcome → welcome_cmd`).
- `EXPECTED_CALLBACKS` — `^onboarding_ → onboarding_callback_cmd`.
- `STUB_STRINGS` — the verbatim stub sentences that must no longer appear (matched exactly, so a real
  message that merely starts the same way is not a false positive).

The test parses `bot/handlers.py` with `ast` (it cannot be imported in a bare environment) and fails
if a symbol is missing or a stub string returns. This is how PR#32's value is protected against a
rebase that silently reintroduces a shadowing local stub.

## Why the framework-free rule exists (R12)

- `tests/architecture/test_import_boundaries.py` tolerates `telegram` only in grandfathered files;
  `bot/surface/` is not one of them.
- Keeping logic framework-free makes every handler unit-testable with `SimpleNamespace` fakes.
- Registration is unaffected: PTB only awaits `handler(update, context)`.

Two probes enforce it: `test_surface_ptb.py::test_the_surface_package_imports_no_telegram` (static)
and `test_surface_onboarding.py::test_the_surface_package_imports_without_telegram` (subprocess import).

## Duplicate-handler rule

`bot/feature_handlers.py` already wires `/calc`, `/tr`, `/convert`, `/remind*`, the games, `/anon_*`,
referrals, and force-join. The surface package deliberately does **not** duplicate them.

## i18n

15 locales × 63 keys under `src/nexus_ai_agent/i18n/locales/`. `tests/unit/test_i18n_parity.py` fails
on any missing key. Add new keys to every locale in the same PR. English is canonical; Persian
summaries defer to English.

## Access control

Register handlers behind the access guard group `-1` in `bot/app.py::build_application`. The access
guard is a real membership check (bot-injected `get_chat_member`, 5-minute cache, verify button,
message-flow gate while enabled).
