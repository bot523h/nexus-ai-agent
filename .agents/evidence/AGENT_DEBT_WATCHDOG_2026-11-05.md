# Engineering-Debt Watchdog — 2026-11-05

- **Run timestamp (UTC):** `2026-11-05T02:51:09Z` (registry `generated_at`; report written immediately after)
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `cf427874e5c6487243ba1371b1c47dd920fc73a3` (`cf42787`, "chore(board): release task-omni-live-closeout-01 (#312)")
- **Observer:** NEXUS engineering-debt watchdog automation — **observe-and-report only**; no product-code edits, no merge, no push to `main`. The only file created is this one under `.agents/evidence/`.
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-04.md` (`main` `5ce9f7d3f558`) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable or a computation was ambiguous, that is stated explicitly.

> **Credential note (this run).** The registry is collected with the **repository PAT** (`GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN`), which reads the branch-protection API (`protection_state: readable`). With the ambient `GITHUB_TOKEN` the same call returns `403 Resource not accessible by integration` and the registry silently degrades to workflow-derived contexts and certifies **no PR as READY** (finding **D8**, still open). §1 uses the PAT run.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `cf427874e5c6` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` on **`main`** (`/tmp/board_main.json`, sha256 `cdd3f02d…`) | schema 2; **88 zones, 186 claims**, 10 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; board `updated_at = 2026-11-04T19:22:49Z` |
| `.agents/board.json` in the **working tree** (branch `automation/debt-watchdog` @ `37e77ed`) | **stale**: 79 zones, 175 claims, `updated_at = 2026-10-31T15:35:11Z`, sha256 `592576dd…` — the branch never absorbed main's 2026-11-02/11-04 board (finding **N3**) |
| Remote branches | **234** (`git ls-remote --heads origin \| wc -l`; **196** are `refs/heads/arena/*`) |
| Open PRs | **44** (`GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py collect --out /tmp/reg.json`) |
| Branch protection | **readable with the PAT** (`protection_state: readable`, `required_approvals: 0`) |

---

## 1. Live PR registry

```
$ GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: 44 open PRs → /tmp/reg.json (main cf427874e5c6)
COLLECT EXIT=0

$ GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-05T02:51:09Z`, `main_sha`: `cf427874e5c6487243ba1371b1c47dd920fc73a3`
- `open_total`: `44`, `protection_state`: `readable`, `required_approvals`: `0`
- `required_contexts`: `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "merge-base-guard (base == main)"]` (the API `checks[]` list)
- `ci_state` histogram: `{SUCCESS: 19, FAILURE: 5, PENDING: 20}`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-11-04 |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 11 | 0 |
| CONFLICT | 27 | 0 |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 2 | **−1** |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 2 | **+1** |
| PROTECTION_UNREADABLE | 0 | 0 |
| **READY** | **2** | **−3** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 11 · `CONFLICT` 34 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 5 · `CI_PENDING` 20 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 42 · `PROTECTION_UNREADABLE` 0.

**Non-READY PRs, controlling blocker (first in the ordered list):**

| PR | Tier | Controlling blocker | CI state (registry) | Head SHA | Title |
|---|---|---|---|---|---|
| #33 | CONFLICT | CONFLICT | SUCCESS | `9942939713` | v3.13.0 — deliver the P0 security code, wire the dead engines, fix 4 P0 … |
| #57 | CONFLICT | CONFLICT | PENDING | `2f591618e1` | feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-160) |
| #58 | DRAFT | DRAFT | PENDING | `bb06da1cb3` | Security Boundary hardening — S1/S2/S3/S4/S5 (guard STOP, force-join, …) |
| #60 | CONFLICT | CONFLICT | PENDING | `efea9bae3e` | M0 runtime integration: InstrumentedJobQueue + readyz factory + Q1–Q5 |
| #63 | CONFLICT | CONFLICT | PENDING | `3e376bea06` | task-163: board-Git truth gate + scale-to-zero state tiers (#1 + #3) |
| #64 | CI_RED | CI_RED | FAILURE | `ab564d7cc7` | Creative Runtime (task-176 s2): execution semantics, multi-segment … |
| #66 | CONFLICT | CONFLICT | PENDING | `db18cf66f3` | P0 creative integration: one production path, closed legacy lane, … |
| #69 | CONFLICT | CONFLICT | PENDING | `7498074867` | Gate 4: cross-layer vertical slice verification (T01·T02·T22·T31·T64) |
| #73 | CONFLICT | CONFLICT | PENDING | `1a35ec0697` | Gate 2.2 — Operation Truth → Executable Evidence Gate (anti-drift) |
| #84 | CONFLICT | CONFLICT | PENDING | `89e04c6a24` | Register the NEXUS Engineering Constitution as an enforced contract |
| #89 | CONFLICT | CONFLICT | PENDING | `2f0df4f11e` | task-122: 12 real command wirings + moderation ingress — forensic close |
| #92 | DRAFT | DRAFT | SUCCESS | `40c1163314` | fix(storage): honor SQLite defaults; preserve inherited release audit … |
| #97 | CI_RED | CI_RED | FAILURE | `3307d3978f` | feat(vision): complete portrait and scene Vision packs |
| #99 | CONFLICT | CONFLICT | SUCCESS | `9a862e26ad` | task-193: command truth — 11 fake/silent commands wired to the engines |
| #113 | CONFLICT | CONFLICT | PENDING | `6a867a77ca` | feat(runtime): W1 final close-out — engine-owned schema fast-path (E2/…) |
| #114 | DRAFT | DRAFT | FAILURE | `6ca1b13bfa` | feat(cockpit): Persian capability explorer and input-only command lab |
| #115 | CONFLICT | CONFLICT | SUCCESS | `f198734859` | W1: True Runtime Closure / Lifecycle Ownership / Zero Semantic Bypass |
| #117 | DRAFT | DRAFT | PENDING | `4a4b30c2d1` | fix(runtime): close audited session/media contracts and wheel resource … |
| #118 | DRAFT | DRAFT | PENDING | `d5a205f40a` | W2 golden hardening: authority races, probe permits and independent pr… |
| #121 | CONFLICT | CONFLICT | PENDING | `b4a5965320` | feat(memory): retrieval truth — noise-ranked recall (hit@3 0.167) → gu… |
| #122 | CONFLICT | CONFLICT | SUCCESS | `8b8ba31d44` | fix(orchestration): H1 — route chat through the memory reader (task-20…) |
| #129 | CONFLICT | CONFLICT | PENDING | `75f646a52e` | feat(creative/studio): staged plan transactions and atomic commit |
| #135 | CONFLICT | CONFLICT | PENDING | `f83b12835c` | feat(creative/studio): canonical PlanTransaction substrate and durable … |
| #137 | CONFLICT | CONFLICT | PENDING | `e2a4614472` | board(task-227,task-228): collision preflight + executable governance |
| #140 | CONFLICT | CONFLICT | PENDING | `25da562986` | Harden Studio CommandBus: PlanTransaction, Logical Identity, and Target… |
| #149 | CONFLICT | CONFLICT | PENDING | `e865c14650` | Security Wave 1 Verification & Governance Alignment |
| #150 | DRAFT | DRAFT | PENDING | `5f84be4dd2` | feat(agent-intelligence): verified CreativeWork trim path |
| #157 | CONFLICT | CONFLICT | PENDING | `00ccf90326` | feat(governance): Nagar agent constitution — persistent, versioned, CI |
| #193 | CONFLICT | CONFLICT | SUCCESS | `d505d80a4e` | feat(infra): canonical Ubuntu 26.04 + Python 3.14 foundation |
| #204 | DRAFT | DRAFT | SUCCESS | `13078d7926` | fix(memory): resolve sqlite-vec extension fallback and add forensic ch… |
| #205 | CONFLICT | CONFLICT | FAILURE | `ff2792b7ad` | feat(nagar): Asset Library V1 — secure content-addressed ingest + dura… |
| #230 | CONFLICT | CONFLICT | SUCCESS | `9f572aae41` | Arena/aa451c49 nexus ai agent |
| #247 | CONFLICT | CONFLICT | SUCCESS | `d9176f9bcb` | feat(creative): perception lane — measured silence/scene/loudness evid… |
| #257 | DRAFT | DRAFT | SUCCESS | `b057ef5fda` | 🎨 Palette: Improve Telegram inline callback UX for language and agent … |
| #273 | DRAFT | DRAFT | SUCCESS | `9d29722e6e` | feat(nagar): P0 intent NEL semantic time causal contracts |
| #284 | DRAFT | DRAFT | FAILURE | `82d9be0182` | feat(nagar): add read-only Causal Memory and bounded Director preview |
| #285 | DRAFT | DRAFT | SUCCESS | `ce1ede4e8d` | chore(board): synchronize task-278 deferred status |
| #290 | CONFLICT | CONFLICT | SUCCESS | `d6437217e9` | chore(board): register task-282 durable project state lifecycle |
| #294 | CONFLICT | CONFLICT | SUCCESS | `9b60307a14` | repair(#189): land the durable creative-graph package (law R28) on cur… |
| #295 | REBASE_FIRST | REBASE_FIRST | SUCCESS | `c641154d19` | extract(#218): land the self-contained content-addressed Asset Library |
| #297 | CONFLICT | CONFLICT | SUCCESS | `f142f2e923` | evidence(m16): true closeout of the M15 spine + open-PR convergence pl… |
| #301 | REBASE_FIRST | REBASE_FIRST | SUCCESS | `37e77ed140` | chore(automation): engineering-debt watchdog 2026-11-04 |

**The two READY PRs** (reproduce: `gh api repos/bot523h/nexus-ai-agent/pulls/<n>`):

| PR | Head SHA | Mergeable | Merge state | Review decision | Registry `ci_state` |
|---|---|---|---|---|---|
| #313 | `ca7f9ea886` | MERGEABLE | UNSTABLE | (none) | SUCCESS |
| #314 | `55dd1f7584` | MERGEABLE | UNSTABLE | (none) | SUCCESS |

Both READY heads carry a **combined commit status of `failure`** because of the non-required `CircleCI Pipeline` context — finding **D7** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/status --jq .state` → `failure` on both).

**Non-required contexts on the READY heads** (`.../commits/<sha>/status`): `#313` → `CircleCI Pipeline:error`; `#314` → `CircleCI Pipeline:error`.

---

## 2. Board audit

```
$ python -m pytest -q tests/unit/test_agent_board.py
75 passed in 1.64s
EXIT=0
```

The authoritative board is the one on **`main`** (`git show origin/main:.agents/board.json | sha256sum` → `cdd3f02d23d0a7b26913dcea85ee90308c58ccd4dc2013c3a2094f8f3fdfef6d`). The **working tree** board (branch `automation/debt-watchdog` @ `37e77ed`) hashes `592576ddb556f208f0784132b36f0868d0a4f66402f86f5de83c724f446bee33` — 79 zones / 175 claims / `updated_at 2026-10-31T15:35:11Z`. That is **four days stale vs main** and is recorded as finding **N3**. The audit below is against the **main** board.

- **Claims:** 186. **Zones:** 88. Status histogram: `done` 80, `queued` 60, `expired` 20, `completed_merged` 9, `completed_released` 5, `deferred` 3, `available_sequenced_post_33` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1. **`active` 0.**
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).
- **Claims carrying an `agent_branch`:** 21.
- **Board delta vs 2026-11-04:** **+7 zones, +9 claims**, `updated_at 2026-11-02T20:01:41Z → 2026-11-04T19:22:49Z`, sha `ee631362… → cdd3f02d…`.

### 2.1 Active claims — 0 (no live lease)

`python3 -c "…sum(1 for c in claims if c['status']=='active')"` → **0**. The 2026-11-04 report recorded **1** active (stale) lease — `task-nagar-workspace-redelivery-01`. That claim is now `status: expired` in the main board, with `release_reason: "auto-released by gc at 2026-11-04T11:47:02Z (stale active lease)"`. The **stale live lease is resolved** (see §6).

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30 … 2026-11-04 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (234 refs) and the 44 open-PR heads. No **new** mismatch this run.

### 2.3 Stale live leases — 0 (RESOLVED; was 1)

No claim is `active`, so no lease is past its 24 h TTL. Reproduce: for each `active` claim, assert `claimed_at + ttl_hours > now` (now = `2026-11-05T02:51Z`).

### 2.4 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations, plain string-prefix test), and every claim that names a `zone` names a declared one (0 undeclared). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry starts with a `zone.paths` entry.

### 2.5 Unleased non-terminal claims — 60 (context, not a gate)

60 non-terminal claims carry neither `agent_branch` nor `claimed_at` (the 60 `queued` rows). Of these, most are `pr-repair-<n>` / planning rows with no owner; they are not leases and cannot collide, so they are recorded for completeness only. The 7 owner-labelled rows without a branch or clock (`task-122` `assigned_to_B`, `task-123` `assigned_to_E_pr33`, `task-124`, `task-126`, `task-121`, `task-128`, `task-127`) remain the actionable subset — an owner label that no machine check can verify.

---

## 3. Gates

| Gate | Command | Result | Exit |
|---|---|---|---|
| **G1** truth gate | `python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| **G2** matrix + docs | `pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `119 passed in 4.01s` | 0 |
| **G3** board contract | `pytest -q tests/unit/test_agent_board.py` | `75 passed in 1.64s` | 0 |

Environment: Python `3.13.15`, editable install (`nexus_ai_agent` resolves to `src/nexus_ai_agent/__init__.py`). All three gates GREEN — no gate regressed vs the 2026-11-04 run.

---

## 4. CI health — 12 most recently updated open PRs

Required contexts (the API `checks[]` list): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status per context on the **exact head SHA** (from `gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs`, latest run wins), distinguishing `queued`/`in_progress`/`cancelled`/`failure`/`skipped`/`success`/`absent`:

| PR | Head SHA | Updated (UTC) | `lint` | `test` | `merge-base-guard` |
|---|---|---|---|---|---|
| #314 | `55dd1f7584` | 2026-11-04T23:18:46Z | success | success | success |
| #313 | `ca7f9ea886` | 2026-11-04T20:50:07Z | success | success | success |
| #193 | `d505d80a4e` | 2026-11-04T11:52:33Z | success | success | **skipped** |
| #301 | `37e77ed140` | 2026-11-04T03:05:50Z | success | success | success |
| #204 | `13078d7926` | 2026-11-01T20:28:44Z | success | success | success |
| #92 | `40c1163314` | 2026-11-01T20:27:15Z | success | success | success |
| #69 | `7498074867` | 2026-10-31T12:16:59Z | success | success | **absent** |
| #66 | `db18cf66f3` | 2026-10-31T12:16:57Z | success | success | **absent** |
| #290 | `d6437217e9` | 2026-10-31T08:21:47Z | success | success | success |
| #297 | `f142f2e923` | 2026-10-31T08:16:47Z | success | success | success |
| #295 | `c641154d19` | 2026-10-31T08:16:02Z | success | success | success |
| #294 | `9b60307a14` | 2026-10-31T08:14:53Z | success | success | success |

Required-context histograms across all 44 open PRs (latest run per context):

- `lint (ruff + mypy + version lockstep)` → `{success: 40, failure: 4}` (absent: 0)
- `test (pytest -m "not slow")` → `{success: 34, failure: 10}` (absent: 0)
- `merge-base-guard (base == main)` → `{success: 22, skipped: 2, absent: 20}`

`merge-base-guard` is **absent on 20 heads** (unchanged): #57, #58, #60, #63, #66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #135, #137, #140, #149, #150, #157. These are legacy heads the guard workflow never ran against; they are folded into the registry's `PENDING` rather than given their own signal (finding **D2**). The **2 `skipped`** runs are on #205 (`2026-10-15T08:57:22Z`) and #193 (`2026-10-09T04:50:24Z`) — a *skipped* required check is not a pass, so both heads cannot be certified green by this context.

**#301 (the watchdog branch's own PR) is GREEN again (resolves 2026-11-04's N2).** Its head advanced `596d2ef24b → 37e77ed140`; the required `test (pytest -m "not slow")` job now concludes `success` (`2026-11-04T03:26:30Z`) on the exact head. #301 is now `REBASE_FIRST` (`mergeStateStatus: BEHIND`) rather than `CI_RED`.

**Combined commit status on the two READY heads** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/status --jq .state`): `#313`, `#314` → **`failure`** (both, from the non-required `CircleCI Pipeline` `ERROR` context — finding **D7**).

---

## 5. Findings (with deltas)

### D1 — the registry reports `PENDING` where the real verdict is `FAILURE` (P0) — **STILL OPEN (unchanged, 6 PRs)**

Six open PRs have at least one required context that **failed** while another required context (`merge-base-guard`) is **absent**; `_latest_ci_verdict` returns `PENDING` (missing context) before it evaluates the red present one:

| PR | `lint` | `test` | `merge-base-guard` | Registry `ci_state` | True verdict |
|---|---|---|---|---|---|
| #57 | **failure** | success | absent | PENDING | FAILURE |
| #58 | **failure** | **failure** | absent | PENDING | FAILURE |
| #60 | **failure** | **failure** | absent | PENDING | FAILURE |
| #63 | success | **failure** | absent | PENDING | FAILURE |
| #135 | success | **failure** | absent | PENDING | FAILURE |
| #150 | **failure** | **failure** | absent | PENDING | FAILURE |

The registry's `CI_RED` tier (2: #64, #97) therefore undercounts: the true red set is `[57, 58, 60, 63, 64, 97, 114, 135, 150, 205, 284]` (**11 PRs**). Same 6 masked PRs as the 2026-11-02, 2026-11-03 and 2026-11-04 reports. All six failures are stale (latest failing runs dated 2026-09-23 … 2026-10-04), i.e. these heads have not been re-pushed since.

### D2 — `merge-base-guard` absent on 20 heads, reported as `PENDING` (P0) — **STILL OPEN (unchanged)**

20 legacy heads have no `merge-base-guard` run; the registry cannot tell "never ran" from "still running". Unchanged from 2026-11-02 … 2026-11-04 (20 → 20).

### D3 — board hygiene: 13 dead-branch claims + 7 owner-labelled rows with no clock (P1) — **STILL OPEN (unchanged)**

The 13 dead-branch claims (§2.2) and the 7 owner-labelled rows (§2.5) persist byte-identically. No new ones this run.

### D7 — non-required `CircleCI Pipeline` reports `ERROR`; the repo has no `.circleci/config.yml` (P1) — **STILL OPEN, NARROWED**

Five open PRs carry a `CircleCI Pipeline` status context whose conclusion is `ERROR`: **#92, #204, #301, #313, #314** (was 8 on 2026-11-04 — #304–#308 all merged, so the count fell; #313 and #314 are the new arrivals). **Both READY heads are affected**, and each head's GitHub combined commit status is `failure` (§4). The reporting pipeline still has **no config in the tree** (`ls -d .circleci` → *No such file or directory*), while the board carries a `queued` claim `task-nagar-circleci-pilot-01` (zone `nagar-circleci-pilot-01`) whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. The status originates from an external CircleCI project not reproducible from the repository.

### D8 — branch protection is unreadable with the ambient `GITHUB_TOKEN`, silently degrading the registry (P1) — **STILL OPEN (unchanged)**

`pr_convergence.py collect` with the default token prints `branch protection UNREADABLE (… 403 Resource not accessible by integration); degrading to workflow-derived contexts … No PR will be certified READY.` The same call succeeds with the repository PAT. Recorded as P1 because it corrupts the audit signal rather than blocking a required gate.

### N3 — the automation branch still carries a four-day-stale board (P2) — **STILL OPEN (worsening)**

`.agents/board.json` on branch `automation/debt-watchdog` (`37e77ed`) hashes `592576dd…` with 79 zones / 175 claims / `updated_at 2026-10-31T15:35:11Z`; `main` hashes `cdd3f02d…` with 88 zones / 186 claims / `updated_at 2026-11-04T19:22:49Z`. The gap grew from 2 days (2026-11-04) to 4 days: the branch has absorbed no board from main since 2026-10-31, so any tool reading the worktree board (`check`/`next` without an explicit source) sees a board missing 9 zones/11 claims and the current state. Recorded, not fixed (watchdog is read-only outside `.agents/evidence/`).

### N5 — two READY PRs are blocked from a clean merge only by a non-required `ERROR` context (P2) — **NEW**

#313 and #314 are the only two PRs that clear every required gate, yet both heads are `UNSTABLE` solely because the non-required `CircleCI Pipeline` context reports `ERROR` (D7). This is the same coupling that made the five 2026-11-04 READY heads read `failure`; it now gates the two current ones. Reproduce: §1 READY table + `.../commits/<sha>/status`.

---

## 6. Delta vs previous run (2026-11-04, `main` `5ce9f7d3`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-04.md`.

| Metric | 2026-11-04 | 2026-11-05 | Change |
|---|---|---|---|
| `main` SHA | `5ce9f7d3f558` | `cf427874e5c6` | **advanced** |
| Open PRs | 47 | 44 | −3 |
| Open-PR number set | 47 ids | +2 new, −5 gone | #313, #314 new; #304–#308 merged |
| DRAFT tier | 11 | 11 | 0 |
| CONFLICT tier | 27 | 27 | 0 |
| CI_RED tier | 3 | 2 | **−1** |
| REBASE_FIRST tier | 1 | 2 | **+1** |
| READY | 5 | 2 | **−3** |
| Remote branches | 227 | 234 | +7 |
| Board zones / claims (`main`) | 81 / 177 | 88 / 186 | **+7 / +9** |
| Board sha (`main`) | `ee631362…` | `cdd3f02d…` | changed |
| `ci_state` mismatches (D1) | 6 | 6 | 0 |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 1 | 0 | **−1** |
| Stale live leases | 1 | 0 | **−1 (RESOLVED)** |
| `gates_owner` holders | 0 | 0 | 0 |
| Gates G1/G2/G3 | green | green | 0 |

**New PRs since last run:** #313 (`READY`, `chore(board): register task-nagar-preservation-media-profiles-01 (ARENA-B)`), #314 (`READY`, `feat(creative): durable, restartable Studio project revisions (task-282)`).
**Merged since last run (5):** #304, #305, #306, #307, #308 — `main` advanced `5ce9f7d3f558 → cf427874e5c6` via #309–#312.
**Tier changes among common PRs:** #301 `CI_RED → REBASE_FIRST` (head `596d2ef24b → 37e77ed140`; required `test` now green).

**Regressions:**
- **READY count fell 5 → 2.** The five 2026-11-04 READY PRs were all merged; only #313/#314 remain, and both are `UNSTABLE` on the non-required CircleCI context (N5).
- **N3 worsened** (board staleness on the automation branch: 2 days → 4 days).
- No new gate failure (G1/G2/G3 green both runs).
- No new ownership mismatch (still 13, byte-identical list).
- No new stale lease (0 both runs).

**Resolved since last run:**
- **N1 (stale live lease)** — RESOLVED: `task-nagar-workspace-redelivery-01` was auto-released by `gc` at `2026-11-04T11:47:02Z`; 0 active claims now.
- **N2 (PR #301 CI_RED on disk exhaustion)** — RESOLVED: #301's head advanced and the required `test` job is green.
- **N4 (required `test` job failing on runner disk exhaustion)** — RESOLVED: the mitigation #308 (`fix(ci): install CPU-only torch so the required test job cannot exhaust disk`) merged on `2026-11-04T11:27:25Z`; the `test` histogram improved `{success: 36, failure: 11}` → `{success: 34, failure: 10}` on a smaller PR set.
- **D7 narrowed** (8 PRs → 5) but now touches both READY heads.

**Unchanged / still open:** D1 (PENDING masks FAILURE, 6 PRs), D2 (`merge-base-guard` absent on 20 heads), D3 (board hygiene), D7 (CircleCI ERROR), D8 (ambient-token protection 403), N3 (stale automation-branch board).

---

## 7. Next actions (prioritized)

1. **[P0] Land the two READY PRs** (#313, #314). They are the only PRs that clear every required gate; both are `UNSTABLE` solely on the non-required CircleCI `ERROR` context (D7/N5). Confirm that state is benign, get a review (neither has a review decision), and merge before `main` moves again and they join the rebase wall.
2. **[P0] Fix `_latest_ci_verdict` ordering in `scripts/pr_convergence.py`** (D1) so a real `FAILURE` is never masked by an absent required context: compute the red verdict over the present contexts **before** returning `PENDING` for a missing one. 6 of 44 PRs are mis-reported (`PENDING` instead of `FAILURE`): #57, #58, #60, #63, #135, #150; the true `CI_RED` set is 11 PRs, not 2.
3. **[P0] Distinguish "required check never ran" from "still running"** (D2): give the 20 legacy heads missing `merge-base-guard` their own signal rather than folding them into `PENDING`, or re-run the guard on those heads. Also treat the 2 `skipped` runs (#205, #193) as non-green.
4. **[P1] Resolve the `CircleCI Pipeline` `ERROR`** (D7): either commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — a non-required context that reads `ERROR` on both READY heads is what keeps them `UNSTABLE`.
5. **[P1] Re-sync the automation branch's board** (N3): `automation/debt-watchdog` carries a 2026-10-31 board (79/175) while `main` is at 2026-11-04 (88/186); merge `main` into the branch so the worktree board is not stale for `check`/`next`. (Read-only here — recorded, not fixed.)
6. **[P1] Pin the PAT for automation runs** (D8): export `GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN` (or grant the ambient token `branch-protection: read`) so the registry reports the true `READY`/`CI_RED` tiers instead of silently degrading.
7. **[P2] Sweep board hygiene** (D3): remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
8. **[P2] Attack the rebase wall**: 42 of 44 PRs carry `REBASE_FIRST`; triage the 27 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work. Note #295 and #301 are `BEHIND` but otherwise clean, so they are the cheapest rebases.
9. **[P2] Re-push or close the 6 masked stale heads** (#57, #58, #60, #63, #135, #150): their latest required-context failures are dated 2026-09-23 … 2026-10-04; they have not been re-pushed, so they will read red for the registry as soon as D1 is fixed.
10. **[P2] Record the resolved findings** (N1, N2, N4): the stale lease, the watchdog PR's red CI, and the disk-exhaustion mitigation all closed this run — no board action needed, but keep the automation branch's board in sync (item 5) so the next run's delta stays meaningful.
