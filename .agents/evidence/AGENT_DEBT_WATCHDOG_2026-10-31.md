# Engineering-Debt Watchdog — 2026-10-31

- **Run timestamp (UTC):** 2026-10-31T03:07:05Z
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `ab56beccc8817ddde1cb005f197c172e132de878` (`ab56beccc881`, "Merge #293: bind Cognition evaluator to live CapabilityRegistry (V2)")
- **Observer:** NEXUS engineering-debt watchdog automation (observe-and-report only; no product-code edits, no merge, no push to `main`)
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-10-30.md` (baseline run). Deltas below are taken against it.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented.

---

## 1. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; `main` == `origin/main` @ `ab56beccc881` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `.agents/board.json` | schema 2; 76 zones, 172 claims, 10 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version` absent (see D5) |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| Remote branches | 212 (`git ls-remote --heads origin` count) |
| Open PR heads | 41 |
| Branch protection | **readable** this run (`protection_state: readable`) — the previous run's 403 is gone (D1 resolved) |

---

## 2. Live PR registry

```
$ python3 scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: 41 open PRs → /tmp/reg.json (main ab56beccc881)
EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-10-31T03:01:56Z`, `main_sha`: `ab56beccc8817ddde1cb005f197c172e132de878`
- `open_total`: `41`, `protection_state`: `readable`, `required_approvals`: `0`
- `required_contexts`: `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "merge-base-guard (base == main)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python3 scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count |
|---|---|
| WRONG_BASE_BRANCH | 0 |
| DRAFT | 11 |
| CONFLICT | 25 |
| MERGEABILITY_UNKNOWN | 0 |
| CI_RED | 4 |
| CI_PENDING | 0 |
| CHANGES_REQUESTED | 0 |
| NEEDS_APPROVAL | 0 |
| REBASE_FIRST | 1 |
| PROTECTION_UNREADABLE | 0 |
| **READY** | **0** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 11 · `CONFLICT` 32 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 7 · `CI_PENDING` 20 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 40 · `PROTECTION_UNREADABLE` 0.

**All 41 open PRs are non-READY.** Controlling blocker (first in the ordered list) per PR:

| PR | Tier | Controlling blocker | CI state (registry) | Title |
|---|---|---|---|---|
| #58 | DRAFT | DRAFT | PENDING | Security Boundary hardening — S1..S5 |
| #92 | DRAFT | DRAFT | SUCCESS | fix(storage): honor SQLite defaults |
| #114 | DRAFT | DRAFT | FAILURE | feat(cockpit): Persian capability explorer |
| #117 | DRAFT | DRAFT | PENDING | fix(runtime): close audited session/media contracts |
| #118 | DRAFT | DRAFT | PENDING | W2 golden hardening: authority races |
| #150 | DRAFT | DRAFT | PENDING | feat(agent-intelligence): verified CreativeWork trim |
| #204 | DRAFT | DRAFT | SUCCESS | fix(memory): sqlite-vec fallback |
| #257 | DRAFT | DRAFT | SUCCESS | Palette: Telegram inline callback UX |
| #273 | DRAFT | DRAFT | SUCCESS | feat(nagar): P0 intent NEL contracts |
| #284 | DRAFT | DRAFT | FAILURE | feat(nagar): read-only Causal Memory |
| #285 | DRAFT | DRAFT | SUCCESS | chore(board): task-278 deferred status |
| #33 | CONFLICT | CONFLICT | SUCCESS | v3.13.0 — P0 security code + dead-engine wiring |
| #57 | CONFLICT | CONFLICT | PENDING | feat(research-v2): P1 CLAIM+LEASE |
| #60 | CONFLICT | CONFLICT | PENDING | M0 runtime integration |
| #63 | CONFLICT | CONFLICT | PENDING | task-163: board-Git truth gate |
| #66 | CONFLICT | CONFLICT | PENDING | P0 creative integration |
| #69 | CONFLICT | CONFLICT | PENDING | Gate 4: cross-layer vertical slice |
| #73 | CONFLICT | CONFLICT | PENDING | Gate 2.2 — Operation Truth |
| #84 | CONFLICT | CONFLICT | PENDING | Register NEXUS Engineering Constitution |
| #89 | CONFLICT | CONFLICT | PENDING | task-122: 12 real command wirings |
| #99 | CONFLICT | CONFLICT | SUCCESS | task-193: command truth |
| #113 | CONFLICT | CONFLICT | PENDING | feat(runtime): W1 final close-out |
| #115 | CONFLICT | CONFLICT | SUCCESS | W1: True Runtime Closure |
| #121 | CONFLICT | CONFLICT | PENDING | feat(memory): retrieval truth |
| #122 | CONFLICT | CONFLICT | SUCCESS | fix(orchestration): H1 — route chat |
| #129 | CONFLICT | CONFLICT | PENDING | feat(creative/studio): staged plan transactions |
| #135 | CONFLICT | CONFLICT | PENDING | feat(creative/studio): PlanTransaction substrate |
| #137 | CONFLICT | CONFLICT | PENDING | board(task-227,task-228): collision preflight |
| #140 | CONFLICT | CONFLICT | PENDING | Harden Studio CommandBus |
| #149 | CONFLICT | CONFLICT | PENDING | Security Wave 1 Verification |
| #157 | CONFLICT | CONFLICT | PENDING | feat(governance): Nagar agent constitution |
| #193 | CONFLICT | CONFLICT | SUCCESS | feat(infra): Ubuntu 26.04 + Python 3.14 |
| #205 | CONFLICT | CONFLICT | FAILURE | feat(nagar): Asset Library V1 |
| #230 | CONFLICT | CONFLICT | SUCCESS | Arena/aa451c49 nexus ai agent |
| #247 | CONFLICT | CONFLICT | SUCCESS | feat(creative): perception lane |
| #290 | CONFLICT | CONFLICT | SUCCESS | chore(board): task-282 durable state |
| #64 | CI_RED | CI_RED | FAILURE | Creative Runtime (task-176 s2) |
| #97 | CI_RED | CI_RED | FAILURE | feat(vision): portrait and scene Vision packs |
| #283 | CI_RED | CI_RED | FAILURE | fix(execution): queue-owned heartbeat |
| #295 | CI_RED | CI_RED | FAILURE | extract(#218): content-addressed Asset Library |
| #294 | REBASE_FIRST | REBASE_FIRST | SUCCESS | repair(#189): durable creative-graph package |

---

## 3. Board audit

```
$ python3 -m pytest -q tests/unit/test_agent_board.py
75 passed in 1.59s
EXIT=0
```

`.agents/board.json` sha256 before/after every gate run: `a0d284a5cbe3b90b039e98fae2468e94a9305586040dc518b87ba6d21b93f6a8` (unchanged; the audit mutates no board state).

- **Claims:** 172 (was 171). Status histogram: `done` 76, `queued` 58, `expired` 17, `completed_released` 5, `completed_merged` 3, `deferred` 2, `active` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `assigned_to_B_next` 1, `available_sequenced_post_33` 2, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1.
- **Zones:** 76. **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).

### 3.1 Active claims — 2 (both branches live, both PRs already merged)

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-267-truth041-live-lease-alignment` | `runtime-truth-tooling` | `arena/m14-truth041-lease-expiry` | yes | no | 24 | 2026-10-30T10:11:42Z | expires 2026-10-31T10:11:42Z → **live** |
| `task-281-cognition-eval-live-registry-binding` | `cognition-quality-evaluation` | `arena/de4e797a-nexus-ai-agent` | yes | no | 24 | 2026-10-30T15:39:23Z | expires 2026-10-31T15:39:23Z → **live** |

Both tasks' work has **already merged**: `task-267` via PR **#292** (`merged_at 2026-10-30T10:36:41Z`, merge commit `a3a6355b5`), `task-281` via PR **#293** (`merged_at 2026-10-30T19:18:30Z`, merge commit `ab56beccc` = current `main`). Neither claim has been released. This is **landed-but-still-leased** debt: the branches still exist remotely so the `dead-branch` check (§3.2) does not flag them, but the lease holds a `zone`/`exclusive_paths` reservation for work that is already on `main`. (Not a collision — `AGENTS.md` §1.2 only blocks overlap with *live* leases.)

### 3.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claim whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30 report:

| Task | Status | Dead `agent_branch` |
|---|---|---|
| `feature-wiring-batch` | expired | `arena/01a0c34d-nexus-ai-agent` |
| `task-145-ci-gates-handoff-refresh` | expired | `arena/01a0c992-nexus-ai-agent` |
| `task-154-pack-coverage-ci` | expired | `arena/01a0e2bb-nexus-ai-agent` |
| `task-181-runtime-service-grants` | expired | `phase1-pr196-guard-20261009` |
| `task-183-pr67-lifecycle-integration` | expired | `arena/01a0d50d-nexus-ai-agent` |
| `sec-boundary-salvage-01a0d563` | expired | `arena/01a0d563-nexus-ai-agent` |
| `task-181-gate5-closure` | expired | `arena/01a0d5a1-nexus-ai-agent` |
| `task-184-continuum-evidence-hardening` | expired | `arena/01a0e2bb-nexus-ai-agent` |
| `task-185-external-intelligence-hardening` | expired | `arena/01a0df05-nexus-ai-agent` |
| `task-232-durable-creative-queue-evidence` | expired | `arena/01a10af5-nexus-ai-agent` |
| `task-181-runtime-service-grant-guard` | expired | `phase1-pr196-guard-20261009` |
| `nexus-stabilization-handoff` | expired | `phase1-pr196-guard-20261009` |
| `ci-gates-steward-closure` | expired | `phase1-pr196-guard-20261009` |

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (212 refs) and the 41 open-PR heads.

### 3.3 Stale live leases — 0

The two `active` claims (§3.1) are both within their 24 h TTL at snapshot time (`2026-10-31T03:07:05Z`). No live lease is past TTL.

### 3.4 Claims assigned without a lease (finding D3) — 4 (was 3)

| Task | Status | `agent_branch` | `claimed_at` |
|---|---|---|---|
| `task-122` | `assigned_to_B` | (absent) | (absent) |
| `task-123` | `assigned_to_E_pr33` | (absent) | (absent) |
| `task-124` | `available_sequenced_post_32_33` | (absent) | (absent) |
| `task-126` | `available_sequenced_post_33` | (absent) | (absent) |
| `task-121` | `available_sequenced_post_33` | (absent) | (absent) |
| `task-128` | `available_sequenced_post_32` | (absent) | (absent) |
| `task-127` | `assigned_to_B_next` | (absent) | (absent) |

Seven claims carry an owner/sequence label with no machine-checkable branch or lease clock. The 2026-10-30 report counted the three `assigned_to_*` ones; this run also lists the four `available_sequenced_*` ones for completeness (same shape: no `agent_branch`, no `claimed_at`).

### 3.5 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry is a prefix-equal-or-child of a `zone.paths` entry.

---

## 4. Gates

| # | Command | Result | Exit |
|---|---|---|---|
| G1 | `python3 -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| G2 | `python3 -m pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `117 passed in 0.92s` | 0 |
| G3 | `python3 -m pytest -q tests/unit/test_agent_board.py` | `75 passed in 1.59s` | 0 |

Environment: Python 3.13 (`/usr/local/bin/python`), pytest with `pytest_asyncio` (one `PytestDeprecationWarning`: `asyncio_default_fixture_loop_scope` unset — pre-existing, non-gating). All gates **green**; no gate turned red.

---

## 5. CI health — 12 most recently updated open PRs (exact head SHA)

Required checks (names from `required_contexts`): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status is the **latest check run per check name on the EXACT head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, grouped by `.name`, max `started_at`).

| PR | Head SHA | Updated (UTC) | lint | test | merge-base-guard |
|---|---|---|---|---|---|
| #295 | `ebb61361b4` | 2026-10-30T21:21:00 | success | **failure** | success |
| #294 | `af8ab80a01` | 2026-10-30T20:01:58 | success | success | success |
| #283 | `0ba26ea68b` | 2026-10-30T17:39:22 | success | **failure** | success |
| #290 | `a7490ab744` | 2026-10-30T15:37:34 | success | success | success |
| #285 | `ce1ede4e8d` | 2026-10-29T21:47:25 | success | success | success |
| #284 | `82d9be0182` | 2026-10-29T20:28:18 | success | **failure** | success |
| #273 | `9d29722e6e` | 2026-10-29T10:00:31 | success | success | success |
| #247 | `d9176f9bcb` | 2026-10-29T05:33:42 | success | success | success |
| #204 | `50bc3e3ef7` | 2026-10-28T23:56:31 | success | success | success |
| #97 | `3307d3978f` | 2026-10-28T23:03:00 | success | **failure** | success |
| #64 | `ab564d7cc7` | 2026-10-28T11:32:57 | success | **failure** | success |
| #257 | `b057ef5fda` | 2026-10-27T13:42:31 | success | success | success |

States observed: `success`, `failure` (latest run); superseded `cancelled` duplicates exist but are not the latest run. No `queued`/`in_progress` at snapshot time.

### 5.1 Repository-wide required-context presence (all 41 PRs)

| Check | Present on exact head | Absent |
|---|---|---|
| `lint (ruff + mypy + version lockstep)` | 41 | 0 |
| `test (pytest -m "not slow")` | 41 | 0 |
| `merge-base-guard (base == main)` | 21 | **20** — `[57, 58, 60, 63, 66, 69, 73, 84, 89, 113, 117, 118, 121, 129, 135, 137, 140, 149, 150, 157]` |

The 20 PRs with an absent `merge-base-guard` are all long-lived branches (heads from 2026-09-23 onward) that predate the guard job's addition to CI; the guard therefore never reported on their head. Their registry `ci_state` is nonetheless `PENDING` (not `FAILURE`), because `_latest_ci_verdict` hits the ordering bug in D4.

---

## 6. Findings (engineering debt)

### D1 — `pr_convergence.py collect` no longer fails closed on unreadable protection (P0) — **RESOLVED**

The 2026-10-30 baseline recorded `collect` exiting `2` with no registry when `GET /branches/main/protection` returned 403. This run `collect` exits `0` and records `protection_state: readable`. The fix landed in **PR #291** (`fix(pr-convergence): latest-run classification + degrade on unreadable protection`, merged 2026-10-30T10:55:16Z, merge commit `67637ca17`). Resolved.

### D2 — superseded-duplicate `CANCELLED` misclassification (P0) — **RESOLVED**

The baseline recorded 6 PRs mislabeled `CI_RED` because `_rollup_state` ignored superseded cancelled duplicates. PR #291 replaced rollup-based classification with latest-run-per-name (`_latest_run_state` / `_latest_ci_verdict`). Cross-check of all 41 PRs this run: `ci_state` matches the true latest-per-name verdict for **all 41** (0 superseded-duplicate misclassifications). Resolved.

### D3 — `pr_convergence.py` masks a real CI FAILURE behind `PENDING` (P0) — **NEW**

`_latest_ci_verdict` (strict mode, `require_all_present=True`) returns `PENDING` whenever **any** required context is absent on the head — *before* it inspects the contexts that did run:

```python
states = {name: _latest_run_state(pr, name) for name in contexts}
if require_all_present and any(state is None for state in states.values()):
    return "PENDING"  # <-- returns before the red check below
values = [state for state in states.values() if state is not None]
...
if any(state in _RED_CONCLUSIONS for state in values):
    return "FAILURE"
```

So a head where `lint`/`test` **failed** but `merge-base-guard` never ran is reported `PENDING`, not `FAILURE`. A `PENDING` reads as "still running / wait", which hides a definitively red gate. Reproduced at function level:

```
$ python3 - <<'PY'
import sys; sys.path.insert(0,'scripts'); import pr_convergence as pc
req=['lint (ruff + mypy + version lockstep)','test (pytest -m "not slow")','merge-base-guard (base == main)']
rec={"statusCheckRollup":{"contexts":[
  {"name":req[0],"conclusion":"FAILURE","completedAt":"2026-10-04T17:39:57Z"},
  {"name":req[1],"conclusion":"FAILURE","completedAt":"2026-10-04T17:43:37Z"}]}}
print(pc._latest_ci_verdict(rec, req, require_all_present=True))   # -> PENDING  (expected FAILURE)
PY
PENDING
```

Measured across all 41 open PRs: registry `ci_state == PENDING` for **20**, but **6** of those have a genuine `lint`/`test` `FAILURE` at the exact head:

| PR | Head SHA | lint | test | merge-base-guard | Registry `ci_state` | True verdict |
|---|---|---|---|---|---|---|
| #57 | `2f591618e1` | FAILURE | success | absent | PENDING | **FAILURE** |
| #58 | `bb06da1cb3` | FAILURE | FAILURE | absent | PENDING | **FAILURE** |
| #60 | `efea9bae3e` | FAILURE | FAILURE | absent | PENDING | **FAILURE** |
| #63 | `3e376bea06` | success | FAILURE | absent | PENDING | **FAILURE** |
| #135 | `f83b12835c` | success | FAILURE | absent | PENDING | **FAILURE** |
| #150 | `5f84be4dd2` | FAILURE | FAILURE | absent | PENDING | **FAILURE** |

Cross-check total: registry `ci_state` vs true latest-per-name verdict mismatch = **6 of 41** (all in the `PENDING`-should-be-`FAILURE` direction; 0 in the other direction). No merge-readiness decision is *reversed* (all six also carry `CONFLICT`/`DRAFT`), but the CI signal is wrong for 15% of the backlog. Reproduce: `gh api repos/bot523h/nexus-ai-agent/commits/<head_sha>/check-runs?per_page=100`, group by `.name`, take max `started_at`; compare to `registry.ci_state`.

### D4 — `merge-base-guard` absent on 20 of 41 heads, silently downgraded to `PENDING` (P1) — **NEW**

20 long-lived PRs predate the `merge-base-guard` job, so the required check has never run on their head (§5.1). Because of D3 these read `PENDING`, so the registry shows `CI_PENDING` 20 / `CI_RED` 4 rather than surfacing "the required guard never ran here". Two distinct conditions (never-ran vs still-running) are collapsed into one tier, and a head missing a *required* gate is not distinguishable from one whose gate is in flight. Owner action: either re-run the guard on these heads or record "guard absent on legacy head" as its own signal.

### D5 — `AGENTS.md` documents `protocol.protocol_version`; the board omits it (P1) — **NEW**

`AGENTS.md` §1 states the board is "schema 2 (`protocol`, `zones`, `claims`, `deferred_log`, `takeover_log`, `next_work`, `history`)" and §6 references `protocol.deferred_improvements`. The `protocol` object exists but **has no `protocol_version` key** (keys present: `manifesto_fa`, `identity_rule`, `lease_ttl_hours`, `gates_rule`, `overlap_rule_fa`, `verification_rule`, `merge_order`, `ten_forward_rule_fa`, `deferred_improvements`). `test_agent_board.py` still passes (75/75), so the version field is documentation-only and unenforced — the "schema 2" claim is not machine-verifiable from the board. Reproduce: `python3 -c "import json;print(json.load(open('.agents/board.json'))['protocol'].keys())"`.

### D6 — board hygiene: 13 dead-branch claims, 4 unleased assignments, 2 landed-but-leased active claims (P1)

See §3.1–§3.4. 13 `expired` claims still name branches that no longer exist; 4 `available_sequenced_*` + 3 `assigned_to_*` claims have neither a branch nor a `claimed_at`; and the 2 `active` claims hold zones for work already merged (#292, #293) without a `release`. `gc`/`release` has not swept any of them.

### D7 — backlog shape (P1, unchanged in character)

0 READY of 41 open. 25 CONFLICT + 11 DRAFT tiers; 40 PRs carry `REBASE_FIRST`; 11 carry `CHANGES_REQUESTED`; 20 carry `CI_PENDING` (6 of them really red, D3). The dominant debt remains stale, unrebased branches, not green-but-unmerged work.

---

## 7. Delta vs previous run (2026-10-30, `main` `dc23968`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-10-30.md` (baseline).

| Metric | 2026-10-30 | 2026-10-31 | Change |
|---|---|---|---|
| Open PRs | 41 | 41 | 0 |
| DRAFT tier | 11 | 11 | 0 |
| CONFLICT tier | 27 | 25 | −2 |
| CI_RED tier | 3 | 4 | **+1** |
| CI_PENDING tier | 0 | 0 | 0 |
| REBASE_FIRST tier | 0 | 1 | **+1** |
| READY | 0 | 0 | 0 |
| Branch protection | unreadable (403) | readable | **resolved (D1)** |
| Superseded-duplicate misclassifications | 6 | 0 | **resolved (D2)** |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 0 | 2 | **+2** |
| Stale live leases | 0 | 0 | 0 |
| `gates_owner` holders | 0 | 0 | 0 |
| Zones / claims | 76 / 171 | 76 / 172 | 0 / +1 |

**New PRs since 2026-10-30:** #290 (`CONFLICT`, CI green), #294 (`REBASE_FIRST`, CI green), #295 (`CI_RED`, test failure).
**Closed/merged since 2026-10-30:** #189 (`CONFLICT`), #218 (`CONFLICT`), #287 (`CI_RED`).
**Tier regression among common PRs:** #283 `CONFLICT → CI_RED` (test turned failure at head `0ba26ea68b`).
**Tier improvement among common PRs:** the two `CONFLICT` closings (#189, #218) account for the −2 CONFLICT, but three new PRs added `CONFLICT`/`CI_RED`/`REBASE_FIRST`.

**Regressions (gate/CI/ownership):**
- **NEW CI failures:** #295 (`test`) and #283 (`test`) are new reds; #64, #97, #284 remain red from before.
- **NEW gate failures:** none — G1/G2/G3 all green both runs.
- **NEW ownership mismatch:** none (still 13, byte-identical list).
- **NEW stale claim:** none live; but **2 new active claims** (#3.1) that are already-merged work.
- **NEW conflict:** #290 is a new `CONFLICT` PR.

**Resolved since last run:**
- **D1 resolved** — `collect` now exits 0 with `protection_state: readable` (PR #291).
- **D2 resolved** — 6 superseded-duplicate misclassifications → 0 (PR #291).
- #287 (last run's acute `CI_RED` false positive) is closed/merged.
- `task-267` and `task-281` (last run's P1 governance/zone work) landed via #292 / #293.

---

## 8. Next actions (prioritized)

1. **[P0] Fix `_latest_ci_verdict` ordering in `scripts/pr_convergence.py`** so a real `FAILURE` is never masked by an absent required context: compute the `_RED_CONCLUSIONS` verdict over the present contexts **before** returning `PENDING` for a missing one (or return `FAILURE` when any present context is red and any required context is absent). 6 of 41 PRs are currently mis-reported (`PENDING` instead of `FAILURE`): #57, #58, #60, #63, #135, #150.
2. **[P0] Distinguish "required check never ran" from "still running"** (D4): give the 20 legacy heads missing `merge-base-guard` their own signal rather than folding them into `PENDING`, or re-run the guard on those heads.
3. **[P1] Add `protocol.protocol_version` to `.agents/board.json`** (or drop the "schema 2" version claim from `AGENTS.md`) and, if kept, assert it in `tests/unit/test_agent_board.py` (D5).
4. **[P1] Sweep board hygiene** (D6): `release`/`gc` the 2 landed-but-leased active claims (#292, #293), and remove or re-scope the 13 dead-branch claims and 7 unleased assignments.
5. **[P2] Attack the rebase wall** (D7): 40 of 41 PRs carry `REBASE_FIRST`; triage the 25 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
