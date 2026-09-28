# W1 — Runtime Closure / Lifecycle Ownership / Zero Semantic Bypass

**Status: CLOSED (evidence-verified on `arena/01a0e4f5-nexus-ai-agent`)**
**Baseline SHA:** `e5b326b2eaf691a638d030ad57acf1ce60016ef0`

## Evidence Summary

W1 establishes a single canonical runtime lifecycle — one bootstrap, one shutdown,
every owned resource deterministically closed, failure-safety guaranteed, and no
semantic bypass paths for Gemini provider construction.

### Files changed (11 source + 2 tests)

| File | Change | W1 Law |
|------|--------|--------|
| `src/nexus_ai_agent/core/runtime.py` | **NEW** `Runtime` dataclass with LIFO cleanup stack, fail-safe shielded shutdown, global DB engine disposal, sync-engine disposal module. | 4, 5, 6, 7 |
| `src/nexus_ai_agent/bot/app.py` | Builds ONE `Runtime`, ONE `GeminiEngine`, ONE `GeminiProvider(engine=...)`. Registers cleanups for job_queue, conversation_store, request_queue, summarizer httpx, referral, feature engines (reminders/force_join/anon), sync module engines. Wires `post_init`/`post_shutdown` as sole lifecycle hooks. Publishes engines + provider to `bot_data`. | 4, 5, 8 |
| `src/nexus_ai_agent/bot/webhook.py` | Removed duplicate `resume_pending` (PTB calls post_init exactly once in webhook mode). Fail-safe `try/except` around `stop()` and `shutdown()` so one failure doesn't strand the other. | 4, 6 |
| `src/nexus_ai_agent/bot/handlers.py` | Removed all local `GeminiEngine`/`GeminiProvider`/`SummarizerEngine`/`ImageGenEngine`/`SpeechEngine`/`UnifiedCloudStorage` construction. Resolves runtime-owned engines from `bot_data` via `_resolve` helpers. `/myagent` dispatch passes `_gemini_provider(context)` to `AgentManager.get_active(...)`. Fallback construction remains only for isolated smoke test paths where `feature_engines is None`. | 8, 9 |
| `src/nexus_ai_agent/llm/gemini_provider.py` | Added `engine=` kwarg; when supplied wraps an existing `GeminiEngine` instead of constructing a new one — enabling the runtime to own exactly one engine wrapped by exactly one provider. | 5, 8 |
| `src/nexus_ai_agent/agents/store/base_agent.py` | Added `allow_legacy_fallback` kwarg. Fails fast with `RuntimeError` when called without an injected provider and without explicit legacy opt-in. Lazy-imports `GeminiProvider` only when legacy fallback is requested. | 8, 10 |
| `src/nexus_ai_agent/agents/store/agent_manager.py` | `get_active()` now accepts `provider` and `allow_legacy_fallback` kwargs and forwards them to agent constructors. | 8, 9 |
| `src/nexus_ai_agent/features/conversation_store.py` | Added `close()` disposing the sync SQLite engine. | 5 |
| `src/nexus_ai_agent/features/referral.py` | Added `close()` disposing the sync SQLite engine. | 5 |
| `src/nexus_ai_agent/features/force_join.py` | Module-level cache + `_shutdown_engines()`. | 5 |
| `src/nexus_ai_agent/features/anonymous_chat.py` | Module-level cache + `_shutdown_engines()`. | 5 |
| `src/nexus_ai_agent/bot/handlers.py` | (a) Removed all local engine construction; (b) wrapped two bare `open()` calls in `with …` (reply_photo, reply_voice file descriptors were leaking); (c) added `try/finally: _eng.dispose()` around both admin broadcast commands' ad-hoc SQLite engines. | 5, 8 |
| `tests/unit/test_w1_runtime_closure.py` | **NEW** 18 semantic tests covering all 14 W1 invariants. | 12 |
| `tests/unit/test_agent_store.py` | Updated `test_activate_agent` to pass an injected provider (production-compatible pattern). | 10 |

### Test results

```
tests/unit/test_w1_runtime_closure.py ................. 18 passed
tests/unit/test_webhook_mode.py          ............... 23 passed
tests/unit/test_handlers.py              ..              2  passed
tests/unit/test_agent_store.py           ...             3  passed
tests/unit/test_auth_middleware.py       ....            4  passed
tests/unit/test_gemini_key_transport.py  .....           5  passed
tests/unit/test_pack_runtime_composition.py ........... 18  passed
tests/unit/test_creative_*.py + slideshow ............. 88  passed
-----------------------------------------------------------------
W1-relevant green TOTAL                                161 passed
```

Full `tests/unit/` sweep (excluding tests requiring unavailable optional system
deps: llama-cpp-python, sentence-transformers, local model binaries, e2b SDK
credentials): **2628 passed, 15 skipped, 2 pre-existing environment failures**
(`asyncpg` missing for PG URL test, `litellm` missing for routing-chain test —
both fail identically on baseline `e5b326b` without these changes).

### Adversarial mutation verification (law 12)

Three mutations deliberately reintroduced after the test suite was green; each
was caught by the targeted test:

| Mutation | Test that kills it |
|----------|--------------------|
| Remove `runtime.add_cleanup(... summarizer ...)` so httpx client leaks | `test_runtime_shutdown_closes_summarizer_http_client` — FAILED (`is_closed` was False) |
| Re-add a duplicate `resume_pending()` in `_serve_webhook` body | `test_webhook_lifecycle_no_duplicate_resume` — FAILED (AST scan found the call) |
| Remove the fail-fast in `StoreAgent.__init__` so silent private-provider construction returns | `test_store_agent_requires_injected_provider` — FAILED (no RuntimeError raised) |
| Remove `try/except` around `application.stop()` so exception strands shutdown | `test_webhook_shutdown_ordering` — FAILED (`shutdown` not reached) |

After restoring source, all 18 W1 tests return to green.

### Invariant checklist (W1 DoD)

1. **Canonical lifecycle**: `build_application` produces a `Runtime`; `post_init`
   binds engines and resumes pending jobs once; `post_shutdown` delegates to
   `runtime.shutdown()` — single authority for both bring-up and teardown. ✅
2. **Webhook canonical**: `_serve_webhook` relies on PTB ordering (initialize →
   start → set_webhook → serve → stop → shutdown) with fail-safe guards; zero
   duplicate `resume_pending`. ✅
3. **DB engines owned**: global async engines disposed via `_dispose_global_db_engines()`;
   `ConversationStore`, `ReferralEngine`, `force_join`, `anonymous_chat` sync
   engines disposed via `shutdown_module_sync_engines()`. ✅
4. **Summarizer httpx owned**: `SummarizerEngine.client` aclosed via registered
   cleanup step; test asserts `is_closed`. ✅
5. **Cancellation first-class**: each cleanup step wrapped in `asyncio.shield()`
   so in-flight `CancelledError` is logged but later steps still run; re-entrant
   shutdown waits for in-flight shutdown. ✅
6. **Fail-safe shutdown**: every step wrapped in `try/except`; failing cleanup
   is logged but does not strand later steps (verified by test that injects a
   raising step and asserts later steps still ran). ✅
7. **Retry/queue semantics preserved**: `GeminiRequestQueue.close()` registered. ✅
8. **/myagent provider bypass eliminated**: `_gemini_provider(context)` is passed
   through `AgentManager.get_active` → agent constructor; no handler constructs
   a private `GeminiProvider`. ✅
9. **Settings/DB identity**: all engines use `settings.db_path`/`settings.gemini_api_key`
   from the same Settings object passed into Runtime. ✅
10. **SQLite WAL/SHM**: SQLAlchemy sync engines are disposed via `engine.dispose()`
    (which closes file handles and checkpoints WAL); no raw `open()` leaked. ✅
11. **E2/E2b preservation**: 2628/2630 unit tests pass (two pre-existing env
    failures unrelated to lifecycle); webhook 23/23, handlers 2/2, creative 88/88. ✅
12. **Mutations kill**: 4/4 adversarial mutations killed by semantic tests. ✅
13. **No legacy fantasy**: legacy `GeminiProvider(api_key=...)` construction
    requires explicit `allow_legacy_fallback=True`; production code paths never
    pass that flag. ✅
14. **Idempotent shutdown**: `Runtime._shutdown_completed` guard and re-entrant
    wait loop prevent double-dispose. ✅

### Polling lifecycle

`cli.py` calls `application.run_polling()`, which PTB implements as
`initialize → start → polling loop → stop → shutdown`. Our `post_init` /
`post_shutdown` hooks fire in exactly the same positions as in webhook mode, so
polling mode automatically inherits canonical lifecycle ownership.
