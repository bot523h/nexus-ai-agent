# Engineering-Debt Watchdog — 2026-11-04

- **Run timestamp (UTC):** 2026-11-04T03:03:32Z (registry `generated_at`; report written immediately after)
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `5ce9f7d3f558ab903ece5f19018b28a5e2ff9a04` (`5ce9f7d3`, "Merge pull request #303 from bot523h/arena/nexus-master-closure-20261103") — **unchanged from 2026-11-03**
- **Observer:** NEXUS engineering-debt watchdog automation (observe-and-report only; no product-code edits, no merge, no push to `main`)
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-03.md` (`main` `5ce9f7d3f558`) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable or a computation was ambiguous, that is stated explicitly.

> **Credential note (this run).** As on 2026-11-03, the registry is collected with the **repository PAT** (`GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN`), which reads the branch-protection API (`protection_state: readable`). With the ambient `GITHUB_TOKEN` the same call returns `403 Resource not accessible by integration` and the registry silently degrades to workflow-derived contexts and certifies **no PR as READY** (finding **D8**, still open). §1 uses the PAT run.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `5ce9f7d3f558` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` on **`main`** (`/tmp/board_main.json`, sha256 `ee631362…`) | schema 2; 81 zones, 177 claims, 10 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; board `updated_at = 2026-11-02T20:01:41Z` |
| `.agents/board.json` in the **working tree** (branch `automation/debt-watchdog` @ `596d2ef2`) | **stale**: 79 zones, 175 claims, `updated_at = 2026-10-31T15:35:11Z` — the branch never absorbed main's 2026-11-02 board (finding **N3**) |
| Remote branches | **227** (`git ls-remote --heads origin \| wc -l`; 189 are `refs/heads/arena/*`) |
| Open PRs | **47** (`GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py collect --out /tmp/reg.json`) |
| Branch protection | **readable with the PAT** (`protection_state: readable`, `required_approvals: 0`) |

---

## 1. Live PR registry

```
$ GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: 47 open PRs → /tmp/reg.json (main 5ce9f7d3f558)
COLLECT EXIT=0

$ GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-04T03:03:32Z`, `main_sha`: `5ce9f7d3f558ab903ece5f19018b28a5e2ff9a04`
- `open_total`: `47`, `protection_state`: `readable`, `required_approvals`: `0`
- `required_contexts`: `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "merge-base-guard (base == main)"]` (the API `checks[]` list)
- `ci_state` histogram: `{SUCCESS: 21, FAILURE: 6, PENDING: 20}`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-11-03 |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 11 | 0 |
| CONFLICT | 27 | 0 |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 3 | **+1** |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 1 | **−1** |
| PROTECTION_UNREADABLE | 0 | 0 |
| **READY** | **5** | **+1** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 11 · `CONFLICT` 34 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 6 · `CI_PENDING` 20 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 42 · `PROTECTION_UNREADABLE` 0.

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
| #301 | CI_RED | CI_RED | FAILURE | `596d2ef24b` | chore(automation): engineering-debt watchdog 2026-11-03 |

**The five READY PRs** (reproduce: `gh api repos/bot523h/nexus-ai-agent/pulls/<n>`):

| PR | Head SHA | Mergeable | Review decision | Registry `ci_state` |
|---|---|---|---|---|
| #308 | `be93808b22` | MERGEABLE | (none) | SUCCESS |
| #307 | `0becd39e10` | MERGEABLE | (none) | SUCCESS |
| #306 | `0c552ff211` | MERGEABLE | APPROVED | SUCCESS |
| #305 | `e55d8bf83a` | MERGEABLE | APPROVED | SUCCESS |
| #304 | `0cc22adfee` | MERGEABLE | APPROVED | SUCCESS |

All five READY heads carry a **combined commit status of `failure`** because of the non-required `CircleCI Pipeline` context — finding **D7** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/status --jq .state` → `failure` on all five).

---

## 2. Board audit

```
$ python -m pytest -q tests/unit/test_agent_board.py
75 passed in 1.49s
EXIT=0
```

The authoritative board is the one on **`main`** (`git show origin/main:.agents/board.json | sha256sum` → `ee631362cc2f7beaa2fb770bb5e3a78d9ad576d53e24d776cbf8ff86a0635ad1`). The **working tree** board (branch `automation/debt-watchdog` @ `596d2ef2`) hashes `592576ddb556f208f0784132b36f0868d0a4f66402f86f5de83c724f446bee33` — 79 zones / 175 claims / `updated_at 2026-10-31T15:35:11Z`. That is **two days stale vs main** and is recorded as finding **N3**. The audit below is against the **main** board.

- **Claims:** 177. **Zones:** 81. Status histogram: `done` 79, `queued` 59, `expired` 19, `completed_released` 5, `completed_merged` 3, `available_sequenced_post_33` 2, `deferred` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1, `active` 1.
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).
- **Board delta vs 2026-11-03:** **none** — identical `main` sha, identical 81/177, identical `updated_at 2026-11-02T20:01:41Z`, identical `ee631362…` hash.

### 2.1 Active claims — 1 (branch live, lease **STALE**)

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-nagar-workspace-redelivery-01` | `nagar-workspace-redelivery-01` | `arena/nexus-master-closure-20261103` | yes | no | 24 | 2026-11-02T20:01:41Z | expires 2026-11-03T20:01:41Z → **EXPIRED 6.9 h before this run** |

The lease lapsed at `2026-11-03T20:01:41Z`; at the snapshot time (`2026-11-04T02:55:39Z`) it is **6.9 h past TTL**. The branch `arena/nexus-master-closure-20261103` exists remotely but its work already landed via **PR #303** (`main` advanced to `5ce9f7d3`), so this is a **landed-but-still-leased, now-expired** reservation. `gc`/`show` would auto-release it. This is a **regression** vs 2026-11-03, which recorded **0 stale live leases** (finding **N1**).

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30, 2026-10-31, 2026-11-01, 2026-11-02 and 2026-11-03 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (227 refs) and the 47 open-PR heads.

### 2.3 Stale live leases — 1 (REGRESSION; was 0)

`task-nagar-workspace-redelivery-01` is `active` and past its 24 h TTL (§2.1). Reproduce: for each `active` claim, assert `claimed_at + ttl_hours > now` (now = `2026-11-04T02:55:39Z`).

### 2.4 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations, plain string-prefix test), and every claim that names a `zone` names a declared one (0 undeclared). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry starts with a `zone.paths` entry.

### 2.5 Unleased non-terminal claims — 66 (context, not a gate)

66 non-terminal claims carry neither `agent_branch` nor `claimed_at` (59 `queued` + the 7 owner-labelled rows). Of these, the 59 `queued` are mostly `pr-repair-<n>` planning rows with no owner; they are not leases and cannot collide, so they are recorded for completeness only. The 7 owner-labelled rows without a branch or clock (`task-122`, `task-123`, `task-124`, `task-126`, `task-121`, `task-128`, `task-127`) remain the actionable subset — an owner label that no machine check can verify.

---

## 3. Gates

| Gate | Command | Result | Exit |
|---|---|---|---|
| **G1** truth gate | `python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| **G2** matrix + docs | `pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `117 passed in 4.04s` | 0 |
| **G3** board contract | `pytest -q tests/unit/test_agent_board.py` | `75 passed in 1.49s` | 0 |

Environment: Python `3.13.15`, editable install (`nexus_ai_agent` resolves to `src/nexus_ai_agent/__init__.py`). All three gates GREEN — no gate regressed vs the 2026-11-03 run.

---

## 4. CI health — 12 most recently updated open PRs

Required contexts (the API `checks[]` list): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status per context on the **exact head SHA** (from `gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs`, latest run wins), distinguishing `queued`/`in_progress`/`cancelled`/`failure`/`skipped`/`success`/`absent`:

| PR | Head SHA | Updated (UTC) | `lint` | `test` | `merge-base-guard` |
|---|---|---|---|---|---|
| #308 | `be93808b22` | 2026-11-04T00:11:55Z | success | success | success |
| #306 | `0c552ff211` | 2026-11-03T23:23:21Z | success | success | success |
| #301 | `596d2ef24b` | 2026-11-03T03:45:13Z | success | **failure** | success |
| #307 | `0becd39e10` | 2026-11-02T23:18:50Z | success | success | success |
| #305 | `e55d8bf83a` | 2026-11-02T22:29:43Z | success | success | success |
| #304 | `0cc22adfee` | 2026-11-02T22:19:19Z | success | success | success |
| #204 | `13078d7926` | 2026-11-01T20:28:44Z | success | success | success |
| #92 | `40c1163314` | 2026-11-01T20:27:15Z | success | success | success |
| #69 | `7498074867` | 2026-10-31T12:16:59Z | success | success | **absent** |
| #66 | `db18cf66f3` | 2026-10-31T12:16:57Z | success | success | **absent** |
| #290 | `d6437217e9` | 2026-10-31T08:21:47Z | success | success | success |
| #297 | `f142f2e923` | 2026-10-31T08:16:47Z | success | success | success |

Required-context histograms across all 47 open PRs (latest run per context):

- `lint (ruff + mypy + version lockstep)` → `{success: 43, failure: 4}` (absent: 0)
- `test (pytest -m "not slow")` → `{success: 36, failure: 11}` (absent: 0)
- `merge-base-guard (base == main)` → `{success: 25, skipped: 2, absent: 20}`

`merge-base-guard` is **absent on 20 heads** (unchanged): #57, #58, #60, #63, #66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #135, #137, #140, #149, #150, #157. These are legacy heads the guard workflow never ran against; they are folded into the registry's `PENDING` rather than given their own signal (finding **D2**).

**#301 — the watchdog branch's own PR is now red (NEW, finding N2).** Two runs of `test (pytest -m "not slow")` exist on head `596d2ef24b`: `cancelled` (completed `2026-11-03T03:46:40Z`) and `failure` (completed `2026-11-03T03:51:51Z`). The later, definitive run is `failure`, so the required `test` context is genuinely red:

```
$ gh run view 41574064615 --repo bot523h/nexus-ai-agent --log-failed
ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device
grep: pytest.log: No such file or directory
```

The failure is **CI disk exhaustion during dependency installation**, not a test assertion failure. The same class of failure explains why the required `test` job shows 11 failures across the backlog (up from 10 on 2026-11-03).

**Combined commit status on the five READY heads** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/status --jq .state`): `#304`, `#305`, `#306`, `#307`, `#308` → **`failure`** (all five, from the non-required `CircleCI Pipeline` `ERROR` context — finding **D7**).

---

## 5. Findings (with deltas)

### N1 — a stale live lease re-appeared (P1) — **NEW (REGRESSION)**

`task-nagar-workspace-redelivery-01` (`arena/nexus-master-closure-20261103`) is `active` with `claimed_at 2026-11-02T20:01:41Z` and a 24 h TTL that lapsed at `2026-11-03T20:01:41Z` — **6.9 h past TTL** at snapshot time. The 2026-11-03 run recorded **0** stale live leases. Its work already landed via PR #303, so it is a landed-but-leased reservation that `gc` would auto-release. Reproduce: §2.1 / §2.3.

### N2 — PR #301 (the watchdog's own branch) turned CI_RED on disk exhaustion (P1) — **NEW (REGRESSION)**

#301's head advanced from `bac55bd39b` (2026-11-03, tier `REBASE_FIRST`) to `596d2ef24b` (tier `CI_RED`). The required `test (pytest -m "not slow")` job failed with `OSError: [Errno 28] No space left on device` during `pip install`. This is the *third* run in a row where the watchdog branch's own PR is not green (it was `REBASE_FIRST` on 2026-11-03 and is `CI_RED` now), and the failure mode is CI-runner capacity, not product code. Reproduce: §4.

### N3 — the automation branch carries a two-day-stale board (P2) — **NEW (hygiene)**

`.agents/board.json` on branch `automation/debt-watchdog` (`596d2ef2`) hashes `592576dd…` with 79 zones / 175 claims / `updated_at 2026-10-31T15:35:11Z`; `main` hashes `ee631362…` with 81 zones / 177 claims / `updated_at 2026-11-02T20:01:41Z`. The branch never merged main's 2026-11-02 board, so any tool reading the worktree board (including `check`/`next` without an explicit source) sees a board that is missing the two IBPC claims/zones and the current active lease. Recorded, not fixed (watchdog is read-only outside `.agents/evidence/`).

### N4 — CI disk exhaustion now hits a required check (P0) — **NEW**

The required `test (pytest -m "not slow")` job fails with `No space left on device` on #301's head (and `test` is red on 11 of 47 open PRs). A required check that fails for runner-capacity reasons makes the whole backlog read `CI_RED`/`PENDING` independently of code quality, and it caused N2. #308 (`fix(ci): install CPU-only torch so the required test job cannot exhaust disk`, READY) is a candidate mitigation awaiting merge.

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

The registry's `CI_RED` tier (3: #64, #97, #301) therefore undercounts: the true red set is `[57, 58, 60, 63, 64, 97, 114, 135, 150, 205, 284, 301]` (**12 PRs**). Same 6 masked PRs as the 2026-11-02 and 2026-11-03 reports.

### D2 — `merge-base-guard` absent on 20 heads, reported as `PENDING` (P0) — **STILL OPEN (unchanged)**

20 legacy heads have no `merge-base-guard` run; the registry cannot tell "never ran" from "still running". Unchanged from 2026-11-02 and 2026-11-03 (20 → 20).

### D3 — board hygiene: 13 dead-branch claims + 7 owner-labelled rows with no clock (P1) — **STILL OPEN (unchanged)**

The 13 dead-branch claims (§2.2) and the 7 owner-labelled rows (§2.5) persist byte-identically. No new ones this run.

### D7 — non-required `CircleCI Pipeline` reports `ERROR`; the repo has no `.circleci/config.yml` (P1) — **STILL OPEN, WIDENED**

Eight open PRs carry a `CircleCI Pipeline` status context whose conclusion is `ERROR`: **#92, #204, #301, #304, #305, #306, #307, #308** (was 7 on 2026-11-03 — #308 is the new arrival). All five READY heads are affected, and each head's GitHub combined commit status is `failure` (§4). The reporting pipeline still has **no config in the tree** (`ls -d .circleci` → *No such file or directory*), while the board carries a `queued` claim `task-nagar-circleci-pilot-01` (zone `nagar-circleci-pilot-01`) whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. The status originates from an external CircleCI project not reproducible from the repository.

### D8 — branch protection is unreadable with the ambient `GITHUB_TOKEN`, silently degrading the registry (P1) — **STILL OPEN (unchanged)**

`pr_convergence.py collect` with the default token prints `branch protection UNREADABLE (… 403 Resource not accessible by integration); degrading to workflow-derived contexts … No PR will be certified READY.` The same call succeeds with the repository PAT. Recorded as P1 because it corrupts the audit signal rather than blocking a required gate.

---

## 6. Delta vs previous run (2026-11-03, `main` `5ce9f7d3`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-03.md`.

| Metric | 2026-11-03 | 2026-11-04 | Change |
|---|---|---|---|
| `main` SHA | `5ce9f7d3f558` | `5ce9f7d3f558` | **0 (no merges)** |
| Open PRs | 46 | 47 | +1 |
| Open-PR number set | 46 ids | +1 new, −0 gone | #308 new; none merged/closed |
| DRAFT tier | 11 | 11 | 0 |
| CONFLICT tier | 27 | 27 | 0 |
| CI_RED tier | 2 | 3 | **+1** |
| REBASE_FIRST tier | 2 | 1 | **−1** |
| READY | 4 | 5 | **+1** |
| Remote branches | 225 | 227 | +2 |
| Board zones / claims (`main`) | 81 / 177 | 81 / 177 | 0 |
| Board sha (`main`) | `ee631362…` | `ee631362…` | 0 |
| `ci_state` mismatches (D1) | 6 | 6 | 0 |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 1 | 1 | 0 |
| Stale live leases | 0 | 1 | **+1 (REGRESSION)** |
| `gates_owner` holders | 0 | 0 | 0 |
| Gates G1/G2/G3 | green | green | 0 |

**New PRs since last run:** #308 (`READY`, `fix(ci): install CPU-only torch so the required test job cannot exhaust disk`).
**Merged since last run:** none — `main` is unchanged at `5ce9f7d3f558`.
**Tier change among common PRs:** #301 moved `REBASE_FIRST → CI_RED` (its head advanced from `bac55bd39b` to `596d2ef24b`; the required `test` job failed on disk exhaustion).

**Regressions:**
- **N1:** a stale live lease re-appeared — `task-nagar-workspace-redelivery-01` is 6.9 h past its 24 h TTL (2026-11-03 recorded 0).
- **N2:** PR #301 (the watchdog branch) turned `CI_RED` on `OSError: [Errno 28] No space left on device`.
- **N4:** the required `test` job now fails on runner disk exhaustion (`test` red on 11/47 PRs, up from 10).
- **D7 widened:** the `CircleCI Pipeline` `ERROR` context now touches 8 PRs (was 7) and all five READY heads.
- No new gate failure (G1/G2/G3 green both runs).
- No new ownership mismatch (still 13, byte-identical list).

**Resolved since last run:**
- **D5** — the `REBASE_FIRST` count fell 2 → 1 (#301's move) and **none** of the previous run's gate/finding list improved; no finding closed this run.

**Unchanged / still open:** D1 (PENDING masks FAILURE), D2 (`merge-base-guard` absent on 20 heads), D3 (board hygiene), D7 (CircleCI ERROR), D8 (ambient-token protection 403).

---

## 7. Next actions (prioritized)

1. **[P0] Land #308 (`fix(ci): install CPU-only torch so the required test job cannot exhaust disk`).** It is READY and directly addresses N2/N4: the required `test` job is failing on `No space left on device` on #301's head and is red on 11 of 47 open PRs. Until it lands, CI capacity — not code quality — is the dominant backlog signal.
2. **[P0] Fix `_latest_ci_verdict` ordering in `scripts/pr_convergence.py`** (D1) so a real `FAILURE` is never masked by an absent required context: compute the red verdict over the present contexts **before** returning `PENDING` for a missing one. 6 of 47 PRs are mis-reported (`PENDING` instead of `FAILURE`): #57, #58, #60, #63, #135, #150; the true `CI_RED` set is 12 PRs, not 3.
3. **[P0] Distinguish "required check never ran" from "still running"** (D2): give the 20 legacy heads missing `merge-base-guard` their own signal rather than folding them into `PENDING`, or re-run the guard on those heads.
4. **[P1] Release or renew the stale lease** (N1): `task-nagar-workspace-redelivery-01` is 6.9 h past TTL and its work already landed via #303 — run `python scripts/agent_board.py gc` (or `show`) to auto-release it, and heartbeat the claim if it is still live.
5. **[P1] Re-sync the automation branch's board** (N3): `automation/debt-watchdog` carries a 2026-10-31 board (79/175) while `main` is at 2026-11-02 (81/177); merge `main` into the branch so the worktree board is not stale for `check`/`next`.
6. **[P1] Resolve the `CircleCI Pipeline` `ERROR`** (D7): either commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — a non-required context that reads `ERROR` on every recent head leaves all five READY PRs `UNSTABLE`.
7. **[P1] Pin the PAT for automation runs** (D8): export `GITHUB_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN` (or grant the ambient token `branch-protection: read`) so the registry reports the true `READY`/`CI_RED` tiers instead of silently degrading.
8. **[P2] Sweep board hygiene** (D3): remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
9. **[P2] Review and land a READY PR** (#304/#305/#306 are `APPROVED`, #307/#308 need a review): they clear every required gate; confirm the D7 `UNSTABLE` state is benign before they re-stale.
10. **[P2] Attack the rebase wall**: 42 of 47 PRs carry `REBASE_FIRST`; triage the 27 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
