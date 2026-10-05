# NEXUS/NAGAR — Post-Merge Lineage Backbone Handoff

**Captured:** 2026-10-05 16:16 UTC
**Mission status:** `BLOCKED` — no runtime implementation was started because the complete vertical slice necessarily overlaps a live typed-spine lease and unresolved queue ownership. This is not a claim that lineage is complete.
**Session branch:** `arena/01a10ccd-nexus-ai-agent`
**Main/base at start:** `5a228ea9a114363f6b77a4becd0681eeab3527a2`
**Initial local HEAD:** `5a228ea9a114363f6b77a4becd0681eeab3527a2`; working tree was clean.
**Board successor:** `task-240-durable-creative-project-revision-lineage`, zone `creative-revision-lineage`, recorded as `deferred` (not claimed).
**Implementation PR:** none at the time this handoff was drafted; any follow-up PR must remain draft until implementation and its exact-head proof exist.

## Live truth

- Live `origin/main` was `5a228ea9a114363f6b77a4becd0681eeab3527a2`, a merge commit for PR #156. `gh pr view 156` reports `MERGED`, approved, merge commit `5a228ea9a114363f6b77a4becd0681eeab3527a2`, source head `eb18091777330b1c95a966b92548ec72dd79cbe6`. `git cat-file -p origin/main` shows parents `6e41123b40f15a63241c8db27cd884010f55db38` and `eb18091777330b1c95a966b92548ec72dd79cbe6`; the second parent exactly matches the fetched PR head. The current main tree contains the merged queue adapter, creative passport, recovery tests, provenance recording tests, and lifecycle documentation.
- PR #154 is `MERGED`, not an open/superseded branch to revive. PR #156 is the current merged queue-evidence baseline.
- Exact-current-main CI is GitHub Actions run **37337104871** on exactly `5a228ea9a114363f6b77a4becd0681eeab3527a2`: **13/16 jobs completed successfully, 0 failures, 3 `continuum-evidence` jobs still in progress** at the last query (2026-10-05 16:16 UTC). This is pending, not green overall. Re-query the run before using it as evidence.
- The main board initially still had merged `task-232-durable-creative-queue-evidence` active, with `gates_owner: true`, and its queue paths fenced. This handoff branch releases only that verified-merged claim by the existing stewardship-release precedent. Main already has `task-231-provenance-ledger` as `completed_merged`; no unrelated claim was changed.
- `.agents/board.json` now records task-240 with acceptance/evidence criteria and the smallest proposed lineage paths, but status is `deferred`; **there is no active task claim and no runtime file has been edited**. The forward-task entry replaces task-122, which its existing board note says was delivered through PR #47.

### Live overlapping PR / lease scan

| PR | Live state at scan | Relevant ownership/conflict |
|---|---|---|
| #150, head `5f84be4dd22fa6e6d73e9cd06be283e89bc33c36` | Open draft, dirty; base `main` | Its pushed branch board carries active-in-review `task-230-agent-intelligence-execution`, claimed `2026-10-04T17:19:00Z`, TTL 72h (expires `2026-10-07T17:19:00Z`). It fences `creative/spine/`, `creative/render_jobs.py`, `jobs/lifecycle.py`, and related intent/orchestration paths. Its own PR body says not to merge until the competing #126–#128 spine contracts are reconciled. This is the canonical typed-work critical path needed by the target. |
| #152, head `55514b630d68cdbbd186174d444b79e01e3d96b1` | Open draft, dirty; based on old `e5b326b2eaf691a638d030ad57acf1ce60016ef0` | Its pushed branch board still carries `task-231-causal-evidence-ledger` active-in-review, claimed `2026-10-04T20:25:57Z`, TTL 24h, fencing `adapters/in_process_job_queue.py`. The task-231 identity is already completed on current main by PR #153, so this branch-board claim is stale/contradictory; do not silently treat its competing causal ledger as main authority. |
| #157, head `e0b0ac9171bd0b524313f8cb9a9c92d8c507fd3c` | Open, changes requested, dirty | Its branch board reactivates `task-231-provenance-ledger` on `adapters/in_process_job_queue.py` (claimed `2026-10-05T03:49:39Z`, TTL 24h). More importantly, `git diff origin/main origin/pr/157` shows deletion of the PR #156 queue/passport/recovery implementation (3,156 deletions across the queue/passport/recovery files in the focused diff). It must be rebased/reconciled by its owner/reviewer; do not merge or modify that branch here. |
| #131/#134 and #126–#128 | Open; multiple heads overlap the Creative Intelligence / typed-spine surface | Competing, unmerged identity/revision/intent contracts. No canonical CreativeWork model is present on current main. Re-check their live state after #150 is resolved. |
| #148/#149/#151 | Open and touch Studio CommandBus/models | Additional integration-conflict surface; not needed to establish the blocker above, but recheck before claiming Studio paths. |

The open-PR scan used live GitHub PR metadata and fetched PR-head board files. A branch board is evidence of a pushed lease, but it does **not** supersede current-main tree truth; contradictions must be reconciled rather than guessed away.

## Search-10 architectural reconnaissance

1. **Project identity:** `src/nexus_ai_agent/creative/studio/models.py:300-321` defines `Project.project_id`, `state_revision`, and derived `state_hash`. `creative/render_jobs.py` creates a project per request (`_build_project`). These are typed but in-memory/request-scoped, not durable Project records.
2. **CreativeWork identity:** no `CreativeWork` class or `creative/spine/` module exists in current main (`rg` over `src/nexus_ai_agent`). PR #150 proposes a `creative.spine` CreativeWork/Intent path, but it is open, draft, and under the active task-230 lease; it is not main truth.
3. **Revision changes:** `Project.state_revision` and `EditTransaction.parent_revision`/hashes exist (`models.py:300-321, 534-551`). `CommandBus` increments revision and appends to `_history` (`creative/studio/bus.py:167-174, 350-389`), which is in-memory only. Queue passports currently label their revision as `execution_projection_only` with `parent_revision_id: null` (`jobs/creative_passport.py:475-482`).
4. **Typed intent persistence:** current main has no canonical typed Creative Intent. It persists the validated creative queue payload (command/operation/args) as JSON; that payload is a request snapshot, not a CreativeWork intent identity. Studio `TypedCommand` is typed, but the current worker does not return its full command/bus receipt into the durable queue result.
5. **Artifact lineage persistence:** PR #156 adds authoritative queue-row request identity, attempt-history JSON, verified artifact passport JSON, archived input/output hashes, and restart re-verification. The existing provenance ledger observes queue lifecycle transitions and projects evidence; neither records canonical CreativeWork/Project revisions.
6. **CommandBus execution identity persistence:** `CommandBus._apply` creates a `tx_<uuid>` `EditTransaction` and stores it in its private in-memory history. The durable queue’s `transaction_id` is a different deterministic queue-request identity. `creative_passport.py:501-506` explicitly sets `canonical_commandbus_transaction_id` to `None`.
7. **Successful execution → intent:** the durable row links job/request to its persisted payload and verified artifact. Passport intent is a payload-derived projection (`creative_passport.py:517-520`), not an authoritative typed Intent ID or persisted CreativeWork relation.
8. **New revision → parent:** only the in-memory `EditTransaction.parent_revision`/previous hash points to the preceding Bus revision. Current queue passport history explicitly has no canonical parent revision; a new durable revision cannot presently name its parent.
9. **Smallest missing durable link:** a canonical typed lineage fact joining selected Project/CreativeWork/revision/parent/Intent identities to the *actual* CommandBus command/transaction and queue job/request, then to the queue-verified artifact/passport. It must remain immutable per historical execution, explicitly null for unknown legacy values, and readable after restart. Do not duplicate the queue’s request/transaction IDs or create a second provenance authority.
10. **Safest boundary/tests:** preserve the existing CommandBus policy/dispatch boundary; connect its returned receipt to the existing SQLite queue/passport rather than create an executor or domain store. Relevant enforcement is `tests/unit/test_creative_studio.py`, `tests/unit/test_creative_passport.py`, `tests/integration/test_job_lifecycle_queue.py`, `tests/integration/test_creative_execution_recovery.py`, and `tests/integration/test_provenance_queue_recording.py`. Required adapter/worker paths overlap the live task-230/task-231 branch claims above, so no safe implementation point is currently unleased.

## Problem

The durable queue can prove a verified artifact and its queue request/attempt, but it cannot durably answer which CreativeWork revision and typed Intent produced it, which parent revision that revision derives from, or which real CommandBus transaction committed it. Current main has no canonical CreativeWork/Intent model to attach to.

## Truth

PR #156 is present and its queue/passport/recovery code is the correct baseline. It does **not** deliver Project/CreativeWork revision persistence or a canonical durable CommandBus transaction. `Project`/`CommandBus` revision history is in-memory. The artifact passport honestly says the canonical Bus transaction is null and the revision is only an execution projection. The creative identity layer needed to close that gap exists only in conflicting, unmerged branches.

## Evidence

- `docs/architecture/JOB_LIFECYCLE.md:9-14, 392-394, 471-479, 505-509` explicitly scopes task-232 as queue-only and calls the Project/CreativeWork/CommandBus mission open.
- `tests/integration/test_job_lifecycle_queue.py:826-847` proves request identity and artifact passport survive a fresh queue adapter; it also asserts `canonical_commandbus_transaction_id is None` and `revision.kind == "execution_projection_only"`.
- `src/nexus_ai_agent/creative/studio/models.py:300-321, 534-563` and `src/nexus_ai_agent/creative/studio/bus.py:167-174, 350-389` prove Project/Bus revision and transaction facts exist only in the Bus object/history.
- `src/nexus_ai_agent/jobs/creative_passport.py:475-482, 495-520, 544-550` constructs explicit projection IDs but names them projections, sets the canonical Bus transaction to null, and records the limitation.
- Main merge ancestry/tree proof and exact-current-main CI status are recorded above; PR #150/#152/#157 live state and paths were read from GitHub plus their fetched branch boards.

## Risk

A verified file can be re-read and measured, but the system cannot explain which persistent creative revision/intent or Bus transaction authored it. A later revision cannot durably point to its parent. Any inferred relationship based on a payload convention, filename, timestamp, log, or matching UUID would be non-authoritative and could silently attach an artifact to the wrong work.

## Design

After one canonical typed-spine contract is selected and all ownership conflicts are resolved, extend the **existing** queue/passport/provenance boundary with the exact lineage receipt produced by the canonical CommandBus path. Preserve the queue’s current identities and fencing; store only additional, typed Project/CreativeWork/revision/Intent and Bus-command facts that do not already have an authoritative identity. Bind artifact lineage only after the existing independent verifier succeeds. Read old records as explicitly unknown/null. Do not introduce a parallel CreativeWork type, bus, renderer, execution authority, or second domain store.

## Minimal Complete Fix

Task-240 is recorded in `.agents/board.json` with acceptance criteria and evidence requirements. Once unblocked, implement one real existing queue → canonical typed Intent/CreativeWork revision → CommandBus result → independently verified artifact path; persist the immutable parent/intent/command linkage through the queue’s existing durable store and passport, reject contradictory reassignment, and prove a fresh-instance/process read. This handoff deliberately does not invent the fields until the canonical typed-spine contract is decided.

## Tests

**Run in this blocked handoff:**

- `.venv/bin/python -m pytest --noconftest -q tests/unit/test_agent_board.py` → **18 passed**. (`--noconftest` is necessary in this bare environment: repository-wide `tests/conftest.py` imports the uninstalled application package and `tests/unit/conftest.py` imports NumPy; neither is required by the board test.)
- `python scripts/agent_board.py check --files .agents/board.json,LINEAGE_BACKBONE_HANDOFF_2026-10-05.md --branch arena/01a10ccd-nexus-ai-agent` → **no overlap** for these governance/handoff files against the current-main board.
- Full lint/types/regression were **not run**: no runtime implementation was made, and the local environment is not fully installed. Current-main CI remains pending as stated above. Do not report those gates green.

**Required when task-240 is claimed:** focused identity/nullability and invalid-reference tests; queue/Bus/artifact integration through the real verifier; SQLite close/reopen or process-restart readback; replay/retry/recovery immutability; failed execution and missing-evidence incomplete behavior; then all relevant queue/passport/provenance and Studio regression suites, docs integrity, and exact-head CI.

## Proof

- **Main merge truth:** verified from GitHub merge metadata, direct merge-commit parent match to PR #156 head, and the current main tree. This proves PR #156 is the baseline, not that Project/CreativeWork lineage is implemented.
- **Local governance proof:** board schema/behavior test 18/18 passed; changed-file board check passed. No application code was changed.
- **Main CI:** run 37337104871 is exact-head but pending (13 success / 3 in progress / 0 failed at last query). No green claim.
- **Branch/PR/production evidence:** no runtime branch evidence exists for task-240; production behavior is not claimed. Any governance-only draft PR opened from this branch must remain marked as a handoff, not as implementation completion.

## Next action — required before any implementation

1. Re-fetch `origin/main`, re-run `gh pr list`, inspect current boards on every open PR head touching these paths, and re-query run `37337104871`.
2. Resolve the canonical typed `Intent`/CreativeWork/Revision contract across PRs #126–#128, #131/#134, and #150; PR #150’s own description requires this arbitration.
3. Have the owners/reviewers reconcile/release the task-230 spine/render lease and the contradictory task-231 queue claims on PR #152/#157. Confirm PR #157 no longer removes the PR #156 queue/passport files. Do not alter or merge those PRs from this session.
4. Only then claim `task-240-durable-creative-project-revision-lineage` on `arena/01a10ccd-nexus-ai-agent`, run `agent_board.py check` for the exact final file set, and commit/push the claim before code.
5. Implement the smallest vertical slice, run required tests and exact-head CI, and update this artifact with new SHAs/results. Human merge remains required.

**Next agent MUST verify live Git/GitHub/board state again; every SHA, PR state, TTL, and CI status above is a dated observation, not a standing truth.**
