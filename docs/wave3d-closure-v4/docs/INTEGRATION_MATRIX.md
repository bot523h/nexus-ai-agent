# Fresh overlap / integration matrix (step 14)

Computed **2026-10-14** with `git diff --name-only <merge-base>..<head>` per PR, then intersected.
Counts are files, not hunks.

| PR | head | merge-base with its base | files changed |
|---|---|---|---|
| #198 | `25c4965b` | `6b94f3d244a4` | 30 |
| #210 | `1d7390cc` | `6b398565cc1c` (= main) | 41 |
| #211 | `878842b6` | `e69ddb0c1ec2` | 8 |
| #213 | `29ca3d66` | `df3c9d16635d` | 42 |
| #118 | `d5a205f4` | `e5b326b2eaf6` | 52 |

## Pairwise intersections

| Pair | Files in common | The ones that matter |
|---|---|---|
| #210 ∩ #213 | **41** | whole PR: `.agents/board.json`, `.github/workflows/ci.yml`, `docs/DECISION_LOG.md`, `docs/architecture/*`, `migrations/versions/e575469d90b9_*`, `src/…` — #213 is stacked **on #210's branch** |
| #118 ∩ #198 | 7 | `.agents/board.json`, `agents/store/base_agent.py`, `features/ai_chat.py`, `llm/fallback_provider.py`, `llm/gemini_provider.py`, `llm/litellm_provider.py` |
| #210 ∩ #211 | 2 | `.agents/board.json`, **`.github/workflows/ci.yml`** |
| #118 ∩ #210 | 2 | `.agents/board.json`, `docs/DECISION_LOG.md` |
| #118 ∩ #213 | 2 | `.agents/board.json`, `docs/DECISION_LOG.md` |
| #198 ∩ #210 / #198 ∩ #211 / #198 ∩ #213 / #211 ∩ #213 | 1 each | `.agents/board.json` |

## Patch ↔ PR intersections

| Patch | Intersects | Consequence |
|---|---|---|
| `checker_v3.patch` | **#211**: `scripts/agent_board.py`, `tests/unit/test_agent_board.py` | if #211 lands, the checker patch must be re-based and re-validated on the new main; it must stay a **separate** reviewable change (never folded into #211 or #210) |
| `patch_a_async_session.patch` | **#198**: `src/nexus_ai_agent/bot/handlers.py` (outside #198's own diff) | same file, different hunks — the composed leg proves they coexist |
| `dispatch_async_session_repair.patch` | **#198**: `src/nexus_ai_agent/bot/handlers.py` | subset of the above; required for the dispatch path |
| `patch_b_prompt_placement.patch` | **#198**: `src/nexus_ai_agent/orchestration/graph.py` (inside #198's own diff) | fixes the prompt authority defect introduced by #198 |
| `tiso01_approval_isolation.patch` | — | touches only `tests/unit/test_approval.py` |

## Integration order (declared, and what was actually proven)

1. **#211 must land first** (or be re-based by its owner) — it owns `scripts/agent_board.py` and
   `.github/workflows/ci.yml` and is the governance gate. Nothing in this bundle may be pushed
   through it.
2. **`checker_v3.patch`** re-validates against the post-#211 main (currently validated against
   `6b398565`; the recorded after-hash `b43cda74…` is the acceptance value).
3. **#210** (SQLite init race) — carries the async-session boundary defect that Patch A repairs;
   Patch A stays isolated until #210 is authorized.
4. **Patch B → dispatch repair on the #198 tree.** Proven in leg 11: Patch B alone leaves the
   dispatch path dead (2 failed / 4 passed) because the defect is in the session boundary, not the
   prompt; both patches together give 6 passed. The two patches touch disjoint files, so the reverse
   order also applies cleanly — the dependency is *required*, not *order-sensitive*.
5. **#118** is the furthest behind (merge-base `e5b326b2`) and overlaps #198 on the LLM/agent
   taxonomy files; it must be re-based and its taxonomy reconciled with #198 before any combined
   validation is attempted.

## Shared governance record

`.agents/board.json` is modified by **all five** PRs and by none of the patches in this bundle.
No governance record was edited by this work; the bundle only *reads* the board.
