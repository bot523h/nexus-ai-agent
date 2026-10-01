---
status: accepted
date: 2026-10-01
deciders: session arena/01a0181931 (task-227, task-228)
consulted: AGENTS.md protocol v2, docs/MULTI_AGENT_PROTOCOL.md, .agents/board.json
informed: all agents working on this repository
---

# 0012. Composition and evidence governance in the board CLI

## Context and Problem Statement

The board CLI answers "may I push this file?" (`check`, path leases) but nothing
answered the question that actually broke a release: **may these two PRs merge
next to each other?** Two pull requests can each be correct in isolation and still
compose into one broken file — PR #128 (identity-addressed undo) and PR #129
(staged plan transactions) both rewrite `creative/studio/capabilities.py`, and a
naive "take ours / take theirs" resolution silently drops one invariant. Separately,
PR #128's body cited two tests that **do not exist at its own head**
(`test_undo_without_identity_still_rewinds_the_newest`,
`test_undo_naming_a_stale_transaction_is_refused`) and quoted counts (`126`, `122`)
that no file in the tree produces — prose that looked like evidence but was not
mechanically derivable. Finally, AGENTS.md §2 declares two rules — every claimable
task carries `evidence_required`, and exactly one `gates_owner` — that no script or
test read, so both could be violated silently.

## Decision Drivers

- **Composition safety**: the board's job is to keep parallel agents from breaking
  `main`, and independent-but-overlapping PRs are exactly that failure.
- **Evidence must be reproducible**: a number or test name in a PR body is only
  evidence if a command re-derives it from the tree (AGENTS.md §2).
- **Zero new dependencies**: the board CLI is stdlib-only and offline-first; the
  repository is the only medium shared between sandboxes.
- **No rewriting history**: the live board has legacy gaps; governance must surface
  them without mutating recorded state.

## Considered Options

1. A CI job that runs `git merge` of every open PR pair and reports conflicts.
2. Rely on reviewers to notice overlap and on PR prose for evidence.
3. Extend the board CLI with three read-only, deterministic subcommands —
   `collision` (composition preflight), `evidence` (cited-name resolver), and
   `validate` (executable governance invariants) — plus claim-time refusal.

## Decision Outcome

Chosen option: **3**, because it keeps the answer inside the shared, offline,
zero-dependency medium the protocol already depends on, and because it converts
both failure modes into *executable* checks rather than prose discipline.

`collision` computes each pair's merge base from git objects and classifies the
overlap as `SAFE_INDEPENDENT`, `SAFE_OVERLAP`, `REQUIRES_MANUAL_RECONCILIATION`,
or `SECURITY_SENSITIVE_COLLISION` (a shared file in a security-sensitive zone —
`security`/`gate`/`contract`/`trust`/`auth`). It never merges or resolves.
`stacked` is reported but informational: stacked PRs still must merge cleanly, so
stacking does not lower a classification. `evidence` resolves each cited test name
against a ref (`tests/**/NAME.py` or `def NAME(`). `validate` fails on zero or
multiple active `gates_owner` holders and on missing `evidence_required`
(ERROR for `next_work` and active claims; WARN for grandfathered legacy); `claim`
refuses a task that has no `evidence_required`.

### Consequences

- (+) Two correct PRs that would compose badly fail a preflight instead of
  surfacing as a silent regression after merge.
- (+) A PR body can be checked mechanically; a phantom test name is a `1` exit.
- (+) The two AGENTS.md §2 rules become executed, not documented.
- (−) `collision` needs both refs' objects present; in a shallow clone
  `--is-ancestor` can report a false non-stack, which is why `stacked` is
  informational and the security/conflict classification never depends on it.
- (~) Legacy claimable tasks without `evidence_required` stay green (WARN) until
  they are claimed, at which point they must be backfilled.

## Confirmation

- `tests/unit/test_agent_board_collision.py` — every classification, the
  security-zone derivation from the board, the coordination-file exclusion, the
  determinism of the report, and the CLI exit codes. Mutation proofs: neutering
  `SECURITY_ZONE_HINTS` and removing `COORDINATION_FILES` each turn it red.
- `tests/unit/test_agent_board_governance.py` — `evidence_required` severity by
  claim state, `gates_owner` cardinality (0/1/2, non-active holders excluded),
  claim-time refusal, and the evidence resolver (real/phantom/deterministic).
  Mutation proofs: removing the cardinality check or the claim-time refusal each
  turn it red.
- `python scripts/agent_board.py validate` on the live board → 0 errors;
  `collision --refs 830edbc,75f646a` → `SECURITY_SENSITIVE_COLLISION` (exit 1);
  `evidence --ref 830edbc --names <phantom>` → exit 1.

## Pros and Cons of the Options

### Option 1 — merge every open PR pair in CI

- (+) closest to reality (it actually merges)
- (−) mutating and slow; needs a checkout of every head; reports only after CI,
  not before a push; and a merge attempt can leave a dirty worktree

### Option 2 — reviewer discipline

- (+) zero build
- (−) exactly the discipline that failed: the phantom citations and the
  unread `gates_owner`/`evidence_required` rules are both "a human should have
  noticed"

### Option 3 — read-only CLI subcommands (chosen)

- (+) offline, stdlib-only, deterministic, usable pre-push and in CI
- (−) classifies rather than merges, so a `SAFE_OVERLAP` still needs a real merge
  in the owning PR

## More Information

- [`../../../AGENTS.md`](../../../AGENTS.md) — §2 evidence rules, §4 one gates owner
- [`../../MULTI_AGENT_PROTOCOL.md`](../../MULTI_AGENT_PROTOCOL.md) §4 — command reference
- [`../../../scripts/agent_board.py`](../../../scripts/agent_board.py) — the CLI
- [`0004-board-schema-2.md`](0004-board-schema-2.md) — the schema these fields live in
- Board tasks `task-227` (collision preflight), `task-228` (governance invariants)
