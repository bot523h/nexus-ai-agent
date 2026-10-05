---
name: nexus-bot-surface-command
description: This skill should be used when adding or changing a Telegram command, callback, or user-facing handler in nexus-ai-agent, when asked to "add a bot command", "wire a handler", "add a surface module", "add a locale string", "why does the surface test fail", or before editing bot/handlers.py. It encodes the framework-free surface pattern, the static registration contract, the lazy-import rule that keeps the package importable without telegram, and the i18n parity gate.
---

# NEXUS Bot Surface Command

## Purpose

Telegram command logic lives in framework-free modules under `src/nexus_ai_agent/bot/surface/`.
They are plain coroutines taking `(update, context)` — registrable as-is with PTB's handlers — but
they **never import `telegram`**. This keeps the command layer unit-testable with `SimpleNamespace`
fakes (no PTB install, no event loop, no network) and inside the frozen import boundary.

## The three invariants

1. **No `telegram` import in `bot/surface/`.** No module in the package imports PTB directly, and no
   module-level import may transitively pull an engine that does (such engines — `channel_manager`,
   `onboarding` — are imported **lazily inside the function**). Enforced by
   `tests/unit/test_surface_ptb.py` and `tests/unit/test_surface_onboarding.py` (a subprocess probe).
2. **Registration is verified statically, from the AST.** `bot/handlers.py` cannot be imported in a
   bare environment, so `tests/unit/test_surface_registration.py` checks the `EXPECTED` command→symbol
   map, the callback map, and that no stub string is left behind. A rebase that silently reintroduces
   a local stub shadowing the imported handler fails this test.
3. **No duplicate handlers.** A command already wired by `bot/feature_handlers.py` (`/calc`, `/tr`,
   `/convert`, `/remind*`, the games, `/anon_*`, referrals, force-join) must **not** be re-implemented
   here — two handlers for one command is the "dead code behind the live handler" defect.

## Procedure: add a command

1. **Choose the file.** Add to an existing `bot/surface/<area>.py`, or a new one. Do **not** edit
   `bot/handlers.py` (the single highest-conflict file in the repo) without an explicit board note.
2. **Write the handler** as `async def <name>_cmd(update, context)` using the duck-typed accessors
   in `bot/surface/_ptb.py` (`args`, `bot_of`, `callback_data`, ...). They tolerate malformed updates.
3. **Export it** from `bot/surface/__init__.py` and re-export in `bot/handlers.py`.
4. **Register it** in `bot/app.py::build_application`, behind the access guard group `-1`.
5. **Add the symbol** to the `EXPECTED` map in `tests/unit/test_surface_registration.py`.
6. **Localise** the strings for all 15 locales under `src/nexus_ai_agent/i18n/locales/`.
   `tests/unit/test_i18n_parity.py` fails on a missing key in any locale.
7. **Long work** (media, LLM batch) is enqueued through `JobQueuePort` — never awaited inside the
   handler. See the `nexus-job-lifecycle` skill.

## Verify

```bash
python -m pytest -q tests/unit/test_surface_registration.py \
  tests/unit/test_surface_ptb.py tests/unit/test_surface_onboarding.py \
  tests/unit/test_i18n_parity.py
```

## Additional Resources

- **`references/surface-contract.md`** — the full surface module map, the `_ptb` accessor list, the
  static-registration contract, the stub-string rule, and the import-boundary reason.

## Common mistakes

- Importing `telegram` (or a module that does) at module level in `bot/surface/` — the PTB probe fails.
- Editing `bot/handlers.py` without coordinating — it is the highest-conflict file.
- Adding a command to the code but not to `EXPECTED` — the registration contract silently loses value.
- Adding a locale key to English only — i18n parity fails.
- Awaiting a render/LLM batch inside the handler — it must be a durable job.
