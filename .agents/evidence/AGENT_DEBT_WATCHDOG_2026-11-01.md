# Engineering-Debt Watchdog — 2026-11-01

- **Run timestamp (UTC):** 2026-11-01T20:03:57Z
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `d52a2434150d4e3a422d71b5f42b354d24bf61eb` (`d52a243`, "Merge pull request #300 from bot523h/arena/0d9b67b6-nexus-ai-agent")
- **Observer:** NEXUS engineering-debt watchdog automation (observe-and-report only; no product-code edits, no merge, no push to `main`)
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-01.md` at the earlier run in this file's history (`2d84634`, 02:53Z snapshot) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable or a computation was ambiguous, that is stated explicitly.

> **Revision note.** This is a *second* 2026-11-01 run (02:53Z → 20:03Z), committed on the same branch `automation/debt-watchdog` (PR #301) rather than a new file, because the file name is date-keyed. The previous revision reported `main` `48aae28a`; `main` advanced by two commits in the window (see §6). This revision also records a defect **in the previous revision's own output** (§5, D6) and merges `origin/main` into the branch so the audited tree equals the authoritative `main` board.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `d52a2434150d` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` | schema 2; 79 zones, 175 claims, 10 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; `updated_at = 2026-10-31T15:35:11Z` |
| Remote branches | 219 (`git ls-remote --heads origin \| wc -l`) |
| Open PR heads | 44 (`gh api "repos/bot523h/nexus-ai-agent/pulls?state=open&per_page=100" --paginate --jq 'length'`) |
| Branch protection | **readable** (`protection_state: readable`, `required_approvals: 0`, `protection_error: ''`) |

> **Clone note.** The clone was unshallowed this run (`git fetch --unshallow origin`; `.git/shallow` no longer exists), so commit ancestry is now verifiable locally. The branch `automation/debt-watchdog` was merged with `origin/main` (`git merge origin/main` → `d40dd5a`) so the audited tree carries the authoritative board. The board `sha256` above is therefore the `main` board, not the stale branch board the previous revision carried.

---

## 1. Live PR registry

```
$ python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: 44 open PRs → /tmp/reg.json (main d52a2434150d)
COLLECT EXIT=0

$ python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-01T19:46:09Z`, `main_sha`: `d52a2434150d4e3a422d71b5f42b354d24bf61eb`
- `open_total`: `44`, `protection_state`: `readable`, `required_approvals`: `0`
- `required_contexts`: `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "merge-base-guard (base == main)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-11-01 (02:53Z) |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 12 | 0 |
| CONFLICT | 26 | 0 |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 2 | 0 |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 3 | +1 |
| PROTECTION_UNREADABLE | 0 | 0 |
| **READY** | **1** | **0** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 12 · `CONFLICT` 34 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 5 · `CI_PENDING` 20 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 43 · `PROTECTION_UNREADABLE` 0.

**Non-READY PRs, controlling blocker (first in the ordered list):**

| PR | Tier | Controlling blocker | CI state (registry) | Title |
|---|---|---|---|---|
| #33 | CONFLICT | CONFLICT | SUCCESS | v3.13.0 — deliver the P0 security code, wire the dead engines |
| #57 | CONFLICT | CONFLICT | PENDING | feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-160) |
| #58 | DRAFT | DRAFT | PENDING | Security Boundary hardening — S1/S2/S3/S4/S5 |
| #60 | CONFLICT | CONFLICT | PENDING | M0 runtime integration: InstrumentedJobQueue + readyz factory |
| #63 | CONFLICT | CONFLICT | PENDING | task-163: board-Git truth gate + scale-to-zero state tiers |
| #64 | CI_RED | CI_RED | FAILURE | Creative Runtime (task-176 s2): execution semantics |
| #66 | CONFLICT | CONFLICT | PENDING | P0 creative integration: one production path |
| #69 | CONFLICT | CONFLICT | PENDING | Gate 4: cross-layer vertical slice verification |
| #73 | CONFLICT | CONFLICT | PENDING | Gate 2.2 — Operation Truth → Executable Evidence Gate |
| #84 | CONFLICT | CONFLICT | PENDING | Register the NEXUS Engineering Constitution |
| #89 | CONFLICT | CONFLICT | PENDING | task-122: 12 real command wirings + moderation ingress |
| #92 | DRAFT | DRAFT | SUCCESS | fix(storage): honor SQLite defaults |
| #97 | CI_RED | CI_RED | FAILURE | feat(vision): complete portrait and scene Vision packs |
| #99 | CONFLICT | CONFLICT | SUCCESS | task-193: command truth — 11 fake/silent commands wired |
| #113 | CONFLICT | CONFLICT | PENDING | feat(runtime): W1 final close-out |
| #114 | DRAFT | DRAFT | FAILURE | feat(cockpit): Persian capability explorer and command lab |
| #115 | CONFLICT | CONFLICT | SUCCESS | W1: True Runtime Closure / Lifecycle Ownership |
| #117 | DRAFT | DRAFT | PENDING | fix(runtime): close audited session/media contracts |
| #118 | DRAFT | DRAFT | PENDING | W2 golden hardening: authority races |
| #121 | CONFLICT | CONFLICT | PENDING | feat(memory): retrieval truth — noise-ranked recall |
| #122 | CONFLICT | CONFLICT | SUCCESS | fix(orchestration): H1 — route chat through the memory reader |
| #129 | CONFLICT | CONFLICT | PENDING | feat(creative/studio): staged plan transactions |
| #135 | CONFLICT | CONFLICT | PENDING | feat(creative/studio): canonical PlanTransaction substrate |
| #137 | CONFLICT | CONFLICT | PENDING | board(task-227,task-228): collision preflight |
| #140 | CONFLICT | CONFLICT | PENDING | Harden Studio CommandBus: PlanTransaction |
| #149 | CONFLICT | CONFLICT | PENDING | Security Wave 1 Verification & Governance Alignment |
| #150 | DRAFT | DRAFT | PENDING | feat(agent-intelligence): verified CreativeWork trim path |
| #157 | CONFLICT | CONFLICT | PENDING | feat(governance): Nagar agent constitution |
| #193 | CONFLICT | CONFLICT | SUCCESS | feat(infra): canonical Ubuntu 26.04 + Python 3.14 |
| #204 | DRAFT | DRAFT | SUCCESS | fix(memory): resolve sqlite-vec extension fallback |
| #205 | CONFLICT | CONFLICT | FAILURE | feat(nagar): Asset Library V1 |
| #230 | CONFLICT | CONFLICT | SUCCESS | Arena/aa451c49 nexus ai agent |
| #247 | CONFLICT | CONFLICT | SUCCESS | feat(creative): perception lane |
| #257 | DRAFT | DRAFT | SUCCESS | Palette: Improve Telegram inline callback UX |
| #273 | DRAFT | DRAFT | SUCCESS | feat(nagar): P0 intent NEL semantic time causal contracts |
| #284 | DRAFT | DRAFT | FAILURE | feat(nagar): add read-only Causal Memory and bounded Director preview |
| #285 | DRAFT | DRAFT | SUCCESS | chore(board): synchronize task-278 deferred status |
| #290 | CONFLICT | CONFLICT | SUCCESS | chore(board): register task-282 durable project state lifecycle |
| #294 | REBASE_FIRST | REBASE_FIRST | SUCCESS | repair(#189): land the durable creative-graph package |
| #295 | REBASE_FIRST | REBASE_FIRST | SUCCESS | extract(#218): land the content-addressed Asset Library |
| #297 | CONFLICT | CONFLICT | SUCCESS | evidence(m16): true closeout of the M15 spine |
| #298 | DRAFT | DRAFT | SUCCESS | feat(nagar-ibpc): proof-carrying live editing |
| #301 | REBASE_FIRST | REBASE_FIRST | SUCCESS | chore(automation): engineering-debt watchdog 2026-11-01 |

**The single READY PR:** #302, head `bd4988933c`, `mergeable: MERGEABLE`, `mergeable_state: unstable`, no review decision, `ci_state: SUCCESS`. Reproduce: `gh api repos/bot523h/nexus-ai-agent/pulls/302`.

---

## 2. Board audit

```
$ python -m pytest -q tests/unit/test_agent_board.py
75 passed in 1.63s
EXIT=0
```

`.agents/board.json` sha256: `592576ddb556f208f0784132b36f0868d0a4f66402f86f5de83c724f446bee33` (the `main` board; unchanged across all gate runs — the audit mutates no board state).

- **Claims:** 175 (was 173 at `main` `48aae28a`). **Zones:** 79 (was 77). Status histogram: `done` 77, `queued` 60, `expired` 17, `completed_released` 5, `completed_merged` 3, `available_sequenced_post_33` 2, `deferred` 2, `active` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1.
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).

### 2.1 Active claims — 2 (both branches live, both leases STALE)

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-267-truth041-live-lease-alignment` | `runtime-truth-tooling` | `arena/m14-truth041-lease-expiry` | yes | no | 24 | 2026-10-30T10:11:42Z | expired 2026-10-31T10:11:42Z → **STALE** |
| `task-281-cognition-eval-live-registry-binding` | `cognition-quality-evaluation` | `arena/de4e797a-nexus-ai-agent` | yes | no | 24 | 2026-10-30T15:39:23Z | expired 2026-10-31T15:39:23Z → **STALE** |

Both tasks' work has **already merged**: `task-267` via PR **#292** (`merged_at 2026-10-30T10:36:41Z`), `task-281` via PR **#293** (`merged_at 2026-10-30T19:18:30Z`). Neither claim has been released, and both 24 h leases are **past TTL** at this snapshot. This is **landed-but-still-leased + stale** debt: the branches still exist remotely so the dead-branch check does not flag them, but the lease holds a `zone`/`exclusive_paths` reservation for work already on `main`. Not a collision — `AGENTS.md` §1.2 only blocks overlap with *live* leases, and `show`/`gc` auto-release stale leases.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30, 2026-10-31 and earlier 2026-11-01 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (219 refs) and the 44 open-PR heads.

### 2.3 Stale live leases — 2 (unchanged; both are the landed-but-leased claims of §2.1)

Both `active` claims are past their 24 h TTL at snapshot time. `gc`/`show` would auto-release them.

### 2.4 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry is a prefix-equal-or-child of a `zone.paths` entry.

### 2.5 Unleased non-terminal claims — 68 (context, not a gate)

68 non-terminal claims carry neither `agent_branch` nor `claimed_at` (60 `queued` + the 7 labelled `assigned_to_*` / `available_sequenced_*` rows + 1 `deferred`). Of these, the 60 `queued` are mostly `pr-repair-<n>` planning rows with no owner; they are not leases and cannot collide, so they are recorded for completeness only. The 7 owner-labelled rows without a branch or clock (`task-122`, `task-123`, `task-124`, `task-126`, `task-121`, `task-128`, `task-127`) remain the actionable subset — an owner label that no machine check can verify.

---

## 3. Gates

| # | Command | Result | Exit |
|---|---|---|---|
| G1 | `python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| G2 | `python -m pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `117 passed in 4.55s` | 0 |
| G3 | `python -m pytest -q tests/unit/test_agent_board.py` | `75 passed in 1.63s` | 0 |

Environment: Python 3.13 (`.venv`, editable install `pip install -e ".[dev]"` per `AGENTS.md` §3). All gates **green**; no gate turned red versus the previous run.

---

## 4. CI health — 12 most recently updated open PRs (exact head SHA)

Required checks (names from `required_contexts`): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status is the **latest check run per check name on the EXACT head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, grouped by `.name`, max `started_at`).

| PR | Head SHA | Updated (UTC) | lint | test | merge-base-guard |
|---|---|---|---|---|---|
| #302 | `bd4988933c` | 2026-11-01T19:23:31Z | success | success | success |
| #301 | `e090330879` | 2026-11-01T02:59:21Z | success | success | success |
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

States observed: `success`, `failure` (latest run), and `absent` (the required `merge-base-guard` check has never run on that head — see §4.1). No `queued`/`in_progress`/`cancelled` was the latest run at snapshot time.

### 4.1 Repository-wide required-context presence (all 44 PRs)

| Check | Present on exact head | Absent |
|---|---|---|
| `lint (ruff + mypy + version lockstep)` | 44 | 0 |
| `test (pytest -m "not slow")` | 44 | 0 |
| `merge-base-guard (base == main)` | 24 | **20** — `[57, 58, 60, 63, 66, 69, 73, 84, 89, 113, 117, 118, 121, 129, 135, 137, 140, 149, 150, 157]` |

The 20 PRs with an absent `merge-base-guard` are long-lived branches (heads from 2026-09-23 onward) that predate the guard job's addition to CI; the guard has therefore never reported on their head. Two of them (#69, #66) were **re-pushed on 2026-10-31** yet still do not carry the guard on the new head.

---

## 5. Findings (engineering debt)

### D1 — `pr_convergence.py` masks a real CI FAILURE behind `PENDING` (P0) — **STILL OPEN (3rd run)**

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

Measured across all 44 open PRs: registry `ci_state == PENDING` for **20**, but **6** of those have a genuine `lint`/`test` `FAILURE` at the exact head:

| PR | Head SHA | lint | test | merge-base-guard | Registry `ci_state` | True verdict |
|---|---|---|---|---|---|---|
| #57 | `2f591618e1` | failure | success | absent | PENDING | **FAILURE** |
| #58 | `bb06da1cb3` | failure | failure | absent | PENDING | **FAILURE** |
| #60 | `efea9bae3e` | failure | failure | absent | PENDING | **FAILURE** |
| #63 | `3e376bea06` | success | failure | absent | PENDING | **FAILURE** |
| #135 | `f83b12835c` | success | failure | absent | PENDING | **FAILURE** |
| #150 | `5f84be4dd2` | failure | failure | absent | PENDING | **FAILURE** |

Cross-check total: registry `ci_state` vs true latest-per-name verdict mismatch = **6 of 44** (all in the `PENDING`-should-be-`FAILURE` direction; 0 in the other direction). No merge-readiness decision is *reversed* (all six also carry `CONFLICT`/`DRAFT`), but the CI signal is wrong for ~14% of the backlog. Note: the registry's `CI_RED` tier count (2) under-reports the real red count — **11 PRs** have a genuine `lint`/`test` failure at head (`[57, 58, 60, 63, 64, 97, 114, 135, 150, 205, 284]`); the other 5 (#64, #97, #114, #205, #284) are classified `CI_RED`/`DRAFT` by their harder blocker, so the tier is not wrong for them, but the `PENDING` label on the 6 above is. Reproduce: `gh api repos/bot523h/nexus-ai-agent/commits/<head_sha>/check-runs?per_page=100`, group by `.name`, take max `started_at`; compare to `registry.ci_state`.

### D2 — `merge-base-guard` absent on 20 of 44 heads, silently downgraded to `PENDING` (P1) — **STILL OPEN (3rd run)**

20 long-lived PRs predate the `merge-base-guard` job, so the required check has never run on their head (§4.1). Because of D1 these read `PENDING`, so the registry shows `CI_PENDING` 20 / `CI_RED` 2 rather than surfacing "the required guard never ran here". Two distinct conditions (never-ran vs still-running) are collapsed into one tier, and a head missing a *required* gate is not distinguishable from one whose gate is in flight. Owner action: either re-run the guard on these heads or record "guard absent on legacy head" as its own signal.

### D3 — board hygiene: 13 dead-branch claims, 2 stale/landed leases, 68 unleased rows (P1) — **unchanged**

See §2.1–§2.5. 13 `expired` claims still name branches that no longer exist; the 2 `active` claims are **past TTL** and hold zones for work already merged (#292, #293) without a `release`; and 68 non-terminal claims carry no branch and no `claimed_at` (60 `queued` planning rows + 7 owner-labelled rows + 1 `deferred`). `gc`/`release` has not swept any of them.

### D4 — `protocol.protocol_version` present (P1) — **RESOLVED (previous run)**

The 2026-10-31 report noted `AGENTS.md` claimed "schema 2" while `protocol.protocol_version` was absent. It is present and equals `2` (`python -c "import json;print(json.load(open('.agents/board.json'))['protocol']['protocol_version'])"` → `2`). Still resolved.

### D5 — backlog shape (P1, character unchanged)

1 READY of 44 open. 26 CONFLICT + 12 DRAFT tiers; 43 PRs carry `REBASE_FIRST`; 11 carry `CHANGES_REQUESTED`; 20 carry `CI_PENDING` (6 of them really red, D1). The dominant debt remains stale, unrebased branches, not green-but-unmerged work.

### D6 — the previous revision of THIS report (02:53Z) cited board state that does not match its own commit (P1) — **NEW (self-audit defect)**

The earlier 2026-11-01 revision (commit `2d84634`, still in this file's history) reported the board as `schema 2; 77 zones, 173 claims` with `sha256 d4c38cb4ef0defb49598439b0d73adc192716bdc83c6a02597c9e76a6a0a3992`. The `board.json` actually committed in that commit's own tree is `a0d284a5cbe3b90b039e98fae2468e94a9305586040dc518b87ba6d21b93f6a8` with **76 zones / 172 claims** — the values cited instead match the PR *base* `48aae28a` (a later, unrelated commit whose board happens to hash to `d4c38cb4`).

```
$ git show 2d84634:.agents/board.json | sha256sum
a0d284a5cbe3b90b039e98fae2468e94a9305586040dc518b87ba6d21b93f6a8  -
$ git show 2d84634:.agents/board.json | python3 -c "import json,sys;b=json.load(sys.stdin);print(len(b['zones']),len(b['claims']))"
76 172
$ git show 48aae28a4ef4:.agents/board.json | sha256sum
d4c38cb4ef0defb49598439b0d73adc192716bdc83c6a02597c9e76a6a0a3992  -
```

The cause is the shallow/grafted checkout noted in the previous revision: with ancestry unavailable, the cited `main`/board values were read from a different tree than the one the report was committed onto. This is exactly the reproducibility class the watchdog exists to catch, so it is recorded against the watchdog's own output. **This revision corrects the practice** by (a) unshallowing the clone, (b) merging `origin/main` into the branch so the audited board is the `main` board, and (c) hashing the on-disk file directly (`sha256sum .agents/board.json`). The stale figures are preserved in the previous commit's history rather than rewritten.

---

## 6. Delta vs previous run (2026-11-01 02:53Z, `main` `48aae28a`)

**Previous report:** the earlier revision of `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-01.md` (commit `2d84634`).

| Metric | 2026-11-01 02:53Z | 2026-11-01 20:03Z | Change |
|---|---|---|---|
| `main` SHA | `48aae28a4ef4` | `d52a2434150d` | **+2 commits** |
| Open PRs | 43 | 44 | **+1** |
| DRAFT tier | 12 | 12 | 0 |
| CONFLICT tier | 26 | 26 | 0 |
| CI_RED tier | 2 | 2 | 0 |
| CI_PENDING tier | 0 | 0 | 0 |
| REBASE_FIRST tier | 2 | 3 | **+1** |
| READY | 1 | 1 | 0 |
| Remote branches | 218 | 219 | +1 |
| Board zones / claims (`main`) | 77 / 173 | 79 / 175 | **+2 / +2** |
| Board sha (`main`) | `d4c38cb4…` | `592576dd…` | advanced |
| `ci_state` mismatches (D1) | 6 | 6 | 0 |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 2 | 2 | 0 |
| Stale live leases | 2 | 2 | 0 |
| `gates_owner` holders | 0 | 0 | 0 |
| Gates G1/G2/G3 | green | green | 0 |

**New PRs since 02:53Z:** #302 (`READY`, CI green, `MERGEABLE`/`UNSTABLE`) — a new clean candidate.
**Closed/merged since 02:53Z:** #300 (`chore(board): register Nagar workspace and CircleCI tasks`, merged 2026-11-01T04:36:22Z) — the single READY PR the previous revision flagged is now landed, and it is what advanced `main` (`48aae28a` → `d52a243`, board 77/173 → 79/175).
**Board change on `main`:** PR #300 added 2 zones and 2 claims (`Nagar redelivery` + `CircleCI` tasks).
**Tier change among common PRs:** none — the REBASE_FIRST +1 is the newly-registered PR #301 (this PR; absent from the 02:53Z registry), whose base `48aae28a` is now behind `main` (`mergeable_state: behind`); the new READY PR #302 replaced the merged #300 in the READY slot.

**Regressions (gate/CI/ownership):**
- **NEW self-audit defect (D6):** the previous revision of this report cited board state from a different tree than its own commit.
- **NEW PR #301 is behind `main`** (`mergeable_state: behind`) — the watchdog branch itself carries `REBASE_FIRST`; this run's `origin/main` merge resolves it.
- No new gate failures (G1/G2/G3 green both runs).
- No new ownership mismatch (still 13, byte-identical list).
- No new stale lease (still 2; both were already stale at 02:53Z).
- No new conflict (still 26 `CONFLICT`-tier PRs).

**Resolved since last run:**
- #300 landed → the "single READY PR" from the previous revision merged cleanly; `main` advanced two commits.
- No D1/D2/D3 change (all persist).

**Unchanged / still open:** D1 (PENDING masks FAILURE), D2 (`merge-base-guard` absent on 20 heads), D3 (board hygiene), D5 (backlog shape).

---

## 7. Next actions (prioritized)

1. **[P0] Fix `_latest_ci_verdict` ordering in `scripts/pr_convergence.py`** so a real `FAILURE` is never masked by an absent required context: compute the `_RED_CONCLUSIONS` verdict over the present contexts **before** returning `PENDING` for a missing one (or return `FAILURE` when any present context is red and any required context is absent). 6 of 44 PRs are mis-reported (`PENDING` instead of `FAILURE`): #57, #58, #60, #63, #135, #150.
2. **[P0] Distinguish "required check never ran" from "still running"** (D2): give the 20 legacy heads missing `merge-base-guard` their own signal rather than folding them into `PENDING`, or re-run the guard on those heads.
3. **[P1] Make the watchdog's own report reproducible** (D6): read board/`main` values from the same commit the report is committed onto, and hash the on-disk file; do not cite figures from a shallow/grafted checkout's base. This run's merge of `origin/main` into the branch is the corrective.
4. **[P1] Sweep board hygiene** (D3): `release`/`gc` the 2 stale, landed-but-leased active claims (#292, #293), and remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
5. **[P2] Review and land the READY PR** (#302, `MERGEABLE`/`UNSTABLE`): it is the only PR that clears every required gate; get a review and confirm the `UNSTABLE` state is benign before it re-stales.
6. **[P2] Attack the rebase wall** (D5): 43 of 44 PRs carry `REBASE_FIRST`; triage the 26 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
