---
status: accepted
date: 2026-10-15
deciders: arena/26f7d958-nexus-ai-agent (task-270 full-audit remediation, gates owner; owner brief "Overnight Autonomous Stabilization" §3)
consulted: git-push(1) `--force-with-lease=<refname>:<expect>` semantics; git-update-ref/commit-tree/mktree plumbing; audit finding A-04 (`docs/audits/2026-10-15-closeout/PR_CONVERGENCE.md`); PR census 2026-10-15T20:13Z (`docs/audits/2026-10-15-overnight-stabilization/pr_census.json`)
informed: every agent session (AGENTS.md rule 1/2), repository owner
---

# 0012. The claim board lives on a coordination ref, not in the code tree

## Context and Problem Statement

Protocol v2 stored the live claim board in `.agents/board.json` and required
every claim to be *committed and pushed through the agent's own code branch*.
That put coordination state on the merge path of every pull request. The
2026-10-15 audit measured the consequence (finding **A-04**): of 63 open PRs,
**52 touch `.agents/board.json`**, **43 have it changed on both sides** of the
merge, and **7 are dirty *solely* because of the board** — code that would
merge cleanly except for a file that was never meant to be reviewed. The
multi-source referee hardening (ADR 0007, task-219 fencing) made the board
*honest* but could not make it *disjoint*: a lease recorded in a PR's tree is
simultaneously (a) untrusted content from an unreviewed branch and (b) a merge
conflict waiting for every other branch.

The owner brief asked for a structural remedy — per-claim files **or** a
dedicated coordination ref — preserving: single lease authority, compare-and-swap
updates, expiry/takeover, exclusive-path collision detection, one gates owner,
generation fencing, deterministic recovery when the remote is unavailable, and no
adoption of untrusted PR content.

## Decision Drivers

- A claim must be visible to every sandbox the moment it is written, without a
  pull request and without touching any code branch.
- Two sandboxes writing "at the same time" must resolve to exactly one winner
  (compare-and-swap), never a last-writer-wins overwrite.
- A code tree — ours, `main`, or any PR branch — must stop being a *source* of
  authority: a planted `gates_owner: true` or a world-fencing `exclusive_paths`
  entry in an unreviewed branch must grant nothing.
- Unreadable authority is exit 2, never a pass and never a silent fallback.
- Zero new dependencies; everything must be testable hermetically against a
  local bare repository.
- Existing command semantics (generation fencing, 24 h TTL, `gc`, exit codes
  0/1/2, the Persian defer template) and the governance mutation harness
  (`scripts/board_referee_mutations.py`, 14 verbatim-snippet mutants) must keep
  working unchanged.

## Considered Options

1. **Per-claim files** (`.agents/claims/<task>.json`, one file per lease).
2. **Dedicated coordination ref** `refs/nexus/board` on `origin`, written with
   git plumbing and `git push --force-with-lease=<ref>:<tip-we-read>`.
3. **GitHub Git Data API as the CAS primitive** (`PATCH /git/refs/...` without
   `force` is fast-forward-only).
4. Keep the tree file and add more referee sources (status quo plus).

## Decision Outcome

Chosen option: **2 — a dedicated coordination ref**, because it is the only
option that takes the board *off the merge path entirely* while keeping a
single, git-native, hermetically testable authority.

- **Authority**: `refs/nexus/board` on `origin`; tree of that ref contains
  `board.json` (schema 2, unchanged). Override for forks/tests:
  `NEXUS_BOARD_REF`.
- **Write path** (`claim`/`release`/`defer`, and `show` only when `gc` frees a
  lease): `git fetch origin +<ref>:<ref>` → read `<tip>:board.json` → mutate in
  memory with the *unchanged* command bodies → `hash-object`/`mktree`/
  `commit-tree -p <tip>` → `git push origin <sha>:<ref>
  --force-with-lease=<ref>:<tip>`. A rejected lease is a lost race: the command
  is **redone from a fresh read** (bounded to 3 attempts); if the redo now sees a
  live foreign lease it refuses exactly as before. Three consecutive losses →
  exit 2 with a `REFUSED` line.
- **Read path** (`check`): consults **only** the ref. Worktrees, `origin/main`
  and PR-branch boards are no longer sources; `--no-remote`, `--repo` and
  `--token` remain for `--store tree` (fixtures) only.
- **Frozen archive**: `.agents/board.json` stays in the tree at the content of
  `main@6b398565` (sha256 `94a7642f…b0a160`) so no modify/delete conflicts are
  created for the 56 PRs that still carry it; `scripts/agent_board.py guard`
  fails when it differs, and CI runs `guard` on every pull request
  (`merge-base-guard` job). Rebasing an open PR therefore means
  `git checkout origin/main -- .agents/board.json` — after which none of the
  7 board-only-dirty PRs is dirty any more.
- **Bootstrap**: `init-ref` creates the ref once from a seed board and refuses
  (exit 2) if the ref already exists. The seed on 2026-10-15T20:23Z was the
  union of all live leases across all 56 distinct open-PR boards, which was
  exactly one lease (`task-270-full-audit-remediation`, generation 3).
- **Identity**: commits on the ref use the sandbox's own git identity (a marker
  identity `nexus-agent-board` only when none is configured), so authorship of
  every claim stays attributable.

The ref is **not a branch** (`refs/nexus/*`, outside `refs/heads/`): it never
appears in branch lists, branch protection or pull requests, and pushing to it
does not push to anyone's branch.

### Consequences

- (+) A claim is one small commit on one ref; no PR, no rebase, no conflict.
  Measured acceptance: PRs whose only two-sided file is the board drop from 7 to
  0 once they restore the frozen archive (re-runnable via
  `python scripts/pr_census.py --reference 218`).
- (+) Exactly one writer wins each update by construction (`--force-with-lease`);
  the losing writer re-reads and re-decides — a stale generation or a now-live
  foreign lease is refused with the pre-existing messages.
- (+) Code trees stop being trusted: a hostile board planted in a PR cannot grant
  a lease, hide one, or claim the gates.
- (−) Writing a claim now requires network access to `origin`; offline work can
  only read a stale copy (`git show refs/nexus/board:board.json`) and must not
  pretend it is current. `--store tree` exists for fixtures and tests, and the
  CI `guard` makes any accidental tree write a red build.
- (−) The frozen archive is a deliberate fossil: it is wrong about the present
  by design, and `AGENTS.md §4` now says so.
- (~) `git clone` does not fetch `refs/nexus/*`; the CLI fetches it explicitly on
  every command. Humans use `git fetch origin +refs/nexus/board:refs/nexus/board`.
- (~) Platform/hosting constraint to confirm with the owner: the ref must stay
  writable by every agent account that may claim work (it is pushed with a
  normal `git push`, no API token). If the hosting later restricts non-branch
  refs, option 3 (fast-forward-only `PATCH /git/refs`) is the documented
  fallback with identical semantics.

## Confirmation

- `tests/unit/test_agent_board_ref.py` — a local bare origin plus clones:
  claim visible from a second clone; tree clean after claim/release; lost CAS
  race redone and refused with one commit per winner; persistent movement
  refused after 3 attempts; real `--force-with-lease` rejection without mocks;
  expired-lease takeover advances the generation and requires the fencing token;
  stale `--expected-generation` refused; unreachable origin and missing ref are
  exit 2 with nothing written; schema≠2 / non-list claims / non-JSON boards are
  exit 2; planted tree board ignored; world-fence `exclusive_paths`
  (`""`, `/`, `.`, `*`, …) inert and non-crashing; two live gates owners and an
  expired gates owner are exit 2; `--store tree` cannot touch the authority;
  `init-ref` never overwrites; `guard` pins the archive and the repository's
  archive matches the pinned digest.
- `tests/unit/test_agent_board.py` (50 tests, pinned to `NEXUS_BOARD_STORE=tree`
  by its autouse fixture) and `scripts/board_referee_mutations.py` (14/14 mutants
  killed) keep guarding the unchanged command bodies.
- CI: `merge-base-guard` runs `python scripts/agent_board.py guard` on every
  pull request.

## Pros and Cons of the Options

### 1. Per-claim files

- (+) Small diffs, fewer textual conflicts between unrelated claims.
- (−) Still flows through code PRs: every claim is a PR commit, every rebase can
  still conflict on shared arrays (`next_work`, `history`), and PR trees remain
  *sources* of authority — the untrusted-content problem is untouched.
- (−) The board's single-authority invariant becomes a directory scan with no
  compare-and-swap.

### 2. Coordination ref (chosen)

- (+) Off the merge path; git-native CAS; hermetically testable; no dependencies.
- (−) Needs network to write; needs an explicit fetch to read.

### 3. GitHub Git Data API

- (+) Fast-forward-only `PATCH` is a clean CAS; works where `git push` to custom
  refs might be restricted.
- (−) Requires a token for every claim, is not testable against a local bare
  repository, and couples coordination to one hosting provider. Kept as the
  documented fallback.

### 4. Status quo plus more sources

- (−) Measured not to work: 52/63 open PRs still collide on the file.
