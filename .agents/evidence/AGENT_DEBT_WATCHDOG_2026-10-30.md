# Engineering-Debt Watchdog — 2026-10-30

- **Run timestamp (UTC):** 2026-10-30T02:52:44Z
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `dc23968d670e8949c9453ee5392e37f61134542a` (`dc23968`, "Merge #288: M13 final report + task-280 defer")
- **Observer:** NEXUS engineering-debt watchdog automation (observe-and-report only; no product-code edits, no merge, no push to `main`)
- **Previous watchdog report:** none present (`.agents/evidence/AGENT_DEBT_WATCHDOG_*.md` does not exist). This is the **baseline** run of the series; deltas below are taken against the nearest prior artifact, `PR_REGISTRY_20261029T1400Z_M11final.json`.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated.

---

## 1. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; `main` == `origin/main` @ `dc23968` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` (164 lines, v1.0) | read |
| `.agents/board.json` | schema 2; 76 zones, 171 claims, 9 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history` |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| Remote branches | 204 (`git ls-remote origin` count) |
| Open PR heads | 41 |

---

## 2. Live PR registry

### 2.1 Shipped command fails closed on an unreadable source (finding D1)

```
$ python3 scripts/pr_convergence.py collect --out /tmp/reg_official.json
pr-registry: source unreadable — gh api repos/bot523h/nexus-ai-agent/branches/main/protection failed: gh: Resource not accessible by integration (HTTP 403)
EXIT=2
```

Branch protection is **not readable by this token** (HTTP 403; `gh api .../rulesets` returns `[]`). `collect` treats that as fatal and produces **no registry at all** — the shipped CLI cannot emit a registry in this environment. This is a robustness gap, not a data problem: the tool cannot distinguish "unreadable" from "abort".

To keep the audit trail complete, the registry below was produced by a read-only harness that reuses the pure functions in `scripts/pr_convergence.py` (`fetch_open_prs`, `blockers`, `classify`, `TIERS`) and records `protection_state: "unreadable"` explicitly instead of aborting.

### 2.2 Registry (fallback harness)

- `generated_at`: `2026-10-30T02:40:20Z`, `main_sha`: `dc23968d670e`, `open_total`: `41`, `protection_state`: `unreadable`.

**Tier counts** (`tier` = hardest blocker; a PR may carry several):

| Tier | Count |
|---|---|
| WRONG_BASE_BRANCH | 0 |
| DRAFT | 11 |
| CONFLICT | 27 |
| MERGEABILITY_UNKNOWN | 0 |
| CI_RED | 3 |
| CI_PENDING | 0 |
| CHANGES_REQUESTED | 0 |
| NEEDS_APPROVAL | 0 |
| REBASE_FIRST | 0 |
| **READY** | **0** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): WRONG_BASE_BRANCH 0 · DRAFT 11 · CONFLICT 34 · MERGEABILITY_UNKNOWN 0 · CI_RED 19 · CI_PENDING 0 · CHANGES_REQUESTED 12 · NEEDS_APPROVAL 0 · REBASE_FIRST 40.

**All 41 open PRs are non-READY.** Controlling blocker (first in the ordered list) per PR:

| PR | Tier | Controlling blocker | Full blocker list |
|---|---|---|---|
| #33 | CONFLICT | CONFLICT | CONFLICT, CI_RED, CHANGES_REQUESTED, REBASE_FIRST |
| #57 | CONFLICT | CONFLICT | CONFLICT, CI_RED, REBASE_FIRST |
| #58 | DRAFT | DRAFT | DRAFT, CONFLICT, CI_RED, REBASE_FIRST |
| #60 | CONFLICT | CONFLICT | CONFLICT, CI_RED, REBASE_FIRST |
| #63 | CONFLICT | CONFLICT | CONFLICT, CI_RED, REBASE_FIRST |
| #64 | CI_RED | CI_RED | CI_RED, REBASE_FIRST |
| #66 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #69 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #73 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #84 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #89 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #92 | DRAFT | DRAFT | DRAFT, REBASE_FIRST |
| #97 | CI_RED | CI_RED | CI_RED, REBASE_FIRST |
| #99 | CONFLICT | CONFLICT | CONFLICT, CHANGES_REQUESTED, REBASE_FIRST |
| #113 | CONFLICT | CONFLICT | CONFLICT, CI_RED, REBASE_FIRST |
| #114 | DRAFT | DRAFT | DRAFT, CI_RED, REBASE_FIRST |
| #115 | CONFLICT | CONFLICT | CONFLICT, CHANGES_REQUESTED, REBASE_FIRST |
| #117 | DRAFT | DRAFT | DRAFT, CONFLICT, REBASE_FIRST |
| #118 | DRAFT | DRAFT | DRAFT, CONFLICT, REBASE_FIRST |
| #121 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #122 | CONFLICT | CONFLICT | CONFLICT, CHANGES_REQUESTED, REBASE_FIRST |
| #129 | CONFLICT | CONFLICT | CONFLICT, CHANGES_REQUESTED, REBASE_FIRST |
| #135 | CONFLICT | CONFLICT | CONFLICT, CI_RED, CHANGES_REQUESTED, REBASE_FIRST |
| #137 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #140 | CONFLICT | CONFLICT | CONFLICT, CHANGES_REQUESTED, REBASE_FIRST |
| #149 | CONFLICT | CONFLICT | CONFLICT, CHANGES_REQUESTED, REBASE_FIRST |
| #150 | DRAFT | DRAFT | DRAFT, CONFLICT, CI_RED, REBASE_FIRST |
| #157 | CONFLICT | CONFLICT | CONFLICT, CHANGES_REQUESTED, REBASE_FIRST |
| #189 | CONFLICT | CONFLICT | CONFLICT, CHANGES_REQUESTED, REBASE_FIRST |
| #193 | CONFLICT | CONFLICT | CONFLICT, CI_RED, CHANGES_REQUESTED, REBASE_FIRST |
| #204 | DRAFT | DRAFT | DRAFT, REBASE_FIRST |
| #205 | CONFLICT | CONFLICT | CONFLICT, CI_RED, REBASE_FIRST |
| #218 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #230 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #247 | CONFLICT | CONFLICT | CONFLICT, CI_RED, CHANGES_REQUESTED, REBASE_FIRST |
| #257 | DRAFT | DRAFT | DRAFT, CI_RED, REBASE_FIRST |
| #273 | DRAFT | DRAFT | DRAFT, CONFLICT, CI_RED, REBASE_FIRST |
| #283 | CONFLICT | CONFLICT | CONFLICT, REBASE_FIRST |
| #284 | DRAFT | DRAFT | DRAFT, CONFLICT, CI_RED, REBASE_FIRST |
| #285 | DRAFT | DRAFT | DRAFT, CONFLICT, CI_RED, REBASE_FIRST |
| #287 | CI_RED | CI_RED | CI_RED |

---

## 3. Board audit

```
$ python3 -m pytest -q tests/unit/test_agent_board.py
75 passed in 1.45s
EXIT=0
```

`.agents/board.json` sha256 is byte-identical before and after every gate run (`sha256sum -c` → `OK`): the audit mutated no board state.

- **Claims:** 171. Status histogram: `done` 76, `queued` 59, `expired` 17, `completed_released` 5, `completed_merged` 3, `deferred` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `assigned_to_B_next` 1, `available_sequenced_post_33` 2, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1.
- **Zones:** 76. **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy in `AGENTS.md` §1.4).

### 3.1 Ownership mismatch (finding D2) — 13 claims

Active-status claim whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed), so they are *board hygiene debt*, not live collisions:

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

Reproduce: compare each non-terminal claim's `agent_branch` against `git ls-remote origin "refs/heads/*"` (204 refs) and the 41 open-PR head branches. Example: `arena/01a0c3aa-nexus-ai-agent` **does** exist remotely, while every branch above does **not**.

### 3.2 Stale live leases — 0

No claim currently has `status` in `{active, claimed}`; therefore there is no lease past its 24 h TTL. The `expired` claims in §3.1 are the *result* of TTL expiry already having fired.

### 3.3 Claims assigned without a lease (finding D3) — 3

| Task | Status | `agent_branch` | `claimed_at` |
|---|---|---|---|
| `task-122` | `assigned_to_B` | (absent) | (absent) |
| `task-123` | `assigned_to_E_pr33` | (absent) | (absent) |
| `task-127` | `assigned_to_B_next` | (absent) | (absent) |

These carry a human-readable owner label but no machine-checkable branch or lease clock — the exact "stale lease" shape `AGENTS.md` warns about (`assigned_to_B`/`assigned_to_E_pr33` are named in the board summary as stale).

### 3.4 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations). No new cross-zone overlap.

---

## 4. Gates

| # | Command | Result | Exit |
|---|---|---|---|
| G1 | `python3 -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| G2 | `python3 -m pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `117 passed in 1.06s` | 0 |
| G3 | `python3 -m pytest -q tests/unit/test_agent_board.py` | `75 passed in 1.45s` | 0 |

Environment: editable install `nexus-ai-agent 3.13.0` (pytest 9.2.0, ruff, mypy) — the install matters, per `docs/architecture/TESTING.md` §5. All gates **green**; no gate turned red.

---

## 5. CI health — 12 most recently updated open PRs

Required checks (names from `.github/workflows/ci.yml`): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`.

**Important:** the rollup shown here is computed as the **latest check run per check name on the EXACT head SHA** (`GET /repos/{repo}/commits/{sha}/check-runs`, sorted by `started_at`). The GraphQL `statusCheckRollup.state` used by `pr_convergence.py` does **not** do this — see §6.1.

| PR | Head SHA | Draft | Mergeable | Review | lint | test | merge-base | Latest-per-name verdict |
|---|---|---|---|---|---|---|---|---|
| #287 | `4216f1112a` | no | MERGEABLE | APPROVED | success | success | success | all green |
| #283 | `f4886726e9` | no | CONFLICTING | — | success | success | success | all green |
| #285 | `ce1ede4e8d` | yes | CONFLICTING | — | success | success | success | all green (superseded cancelled dupes) |
| #284 | `82d9be0182` | yes | CONFLICTING | — | success | **failure** | success | **failure** |
| #273 | `9d29722e6e` | yes | CONFLICTING | — | success | success | success | all green |
| #247 | `d9176f9bcb` | no | CONFLICTING | CHANGES_REQUESTED | success | success | success | all green |
| #204 | `50bc3e3ef7` | yes | MERGEABLE | APPROVED | success | success | success | all green |
| #97 | `3307d3978f` | no | MERGEABLE | — | success | **failure** | success | **failure** |
| #64 | `ab564d7cc7` | no | MERGEABLE | — | success | **failure** | success | **failure** |
| #257 | `b057ef5fda` | yes | MERGEABLE | — | success | success | success | all green |
| #230 | `9f572aae41` | no | CONFLICTING | — | success | success | success | all green |
| #122 | `8b8ba31d44` | no | CONFLICTING | CHANGES_REQUESTED | success | success | success | all green |

States observed across the sample: `success`, `failure`, `cancelled` (superseded duplicates), `skipped` (e.g. CodeRabbit). No `queued`/`in_progress` at snapshot time.

---

## 6. Findings (engineering debt)

### D1 — `pr_convergence.py collect` fails closed when branch protection is unreadable (P0)
`collect` exits `2` and emits no registry when `GET /branches/main/protection` returns 403 (evidence: §2.1). The tool conflates "a required source is unreadable" with "abort the whole run". Result: on any token lacking branch-protection read, the project's own PR registry cannot be generated.

### D2 — `pr_convergence.py` misclassifies PRs because `_rollup_state` ignores superseded duplicate check runs (P0)
`_rollup_state()` reads only GraphQL `statusCheckRollup.state`. When a PR's head SHA has **two runs of the same check name** (an earlier `CANCELLED`/`FAILURE` superseded by a later `SUCCESS`), the rollup stays `FAILURE`.

Measured across all 41 open PRs: `ci_state == FAILURE` for **19** PRs, but only **13** have a *true* latest-per-name failure → **6 PRs are misclassified**.

| PR | Head SHA | Registry tier | Superseded cancelled runs | Latest-per-name truth |
|---|---|---|---|---|
| #287 | `4216f1112a` | CI_RED | 22 | **all 24 checks green**, APPROVED, MERGEABLE |
| #285 | `ce1ede4e8d` | DRAFT | 22 | all green |
| #273 | `9d29722e6e` | DRAFT | 22 | all green |
| #257 | `b057ef5fda` | DRAFT | 21 | all green |
| #247 | `d9176f9bcb` | CONFLICT | 22 | all green |
| #113 | `6a867a77ca` | CONFLICT | 0 | `python-parity (3.10)` FAILURE @20:57 superseded by SUCCESS @21:03; latest green |

PR **#287** is the acute case: zero blockers by latest-per-name, `APPROVED`, `MERGEABLE`, yet the registry labels it `CI_RED` and lists it as the *only* `CI_RED`-controlled PR alongside genuinely-red #64 and #97. A merge-readiness decision taken from the registry would be wrong for 6 of 41 PRs (15%).

Reproduce:
```
gh api repos/bot523h/nexus-ai-agent/commits/<head_sha>/check-runs?per_page=100   # group by .name, take max started_at
```

### D3 — Board carries 13 dead-branch claims and 3 unleased assignments (P1)
See §3.1 and §3.3. 13 `expired` claims still name `arena/*`/`phase1-*` branches that no longer exist on the remote or as open-PR heads; 3 `assigned_to_*` claims have neither a branch nor a `claimed_at`. `gc`/`release` has not swept them.

### D4 — Backlog shape (P1)
0 READY of 41 open. 27 CONFLICT + 11 DRAFT tiers; 40 PRs carry `REBASE_FIRST`; 12 carry `CHANGES_REQUESTED`. The dominant debt is stale, unrebased branches, not green-but-unmerged work.

---

## 7. Delta vs previous run

**Previous watchdog report:** none → baseline. Deltas taken against `.agents/evidence/PR_REGISTRY_20261029T1400Z_M11final.json` (`main` 2026-10-29).

| Metric | Previous (2026-10-29) | Now (2026-10-30) | Change |
|---|---|---|---|
| Open PRs | 42 | 41 | −1 |
| CI_RED tier | 4 | 3 | −1 |
| DRAFT tier | 9 | 11 | +2 |
| CONFLICT tier | 29 | 27 | −2 |
| READY | 0 | 0 | 0 |

- **New PRs since last registry:** #283 (CONFLICT), #284 (DRAFT), #285 (DRAFT), #287 (CI_RED — misclassified, see D2).
- **Closed/merged since last registry:** #59, #88, #139, #244, #276. Of these, #139 and #276 were `CI_RED` → the CI_RED tier drop 4→3 is real (registry), not a fix.
- **Per-PR tier regressions among common PRs:** none (no PR worsened tier).
- **New gate failures:** none (all gates green both before and now; no red gate).
- **New ownership mismatch:** this is the baseline series run, so all 13 (§3.1) are "new to the watchdog series" but are pre-existing board state, not newly introduced.
- **New stale claim:** none live (§3.2). **New conflict:** the 4 new PRs above are all non-READY.
- **Resolved since last run:** #139, #276 (CI_RED PRs closed). No previously-reported watchdog finding to close (none existed).

---

## 8. Next actions (prioritized)

1. **[P0] Fix `_rollup_state` in `scripts/pr_convergence.py`** to derive CI state from the latest check run per name (or exclude superseded `CANCELLED`/`FAILURE` duplicates). 6 of 41 PRs are currently misclassified; #287 is merge-ready but reported `CI_RED`.
2. **[P0] Make `collect` degrade gracefully** when branch protection is unreadable (record `protection_state: unreadable` and continue, as the watchdog harness does) instead of exiting `2` with no registry.
3. **[P1] Sweep the board:** `release`/`gc` the 13 `expired` dead-branch claims (§3.1) and give `task-122`, `task-123`, `task-127` real branches + `claimed_at`, or `defer` them (§3.3).
4. **[P1] Triage the 40 `REBASE_FIRST` / 27 `CONFLICT` backlog**; close superseded drafts so the open-PR set reflects live work.

---

## 9. Constraints observed

- No product code edited; the only file created is this report under `.agents/evidence/`.
- No claim edited, no other agent's task resolved, no merge, no push to `main`, no force-push.
- Unreadable source reported explicitly (branch protection, 403) and the audit continued with the rest.
