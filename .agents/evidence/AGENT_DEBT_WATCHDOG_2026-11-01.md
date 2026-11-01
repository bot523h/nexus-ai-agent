# Engineering-Debt Watchdog — 2026-11-01

- **Run timestamp (UTC):** 2026-11-01T02:53:31Z
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `48aae28a4ef4a00459530bb177ce4fe4e43c0ad3` (`48aae28a4ef4`, "Merge pull request #299 from bot523h/arena/ac3f046e-nexus-ai-agent")
- **Observer:** NEXUS engineering-debt watchdog automation (observe-and-report only; no product-code edits, no merge, no push to `main`)
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-10-31.md` (baseline for the deltas below).

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable or a computation was ambiguous, that is stated explicitly.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `48aae28a4ef4` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` | schema 2; 77 zones, 173 claims, 10 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; `updated_at = 2026-10-31T15:35:11Z` |
| Remote branches | 218 (`git ls-remote --heads origin \| wc -l`) |
| Open PR heads | 43 (`gh api "repos/bot523h/nexus-ai-agent/pulls?state=open&per_page=100"`) |
| Branch protection | **readable** (`protection_state: readable`, `required_approvals: 0`, `protection_error: ''`) |

> Note on the checkout: the local clone is **shallow/grafted** at `48aae28a4ef4` (`git log` on `main` shows a single grafted commit). Branch/PR/CI facts below are therefore taken from live `git ls-remote` and the GitHub API, not from local commit ancestry. This is a clone artifact, not repository debt.

---

## 1. Live PR registry

```
$ python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: 43 open PRs → /tmp/reg.json (main 48aae28a4ef4)
COLLECT EXIT=0

$ python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-01T02:51:08Z`, `main_sha`: `48aae28a4ef4a00459530bb177ce4fe4e43c0ad3`
- `open_total`: `43`, `protection_state`: `readable`, `required_approvals`: `0`
- `required_contexts`: `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "merge-base-guard (base == main)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-10-31 |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 12 | +1 |
| CONFLICT | 26 | +1 |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 2 | −2 |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 2 | +1 |
| PROTECTION_UNREADABLE | 0 | 0 |
| **READY** | **1** | **+1** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 12 · `CONFLICT` 34 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 5 · `CI_PENDING` 20 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 42 · `PROTECTION_UNREADABLE` 0.

**Non-READY PRs, controlling blocker (first in the ordered list):**

| PR | Tier | Controlling blocker | CI state (registry) | Title |
|---|---|---|---|---|
| #58 | DRAFT | DRAFT | PENDING | Security Boundary hardening — S1/S2/S3/S4/S5 |
| #92 | DRAFT | DRAFT | SUCCESS | fix(storage): honor SQLite defaults |
| #114 | DRAFT | DRAFT | FAILURE | feat(cockpit): Persian capability explorer |
| #117 | DRAFT | DRAFT | PENDING | fix(runtime): close audited session/media contracts |
| #118 | DRAFT | DRAFT | PENDING | W2 golden hardening: authority races |
| #150 | DRAFT | DRAFT | PENDING | feat(agent-intelligence): verified CreativeWork trim |
| #204 | DRAFT | DRAFT | SUCCESS | fix(memory): resolve sqlite-vec extension fallback |
| #257 | DRAFT | DRAFT | SUCCESS | Palette: Improve Telegram inline callback UX |
| #273 | DRAFT | DRAFT | SUCCESS | feat(nagar): P0 intent NEL contracts |
| #284 | DRAFT | DRAFT | FAILURE | feat(nagar): read-only Causal Memory |
| #285 | DRAFT | DRAFT | SUCCESS | chore(board): synchronize task-278 deferred status |
| #298 | DRAFT | DRAFT | SUCCESS | feat(nagar-ibpc): proof-carrying live editing |
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
| #297 | CONFLICT | CONFLICT | SUCCESS | evidence(m16): true closeout of the M15 spine |
| #64 | CI_RED | CI_RED | FAILURE | Creative Runtime (task-176 s2) |
| #97 | CI_RED | CI_RED | FAILURE | feat(vision): portrait and scene Vision packs |
| #294 | REBASE_FIRST | REBASE_FIRST | SUCCESS | repair(#189): durable creative-graph package |
| #295 | REBASE_FIRST | REBASE_FIRST | SUCCESS | extract(#218): content-addressed Asset Library |
| #300 | READY | — | SUCCESS | chore(board): register Nagar workspace and CircleCI tasks |

**The single READY PR:** #300, head `1cbe723b8f`, `mergeable_state: unstable`, no review decision, 1 file `+78/-0`. Reproduce: `gh api repos/bot523h/nexus-ai-agent/pulls/300`.

---

## 2. Board audit

```
$ python -m pytest -q tests/unit/test_agent_board.py
75 passed in 1.42s
EXIT=0
```

`.agents/board.json` sha256: `d4c38cb4ef0defb49598439b0d73adc192716bdc83c6a02597c9e76a6a0a3992` (unchanged across all gate runs; the audit mutates no board state).

- **Claims:** 173 (was 172). **Zones:** 77 (was 76). Status histogram: `done` 77, `queued` 58, `expired` 17, `completed_released` 5, `completed_merged` 3, `active` 2, `deferred` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `assigned_to_B_next` 1, `available_sequenced_post_33` 2, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1.
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).

### 2.1 Active claims — 2 (both branches live, both PRs already merged, both leases now STALE)

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-267-truth041-live-lease-alignment` | `runtime-truth-tooling` | `arena/m14-truth041-lease-expiry` | yes | no | 24 | 2026-10-30T10:11:42Z | expired 2026-10-31T10:11:42Z → **STALE** |
| `task-281-cognition-eval-live-registry-binding` | `cognition-quality-evaluation` | `arena/de4e797a-nexus-ai-agent` | yes | no | 24 | 2026-10-30T15:39:23Z | expired 2026-10-31T15:39:23Z → **STALE** |

Both tasks' work has **already merged**: `task-267` via PR **#292** (`merged_at 2026-10-30T10:36:41Z`), `task-281` via PR **#293** (`merged_at 2026-10-30T19:18:30Z`). Neither claim has been released, and as of this run both 24 h leases are **past TTL** (snapshot `2026-11-01T02:53:31Z`). This is **landed-but-still-leased + now-stale** debt: the branches still exist remotely so the dead-branch check does not flag them, but the lease holds a `zone`/`exclusive_paths` reservation for work already on `main`. Not a collision — `AGENTS.md` §1.2 only blocks overlap with *live* leases, and `show`/`gc` auto-release stale leases.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30 and 2026-10-31 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (218 refs) and the 43 open-PR heads.

### 2.3 Stale live leases — 2 (was 0; both are the landed-but-leased claims of §2.1)

Both `active` claims are past their 24 h TTL at snapshot time. This is a **NEW** stale-lease finding versus the 2026-10-31 run, which measured them while still live. `gc`/`show` would auto-release them.

### 2.4 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry is a prefix-equal-or-child of a `zone.paths` entry.

### 2.5 Unleased non-terminal claims — 67 (context, not a gate)

67 non-terminal claims carry neither `agent_branch` nor `claimed_at` (58 `queued` + the 7 labelled `assigned_to_*` / `available_sequenced_*` + `superseded_by_PR33` + `deferred`). Of these, the 58 `queued` are mostly `pr-repair-<n>` planning rows with no owner; they are not leases and cannot collide, so they are recorded for completeness only. The 7 owner-labelled rows without a branch or clock (`task-122`, `task-123`, `task-124`, `task-126`, `task-121`, `task-128`, `task-127`) remain the actionable subset — an owner label that no machine check can verify.

---

## 3. Gates

| # | Command | Result | Exit |
|---|---|---|---|
| G1 | `python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| G2 | `python -m pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `117 passed in 3.94s` | 0 |
| G3 | `python -m pytest -q tests/unit/test_agent_board.py` | `75 passed in 1.42s` | 0 |

Environment: Python 3.13 (`.venv`). All gates **green**; no gate turned red versus the previous run.

---

## 4. CI health — 12 most recently updated open PRs (exact head SHA)

Required checks (names from `required_contexts`): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status is the **latest check run per check name on the EXACT head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, grouped by `.name`, max `started_at`).

| PR | Head SHA | Updated (UTC) | lint | test | merge-base-guard |
|---|---|---|---|---|---|
| #300 | `1cbe723b8f` | 2026-10-31T21:44:00Z | success | success | success |
| #69 | `7498074867` | 2026-10-31T12:16:59Z | success | success | **absent** |
| #66 | `db18cf66f3` | 2026-10-31T12:16:57Z | success | success | **absent** |
| #298 | `698131b881` | 2026-10-31T11:53:24Z | success | success | success |
| #290 | `d6437217e9` | 2026-10-31T08:21:47Z | success | success | success |
| #297 | `f142f2e923` | 2026-10-31T08:16:47Z | success | success | success |
| #295 | `c641154d19` | 2026-10-31T08:16:02Z | success | success | success |
| #294 | `9b60307a14` | 2026-10-31T08:14:53Z | success | success | success |
| #285 | `ce1ede4e8d` | 2026-10-29T21:47:25Z | success | success | success |
| #284 | `82d9be0182` | 2026-10-29T20:28:18Z | success | **failure** | success |
| #273 | `9d29722e6e` | 2026-10-29T10:00:31Z | success | success | success |
| #247 | `d9176f9bcb` | 2026-10-29T05:33:42Z | success | success | success |

States observed: `success`, `failure` (latest run), and `absent` (the required `merge-base-guard` check has never run on that head — see §4.1). No `queued`/`in_progress`/`cancelled` was the latest run at snapshot time.

### 4.1 Repository-wide required-context presence (all 43 PRs)

| Check | Present on exact head | Absent |
|---|---|---|
| `lint (ruff + mypy + version lockstep)` | 43 | 0 |
| `test (pytest -m "not slow")` | 43 | 0 |
| `merge-base-guard (base == main)` | 23 | **20** — `[57, 58, 60, 63, 66, 69, 73, 84, 89, 113, 117, 118, 121, 129, 135, 137, 140, 149, 150, 157]` |

The 20 PRs with an absent `merge-base-guard` are long-lived branches (heads from 2026-09-23 onward) that predate the guard job's addition to CI; the guard has therefore never reported on their head. Two of them (#69, #66) were **re-pushed on 2026-10-31** yet still do not carry the guard on the new head.

---

## 5. Findings (engineering debt)

### D1 — `pr_convergence.py` masks a real CI FAILURE behind `PENDING` (P0) — **STILL OPEN (2nd run)**

`_latest_ci_verdict` (strict mode, `require_all_present=True`) returns `PENDING` whenever **any** required context is absent on the head — *before* it inspects the contexts that did run. A `PENDING` reads as "still running / wait", which hides a definitively red gate. Function-level reproduction this run:

```
$ python - <<'PY'
import sys; sys.path.insert(0,'scripts'); import pr_convergence as pc
req=['lint (ruff + mypy + version lockstep)','test (pytest -m "not slow")','merge-base-guard (base == main)']
rec={"statusCheckRollup":{"contexts":[
  {"name":req[0],"conclusion":"FAILURE","completedAt":"2026-10-04T17:39:57Z"},
  {"name":req[1],"conclusion":"FAILURE","completedAt":"2026-10-04T17:43:37Z"}]}}
print(pc._latest_ci_verdict(rec, req, require_all_present=True))   # -> PENDING  (expected FAILURE)
PY
PENDING
```

Measured across all 43 open PRs: registry `ci_state == PENDING` for **20**, but **6** of those have a genuine `lint`/`test` `FAILURE` at the exact head:

| PR | Head SHA | lint | test | merge-base-guard | Registry `ci_state` | True verdict |
|---|---|---|---|---|---|---|
| #57 | `2f591618e1` | failure | success | absent | PENDING | **FAILURE** |
| #58 | `bb06da1cb3` | failure | failure | absent | PENDING | **FAILURE** |
| #60 | `efea9bae3e` | failure | failure | absent | PENDING | **FAILURE** |
| #63 | `3e376bea06` | success | failure | absent | PENDING | **FAILURE** |
| #135 | `f83b12835c` | success | failure | absent | PENDING | **FAILURE** |
| #150 | `5f84be4dd2` | failure | failure | absent | PENDING | **FAILURE** |

Cross-check total: registry `ci_state` vs true latest-per-name verdict mismatch = **6 of 43** (all in the `PENDING`-should-be-`FAILURE` direction; 0 in the other direction). No merge-readiness decision is *reversed* (all six also carry `CONFLICT`/`DRAFT`), but the CI signal is wrong for ~14% of the backlog. Note: the registry's `CI_RED` tier count (2) under-reports the real red count — **11 PRs** have a genuine `lint`/`test` failure at head (`[57, 58, 60, 63, 64, 97, 114, 135, 150, 205, 284]`); the other 5 (#64, #97, #114, #205, #284) are classified `CI_RED`/`DRAFT` by their harder blocker, so the tier is not wrong for them, but the `PENDING` label on the 6 above is. Reproduce: `gh api repos/bot523h/nexus-ai-agent/commits/<head_sha>/check-runs?per_page=100`, group by `.name`, take max `started_at`; compare to `registry.ci_state`.

### D2 — `merge-base-guard` absent on 20 of 43 heads, silently downgraded to `PENDING` (P1) — **STILL OPEN (2nd run)**

20 long-lived PRs predate the `merge-base-guard` job, so the required check has never run on their head (§4.1). Because of D1 these read `PENDING`, so the registry shows `CI_PENDING` 20 / `CI_RED` 2 rather than surfacing "the required guard never ran here". Two distinct conditions (never-ran vs still-running) are collapsed into one tier, and a head missing a *required* gate is not distinguishable from one whose gate is in flight. Owner action: either re-run the guard on these heads or record "guard absent on legacy head" as its own signal.

### D3 — board hygiene: 13 dead-branch claims, 2 stale/landed leases, 67 unleased rows (P1) — **partly NEW**

See §2.1–§2.5. 13 `expired` claims still name branches that no longer exist; the 2 `active` claims are now **past TTL** and hold zones for work already merged (#292, #293) without a `release`; and 67 non-terminal claims carry no branch and no `claimed_at` (58 `queued` planning rows + 7 owner-labelled rows). `gc`/`release` has not swept any of them.

### D4 — `protocol.protocol_version` is now present (P1) — **RESOLVED**

The 2026-10-31 report (D5 there) noted `AGENTS.md` claimed "schema 2" while `protocol.protocol_version` was absent. This run it is present and equals `2` (`python -c "import json;print(json.load(open('.agents/board.json'))['protocol']['protocol_version'])"` → `2`). Resolved.

### D5 — backlog shape (P1, character unchanged)

1 READY of 43 open (the first READY in three runs). 26 CONFLICT + 12 DRAFT tiers; 42 PRs carry `REBASE_FIRST`; 11 carry `CHANGES_REQUESTED`; 20 carry `CI_PENDING` (6 of them really red, D1). The dominant debt remains stale, unrebased branches, not green-but-unmerged work.

---

## 6. Delta vs previous run (2026-10-31, `main` `ab56beccc881`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-10-31.md`.

| Metric | 2026-10-31 | 2026-11-01 | Change |
|---|---|---|---|
| Open PRs | 41 | 43 | **+2** |
| DRAFT tier | 11 | 12 | +1 |
| CONFLICT tier | 25 | 26 | +1 |
| CI_RED tier | 4 | 2 | **−2** |
| CI_PENDING tier | 0 | 0 | 0 |
| REBASE_FIRST tier | 1 | 2 | +1 |
| READY | 0 | 1 | **+1** |
| Branch protection | readable | readable | 0 |
| `ci_state` mismatches (D1/D3 shape) | 6 | 6 | 0 |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 2 | 2 | 0 |
| Stale live leases | 0 | 2 | **+2 (NEW)** |
| `gates_owner` holders | 0 | 0 | 0 |
| `protocol_version` present | no | yes | **resolved (D4)** |
| Zones / claims | 76 / 172 | 77 / 173 | +1 / +1 |

**New PRs since 2026-10-31:** #297 (`CONFLICT`, CI green, `DIRTY`), #298 (`DRAFT`, CI green, `DIRTY`), #300 (`READY`, CI green, `UNSTABLE`).
**Closed/merged since 2026-10-31:** #283 (`fix(execution): use queue-owned heartbeat for safe recovery`, merged 2026-10-31T06:30:30Z).
**Tier change among common PRs:** #295 `CI_RED → REBASE_FIRST` (test now green at head `c641154d19`; still behind base) — an improvement.
**Also merged in the window:** the previous watchdog report itself (#296, merged 2026-10-31T12:07:42Z).

**Regressions (gate/CI/ownership):**
- **NEW stale live leases:** the two `active` claims (§2.1) crossed their 24 h TTL since the last run (2 was 0).
- **NEW conflict:** #297 is a new `CONFLICT` PR.
- **NEW CI red at head:** none newly introduced; #284 remains red from before, and #295's red cleared.
- **NEW gate failures:** none — G1/G2/G3 all green both runs.
- **NEW ownership mismatch:** none (still 13, byte-identical list).

**Resolved since last run:**
- **D4 resolved** — `protocol.protocol_version = 2` is now present.
- #283 closed/merged; #295's test failure cleared (`CI_RED → REBASE_FIRST`).
- The first `READY` PR (#300) appeared.

**Unchanged / still open:** D1 (PENDING masks FAILURE) and D2 (`merge-base-guard` absent on 20 heads) both persist for a second run.

---

## 7. Next actions (prioritized)

1. **[P0] Fix `_latest_ci_verdict` ordering in `scripts/pr_convergence.py`** so a real `FAILURE` is never masked by an absent required context: compute the `_RED_CONCLUSIONS` verdict over the present contexts **before** returning `PENDING` for a missing one (or return `FAILURE` when any present context is red and any required context is absent). 6 of 43 PRs are mis-reported (`PENDING` instead of `FAILURE`): #57, #58, #60, #63, #135, #150.
2. **[P0] Distinguish "required check never ran" from "still running"** (D2): give the 20 legacy heads missing `merge-base-guard` their own signal rather than folding them into `PENDING`, or re-run the guard on those heads.
3. **[P1] Sweep board hygiene** (D3): `release`/`gc` the 2 stale, landed-but-leased active claims (#292, #293), and remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
4. **[P2] Protect and land the first READY PR** (#300, `UNSTABLE`): it is the only PR that clears every required gate; get a review and confirm the `UNSTABLE` state is benign before it re-stales.
5. **[P2] Attack the rebase wall** (D5): 42 of 43 PRs carry `REBASE_FIRST`; triage the 26 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
