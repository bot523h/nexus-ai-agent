# Engineering-Debt Watchdog — 2026-11-02

- **Run timestamp (UTC):** 2026-11-02T02:53:14Z
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `d52a2434150d4e3a422d71b5f42b354d24bf61eb` (`d52a243`, "Merge pull request #300 from bot523h/arena/0d9b67b6-nexus-ai-agent")
- **Observer:** NEXUS engineering-debt watchdog automation (observe-and-report only; no product-code edits, no merge, no push to `main`)
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-01.md` (the 20:03Z revision on branch `automation/debt-watchdog`, PR #301) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable or a computation was ambiguous, that is stated explicitly.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `d52a2434150d` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` | schema 2; 79 zones, 175 claims, 10 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; `updated_at = 2026-10-31T15:35:11Z` |
| Remote branches | 220 (`git ls-remote --heads origin \| wc -l`; 182 are `refs/heads/arena/*`) |
| Open PRs | 44 (`python scripts/pr_convergence.py collect --out /tmp/reg.json`) |
| Branch protection | **readable** (`protection_state: readable`, `required_approvals: 0`, `protection_error: ''`) |

> **Clone note.** The clone was unshallowed this run (`git fetch --unshallow origin`; `.git/shallow` no longer exists), so commit ancestry is verifiable locally and no figure below is read from a grafted tree.

---

## 1. Live PR registry

```
$ python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: 44 open PRs → /tmp/reg.json (main d52a2434150d)
COLLECT EXIT=0

$ python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-02T02:38:52Z`, `main_sha`: `d52a2434150d4e3a422d71b5f42b354d24bf61eb`
- `open_total`: `44`, `protection_state`: `readable`, `required_approvals`: `0`
- `required_contexts`: `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "merge-base-guard (base == main)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-11-01 (20:03Z) |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 12 | 0 |
| CONFLICT | 26 | 0 |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 2 | 0 |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 2 | −1 |
| PROTECTION_UNREADABLE | 0 | 0 |
| **READY** | **2** | **+1** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 12 · `CONFLICT` 33 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 5 · `CI_PENDING` 20 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 39 · `PROTECTION_UNREADABLE` 0.

**Non-READY PRs, controlling blocker (first in the ordered list):**

| PR | Tier | Controlling blocker | CI state (registry) | Head SHA | Title |
|---|---|---|---|---|---|
| #33 | CONFLICT | CONFLICT | SUCCESS | `9942939713` | v3.13.0 — deliver the P0 security code, wire the dead engines |
| #57 | CONFLICT | CONFLICT | PENDING | `2f591618e1` | feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-160) |
| #58 | DRAFT | DRAFT | PENDING | `bb06da1cb3` | Security Boundary hardening — S1/S2/S3/S4/S5 |
| #60 | CONFLICT | CONFLICT | PENDING | `efea9bae3e` | M0 runtime integration: InstrumentedJobQueue + readyz factory |
| #63 | CONFLICT | CONFLICT | PENDING | `3e376bea06` | task-163: board-Git truth gate + scale-to-zero state tiers |
| #64 | CI_RED | CI_RED | FAILURE | `ab564d7cc7` | Creative Runtime (task-176 s2): execution semantics |
| #66 | CONFLICT | CONFLICT | PENDING | `db18cf66f3` | P0 creative integration: one production path |
| #69 | CONFLICT | CONFLICT | PENDING | `7498074867` | Gate 4: cross-layer vertical slice verification |
| #73 | CONFLICT | CONFLICT | PENDING | `1a35ec0697` | Gate 2.2 — Operation Truth → Executable Evidence Gate |
| #84 | CONFLICT | CONFLICT | PENDING | `89e04c6a24` | Register the NEXUS Engineering Constitution |
| #89 | CONFLICT | CONFLICT | PENDING | `2f0df4f11e` | task-122: 12 real command wirings + moderation ingress |
| #92 | DRAFT | DRAFT | SUCCESS | `40c1163314` | fix(storage): honor SQLite defaults |
| #97 | CI_RED | CI_RED | FAILURE | `3307d3978f` | feat(vision): complete portrait and scene Vision packs |
| #99 | CONFLICT | CONFLICT | SUCCESS | `9a862e26ad` | task-193: command truth — 11 fake/silent commands wired |
| #113 | CONFLICT | CONFLICT | PENDING | `6a867a77ca` | feat(runtime): W1 final close-out |
| #114 | DRAFT | DRAFT | FAILURE | `6ca1b13bfa` | feat(cockpit): Persian capability explorer and command lab |
| #115 | CONFLICT | CONFLICT | SUCCESS | `f198734859` | W1: True Runtime Closure / Lifecycle Ownership |
| #117 | DRAFT | DRAFT | PENDING | `4a4b30c2d1` | fix(runtime): close audited session/media contracts |
| #118 | DRAFT | DRAFT | PENDING | `d5a205f40a` | W2 golden hardening: authority races |
| #121 | CONFLICT | CONFLICT | PENDING | `b4a5965320` | feat(memory): retrieval truth — noise-ranked recall |
| #122 | CONFLICT | CONFLICT | SUCCESS | `8b8ba31d44` | fix(orchestration): H1 — route chat through the memory reader |
| #129 | CONFLICT | CONFLICT | PENDING | `75f646a52e` | feat(creative/studio): staged plan transactions |
| #135 | CONFLICT | CONFLICT | PENDING | `f83b12835c` | feat(creative/studio): canonical PlanTransaction substrate |
| #137 | CONFLICT | CONFLICT | PENDING | `e2a4614472` | board(task-227,task-228): collision preflight |
| #140 | CONFLICT | CONFLICT | PENDING | `25da562986` | Harden Studio CommandBus: PlanTransaction |
| #149 | CONFLICT | CONFLICT | PENDING | `e865c14650` | Security Wave 1 Verification & Governance Alignment |
| #150 | DRAFT | DRAFT | PENDING | `5f84be4dd2` | feat(agent-intelligence): verified CreativeWork trim path |
| #157 | CONFLICT | CONFLICT | PENDING | `00ccf90326` | feat(governance): Nagar agent constitution |
| #193 | CONFLICT | CONFLICT | SUCCESS | `d505d80a4e` | feat(infra): canonical Ubuntu 26.04 + Python 3.14 |
| #204 | DRAFT | DRAFT | SUCCESS | `13078d7926` | fix(memory): resolve sqlite-vec extension fallback |
| #205 | CONFLICT | CONFLICT | FAILURE | `ff2792b7ad` | feat(nagar): Asset Library V1 |
| #230 | CONFLICT | CONFLICT | SUCCESS | `9f572aae41` | Arena/aa451c49 nexus ai agent |
| #247 | CONFLICT | CONFLICT | SUCCESS | `d9176f9bcb` | feat(creative): perception lane |
| #257 | DRAFT | DRAFT | SUCCESS | `b057ef5fda` | Palette: Improve Telegram inline callback UX |
| #273 | DRAFT | DRAFT | SUCCESS | `9d29722e6e` | feat(nagar): P0 intent NEL semantic time causal contracts |
| #284 | DRAFT | DRAFT | FAILURE | `82d9be0182` | feat(nagar): add read-only Causal Memory and bounded Director preview |
| #285 | DRAFT | DRAFT | SUCCESS | `ce1ede4e8d` | chore(board): synchronize task-278 deferred status |
| #290 | CONFLICT | CONFLICT | SUCCESS | `d6437217e9` | chore(board): register task-282 durable project state lifecycle |
| #294 | REBASE_FIRST | REBASE_FIRST | SUCCESS | `9b60307a14` | repair(#189): land the durable creative-graph package |
| #295 | REBASE_FIRST | REBASE_FIRST | SUCCESS | `c641154d19` | extract(#218): land the content-addressed Asset Library |
| #297 | CONFLICT | CONFLICT | SUCCESS | `f142f2e923` | evidence(m16): true closeout of the M15 spine |
| #298 | DRAFT | DRAFT | SUCCESS | `379deb24b0` | feat(nagar-ibpc): proof-carrying live editing |

**The two READY PRs** (reproduce: `gh api repos/bot523h/nexus-ai-agent/pulls/<n>`):

| PR | Head SHA | Mergeable | `mergeable_state` | Review decision | Registry `ci_state` |
|---|---|---|---|---|---|
| #302 | `53e7cd1bbfaf59dd4b7407816e481fa7ab413671` | MERGEABLE | UNSTABLE | (none) | SUCCESS |
| #301 | `d15ebd43406ebccc8dc58a03db648182658c3ac6` | MERGEABLE | UNSTABLE | APPROVED | SUCCESS |

Both READY heads are `UNSTABLE` — see finding **D7** (a non-required `CircleCI Pipeline` context reports `ERROR` on both).

---

## 2. Board audit

```
$ python -m pytest -q tests/unit/test_agent_board.py
75 passed in 1.69s
EXIT=0
```

`.agents/board.json` sha256: `592576ddb556f208f0784132b36f0868d0a4f66402f86f5de83c724f446bee33` (the `main` board; verified equal to `git show origin/main:.agents/board.json | sha256sum`; unchanged across all gate runs — the audit mutates no board state).

- **Claims:** 175. **Zones:** 79. Status histogram: `done` 77, `queued` 60, `expired` 17, `completed_released` 5, `completed_merged` 3, `available_sequenced_post_33` 2, `deferred` 2, `active` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1.
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).

### 2.1 Active claims — 2 (both branches live, both leases STALE)

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-267-truth041-live-lease-alignment` | `runtime-truth-tooling` | `arena/m14-truth041-lease-expiry` | yes | no | 24 | 2026-10-30T10:11:42Z | expired 2026-10-31T10:11:42Z → **STALE** |
| `task-281-cognition-eval-live-registry-binding` | `cognition-quality-evaluation` | `arena/de4e797a-nexus-ai-agent` | yes | no | 24 | 2026-10-30T15:39:23Z | expired 2026-10-31T15:39:23Z → **STALE** |

Both tasks' work has **already merged**: `task-267` via PR **#292** (`merged_at 2026-10-30T10:36:41Z`), `task-281` via PR **#293** (`merged_at 2026-10-30T19:18:30Z`). Neither claim was released, and both 24 h leases are **past TTL** at this snapshot. This is **landed-but-still-leased + stale** debt: the branches still exist remotely so the dead-branch check does not flag them, but the lease holds a `zone`/`exclusive_paths` reservation for work already on `main`. Not a collision — `AGENTS.md` §1.2 only blocks overlap with *live* leases, and `show`/`gc` auto-release stale leases.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30, 2026-10-31 and both 2026-11-01 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (220 refs) and the 44 open-PR heads.

### 2.3 Stale live leases — 2 (unchanged; both are the landed-but-leased claims of §2.1)

Both `active` claims are past their 24 h TTL at snapshot time. `gc`/`show` would auto-release them.

### 2.4 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations), and every claim that names a `zone` names a declared one (0 undeclared). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry is a prefix-equal-or-child of a `zone.paths` entry.

### 2.5 Unleased non-terminal claims — 68 (context, not a gate)

68 non-terminal claims carry neither `agent_branch` nor `claimed_at` (60 `queued` + the 7 labelled `assigned_to_*` / `available_sequenced_*` rows + 1 `deferred`). Of these, the 60 `queued` are mostly `pr-repair-<n>` planning rows with no owner; they are not leases and cannot collide, so they are recorded for completeness only. The 7 owner-labelled rows without a branch or clock (`task-122`, `task-123`, `task-124`, `task-126`, `task-121`, `task-128`, `task-127`) remain the actionable subset — an owner label that no machine check can verify.

---

## 3. Gates

| # | Command | Result | Exit |
|---|---|---|---|
| G1 | `python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| G2 | `python -m pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `117 passed in 1.12s` | 0 |
| G3 | `python -m pytest -q tests/unit/test_agent_board.py` | `75 passed in 1.69s` | 0 |

Environment: Python 3.13.15 (`python3 -m pytest`, `python3 -m nexus_ai_agent.nagar`). All gates **green**; no gate turned red versus the previous run.

---

## 4. CI health — 12 most recently updated open PRs (exact head SHA)

Required checks (names from `required_contexts`): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status is the **latest check run per check name on the EXACT head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, grouped by `.name`, max `started_at`). Ordering: `updated_at` descending from `/tmp/reg.json`.

| PR | Head SHA | Updated (UTC) | lint | test | merge-base-guard |
|---|---|---|---|---|---|
| #302 | `53e7cd1bbf` | 2026-11-01T20:49:02Z | success | success | success |
| #204 | `13078d7926` | 2026-11-01T20:28:44Z | success | success | success |
| #298 | `379deb24b0` | 2026-11-01T20:27:30Z | success | success | success |
| #92 | `40c1163314` | 2026-11-01T20:27:15Z | success | success | success |
| #301 | `d15ebd4340` | 2026-11-01T20:08:54Z | success | success | success |
| #69 | `7498074867` | 2026-10-31T12:16:59Z | success | success | **absent** |
| #66 | `db18cf66f3` | 2026-10-31T12:16:57Z | success | success | **absent** |
| #290 | `d6437217e9` | 2026-10-31T08:21:47Z | success | success | success |
| #297 | `f142f2e923` | 2026-10-31T08:16:47Z | success | success | success |
| #295 | `c641154d19` | 2026-10-31T08:16:02Z | success | success | success |
| #294 | `9b60307a14` | 2026-10-31T08:14:53Z | success | success | success |
| #285 | `ce1ede4e8d` | 2026-10-29T21:47:25Z | success | success | success |

States observed on the **latest** run per name: `success` and `absent`. No `queued` / `in_progress` / `cancelled` / `failure` / `skipped` is the latest run for any required check on these 12 heads. **Superseded** runs do exist: on #301 and #92 the head carries an earlier `cancelled` run for each of the three required checks alongside the later `success` (`gh api .../commits/<sha>/check-runs --jq '.check_runs[] | select(.name=="lint (ruff + mypy + version lockstep)")'` shows `cancelled` at 20:08:49Z then `success` at 20:10:25Z on #301). The latest-per-name rule correctly resolves these to `success`; they are recorded so the `cancelled` state is not mistaken for absent.

### 4.1 Repository-wide required-context presence (all 44 PRs)

| Check | Present on exact head | Absent |
|---|---|---|
| `lint (ruff + mypy + version lockstep)` | 44 | 0 |
| `test (pytest -m "not slow")` | 44 | 0 |
| `merge-base-guard (base == main)` | 24 | **20** — `[57, 58, 60, 63, 66, 69, 73, 84, 89, 113, 117, 118, 121, 129, 135, 137, 140, 149, 150, 157]` |

The 20 PRs with an absent `merge-base-guard` are long-lived branches (heads from 2026-09-23 onward) that predate the guard job's addition to CI; the guard has therefore never reported on their head. #69 and #66 were re-pushed on 2026-10-31 yet still do not carry the guard on the new head.

---

## 5. Findings (engineering debt)

### D1 — `pr_convergence.py` masks a real CI FAILURE behind `PENDING` (P0) — **STILL OPEN (4th run)**

`_latest_ci_verdict` (strict mode, `require_all_present=True`, `scripts/pr_convergence.py:215`) returns `PENDING` whenever **any** required context is absent on the head, *before* it inspects the contexts that did run. A `PENDING` reads as "still running / wait", which hides a definitively red gate. Function-level reproduction this run:

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

Measured across all 44 open PRs (latest run per name, compared to `registry.ci_state`): **6 of 44** disagree, all in the `PENDING`-should-be-`FAILURE` direction:

| PR | Head SHA | lint | test | merge-base-guard | Registry `ci_state` | True verdict |
|---|---|---|---|---|---|---|
| #57 | `2f591618e1` | failure | success | absent | PENDING | **FAILURE** |
| #58 | `bb06da1cb3` | failure | failure | absent | PENDING | **FAILURE** |
| #60 | `efea9bae3e` | failure | failure | absent | PENDING | **FAILURE** |
| #63 | `3e376bea06` | success | failure | absent | PENDING | **FAILURE** |
| #135 | `f83b12835c` | success | failure | absent | PENDING | **FAILURE** |
| #150 | `5f84be4dd2` | failure | failure | absent | PENDING | **FAILURE** |

Registry `ci_state` histogram: `PENDING` 20 · `SUCCESS` 19 · `FAILURE` 5. The registry's `CI_RED` tier count (2) under-reports the real red count — **11 PRs** have a genuine `lint`/`test` failure at head (`[57, 58, 60, 63, 64, 97, 114, 135, 150, 205, 284]`); the other 5 (#64, #97, #114, #205, #284) are classified `CI_RED`/`DRAFT` by their harder blocker. No merge-readiness decision is *reversed* (all six also carry `CONFLICT`/`DRAFT`). Reproduce: `gh api repos/bot523h/nexus-ai-agent/commits/<head_sha>/check-runs?per_page=100`, group by `.name`, take max `started_at`; compare to `registry.ci_state`.

### D2 — `merge-base-guard` absent on 20 of 44 heads, silently downgraded to `PENDING` (P1) — **STILL OPEN (4th run)**

20 long-lived PRs predate the `merge-base-guard` job, so the required check has never run on their head (§4.1). Because of D1 these read `PENDING`, so the registry shows `CI_PENDING` 20 / `CI_RED` 2 rather than surfacing "the required guard never ran here". Two distinct conditions (never-ran vs still-running) are collapsed into one tier, and a head missing a *required* gate is not distinguishable from one whose gate is in flight. Owner action: either re-run the guard on these heads or record "guard absent on legacy head" as its own signal.

### D3 — board hygiene: 13 dead-branch claims, 2 stale/landed leases, 68 unleased rows (P1) — **unchanged**

See §2.1–§2.5. 13 `expired` claims still name branches that no longer exist; the 2 `active` claims are **past TTL** and hold zones for work already merged (#292, #293) without a `release`; and 68 non-terminal claims carry no branch and no `claimed_at`. `gc`/`release` has not swept any of them.

### D4 — `protocol.protocol_version` present (P1) — **RESOLVED (previous run)**

The 2026-10-31 report noted `AGENTS.md` claimed "schema 2" while `protocol.protocol_version` was absent. It is present and equals `2` (`python3 -c "import json;print(json.load(open('.agents/board.json'))['protocol']['protocol_version'])"` → `2`). Still resolved.

### D5 — backlog shape (P1, character unchanged)

2 READY of 44 open. 26 CONFLICT + 12 DRAFT tiers; 39 PRs carry `REBASE_FIRST`; 11 carry `CHANGES_REQUESTED`; 20 carry `CI_PENDING` (6 of them really red, D1). The dominant debt remains stale, unrebased branches, not green-but-unmerged work.

### D6 — the watchdog's own report reproducibility (P1) — **RESOLVED (previous run's corrective holds)**

The earlier 2026-11-01 revision cited board state read from a grafted tree rather than the tree its own commit carried. This run keeps the corrective in place: the clone was unshallowed (`git fetch --unshallow origin`; `.git/shallow` absent) and the board figures are hashed from the on-disk file, verified equal to `git show origin/main:.agents/board.json | sha256sum`. No recurrence.

### D7 — a non-required `CircleCI Pipeline` context reports `ERROR`, and the repo has no `.circleci/config.yml` (P1) — **NEW**

Five open PRs carry a `CircleCI Pipeline` status context whose conclusion is `ERROR`: **#92, #204, #298, #301, #302** (`python3 -c "import json;reg=json.load(open('/tmp/reg.json'));print(sorted(p['number'] for p in reg['prs'] for c in p['status_contexts'] if c['name']=='CircleCI Pipeline' and c['conclusion']=='ERROR'))"`). Both READY PRs (#301, #302) are affected, and the GitHub combined commit status for their exact heads is `failure`:

```
$ gh api repos/bot523h/nexus-ai-agent/commits/53e7cd1bbfaf59dd4b7407816e481fa7ab413671/status --jq '.state'
failure
$ gh api repos/bot523h/nexus-ai-agent/commits/53e7cd1bbfaf59dd4b7407816e481fa7ab413671/status --jq '.statuses[] | "\(.context) -> \(.state) @ \(.updated_at)"'
CircleCI Pipeline -> error @ 2026-11-01T20:49:00Z
```

Two facts make this debt:
1. `CircleCI Pipeline` is **not** in `required_contexts` (`["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "merge-base-guard (base == main)"]`), so the registry's required-context verdict (`ci_state: SUCCESS`) and `tier: READY` correctly ignore it — but the PR is left `mergeable_state: UNSTABLE` and the commit's legacy combined status is `failure`, which is the signal GitHub shows a human reviewer.
2. The pipeline that reports the `ERROR` has **no config in the tree**: `ls -d .circleci` → *No such file or directory*, and `.circleci/config.yml` is absent, yet the board carries a `queued` claim `task-nagar-circleci-pilot-01` (zone `nagar-circleci-pilot-01`) whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. So the status originates from an external CircleCI project configuration that is not reproducible from the repository.

This finding was **not recorded** in any prior watchdog report (no `CircleCI` mention in `.agents/evidence/AGENT_DEBT_WATCHDOG_*.md` before this run). It is the cause of the `UNSTABLE` state on the only two READY PRs, and it is recorded as P1 rather than P0 because it does not block the required gates — it only confuses the human-facing merge signal and hides an unreproducible external pipeline.

---

## 6. Delta vs previous run (2026-11-01 20:03Z, `main` `d52a243`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-01.md` (20:03Z revision, PR #301).

| Metric | 2026-11-01 20:03Z | 2026-11-02 02:53Z | Change |
|---|---|---|---|
| `main` SHA | `d52a2434150d` | `d52a2434150d` | **0 (unchanged)** |
| Open PRs | 44 | 44 | 0 |
| Open-PR number set | 44 ids | identical 44 ids | **0 new, 0 closed** |
| DRAFT tier | 12 | 12 | 0 |
| CONFLICT tier | 26 | 26 | 0 |
| CI_RED tier | 2 | 2 | 0 |
| CI_PENDING tier | 0 | 0 | 0 |
| REBASE_FIRST tier | 3 | 2 | **−1** |
| READY | 1 | 2 | **+1** |
| Remote branches | 219 | 220 | +1 |
| Board zones / claims (`main`) | 79 / 175 | 79 / 175 | 0 / 0 |
| Board sha (`main`) | `592576dd…` | `592576dd…` | **unchanged** |
| `ci_state` mismatches (D1) | 6 | 6 | 0 |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 2 | 2 | 0 |
| Stale live leases | 2 | 2 | 0 |
| `gates_owner` holders | 0 | 0 | 0 |
| Gates G1/G2/G3 | green | green | 0 |

**New PRs since last run:** none — the 44-PR number set is identical.
**Closed/merged since last run:** none — `main` is unchanged at `d52a2434150d` (the backlog was frozen for the ~7 h window).
**Tier change among common PRs:** #301 moved from `REBASE_FIRST` to `READY` — its head advanced from `e090330879` to `d15ebd4340` (branch merged with `origin/main`, so no longer `behind`) and it now carries an `APPROVED` review decision; #302's head advanced from `bd4988933c` to `53e7cd1bbf` and remains `READY`. The `REBASE_FIRST` tier fell 3→2 and the `REBASE_FIRST` blocker tally fell 43→39; the PR number set did not change, so this is a re-evaluation of existing heads, not arrivals or departures. (The previous run's `/tmp/reg.json` was not committed, so the per-PR blocker shift cannot be attributed further than the observed head advances.)

**Regressions:**
- **NEW finding D7:** a non-required `CircleCI Pipeline` context reports `ERROR` on 5 PRs (#92, #204, #298, #301, #302); both READY heads (#301, #302) are `UNSTABLE` and their combined commit status is `failure`. Not recorded in any prior report.
- No new gate failure (G1/G2/G3 green both runs).
- No new ownership mismatch (still 13, byte-identical list).
- No new stale claim (still 2; both were already stale last run).
- No new conflict (still 26 `CONFLICT`-tier PRs).

**Resolved since last run:** nothing new. D4 (`protocol_version` present) and D6 (watchdog report reproducibility) remain resolved from the previous run.

**Unchanged / still open:** D1 (PENDING masks FAILURE), D2 (`merge-base-guard` absent on 20 heads), D3 (board hygiene), D5 (backlog shape).

---

## 7. Next actions (prioritized)

1. **[P0] Fix `_latest_ci_verdict` ordering in `scripts/pr_convergence.py`** (`scripts/pr_convergence.py:215`) so a real `FAILURE` is never masked by an absent required context: compute the `_RED_CONCLUSIONS` verdict over the present contexts **before** returning `PENDING` for a missing one. 6 of 44 PRs are mis-reported (`PENDING` instead of `FAILURE`): #57, #58, #60, #63, #135, #150.
2. **[P0] Distinguish "required check never ran" from "still running"** (D2): give the 20 legacy heads missing `merge-base-guard` their own signal rather than folding them into `PENDING`, or re-run the guard on those heads.
3. **[P1] Resolve the `CircleCI Pipeline` `ERROR`** (D7): either commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — a non-required context that reads `ERROR` on every recent head leaves both READY PRs `UNSTABLE` with an unexplained `failure` combined status. Confirm whether #301/#302 are safe to land despite it.
4. **[P1] Sweep board hygiene** (D3): `release`/`gc` the 2 stale, landed-but-leased active claims (#292, #293), and remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
5. **[P2] Review and land a READY PR** (#301 `APPROVED`, or #302): they are the only PRs that clear every required gate; confirm the D7 `UNSTABLE` state is benign before they re-stale.
6. **[P2] Attack the rebase wall** (D5): 39 of 44 PRs carry `REBASE_FIRST`; triage the 26 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
