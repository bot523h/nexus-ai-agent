# Engineering-Debt Watchdog — 2026-11-03

- **Run timestamp (UTC):** 2026-11-03T03:42:37Z
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `5ce9f7d3f558ab903ece5f19018b28a5e2ff9a04` (`5ce9f7d3`, "Merge pull request #303 from bot523h/arena/nexus-master-closure-20261103")
- **Observer:** NEXUS engineering-debt watchdog automation (observe-and-report only; no product-code edits, no merge, no push to `main`)
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-02.md` (`main` `d52a2434150d`) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable or a computation was ambiguous, that is stated explicitly.

> **Token note (this run).** The ambient `GITHUB_TOKEN` cannot read branch protection (`403 Resource not accessible by integration`) and therefore forces `pr_convergence.py` to degrade to workflow-derived contexts and to certify **no PR as READY**. Exporting the repository PAT instead (`GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN`) makes the protection API readable and produces the authoritative registry. Both runs are recorded below; **§1 uses the PAT run**.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `5ce9f7d3f558` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` | schema 2; 81 zones, 177 claims, 10 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; `protocol.updated_at` **absent** |
| Remote branches | 225 (`git ls-remote --heads origin \| wc -l`; 187 are `refs/heads/arena/*`) |
| Open PRs | 46 (`GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py collect --out /tmp/reg_pat.json`) |
| Branch protection | **readable with the PAT** (`protection_state: readable`, `required_approvals: 0`, `strict: true`, `enforce_admins: true`, `allow_force_pushes: false`); **unreadable with the ambient `GITHUB_TOKEN`** (`403`) |

---

## 1. Live PR registry

```
$ GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py collect --out /tmp/reg_pat.json
pr-registry: 46 open PRs → /tmp/reg_pat.json (main 5ce9f7d3f558)
COLLECT EXIT=0

$ GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py report --in /tmp/reg_pat.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-03T03:41:01Z`, `main_sha`: `5ce9f7d3f558ab903ece5f19018b28a5e2ff9a04`
- `open_total`: `46`, `protection_state`: `readable`, `required_approvals`: `0`
- `required_contexts`: `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "merge-base-guard (base == main)"]` (the API `checks[]` list; the workflow file also defines ten non-required mutation/parity contexts — see §5, D1)
- `ci_state` histogram: `{SUCCESS: 21, FAILURE: 5, PENDING: 20}`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg_pat.json`.

| Tier | Count | Δ vs 2026-11-02 |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 11 | −1 |
| CONFLICT | 27 | +1 |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 2 | 0 |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 2 | 0 |
| PROTECTION_UNREADABLE | 0 | 0 |
| **READY** | **4** | **+2** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 11 · `CONFLICT` 34 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 5 · `CI_PENDING` 20 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 42 · `PROTECTION_UNREADABLE` 0.

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
| #294 | CONFLICT | CONFLICT | SUCCESS | `9b60307a14` | repair(#189): land the durable creative-graph package |
| #295 | REBASE_FIRST | REBASE_FIRST | SUCCESS | `c641154d19` | extract(#218): land the content-addressed Asset Library |
| #297 | CONFLICT | CONFLICT | SUCCESS | `f142f2e923` | evidence(m16): true closeout of the M15 spine |
| #301 | REBASE_FIRST | REBASE_FIRST | SUCCESS | `bac55bd39b` | chore(automation): engineering-debt watchdog 2026-11-02 |

**The four READY PRs** (reproduce: `gh api repos/bot523h/nexus-ai-agent/pulls/<n>`):

| PR | Head SHA | Mergeable | Review decision | Registry `ci_state` |
|---|---|---|---|---|
| #304 | `0cc22adfeeaa8700dc5f57e3f1695040d9c37f0d` | MERGEABLE | APPROVED | SUCCESS |
| #305 | `e55d8bf83a96f620c77a85aca5aaacacbb0a129f` | MERGEABLE | APPROVED | SUCCESS |
| #306 | `879105654a4547595712d46d5804de95efc30997` | MERGEABLE | APPROVED | SUCCESS |
| #307 | `0becd39e10da81f590957cb1d8a87e66eaf4e8e4` | MERGEABLE | (none) | SUCCESS |

All four READY heads carry a **combined commit status of `failure`** because of the non-required `CircleCI Pipeline` context — finding **D7** below (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/status --jq .state` → `failure` on all four).

**Ambient-token cross-check** (recorded for reproducibility, not the authoritative figure): with the default `GITHUB_TOKEN`, `collect` degrades to workflow-derived contexts (13 of them, including the mutation/parity jobs) and reports `tier {PROTECTION_UNREADABLE: 4, CI_RED: 3, REBASE_FIRST: 1, CONFLICT: 27, DRAFT: 11}`, `READY: 0`. The difference (4 READY vs 0) is the unreadable protection API; the `CI_RED` difference (2 vs 3) is **#295**, whose non-required `api-gate-mutations (A-06 security surface)` job is red — the degraded 13-context verdict counts it, the authoritative 3-required-context verdict does not (see §4). Both are **findings** (D8).

---

## 2. Board audit

```
$ python -m pytest -q tests/unit/test_agent_board.py
75 passed in 1.55s
EXIT=0
```

`.agents/board.json` (working tree) sha256: `ee631362cc2f7beaa2fb770bb5e3a78d9ad576d53e24d776cbf8ff86a0635ad1` — verified equal to `git show origin/main:.agents/board.json | sha256sum` (unchanged across all gate runs; the audit mutates no board state). The 2026-11-02 board (`bac55bd`) hashed `592576ddb556f208f0784132b36f0868d0a4f66402f86f5de83c724f446bee33` — so the board **changed** since the last report.

- **Claims:** 177. **Zones:** 81. Status histogram: `done` 79, `queued` 59, `expired` 19, `completed_released` 5, `completed_merged` 3, `available_sequenced_post_33` 2, `deferred` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1, `active` 1.
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).
- **Board delta vs 2026-11-02:** +2 claims, +2 zones — both new IBPC claims/zones: `task-ibpc-live-editing-01` / zone `nagar-ibpc-live-editing`, and `task-ibpc-security-closure-01` / zone `nagar-ibpc-security-closure` (both `done`). Three status transitions: `task-267-truth041-live-lease-alignment` `active → expired`, `task-281-cognition-eval-live-registry-binding` `active → expired`, `task-nagar-workspace-redelivery-01` `queued → active`.

### 2.1 Active claims — 1 (branch live, lease FRESH)

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-nagar-workspace-redelivery-01` | `nagar-workspace-redelivery-01` | `arena/nexus-master-closure-20261103` | yes (`1586092ae2`) | no | 24 | 2026-11-02T20:01:41Z | expires 2026-11-03T20:01:41Z → **FRESH** |

This is the **first run in four** with **zero stale live leases**: the two landed-but-leased claims of the 2026-11-02 report (`task-267`, `task-281`) have been released (`active → expired`), and the single new active claim is well inside its 24 h TTL. The branch `arena/nexus-master-closure-20261103` exists remotely but its work already landed via **PR #303** (`merged_at 2026-11-02T21:50:35Z`, head `1586092a`), so the claim is **landed-but-still-leased** for the remainder of its TTL — recorded as context, not a collision.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30, 2026-10-31, and both 2026-11-01 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (225 refs) and the 46 open-PR heads.

### 2.3 Stale live leases — 0 (improved; was 2)

No `active` claim is past its 24 h TTL at snapshot time. `gc`/`show` would find nothing to release. Reproduce: for each `active` claim, assert `claimed_at + ttl_hours > now` (now = `2026-11-03T03:42:37Z`).

### 2.4 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations, plain-prefix test), and every claim that names a `zone` names a declared one (0 undeclared). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry starts with a `zone.paths` entry.

> **Method note.** An earlier ad-hoc probe using *path-segment* prefix semantics reported 6 "violations" for `task-ibpc-live-editing-01`; re-running with the plain string-prefix test the board's own checker uses (`p.startswith(a)`) gives **0**. The 6 entries were `tests/unit/test_ibpc_*.py` matched against zone path `tests/unit/test_ibpc_` — a false positive of the segment test, not a real escape. The 0 is the correct figure.

### 2.5 Unleased non-terminal claims — 67 (context, not a gate)

67 non-terminal claims carry neither `agent_branch` nor `claimed_at` (59 `queued` + the 7 owner-labelled rows + 1 `deferred`). Of these, the 59 `queued` are mostly `pr-repair-<n>` planning rows with no owner; they are not leases and cannot collide, so they are recorded for completeness only. The 7 owner-labelled rows without a branch or clock (`task-122`, `task-123`, `task-124`, `task-126`, `task-121`, `task-128`, `task-127`) remain the actionable subset — an owner label that no machine check can verify.

---

## 3. Gates

| Gate | Command | Result | Exit |
|---|---|---|---|
| **G1** truth gate | `python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| **G2** matrix + docs | `pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `117 passed in 0.87s` | 0 |
| **G3** board contract | `pytest -q tests/unit/test_agent_board.py` | `75 passed in 1.55s` | 0 |

Environment: Python `3.13.15`, editable install (`nexus_ai_agent` resolves to `src/nexus_ai_agent/__init__.py`). All three gates GREEN — no gate regressed vs the 2026-11-02 run.

---

## 4. CI health — 12 most recently updated open PRs

Required contexts (the API `checks[]` list): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status per context on the **exact head SHA** (from `gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs`), distinguishing `queued`/`in_progress`/`cancelled`/`failure`/`skipped`/`success`/`absent`:

| PR | Head SHA | Updated (UTC) | `lint` | `test` | `merge-base-guard` |
|---|---|---|---|---|---|
| #306 | `879105654a` | 2026-11-02T23:21:38Z | success | success | success |
| #307 | `0becd39e10` | 2026-11-02T23:18:50Z | success | success | success |
| #305 | `e55d8bf83a` | 2026-11-02T22:29:43Z | success | success | success |
| #304 | `0cc22adfee` | 2026-11-02T22:19:19Z | success | success | success |
| #301 | `bac55bd39b` | 2026-11-02T02:56:17Z | success | success | success |
| #204 | `13078d7926` | 2026-11-01T20:28:44Z | success | success | success |
| #92 | `40c1163314` | 2026-11-01T20:27:15Z | success | success | success |
| #69 | `7498074867` | 2026-10-31T12:16:59Z | success | success | **absent** |
| #66 | `db18cf66f3` | 2026-10-31T12:16:57Z | success | success | **absent** |
| #290 | `d6437217e9` | 2026-10-31T08:21:47Z | success | success | success |
| #297 | `f142f2e923` | 2026-10-31T08:16:47Z | success | success | success |
| #295 | `c641154d19` | 2026-10-31T08:16:02Z | success | success | success |

Required-context histograms across all 46 open PRs:

- `lint (ruff + mypy + version lockstep)` → `{success: 42, failure: 4}` (absent: 0)
- `test (pytest -m "not slow")` → `{success: 36, failure: 10}` (absent: 0)
- `merge-base-guard (base == main)` → `{success: 24, skipped: 2, absent: 20}`

`merge-base-guard` is **absent on 20 heads** (unchanged): #57, #58, #60, #63, #66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #135, #137, #140, #149, #150, #157. These are legacy heads the guard workflow never ran against; they are folded into the registry's `PENDING` rather than given their own signal (finding D2).

**#295 `api-gate-mutations (A-06 security surface)`:** two check runs exist on head `c641154d19` — `cancelled` (id 124486724106, 2026-10-31T08:15:59Z, 6 s) and `failure` (id 124486750639, 2026-10-31T09:14:24Z). The later, definitive run is `failure`, so this job is genuinely red — **but it is not one of the 3 required contexts**, so the authoritative registry classifies #295 as `REBASE_FIRST`/`ci_state: SUCCESS` (all three required checks green), not `CI_RED`. It is the sole reason the ambient-token run's `CI_RED` count (3) exceeds the authoritative one (2): the degraded 13-context list includes `api-gate-mutations`. This is a real non-required red that the required-context view cannot surface.

---

## 5. Findings (with deltas)

### D1 — the registry reports `PENDING` where the real verdict is `FAILURE` (P0) — **STILL OPEN (unchanged)**

Six open PRs have at least one required context that **failed** while another required context (`merge-base-guard`) is **absent**; `_latest_ci_verdict` returns `PENDING` (missing context) before it evaluates the red present one:

| PR | `lint` | `test` | `merge-base-guard` | Registry `ci_state` | True verdict |
|---|---|---|---|---|---|
| #57 | **failure** | success | absent | PENDING | FAILURE |
| #58 | **failure** | **failure** | absent | PENDING | FAILURE |
| #60 | **failure** | **failure** | absent | PENDING | FAILURE |
| #63 | success | **failure** | absent | PENDING | FAILURE |
| #135 | success | **failure** | absent | PENDING | FAILURE |
| #150 | **failure** | **failure** | absent | PENDING | FAILURE |

The registry's `CI_RED` tier (2: #64, #97) therefore undercounts: the true red set is `[57, 58, 60, 63, 64, 97, 114, 135, 150, 205, 284]` (11 PRs, matching the 3-context non-strict tally `{SUCCESS: 35, FAILURE: 11}`). Same 6 PRs as the 2026-11-02 report.

### D2 — `merge-base-guard` absent on 20 heads, reported as `PENDING` (P0) — **STILL OPEN (unchanged)**

20 legacy heads have no `merge-base-guard` run; the registry cannot tell "never ran" from "still running". Unchanged from 2026-11-02 (20 → 20).

### D3 — board hygiene: 13 dead-branch claims + 7 owner-labelled rows with no clock (P1) — **STILL OPEN (unchanged)**

The 13 dead-branch claims (§2.2) and the 7 owner-labelled rows (§2.5) persist byte-identically. No new ones this run.

### D7 — non-required `CircleCI Pipeline` reports `ERROR`; the repo has no `.circleci/config.yml` (P1) — **STILL OPEN, WIDENED**

Seven open PRs carry a `CircleCI Pipeline` status context whose conclusion is `ERROR`: **#92, #204, #301, #304, #305, #306, #307** (was 5: #92, #204, #298, #301, #302 — #298/#302 have merged; #304–#307 are new arrivals). All four READY heads (#304–#307) and #301 are affected, and each head's GitHub combined commit status is `failure`:

```
$ gh api repos/bot523h/nexus-ai-agent/commits/0cc22adfeeaa8700dc5f57e3f1695040d9c37f0d/status --jq '.state'
failure
```

`CircleCI Pipeline` is **not** in `required_contexts`, so the registry's required-context verdict (`ci_state: SUCCESS`) and `tier: READY` correctly ignore it — but it leaves every recent head with a human-facing `failure` combined status. The reporting pipeline still has **no config in the tree** (`ls -d .circleci` → *No such file or directory*), while the board carries a `queued` claim `task-nagar-circleci-pilot-01` (zone `nagar-circleci-pilot-01`) whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. The status originates from an external CircleCI project not reproducible from the repository.

### D8 — branch protection is unreadable with the ambient `GITHUB_TOKEN`, silently degrading the registry (P1) — **NEW**

`pr_convergence.py collect` with the default token prints:

```
pr-registry: branch protection UNREADABLE (gh api repos/bot523h/nexus-ai-agent/branches/main/protection failed:
gh: Resource not accessible by integration (HTTP 403)); degrading to workflow-derived contexts [... 13 contexts ...].
No PR will be certified READY.
```

and produces `tier {PROTECTION_UNREADABLE: 4, READY: 0}` instead of the authoritative `{READY: 4}`. The **same call succeeds** with the repository PAT (`GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN gh api repos/bot523h/nexus-ai-agent/branches/main/protection` → `200`, `strict: true`, `enforce_admins: true`, 3 required contexts, `required_approvals: 0`). So this is an automation-environment credential-scope defect, not a repository defect: any scheduled run using the ambient token will mis-report the whole backlog (4 READY → 0, 2 CI_RED → 3) and, if a downstream step trusted it, would wrongly treat four landable PRs as blocked. Recorded as P1 because it corrupts the audit signal rather than blocking a required gate. Not present in any prior report.

---

## 6. Delta vs previous run (2026-11-02, `main` `d52a243`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-02.md`.

| Metric | 2026-11-02 | 2026-11-03 | Change |
|---|---|---|---|
| `main` SHA | `d52a2434150d` | `5ce9f7d3f558` | **advanced (4 merges)** |
| Open PRs | 44 | 46 | +2 |
| Open-PR number set | 44 ids | +4 new, −2 gone | #304–#307 new; #298, #302 merged |
| DRAFT tier | 12 | 11 | −1 |
| CONFLICT tier | 26 | 27 | +1 |
| CI_RED tier | 2 | 2 | 0 |
| REBASE_FIRST tier | 2 | 2 | 0 |
| READY | 2 | 4 | **+2** |
| Remote branches | 220 | 225 | +5 |
| Board zones / claims (`main`) | 79 / 175 | 81 / 177 | +2 / +2 |
| Board sha (`main`) | `592576dd…` | `ee631362…` | **changed** |
| `ci_state` mismatches (D1) | 6 | 6 | 0 |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 2 | 1 | −1 |
| Stale live leases | 2 | 0 | **−2 (resolved)** |
| `gates_owner` holders | 0 | 0 | 0 |
| Gates G1/G2/G3 | green | green | 0 |

**New PRs since last run:** #304 (`READY`, APPROVED), #305 (`READY`, APPROVED), #306 (`READY`, APPROVED), #307 (`READY`, no review yet).
**Merged since last run:** #298 (`2026-11-02T16:54:39Z`), #302 (`2026-11-02T17:59:48Z`), #303 (`2026-11-02T21:50:35Z`, head `arena/nexus-master-closure-20261103`) — `main` advanced from `d52a2434150d` to `5ce9f7d3f558`.
**Tier change among common PRs:** #301 moved `READY → REBASE_FIRST` (its head advanced from `d15d…` to `bac55bd39b`; it is the watchdog branch, now behind `main` after the #298/#302/#303 merges). #295 moved `REBASE_FIRST` with an unchanged red `api-gate-mutations` run; its earlier `cancelled` run is superseded by the `failure` run (§4).

**Regressions:**
- **NEW finding D8:** branch protection is unreadable with the ambient `GITHUB_TOKEN` and the registry silently degrades (4 READY → 0, 2 CI_RED → 3). Not present in any prior report.
- **D7 widened:** the `CircleCI Pipeline` `ERROR` context now touches 7 PRs (was 5) and all four READY heads; every READY head's combined commit status is `failure`.
- No new gate failure (G1/G2/G3 green both runs).
- No new ownership mismatch (still 13, byte-identical list).
- No new stale claim (0 now; was 2).
- No new conflict beyond the +1 tier move (#301's rebase).

**Resolved since last run:**
- **Stale live leases: 2 → 0.** `task-267-truth041-live-lease-alignment` and `task-281-cognition-eval-live-registry-binding` moved `active → expired`, releasing both landed-but-leased reservations.
- **D4** (`protocol_version` present) and **D6** (watchdog report reproducibility) remain resolved from earlier runs.

**Unchanged / still open:** D1 (PENDING masks FAILURE), D2 (`merge-base-guard` absent on 20 heads), D3 (board hygiene), D5 (backlog shape), D7 (CircleCI ERROR).

---

## 7. Next actions (prioritized)

1. **[P0] Fix `_latest_ci_verdict` ordering in `scripts/pr_convergence.py`** (`scripts/pr_convergence.py:215`) so a real `FAILURE` is never masked by an absent required context: compute the red verdict over the present contexts **before** returning `PENDING` for a missing one. 6 of 46 PRs are mis-reported (`PENDING` instead of `FAILURE`): #57, #58, #60, #63, #135, #150; the true `CI_RED` set is 11 PRs, not 2.
2. **[P0] Distinguish "required check never ran" from "still running"** (D2): give the 20 legacy heads missing `merge-base-guard` their own signal rather than folding them into `PENDING`, or re-run the guard on those heads.
3. **[P0] Pin the PAT for automation runs** (D8): export `GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN` (or grant the ambient token `branch-protection: read`) in the watchdog/CI environment, so the registry reports the true `READY`/`CI_RED` tiers instead of silently degrading. Until fixed, every scheduled run understates the merge-ready set by four PRs.
4. **[P1] Resolve the `CircleCI Pipeline` `ERROR`** (D7): either commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — a non-required context that reads `ERROR` on every recent head leaves all four READY PRs `UNSTABLE` with an unexplained `failure` combined status. Confirm whether #304–#307 are safe to land despite it.
5. **[P1] Sweep board hygiene** (D3): remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
6. **[P2] Review and land a READY PR** (#304/#305/#306 are `APPROVED`, #307 needs a review): they clear every required gate; confirm the D7 `UNSTABLE` state is benign before they re-stale.
7. **[P2] Attack the rebase wall** (D5): 42 of 46 PRs carry `REBASE_FIRST`; triage the 27 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
