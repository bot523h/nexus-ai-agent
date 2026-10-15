# 03 — A-04 structural remedy: the board leaves the merge path (Phase 3)

**Requirement (brief §3).** Pick one structural remedy (per-claim files or a dedicated coordination ref); preserve single lease authority, CAS updates, lease expiry/takeover, exclusive-path collision, single gates owner, referee generation validation, deterministic recovery when the remote is unavailable, no adoption of untrusted PR content; adversarial tests; acceptance = zero code PRs dirty solely because of the board, via a repeatable preflight.

**Root cause.** Protocol v2 put the live board in `.agents/board.json` and required claims to be committed and pushed on the agent's own code branch: coordination state on every PR's merge path, and every PR tree a *source* of claims (52/63 touching, 43 two-sided, 7 board-only-dirty — `02_PR_CENSUS…`).

**Decision.** Dedicated coordination ref **`refs/nexus/board`** (ADR [0012](../../architecture/adr/0012-coordination-ref-board-authority.md); DECISION_LOG D-0026 round 3). Rejected: per-claim files (still through PRs, PR trees still sources), deleting the file (56 modify/delete conflicts), GitHub Git-Data API as CAS (token per claim, not hermetically testable — documented fallback).

## Changed paths (commit `5bc146e`)
| Path | Change |
|---|---|
| `scripts/agent_board.py` | ref store: `fetch_ref_board`, `push_ref_board` (`--force-with-lease=<ref>:<tip>`), `run_with_store` (≤3 CAS attempts, redo from fresh read), `_check_ref` (ref-only referee), `guard`, `init-ref`, `--store {ref,tree}` / `NEXUS_BOARD_STORE`; **existing command bodies unchanged** (mutation harness anchors intact). |
| `.agents/board.json` | reverted to `main@6b398565` content (frozen archive, sha256 `94a7642f64606b4ec69dcfd2b255755d20cf27c0a706797de6e237ad25b0a160`). |
| `.github/workflows/ci.yml` | `merge-base-guard` job runs `python scripts/agent_board.py guard --print`. |
| `tests/unit/test_agent_board_ref.py` (new), `tests/unit/test_agent_board.py` (pinned to tree store) | adversarial suite, see below. |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md`, ADR 0012 + index, `docs/README.md`, `docs/DECISION_LOG.md`, `CHANGELOG.md` | contract and rationale. |

## Invariants → test (all in `tests/unit/test_agent_board_ref.py`, local bare origin + clones, real git)
| Invariant | Test | Result |
|---|---|---|
| single lease authority, visible cross-sandbox | `test_claim_lands_on_the_ref_and_is_visible_from_a_second_clone` | pass |
| code tree untouched by claim/release | `test_code_tree_stays_clean_after_claim_and_release` | pass |
| CAS: lost race redone, first claimant wins, one commit per winner | `test_lost_cas_race_is_redone_and_the_first_claimant_wins`, `test_real_remote_rejects_a_stale_lease_without_mocks` | pass |
| bounded retry then refusal | `test_persistent_ref_movement_is_refused_after_bounded_retries` | pass |
| expiry/takeover + fencing token, generation advances | `test_expired_lease_takeover_advances_generation` | pass |
| stale generation refused | `test_stale_expected_generation_is_refused_on_the_ref` | pass |
| live foreign lease refused, nothing written | `test_live_foreign_lease_is_refused_and_nothing_is_written` | pass |
| unreadable remote / missing ref → exit 2, nothing written, tree clean | `test_unreachable_origin_is_exit_2_and_writes_nothing`, `test_missing_ref_is_unverifiable_not_an_empty_pass` | pass |
| incompatible generation (schema≠2, non-list claims, JSON array, non-JSON) → exit 2 | `test_incompatible_board_generation_on_the_ref_is_exit_2[3]`, `test_non_json_board_on_the_ref_is_exit_2` | pass |
| untrusted PR content never adopted (planted gates owner + world fence in tree) | `test_hostile_tree_board_is_ignored_in_ref_mode` | pass |
| malicious self-exclusion (`""`,`/`,`.`,`./`,`*`,`**`,`../`) inert, no crash; non-list paths → never a pass | `test_world_fence_on_the_ref_is_inert_and_cannot_crash[7]`, `test_non_list_exclusive_paths…[3]` | pass |
| exactly one live gates owner (0 or 2 → exit 2; malformed → exit 2) | `test_two_live_gates_owners…`, `test_expired_gates_owner_is_not_a_live_authority`, `test_malformed_claim_on_the_ref_is_unverifiable` | pass |
| bypass attempts: tree store cannot write authority; env overridden by explicit `--store ref`; unknown store rejected; `init-ref` never overwrites; non-schema-2 seed refused | 5 tests | pass |
| frozen archive pinned (CLI `guard` and repository digest) | `test_guard_pins_the_frozen_archive`, `test_repository_archive_matches_the_pinned_digest` | pass |
| `show` read-only unless gc frees a lease | `test_show_in_ref_mode_is_read_only_unless_gc_frees_a_lease` | pass |

36 passed; `tests/unit/test_agent_board.py` 50 passed (tree mode); `scripts/board_referee_mutations.py` 14/14 killed; ruff/format clean.

## Live bootstrap (real origin, 2026-10-15T20:23Z)
`init-ref` seeded `refs/nexus/board@04bacc8` from the union of live leases across all 56 distinct open-PR boards (exactly one: `task-270`, generation 3). Second `init-ref` → `REFUSED … already exists` (exit 2). `check --files scripts/agent_board.py,AGENTS.md` → exit 0 against the ref. First CAS heartbeat `04bacc8 → b1a5fac`. CI step on the real runner: `merge-base-guard › Board archive is frozen` = success (run 38802616791).

## Acceptance (repeatable, no branch touched)
`python scripts/board_merge_preflight.py --census pr_census.json --out a04_merge_tree_proof.json`: for each of #97 #111 #114 #125 #195 #198 #200, `git merge-tree --write-tree origin/main <head>` conflicts **only** on `.agents/board.json`; after a local synthetic commit restoring the archive, `merge-tree` is **clean — 7/7**. Hence "zero code PRs dirty solely because of the board" holds as soon as each PR applies the one-line restore in its rebase.

## Status and residual risk
* Status: **IMPLEMENTED · TESTED · LIVE-BOOTSTRAPPED · CI-VERIFIED on exact head `5bc146e` (run 38802616791, 21/21 success)** → `REMEDIED_CI_VERIFIED_PENDING_MERGE`; `NOT MERGED`, `NOT PRODUCTION-VERIFIED` (production = first real foreign claim through the ref after merge).
* Residual: the ref must remain pushable for every agent account (hosting assumption; fallback = fast-forward-only `PATCH /git/refs`); offline sandboxes read a stale copy only; 56 PRs still carry stale archives until rebased (harmless: `guard` makes the mistake visible, `merge-tree` proves the fix); `--store tree` exists for fixtures — a sandbox that sets `NEXUS_BOARD_STORE=tree` in production would talk to the fossil (explicit `--store ref` wins; `check` in tree mode still prints the v1.0 scope).
