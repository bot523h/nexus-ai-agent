# 04 — Defects found, fixes made, and the evidence for both

## P1-1 — the HuggingFace provider could not download any model it documented

**Found by** the module review of `storage/providers/huggingface.py`, one of the
thirty-five modules the overnight package listed as untested. Nothing in the PR
description, the CI matrix or the test suite pointed at it: the module is
reachable from `api/app.py`, and `HuggingFaceProvider` is constructed by
`storage/ai_storage_manager.py`.

**Root cause.** The key convention is `hf://<repo_id>@<revision>/<filename>` and a
HuggingFace repository id itself contains a slash. `_parse_key` split the key at
the **first** slash:

```python
repo_and_rev, _, filename = rest.partition("/")
repo, _, rev = repo_and_rev.partition("@")
```

so the module's own example
`hf://TheBloke/Mistral-7B-Instruct-v0.2-GGUF@main/mistral-7b-….gguf` parsed to
`repo="TheBloke"`, `rev="main"`, `filename="Mistral-7B-Instruct-v0.2-GGUF@main/mistral-7b-….gguf"`
and produced the URL

```
https://huggingface.co/TheBloke/resolve/main/Mistral-7B-Instruct-v0.2-GGUF@main/mistral-7b-instruct-v0.2.Q4_K_M.gguf
```

which is a guaranteed 404. The provider could not fetch a single model through its
documented interface; the feature was dead on arrival and no test noticed.

**Fix** (in the same commit as the tests): parse the revision from the **last** `@`,
fall back to `main` when the marker is absent (repository = the first two
segments), and validate every component before it can reach a URL — repository id
must be exactly `<owner>/<name>`, revision and path must match a safe charset, and
`..`/empty segments/leading slashes/query characters are rejected. The key is
interpolated into a URL, so validation now happens *before* the network, not after.

**Discriminating evidence** (a mutation must be killed by a test that would have
caught the original defect):

| Step | Result |
|---|---|
| new tests against the **fixed** parser | `81 passed` across the seven new test files |
| restore the old `partition("/")` parsing, keep the tests | **11 failed** — the documented-format test and all eight unsafe-key cases, i.e. the regression suite fails exactly on the old behaviour |
| restore the fix | green again |

The new suite also pins what the defect hid: the token travels only in the
`Authorization` header (never in the URL), a 404 raises `ProviderUnavailable` while
a 5xx raises `StorageError` and neither leaves a partial file behind, and a refused
upload makes **no** HTTP request at all.

### Input classes the parser must answer (added 2026-10-16, second pass)

The mission asked for malformed, **ambiguous** and **legacy** inputs to be covered
explicitly, not just the happy path. Nine further cases, each with the decision it
encodes:

| Input | Decision | Why |
|---|---|---|
| `hf://o/r@main/model@2.gguf` | **refused** (ambiguous) | two readings exist — revision `main` + file `model@2.gguf`, or revision `main/model` + file `2.gguf`; the convention has no escape, and guessing would download a *different* file than asked |
| `hf://TheBloke__Mistral-7B@main/model.gguf` | **refused** (legacy flattened form) | `safe_key_to_filename` (`a/b` → `a__b`) is applied by `mega.py` and `github_releases.py` only — verified by grep, `ai_storage_manager.py` builds `HuggingFaceProvider` and never flattens — so no stored key can be in that form; accepting it would give one model two names |
| `hf://owner/model@935c7190eb1f/model.gguf` | **accepted** | a revision may be a branch, a tag or a commit sha |
| `hf://owner/model@refs%2Fheads%2Fmain/file.gguf` | refused | percent-encoded separators are not decoded into the URL path |
| `hf://owner/model@main/file.gguf#fragment`, `…?download=1` | refused | query/fragment characters never reach a URL |
| `hf://owner/model@main/sub/../../x.gguf` | refused | traversal below the repository root |
| `hf://owner//model@main/file.gguf`, `hf://owner@/model@main/file.gguf` | refused | empty repository component |
| `hf://owner/model` | refused | a repository is not a file |
| `hf://owner/model/file.gguf` (no revision) | accepted as `main`, path may contain directories | the documented short form |

With these, reverting the parser fails **20 of the 41** provider tests (it failed 11
of 32 in the first pass) — the regression suite is strictly stronger than the
original defect's blast radius.

## The 13 modules that received their first behaviour test

All reachable from a composition root, all previously covered only by lint/typing
gates or by a reachability law:

| Module | Behaviour now pinned |
|---|---|
| `core/instrumentation.py` | event names (`op_started`/`op_completed`/`op_failed`), latency field, the original exception object re-raised unchanged, `log_args` truncated and off by default, slow ops warn instead of info |
| `creative/slideshow/image_fill.py` | only the deficit is generated, requests are text-only at 1280×720 with distinct per-slide prompts, and a failure **or a cancellation** removes every partial image |
| `agents/store/specialized_agents.py` + `AgentManager.list_agents` | registry ids are slugs, every agent carries its own metadata and a non-placeholder prompt, the listing is JSON-safe, ordered, and hides the system prompt |
| `api/studio.py` | 503 with no token, authentication before project checks (401 → 400 → 404 ordering), identical `404` for unconfigured and unauthorized projects (no existence oracle), blank `NEXUS_STUDIO_PROJECT_IDS` entries ignored |
| `application/image_generation.py` | provider follows the setting (free adapter by default, Gemini when configured), one instance per process, paid-tier guard refuses without the operator's opt-in |
| `agents/base.py` | the ABC refuses a subclass without `run()`, the injected provider is retained |
| `bot/agent_handlers.py` | the menu offers every agent id plus the stop button (`agent_select_*`, `agent_stop`), Markdown mode, no-op without a message |
| `bot/tool_handlers.py` | the weather reply is the tool's payload, a missing city never calls the tool, an empty payload reads as a failure |
| `bot/monitor_handlers.py` | a non-owner cannot even construct the approval system, `/approve` validates the id before the call and reports both outcomes, `/health` renders the monitor's numbers (and never says "healthy" when it is not) |
| `bot/knowledge_handlers.py` | the reply is the engine's summary, `close()` runs even when `learn()` raises, an empty wiki answer is reported instead of sent blank |
| `storage/providers/rclone.py` | unconfigured refusal, argv-list construction (a hostile remote key stays one argument), typed failure with the stderr excerpt |
| `storage/providers/mega.py` | credential gate, missing client package is typed, the root folder is created once, a key that must become one filename cannot smuggle a separator |
| `storage/providers/huggingface.py` | see P1-1 |

Test counts: **97 new tests** in eight files
(`test_instrumentation.py` 6, `test_slideshow_image_fill.py` 6,
`test_bot_handler_surface.py` 14, `test_agent_store_registry.py` 5,
`test_studio_api_scope.py` 12, `test_composition_facades.py` 6,
`test_storage_provider_boundaries.py` 41, `test_pr_census_compare.py` 7).
Full local suite: **4278 passed / 37 skipped** on the final tree
(4181 / 37 before this audit, 4262 / 37 after the first pass, 4271 / 37 after the
HuggingFace hardening, 4278 / 37 once the seven census-tool tests exist).

## P1-2 — the census tool fed the integration ledger wrong numbers (tooling)

**Found by** adversarial re-reading of the tool that produces
[`05_PR_CENSUS_AND_INTEGRATION_LEDGER.md`](05_PR_CENSUS_AND_INTEGRATION_LEDGER.md),
after the *first* comparison defect had already been fixed there.

**Root cause (two distinct defects, both in `scripts/pr_census.py`).**

1. The compare direction was `<head>...<base>` and the two returned numbers were
   written into the row under each other's names. #218 read "26 behind" although
   `main` is an ancestor of its head. Fixed in the earlier pass.
2. The compare runs against **each PR's own base branch** — correct for a file list,
   wrong for the column names it was published under. Two PRs here are *stacked*
   (#213 on #210's branch, #208 on #203's branch), and for them the row claimed
   main-relative numbers that were base-relative: #213 read `unique_commits=12`,
   `commits_behind_main=19`, while the main-relative truth is **26 unique / 6
   behind** (42 changed files vs `main`, not 15). A merge order computed from that
   is wrong in the direction that matters.

**Fix.** The base-relative pair is renamed (`unique_commits_vs_base`,
`base_commits_not_in_head`), a `compare_base_ref` column records which base was
used, and a new `_main_relative_counts()` issues a second `main...<head>` compare
whenever the base is not the default branch, so every row carries
`unique_commits_vs_main`, `commits_behind_main` and `changed_files_vs_main`
computed against `main` — with `-1` (never `0`, which would read as "level with
main") when that compare fails.

**Discriminating evidence.** `tests/unit/test_pr_census_compare.py` grew from 4 to
**7 tests**: the direction guard, the returned order, the 300-file truncation flag,
a text guard that the old names cannot come back, plus three new ones — a
main-based PR makes exactly **one** compare call, a stacked PR makes **two** and
takes the main-relative numbers, and a failed main compare yields `-1/-1/-1`. The
regenerated census shows both pairs side by side for #213 and #208, so the
mislabeling is now visible rather than silent.

## Tests whose *setup* changed (disclosed, not hidden)

Wiring the Asset Library into the passport archive makes archived evidence an
immutable (`0o444`) hard link. Three tests simulate corruption by writing to that
file, so they now clear the mode bit first:

| Test | Change | Assertion |
|---|---|---|
| `tests/integration/test_job_lifecycle_queue.py::test_succeeded_implies_valid_stable_traceable_artifact` | `chmod(0o644)` before `write_bytes` | unchanged — the passport must still detect the tamper |
| `tests/integration/test_nagar_durable_handoff_e2e.py::test_tampered_artifact_fails_independent_verification` | same | unchanged |
| `tests/integration/test_provenance_passport_e2e.py::test_tampered_artifact_bytes_flip_passport_to_compromised` | same | unchanged |

This is a **behaviour change in the product**, not a weakened test: evidence files
are now read-only, and a simulator of physical corruption must bypass the mode bit
the way an attacker with filesystem access would. The blast radius is recorded in
`04_ASSET_LIBRARY_AND_CREATIVE_GRAPH.md` of the overnight package: a tampered blob
reaches every attempt that links it, while detection still fires per attempt
because verification hashes the attempt path.

## What was **not** fixed here, and why

* **CommandBus per-command deadline / envelope byte cap / legacy trust path**
  (P1, `05_COMMANDBUS_AUDIT.md`): a real change to the execution contract, and the
  brief for this PR explicitly forbids growing #218 with new features. It is the
  first item of the post-merge queue with its acceptance test named in
  [`07_FINAL_REPORT.md`](07_FINAL_REPORT.md).
* **Asset Library `fsck`/`gc` schedule and the `asset_admitted` journal event**
  (P1): the library ships `fsck`/`gc` and the retention authority is the attempt
  directory, but nothing calls them on a schedule yet. Adding a scheduler now would
  be a second authority for retention; the design decision (who owns the schedule)
  is recorded as the next action.
* **Durable Creative Graph projection** (P1): the journal + passport already carry
  the causal chain; a read-model projection needs an ADR, replay/idempotency
  semantics and crash-recovery tests — out of scope for a stabilization PR.
