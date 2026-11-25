# W11X premise audit — live truth before the grand convergence

**Status: dated record.** This document records what the W11X mission brief assumed and what the
repository actually contained at the audit moment. It is not a roadmap and not a source of truth for
current state; `docs/engineering/NAGAR_ONE_TRUTH_MATRIX.md` and `.agents/board.json` are.

- Audit date: 2026-11-25
- Base at audit time: `origin/main` @ `3ee52c4f` (`Merge pull request #356 …`)
- Base after the mid-flight merge of **#355** (`428492e2`): the branch was merged up to date, and the
  board conflict was resolved newest-state-wins with schema 2 still valid
- Auditing branch: `openhands/w11x-grand-pr-convergence-20261125`
- Clone: full clone (no `.git/shallow`), so ancestry tests are reliable here

## 1. Why this document exists

The W11X brief asked for eight checkpoints that harvest sixteen open pull requests, add six new
mathematical modules, and close a convergence ledger asserting that every legacy PR is
`SUPERSEDED_AND_HARVESTED_100_PERCENT`. Before writing any of that, the brief's premises were checked
against the tree and the GitHub API. Several premises were false.

Recording them is the point: a ledger that claims a PR was harvested when its diff was never read is a
false forensic record, and it is worse than an honest gap. The convergence ledger requested in CP-8 is
therefore **not** written in this branch.

## 2. Reproduction

```bash
git clone https://github.com/bot523h/nexus-ai-agent.git && cd nexus-ai-agent
git fetch origin --prune
git fetch origin "+refs/pull/*/head:refs/remotes/origin/pr/*"

# 1. Is a claimed PR already merged?
git merge-base --is-ancestor <sha> origin/main && echo MERGED || echo open

# 2. How big is the harvest surface?
git diff --shortstat origin/main...origin/pr/<N>

# 3. Do the brief's paths exist?
for f in src/nexus_ai_agent/provenance/outbox.py \
         src/nexus_ai_agent/provenance/spine_bridge.py \
         src/nexus_ai_agent/verification/uncertainty_gum.py \
         src/nexus_ai_agent/creative/ibpc/service.py \
         src/nexus_ai_agent/adapters/durable_plan_dedupe.py \
         src/nexus_ai_agent/governance/license_lattice.py; do
  [ -e "$f" ] && echo "EXISTS  $f" || echo "MISSING $f"
done

# 4. Does a claimed schema object exist?
grep -rn "provenance_transitions" src/ || echo "no such table anywhere"

# 5. Open PR count (the brief said 17)
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/bot523h/nexus-ai-agent/pulls?state=open&per_page=100" \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
```

## 3. Premise corrections

| Brief said | Live truth (verified) |
|---|---|
| 17 open legacy PRs | **46** open PRs at audit time |
| `#356` @ `0d37c711` needs merging | already merged; `main` HEAD **is** the #356 merge (`3ee52c4f`), and `origin/pr/356` tip `4991991a` is an ancestor of `main` |
| `#355` @ `1cbb6ad9` | real `origin/pr/355` head is **`d0b58ea9`**; no commit `1cbb6ad9` is the PR tip |
| `src/nexus_ai_agent/adapters/durable_plan_dedupe.py:6` docstring is stale | the file **did not exist** at audit time; it landed with **#355**, merged into `main` as `428492e2` while this branch was open. Its docstring then still said *"not yet wired to CommandBus"*, which is false: `compose_durable_command_bus()` injects the store into `CommandBus` as its `DurablePlanDedupePort` and `DurableStudioExecutionRuntime` builds it. Corrected (CP-1 item 3) |
| `provenance_transitions` table backs `revision_count` | **no such table**; revisions are recorded in `nexus_graph_chain` (`node_create` / `node_advance`) plus `nexus_graph_node_history` |
| CP-1 1C = new diagnostic code `GRAPH032` | `GRAPH032` is **already used** for history-metadata/monotonic-transition findings; the parity check ships as **`GRAPH034`** |
| `src/nexus_ai_agent/creative/ibpc/{service,approval,privacy,live_session}.py` | the IBPC slice lives at **`src/nexus_ai_agent/nagar/ibpc/`**; there is no `creative/ibpc/` package |
| `src/nexus_ai_agent/verification/uncertainty_gum.py` | no `verification/` package at all |
| `src/nexus_ai_agent/governance/license_lattice.py` | the license lattice lives in **`creative/composition/`** (`compiler.py`, `verifier.py`, `engine.py`) |
| `src/nexus_ai_agent/provenance/outbox.py`, `spine_bridge.py` | neither exists |
| `scripts/w4_mutations.py`, `tests/mutation/test_creative_graph_mutations.py` | neither exists (`scripts/w5_mutations.py`, `scripts/w6_mutations.py`, `scripts/w10x_scientific_mutations.py` do) |
| `docs/architecture/ARCHITECTURE_BASELINE_V3_13.md` | does not exist |

## 4. Harvest surface (measured, not estimated)

| PR | head | state | diff vs `origin/main` |
|---|---|---|---|
| #57 | `2f591618` | open | 14 files, +2375 / −9 |
| #60 | `efea9bae` | open | 29 files, +5837 / −19 |
| #63 | `3e376bea` | open | 35 files, +3710 / −40 |
| #92 | `40c11633` | open | 4 files, +143 / −2 |
| #97 | `3307d397` | open | 17 files, +997 / −1 |
| #114 | `6ca1b13b` | open | 16 files, +7792 |
| #118 | `d5a205f4` | open | 52 files, +23786 / −353 |
| #121 | `b4a59653` | open | 20 files, +4658 / −733 |
| #122 | `8b8ba31d` | open | 11 files, +3464 / −5 |
| #204 | `13078d79` | open | 4 files, +486 / −54 |
| #247 | `d9176f9b` | open | 33 files, +3817 / −49 |
| #257 | `b057ef5f` | open | 4 files, +109 / −7 |
| #273 | `9d29722e` | open | 19 files, +1898 / −1 |
| #284 | `82d9be01` | open | 13 files, +2733 / −5 |
| #327 | `d5d5ead3` | open | 73 files, +14869 / −58 |
| #355 | `d0b58ea9` | open | 26 files, +12249 / −27 |
| #356 | `4991991a` | **merged** | — |

Aggregate: roughly **100 000 added lines across 16 open PRs**. That is the real size of the "harvest
everything" request; it cannot be reviewed, upgraded, and proven in a single session, and pretending
otherwise is the failure mode this document exists to prevent.

## 5. Capability presence audit (CP-2 … CP-8 vocabulary)

`grep -rl <token> src/ tests/` file counts at the audit moment:

| Token | Files | Reading |
|---|---|---|
| `provenance_outbox` | 0 | no transactional outbox exists |
| `attempt_epoch` | 0 | attempts are not epoch-fenced in code today |
| `InvalidHeartbeatLeaseError` | 0 | no fail-closed heartbeat error type |
| `FencedAttemptCommitError` | 0 | no fenced-commit error type |
| `max_plus` / `max-plus` | 0 | no tropical critical-path calculus |
| `PrivacyConsent` | 0 | no durable consent store |
| `StudioActionPermission` | 0 | no per-action studio permission model |
| `license_lattice` / `LicenseLattice` | 6 | present (`creative/composition/`) |
| `DurableApprovalLedger` | 17 | present (`nagar/ibpc/approval.py`) |
| `LiveSession` | 10 | present |
| `AppPrincipal` | 7 | present |
| `scale_to_zero` / `scale-to-zero` | 10 | present |
| `conformal` | 5 | present (`retrieval/conformal_hybrid.py`) |
| `critical_path` | 3 | present, but not as a tropical semiring |
| `GUM` / `Welch` | 16 | partially present; no `uncertainty_gum.py` module |

So CP-2 … CP-8 are neither "all missing" nor "all done": roughly half the named capability exists under
a different path or name, and the rest is genuinely absent. Any convergence work must start from this
table rather than from the brief's file list.

## 6. What this branch actually delivers

CP-1 only, fully gated (see `.agents/board.json` → `task-w11x-cp1-provenance-hardening`):

| Fix | Behaviour | Enforcing test |
|---|---|---|
| 1A | `CreativeGraph.restore()` opens the backup source read-only (`file:…?mode=ro`, `uri=True`); a mistyped path is refused instead of being created as an empty database | `test_restore_opens_backup_source_read_only` |
| 1B | `promote_epistemic_status()` raises `GraphStateTransitionError` on a terminal node status (and on a revoked edge); no chain entry, no history row, no epistemic change | `test_epistemic_promotion_refused_on_terminal_node[superseded\|invalidated\|revoked]`, `test_epistemic_promotion_refused_on_revoked_edge` |
| 1C | `_integrity_check_conn()` emits **`GRAPH034`** when a node's `revision_count` disagrees with its `node_create`/`node_advance` chain entries since the node's most recent creation | `test_revision_count_chain_parity_is_enforced_graph034` |
| 1D (item 3) | `adapters/durable_plan_dedupe.py` docstring now states the real wiring (two seams, one store) instead of claiming it is unwired | `tests/unit/test_w4_cp1_durable_dedupe_and_approvals.py`, `tests/unit/test_w4_durable_bus_and_dedupe.py` |
| — | AST mutation campaign extended **17 → 20**: `M18` weakens the terminal guard, `M19` downgrades `mode=ro` to `mode=rwc`, `M20` weakens the `GRAPH034` condition — 20/20 killed, 0 survived, 0 invalid | `tests/unit/test_creative_graph_mutations.py` |

1C needed one design decision worth recording: parity is measured **from the node's most recent
`node_create`**. A `replace=True` journal rebuild deletes and re-creates projected nodes, so
`revision_count` restarts at 1 while the append-only hash chain legitimately retains the deleted
projection's entries. Counting from the last create keeps the invariant exact in both cases and still
catches a forged `revision_count` on a rebuilt node.

Every one of the four tests above was run against the pre-fix code and fails there, so none of them is
vacuous. 1A in particular was initially vacuous — asserting "the backup file is unchanged" passed even
with a read-write connection, because a read-only restore simply never writes. The non-vacuous form
asserts that a **missing** source is refused *and not created*, which only holds under `mode=ro`.

## 7. What remains, in dependency order

1. **CP-2** — transactional outbox (`provenance_outbox`, deterministic `outbox_event_id`,
   `OutboxReconciler` with idempotent replay), attempt-epoch fencing, and the
   `creative/spine` → `provenance` bridge. All of it is greenfield in this tree.
2. **CP-3** — harvest #327's composition runtime, then add exact-rational timeline calculus and the
   max-plus critical-path pass before `CommandBus` dispatch.
3. **CP-4** — `verification/uncertainty_gum.py` (GUM first-order propagation, Welch–Satterthwaite,
   three-valued compliance gating) and the perception pack wiring.
4. **CP-5** — live wiring of `nagar/causal_memory/do_calculus.py` (already present) into the runtime,
   plus Lagrangian budget diagnostics for the bounded director.
5. **CP-6** — nine-PR harvest; the mathematical cores it names (`riemannian_vector_fallback.py`,
   `conformal_hybrid.py`, `slo_burn_calculus.py`, the license lattice) already exist and need wiring,
   not rewriting.
6. **CP-7** — durable IBPC live sessions, single-use approval CAS, privacy consent, and
   `StudioAuthorizationService` (note `creative/studio/authorization.py` already exists).
7. **CP-8** — the convergence ledger. It can only be written **after** items 1–6 are real; each entry
   must name the module and the test that proves the harvested capability, otherwise it is fiction.

Each item should be its own PR against `main` with its own gates, per `AGENTS.md` §1 and §6.
