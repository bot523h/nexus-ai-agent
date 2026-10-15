# Module audit — classification of the 283 production modules at the #218 head

Generated from `module_inventory.json` (head `a0c2027`, base `main` `6b398565`; the later
commits of #218 change one module, `tools/filesystem_policy.py`, and docs only — re-run the
command in `INDEX.md` to refresh on any head).

## How a module gets its status (mechanical, no judgement calls)

| closeout_status | Rule | Count |
|---|---|---|
| `REMEDIATED_WITH_TESTS` | changed in #218 **and** at least one test file changed in the *same commit* imports or names the module (`remediation_map.json`) | **34** |
| `VERIFIED` | unchanged in #218, Mission-2 verdict `VERIFIED` on `6b398565` (23 then; 2 were since changed and re-classified) | **21** |
| `VERIFIED_WITH_LIMITATIONS` | unchanged, Mission-2 verdict `VERIFIED_WITH_LIMITATIONS` (53 then) | **39** |
| `CHANGED_UNREVIEWED` | changed in #218 without a same-commit test naming it — annotation-only (A-13), lint-only (A-14) and `asyncio.to_thread` (A-12) edits; covered by `mypy --strict`, the ruff ratchet (incl. `ASYNC`) and the full suite, **not** by a module-specific test | **50** |
| `DEFERRED` | everything else: unchanged and `NOT_ASSESSED` / `BLOCKED` / `HARDENED_BUT_NOT_COMPLETE` in Mission 2, or a package `__init__` without a prior row | **139** |

`VERIFIED` is never assigned by this pass to a module that only "looks fine": a module is
`VERIFIED` only if Mission 2 proved all applicable criteria on the same bytes, and
`REMEDIATED_WITH_TESTS` only if the pairing rule finds a test. Mission 2's own sentence
still holds for the untouched remainder: *23 of 244 non-trivial modules were VERIFIED on
`main` 6b398565 — not 202/202.*

## Size facts

| Fact | Value |
|---|---|
| Production modules / LOC (non-blank, non-comment) | 283 / 54 542 |
| Non-trivial modules (> 20 LOC) | 244 |
| Test files / LOC | 277 / 52 656 |
| Production modules changed by #218 | 84 (30 %) |
| Production modules with **no** test file referencing them | 35 (32 of them > 20 LOC) — listed by filtering `test_files_referencing == 0` in the CSV |
| Scripts / migrations / workflows / container files | 24 / 6 / 2 / 2 |

## The 50 `CHANGED_UNREVIEWED` modules — what covers them

`adapters/langgraph/lifecycle_recording.py, agent/self_monitor.py, agents/phi_agent.py,
agents/store/agent_manager.py, agents/store/base_agent.py, api/dashboard.py, bot/access_guard.py,
bot/feature_handlers.py, bot/slideshow_notify.py, bot/surface/ads.py,
bot/surface/channel_management.py, creative/image_gen/pollinations_adapter.py,
creative/image_post.py, creative/rendering/ir.py, creative/spine/__init__.py,
creative/spine/reference.py, creative/temporal/core.py, diagnostics/truth.py, features/ads.py,
features/ai_chat.py, features/analytics.py, features/anonymous_chat.py, features/engagement.py,
features/games.py, features/gamification.py, features/image_gen.py, features/owner_control.py,
features/personality.py, features/speech.py, features/summarizer.py, llm/litellm_provider.py,
llm/local_llama_cpp.py, memory/eval.py, memory/long_term.py, nagar/cognition/gateway.py,
nagar/creative/handoff.py, observability/logging.py, orchestration/state.py,
provenance/journal.py, storage/ai_storage_manager.py, storage/checkpoint_lifecycle_pg_store.py,
storage/checkpoint_reconciler.py, storage/langgraph_checkpoint.py, storage/migration_metadata.py,
storage/unified_cloud.py, tools/base.py, tools/files.py, tools/registry.py,
tools/system_shell.py, worker.py`

Each of these edits is one of three mechanical kinds, each with a repository-wide guard:

1. **A-13 strict typing** — annotations, `-> None`, typed containers, boundary validation
   (`tests/unit/test_strict_typing_boundaries.py` covers the three places behaviour could
   change). Guard: `[tool.mypy] strict = true`, pinned by
   `tests/unit/test_ci_lint_parity.py`.
2. **A-14 lint ratchet** — `# noqa` hygiene, `check=` on `subprocess.run`, logging f-strings,
   naive datetimes, etc. Guard: the ruff `select` list in `pyproject.toml` at zero violations.
3. **A-12 event-loop census** — blocking file I/O wrapped in `asyncio.to_thread` with identical
   call semantics. Guard: ruff `ASYNC` for `src/`; the two user-visible helpers have direct
   tests (`tests/unit/test_async_offload.py`).

Note `api/dashboard.py` appears here although A-03 (fail-closed dashboard) has a dedicated test
(`tests/unit/test_dashboard_api.py`): that test was changed in commit `9d31b39`, which touched
the module too, but the module's `__init__` import path is `nexus_ai_agent.api.dashboard` and the
test imports the FastAPI app via `nexus_ai_agent.api.app` — the pairing rule is deliberately
literal and under-claims rather than over-claims.

## Limitations

* The pairing rule attributes **all** finding ids of a multi-finding commit to each module it
  touched (e.g. `storage/db.py` lists R0/R1/A-01…A-19); the `tests` column is the precise part.
* `test_files_referencing` is a text search for the dotted module path or a
  `from <pkg> import <name>` line; indirect coverage through the composition root is not counted.
* No coverage run was repeated in this pass; Mission 2's `cov%` column (in
  `mission2_module_status_6b398565.csv`) is the last measured line coverage per module.
