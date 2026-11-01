# Phase 0 — Forensic baseline & phased plan (2026-10-11)

Session branch (canonical identity): `arena/b561a2be-nexus-ai-agent`
Author: this session only. No other agent's file was modified, no PR was merged/closed,
no branch/tag was deleted, no force-push was performed.

Every claim below is reproducible from the commands shown. **A green CI on an older SHA
is not evidence for a newer one**, and no finding here is inherited from a previous report.

---

## A. Live Git / GitHub truth

| Fact | Value | Command |
|---|---|---|
| `origin/main` SHA | `6b94f3d244a42a2e6515ddc575411a7f00c90aad` | `git rev-parse HEAD` / `gh api` |
| Local branch | `arena/b561a2be-nexus-ai-agent` @ `6b94f3d` | `git branch -vv` |
| Worktree state | clean (no tracked/untracked changes at session start) | `git status --porcelain` |
| Clone depth | **shallow — 1 commit**… | `git log --oneline \| wc -l` = 1 |
| Open PRs | **55** (34 `app/arena-ai-coding-agent`, 20 `bot523h`, 1 `app/freebuff-web`) | `gh pr list --state open --limit 200` |
| PR base branch | all 55 → `main` | `gh api .../pulls` |
| `main` protection | enabled; required checks `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`; enforcement `everyone` | `gh api repos/.../branches/main` |

### PR census (evidence: `/tmp` scratch, reproduced via `gh api`)

| Class | Count | Numbers |
|---|---|---|
| `CONFLICTING` / `DIRTY` | 45 | 33, 56–60, 63, 64, 66, 69, 70, 73, 83, 84, 87–89, 92, 96, 97, 99, 100, 104, 111, 113, 114, 117, 118, 121, 124, 125, 129, 134, 135, 137, 139, 140, 144, 149, 150, 157, 158, 173, 189, 193 |
| `MERGEABLE` / `CLEAN` | 4 | 195, 199, 200, 201 |
| `MERGEABLE` / `BLOCKED` (review/checks pending) | 3 | 196, 197, 198 |
| `MERGEABLE` / `BEHIND` | 3 | 115, 122, 190 |
| Drafts | 6 | 58, 92, 114, 117, 118, 150 |

Categorisation (analysis only — **no merge/close authority is implied or exercised**):

- `READY_FOR_REVIEW`: **195, 199, 200, 201** — base SHA == current `main`, `CLEAN`, required
  checks green. (#199/#201 show `rollup=FAILURE` only because a second concurrency-cancelled
  run left `CANCELLED` check-runs on the same head SHA; the required contexts pass and
  GitHub reports `CLEAN`.)
- `BLOCKED_BY_DEPENDENCY`: **196** (service grants) → **197** (the guard test for it), **198**;
  all three are `MERGEABLE` on a current base but need the stack to land in order.
- `BEHIND`: **115, 122, 190** — mergeable, base drifted, needs a rebase decision by its owner.
- `POTENTIALLY_STALE` / `CLOSE_CANDIDATE — REVIEW REQUIRED`: **33, 56–60, 63, 64, 66, 69, 70,
  73, 83, 84, 87–89, 92, 96, 97, 99, 100, 104, 111, 113, 114, 117, 118, 121, 124, 125, 129,
  134, 135, 137, 139, 140, 144, 149, 150, 157, 158, 173, 189** — 45 conflicting PRs, 14 of them
  last touched on 2026-09-28 by the same batch, several superseded by work already on `main`.
- `DUPLICATE_OR_OVERLAPPING`: **137 ⊃ 84** and **137 ⊃ 117** on
  `scripts/agent_board.py` + `tests/unit/test_agent_board.py`; **135 ≈ 129 ≈ 140** on the
  Studio `PlanTransaction` substrate; **140** ≈ merged #183/#128 (task-222/223).
- `CI_BLOCKED`: 33, 57, 58, 60, 63, 64, 87, 92, 113, 134, 135, 150, 193 (failing `lint`/`test`/
  `python-parity`), plus the `CANCELLED`-only rollups noted above.
- `UNKNOWN / NOT OBSERVED`: review state (approvals/change-requests) was **not** read — GitHub
  report `reviewDecision` was not queried. Treat review status as unobserved, not as clean.

### Collision map (files touched by more than one open PR; full map: 162 files)

| File | # open PRs | Governance relevance |
|---|---|---|
| `.agents/board.json` | 45 | shared coordination medium (expected) |
| `docs/README.md` | 24 | docs index — a conflict sink |
| `docs/DECISION_LOG.md` | 13 | decisions |
| `src/nexus_ai_agent/bot/handlers.py` | 8 | known highest-conflict file |
| `.github/workflows/ci.yml` | 8 | CI |
| **`scripts/agent_board.py`** | **3 (84, 117, 137)** | **referee CLI** |
| **`tests/unit/test_agent_board.py`** | **2 (84, 137)** | **referee tests** |
| **`src/nexus_ai_agent/diagnostics/truth.py`** | **1 (189)** | **board truth validator** |
| `AGENTS.md` | 4 (84, 117, 157, 189) | coordination contract |

---

## B. Governance truth at session start

- `.agents/board.json` — schema 2, `updated_at = 2026-10-09T00:17:05Z`, **94 claims**.
- **Live leases: 0.** Status histogram: `done` 48, `queued` 17, `expired` 11,
  `completed_*` 9, `available_sequenced_*` 4, `assigned_*` 3, `deferred` 1, `superseded` 1.
  Only 11 of 94 claims have ever carried `claimed_at`.
- **`gates_owner: true` claims: 0.**
  (`_active_gates_owners()` returns `(0 owners, 0 malformed)` — command below.)

```
python - <<'PY'
import json,sys; sys.path.insert(0,'scripts'); import agent_board as ab
b=json.load(open('.agents/board.json'))
print(len([c for c in b['claims'] if ab._claim_live(c)]))       # 0 live claims
print(len(ab._active_gates_owners(b)[0]))                       # 0 gates owners
PY
```

**Is zero gates owners a validator defect? Evidence says no.** The gate is *lease-scoped*:
`claim --gates` sets it, and `gc_expired` / `release` / `defer` clear it
(`scripts/agent_board.py:198,402,432,469`). AGENTS.md line 74 records the previous holder
(`ci-gates-steward`) as `completed_released` with "no live gates_owner". Zero is therefore the
documented *idle* state, and `check` treats it as fail-closed (`exit 2`) rather than a pass
(`scripts/agent_board.py:781-791`) — that is correct behaviour, not the bug.

```
$ python scripts/agent_board.py check --files src/nexus_ai_agent/diagnostics/truth.py \
      --branch arena/b561a2be-nexus-ai-agent
UNVERIFIABLE: exactly one active, unexpired gates_owner is required; found 0
  (zero or multiple is fail-closed)                                             EXIT=2
$ ... same command with --no-remote
no overlap — safe to proceed (every consulted source was readable).            EXIT=0
```

---

## C. Confirmed defects (ranked by evidence strength × blast radius)

### C-1 [P0, PROVEN RED] The gate bootstrap silently no-ops on an existing live lease

`_claim_locked` honours `--gates` on the *fresh/takeover* path but **ignores it entirely on the
same-owner heartbeat path** (`scripts/agent_board.py:346-361`): the flag is never read, and the
command still prints `renewed lease … generation N` and **exits 0**.

Reproduction (stdlib import-by-path, `BOARD` pointed at a temp copy — the real board is not
touched; probe script is in the session scratch, the assertion is reproduced as a regression
test in this PR):

```
BEFORE: status=active gates_owner=False generation=0
$ claim synthetic-dir-claim --branch arena/111-someone-else --gates
renewed lease for synthetic-dir-claim (owner: arena/111-someone-else, generation 0 — heartbeat keeps the epoch)
exit code: 0
AFTER : status=active gates_owner=False generation=0   -> live gates owners: 0
```

Blast radius — this is the *documented onboarding loop*:

1. `AGENTS.md` §1 and `docs/MULTI_AGENT_PROTOCOL.md` §4 tell an arriving agent to run
   `next` → `claim <task> --branch <branch>`; `cmd_next` prints that command **without
   `--gates`** (`scripts/agent_board.py:494`).
2. Rule 4 / `--help` then say the way to own the CI gate is `claim … --gates`. The agent is
   already the live holder of that lease, so the command takes the heartbeat path.
3. Result: exit 0 + a success line, and the board still holds **zero** gates owners. Every
   other agent's non-`--no-remote` `check` keeps returning `exit 2`, and the PR body's claim
   "I own the gates" is false.

This is silent intent loss with a success status — precisely the failure class the mission's
invariants forbid. It is *not* covered by any open PR (see D.2).

### C-2 [P1, PROVEN, DEFERRED — covered by open PR #189] `TRUTH041` disagrees with the referee

`src/nexus_ai_agent/diagnostics/truth.py:571` flags `gates_owner and status != "active"`, but the
referee treats `active_in_review` as a live lease (`ACTIVE_STATUSES`,
`scripts/agent_board.py:98`) and counts such a claim as the one valid gates owner. The two
validators therefore disagree about the same board record, in both directions:

| Board record | referee (`_active_gates_owners`) | truth doctor (`TRUTH041`) |
|---|---|---|
| `active_in_review` + `gates_owner` + unexpired | **valid owner** (1) | **ERROR** |
| `active` + `gates_owner` + *expired* by wall clock | not an owner (0) | **clean** |

`tests/architecture/test_repo_truth_consistency.py::test_real_repository_has_no_truth_findings`
runs the doctor against the real tree in CI, so the first row is a red main for a state the
referee accepts.

**Not fixed here — already implemented by open PR #189** (`LIVE_LEASE_STATUSES` + a
cross-module equality test + a positive `active_in_review` test + a negative deferred-lease
test). Duplicating it would create exactly the merge collision this protocol exists to prevent.
Recorded as `DEFERRED` in the board with `resume_when: PR#189 lands or is closed`.

### C-3 [P1, PROVEN, DEFERRED — covered by open PR #137] No repo-level invariant for "at most one gate"

Nothing in the test suite asserts the gate invariant as a property of the board file: the
refusal branch in `check` (`len(owners) != 1`) has **no test at all**, and the only gate-related
board test is the GC case. PR #137 adds a `validate` command with "exactly one active claim may
hold gates_owner" plus a collision preflight. Scope overlap → this session adds only the
*missing boundary coverage* for the referee's own fail-closed contract (0 owners, ≥2 owners),
which #137's `validate` does not touch, and leaves `validate` itself to #137.

### C-4 [P1, PROVEN] Container still runs as root

`Dockerfile` has **no `USER` directive** (`grep -n USER Dockerfile` → nothing) and
`tests/unit/test_dockerfile_contract.py` has no root/non-root assertion
(`grep -n "USER\|non-root\|root\|uid"` → nothing). `docker-compose.yml` binds the dashboard to
`127.0.0.1:8000` (good) but mounts `./data` and `./assets`, so a non-root user must own those
paths before the change is safe. No open PR touches `Dockerfile` except #193 (a whole-base-image
replacement, `CONFLICTING/DIRTY`, failing `runtime-foundation`). **Deferred to its own PR** after
Phase 1 — it needs the runtime/FFmpeg path tested, not asserted.

---

## D. Decision: what this session executes

### D.1 Task selection (mission §5 ordering applied to live evidence)

1. *Blocking governance defect* → **C-1** (unowned, unclaimed, no open PR covers it). ✅ chosen
2. *Container hardening* → C-4, deferred (needs its own slice + FFmpeg runtime proof).
3. *Backup/restore proof* → blocked at the owner-secret boundary (`defer` entry in the board:
   `task-164`, "owner sets NEXUS_R2_* / Neon secrets" — not agent-actionable).
4. *Durable/idempotent execution invariants* → PRs #196/#197/#198 hold the open stack; do not
   enter.
5. *Observability / Web Studio* → after the above.

### D.2 Collision check before coding

- `.agents/board.json` live leases: **0** → the board fences nothing today.
- Open PRs touching `scripts/agent_board.py`: **84, 117, 137** — all `CONFLICTING/DIRTY`,
  none with a board claim, none merged; **137** is the only one that adds gate invariants and
  it is already `DEFERRED` in this plan (C-3) so the two do not overlap in content.
- `src/nexus_ai_agent/diagnostics/truth.py`: **189** → C-2 deferred, file **not touched** here.
- Every open PR's *pushed board* was consulted by the referee itself (`check` without
  `--no-remote`, evidence in §F) rather than assumed clean.

### D.3 The phased plan

```
[ ] Phase 1  P0 governance repair — C-1
    Goal        `claim --gates` on an already-live own lease must take the gate or refuse;
                never report success without the effect.
    Acceptance  (a) heartbeat + --gates on a gate-less live own lease -> gates_owner True,
                    exactly one live owner, others cleared;
                (b) heartbeat + --gates when already the owner -> idempotent, one owner;
                (c) heartbeat without --gates -> flag unchanged (no silent drop);
                (d) stale generation + --gates -> refused (exit 2), board byte-identical;
                (e) referee fail-closed at 0 owners and at 2 owners (new coverage);
                (f) mutation campaign kills the new guards.
    Paths       scripts/agent_board.py, tests/unit/test_agent_board.py,
                scripts/board_referee_mutations.py, docs/MULTI_AGENT_PROTOCOL.md
                (all inside the `coordination-referee` zone)
    Forbidden   src/nexus_ai_agent/diagnostics/truth.py, tests/unit/test_truth_doctor.py (PR#189);
                AGENTS.md §4 restructure (PRs #84/#117/#157/#189 all touch it)
    Tests       tests/unit/test_agent_board.py (the file the mutation harness runs)
    Risk        the referee is the tool every agent uses — a wrong change blocks the whole
                repo. Rollback = revert the PR; the board file is untouched by the code fix.
    Evidence    RED probe before, GREEN regression tests after, N/N mutations killed,
                full `make lint && make types && make test`, exact run URLs on the final SHA.

[ ] Phase 2  Container hardening — C-4 (own PR, after Phase 1 lands)
    Goal        non-root runtime UID, correct ownership of writable paths, real healthcheck,
                graceful shutdown, no root-owned persistent writes.
    Tests       build from a clean tree, run as the non-root UID, FFmpeg encode path,
                healthcheck success/failure, volume persistence, existing suite green.
    Forbidden   Dockerfile is otherwise free; #193 must not be touched.

[x] Phase 3..7  scheduled behind 1-2, gated on live truth + path ownership at that time:
    backup/restore drill level-2 (blocked on owner secrets -> then ISOLATED_RESTORE_VERIFIED),
    execution-core invariants (blocked by open #196/#197/#198), observability correlation IDs,
    supply-chain pinning/SBOM (needs a runnable tool, not a YAML).
```

### D.4 Claim boundary for this session

Task `task-266-gate-bootstrap-integrity` (new, zone `coordination-referee`) with explicit
`exclusive_paths` and `acceptance_criteria` / `evidence_required`, claimed through the CLI with
`--gates` so that the repository holds **exactly one live, unexpired gates owner** while this
main-bound work runs — the invariant the board is supposed to guarantee. The claim is committed
and pushed immediately, because an unpushed claim does not exist for the other sandbox.

---

## E. Explicit limits of this baseline

- The clone is **shallow (1 commit)**; local `git log`-based history is unavailable, so repository
  forensics here use GitHub's API rather than local history. Stated rather than papered over.
- PR **review** state (approvals / requested changes) was not read → `UNKNOWN`.
- CI evidence is per-head-SHA from `gh`; the `CANCELLED` check-runs on #199/#201 are a
  concurrency artefact of the push+pull_request double trigger, not a code failure — verified by
  the required contexts passing on the same head SHA and `mergeStateStatus=CLEAN`.
- No production, R2, Neon or Telegram credential was used or is available in this session, so
  nothing here claims `PRODUCTION_RECOVERY_DRILL_VERIFIED` or any live-service verification.
