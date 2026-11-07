# Engineering-Debt Watchdog — 2026-11-07

- **Run timestamp (UTC):** `2026-11-07T02:40:00Z` (registry `generated_at`; report written immediately after)
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `6838c54378d24f34680cf1d903a1fd04562af046` (`6838c54378d2`, "Merge pull request #317 from bot523h/board-reconcile-282-stale-lease")
- **Observer:** NEXUS engineering-debt watchdog automation — **observe-and-report only**; no product-code edits, no merge, no push to `main`. The only file created is this one under `.agents/evidence/`.
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-05.md` (branch-only; `main` `cf427874e5c6`) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable or a computation was ambiguous, that is stated explicitly.

> **Credential note (this run).** Branch protection is **unreadable with BOTH tokens** — the ambient `GITHUB_TOKEN` *and* the repository PAT (`GITHUB_PERSONAL_ACCESS_TOKEN`) — each returning `403 Resource not accessible by integration`. This is a **regression of D8**: on 2026-11-05 the PAT read protection (`protection_state: readable`). The registry therefore degrades to 15 workflow-derived contexts and certifies **no PR as READY**. `collect` still exits `0` (the degradation is graceful, per the #291 contract).

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `6838c54378d2` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` (working tree == `main`; sha256 `a0803cd05544dd5576c9e0d45af2d0cab5e008eac20d2e373a5fa45dfaad5f21`) | schema 2; **89 zones, 189 claims**, 14 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; board `updated_at = 2026-11-05T10:30:34Z` |
| Remote branches | **243** (`git ls-remote --heads origin \| wc -l`; **196** are `refs/heads/arena/*`) |
| Open PRs | **46** (`python scripts/pr_convergence.py collect --out /tmp/reg.json`) |
| Branch protection | **unreadable** (`protection_state: unreadable`, both tokens 403) |

---

## 1. Live PR registry

```
$ python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: branch protection UNREADABLE (… 403 Resource not accessible by integration);
             degrading to workflow-derived contexts […]; No PR will be certified READY.
pr-registry: 46 open PRs → /tmp/reg.json (main 6838c54378d2)
COLLECT EXIT=0

$ python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-07T02:40:00Z`, `main_sha`: `6838c54378d24f34680cf1d903a1fd04562af046`
- `open_total`: `46`, `protection_state`: `unreadable`
- `required_contexts` (15, workflow-derived because protection is unreadable): `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "extras-matrix (${{ matrix.leg }})", "python-parity (${{ matrix.python-version }})", "continuum-evidence (${{ matrix.python-version }})", "trust-mutations (pack trust plane + IBPC security closure)", "b2-blob-tier-mutations (maintenance blob tier)", "temporal-mutations (temporal truth algebra)", "governance-mutations (board referee 42 + Gate-5 fencing 17)", "execution-core-mutations (V1 execution core 25)", "remote-key-mutations (ingress + cache containment)", "api-gate-mutations (A-06 security surface)", "merge-base-guard (base == main)", "lint-fast (lockstep + pinned ruff, no install)", "dependency-audit (pip-audit vs reviewed allow-list)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-11-05 |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 11 | 0 |
| CONFLICT | 27 | 0 |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 3 | **+1** |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 1 | −1 |
| PROTECTION_UNREADABLE | 4 | **+4 (new tier this run)** |
| **READY** | **0** | **−2** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 11 · `CONFLICT` 34 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 12 · `CI_PENDING` 0 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 42 · `PROTECTION_UNREADABLE` 46.

**All 46 open PRs are non-READY.** Controlling blocker (first in the ordered list) per PR:

| PR | Tier | Controlling blocker | CI state | Head SHA | Title |
|---|---|---|---|---|---|
| #33 | CONFLICT | CONFLICT | SUCCESS | `9942939713` | v3.13.0 — deliver the P0 security code, wire the dead engine |
| #57 | CONFLICT | CONFLICT | FAILURE | `2f591618e1` | feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-1 |
| #58 | DRAFT | DRAFT | FAILURE | `bb06da1cb3` | Security Boundary hardening — S1/S2/S3/S4/S5 (guard STOP, fo |
| #60 | CONFLICT | CONFLICT | FAILURE | `efea9bae3e` | M0 runtime integration: InstrumentedJobQueue + readyz factor |
| #63 | CONFLICT | CONFLICT | FAILURE | `3e376bea06` | task-163: board-Git truth gate + scale-to-zero state tiers ( |
| #64 | CI_RED | CI_RED | FAILURE | `ab564d7cc7` | Creative Runtime (task-176 s2): execution semantics, multi-s |
| #66 | CONFLICT | CONFLICT | SUCCESS | `db18cf66f3` | P0 creative integration: one production path, closed legacy |
| #69 | CONFLICT | CONFLICT | SUCCESS | `7498074867` | Gate 4: cross-layer vertical slice verification (T01·T02·T2 |
| #73 | CONFLICT | CONFLICT | SUCCESS | `1a35ec0697` | Gate 2.2 — Operation Truth → Executable Evidence Gate (anti- |
| #84 | CONFLICT | CONFLICT | SUCCESS | `89e04c6a24` | Register the NEXUS Engineering Constitution as an enforced c |
| #89 | CONFLICT | CONFLICT | SUCCESS | `2f0df4f11e` | task-122: 12 real command wirings + moderation ingress — for |
| #92 | DRAFT | DRAFT | SUCCESS | `40c1163314` | fix(storage): honor SQLite defaults; preserve inherited relea |
| #97 | CI_RED | CI_RED | FAILURE | `3307d3978f` | feat(vision): complete portrait and scene Vision packs |
| #99 | CONFLICT | CONFLICT | SUCCESS | `9a862e26ad` | task-193: command truth — 11 fake/silent commands wired to th |
| #113 | CONFLICT | CONFLICT | SUCCESS | `6a867a77ca` | feat(runtime): W1 final close-out — engine-owned schema fast- |
| #114 | DRAFT | DRAFT | FAILURE | `6ca1b13bfa` | feat(cockpit): Persian capability explorer and input-only com |
| #115 | CONFLICT | CONFLICT | SUCCESS | `f198734859` | W1: True Runtime Closure / Lifecycle Ownership / Zero Semant |
| #117 | DRAFT | DRAFT | SUCCESS | `4a4b30c2d1` | fix(runtime): close audited session/media contracts and wheel |
| #118 | DRAFT | DRAFT | SUCCESS | `d5a205f40a` | W2 golden hardening: authority races, probe permits and inde |
| #121 | CONFLICT | CONFLICT | SUCCESS | `b4a5965320` | feat(memory): retrieval truth — noise-ranked recall (hit@3 0. |
| #122 | CONFLICT | CONFLICT | SUCCESS | `8b8ba31d44` | fix(orchestration): H1 — route chat through the memory reader |
| #129 | CONFLICT | CONFLICT | SUCCESS | `75f646a52e` | feat(creative/studio): staged plan transactions and atomic co |
| #135 | CONFLICT | CONFLICT | FAILURE | `f83b12835c` | feat(creative/studio): canonical PlanTransaction substrate an |
| #137 | CONFLICT | CONFLICT | SUCCESS | `e2a4614472` | board(task-227,task-228): collision preflight + executable go |
| #140 | CONFLICT | CONFLICT | SUCCESS | `25da562986` | Harden Studio CommandBus: PlanTransaction, Logical Identity, |
| #149 | CONFLICT | CONFLICT | SUCCESS | `e865c14650` | Security Wave 1 Verification & Governance Alignment |
| #150 | DRAFT | DRAFT | FAILURE | `5f84be4dd2` | feat(agent-intelligence): verified CreativeWork trim path |
| #157 | CONFLICT | CONFLICT | SUCCESS | `00ccf90326` | feat(governance): Nagar agent constitution — persistent, ver |
| #193 | CONFLICT | CONFLICT | SUCCESS | `d505d80a4e` | feat(infra): canonical Ubuntu 26.04 + Python 3.14 foundation |
| #204 | DRAFT | DRAFT | SUCCESS | `13078d7926` | fix(memory): resolve sqlite-vec extension fallback and add fo |
| #205 | CONFLICT | CONFLICT | FAILURE | `ff2792b7ad` | feat(nagar): Asset Library V1 — secure content-addressed inge |
| #230 | CONFLICT | CONFLICT | SUCCESS | `9f572aae41` | Arena/aa451c49 nexus ai agent |
| #247 | CONFLICT | CONFLICT | SUCCESS | `d9176f9bcb` | feat(creative): perception lane — measured silence/scene/loud |
| #257 | DRAFT | DRAFT | SUCCESS | `b057ef5fda` | 🎨 Palette: Improve Telegram inline callback UX for language |
| #273 | DRAFT | DRAFT | SUCCESS | `9d29722e6e` | feat(nagar): P0 intent NEL semantic time causal contracts |
| #284 | DRAFT | DRAFT | FAILURE | `82d9be0182` | feat(nagar): add read-only Causal Memory and bounded Director |
| #285 | DRAFT | DRAFT | SUCCESS | `ce1ede4e8d` | chore(board): synchronize task-278 deferred status |
| #294 | CONFLICT | CONFLICT | SUCCESS | `9b60307a14` | repair(#189): land the durable creative-graph package (law R |
| #295 | CI_RED | CI_RED | FAILURE | `c641154d19` | extract(#218): land the self-contained content-addressed Asse |
| #297 | CONFLICT | CONFLICT | SUCCESS | `f142f2e923` | evidence(m16): true closeout of the M15 spine + open-PR conve |
| #301 | REBASE_FIRST | REBASE_FIRST | SUCCESS | `9c168445d8` | chore(automation): engineering-debt watchdog 2026-11-05 |
| #313 | CONFLICT | CONFLICT | SUCCESS | `ca7f9ea886` | chore(board): register task-nagar-preservation-media-profiles |
| #319 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `58deaa097e` | feat(task-282 slice-2): durable artifact→revision binding (St |
| #320 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `cf037b455d` | docs(nagar-one stage-0): product contract + technical truth m |
| #321 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `6f2e54b8c1` | fix(nagar-one stage-2): make the boundary-law register unambi |
| #322 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `8013704fd7` | docs(nagar-one): handoff report for the 2026-11-06 campaign |

**PRs whose canonical required checks** (`lint`, `test`, `merge-base-guard`) are **all `success`**: **20 of 46** — #33, #92, #99, #115, #122, #204, #230, #247, #257, #273, #285, #294, #295, #297, #301, #313, #319, #320, #321, #322. (This is a *CI-green* set, not a mergeable set: most also carry `CONFLICT`/`DRAFT`/`CHANGES_REQUESTED`.) The four `PROTECTION_UNREADABLE` PRs (#319–#322) clear every canonical check and are `UNSTABLE`, not `DIRTY`; the only reason none is certified `READY` is that the merge gate itself could not be read (D8). #295 is canonical-clean but `FAILURE` on the extended 15-context set (§5, D8).

---

## 2. Board audit

```
$ python -m pytest -q tests/unit/test_agent_board.py
134 passed in 4.04s
EXIT=0
```

The authoritative board is the one on **`main`**; the working-tree copy is byte-identical this run (`sha256 a0803cd0…`, `git show origin/main:.agents/board.json | sha256sum` → same). No board-state mutation is performed by the audit.

- **Claims:** 189. **Zones:** 89. Status histogram: `done` 80, `queued` 62, `expired` 20, `completed_merged` 9, `completed_released` 6, `deferred` 3, `available_sequenced_post_33` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1. **`active` 0.**
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).
- **Claims carrying an `agent_branch`:** 21.
- **`protocol.protocol_version`:** present and `== 2` (finding D5 of the 2026-10-31 report stays resolved).
- **Board delta vs 2026-11-05:** +1 zone, +3 claims (88→89 zones, 186→189 claims); `updated_at 2026-11-04T19:22:49Z → 2026-11-05T10:30:34Z`.

### 2.1 Active claims — 0 (no live lease)

`python3 -c "…sum(1 for c in claims if c['status']=='active')"` → **0**. No live lease exists, so no zone is reserved.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30 … 2026-11-05 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (243 refs) and the 46 open-PR heads. No **new** mismatch this run.

### 2.3 Stale live leases — 0

No claim is `active`, so no lease is past its 24 h TTL. Reproduce: for each `active` claim, assert `claimed_at + ttl_hours > now` (now = `2026-11-07T02:40Z`).

### 2.4 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations, plain string-prefix test), and every claim that names a `zone` names a declared one (0 undeclared). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry starts with a `zone.paths` entry.

### 2.5 Owner-labelled rows with no branch and no clock — 7 (unchanged)

| Task | Status | `agent_branch` | `claimed_at` |
|---|---|---|---|
| `task-122` | `assigned_to_B` | (absent) | (absent) |
| `task-123` | `assigned_to_E_pr33` | (absent) | (absent) |
| `task-124` | `available_sequenced_post_32_33` | (absent) | (absent) |
| `task-126` | `available_sequenced_post_33` | (absent) | (absent) |
| `task-121` | `available_sequenced_post_33` | (absent) | (absent) |
| `task-128` | `available_sequenced_post_32` | (absent) | (absent) |
| `task-127` | `assigned_to_B_next` | (absent) | (absent) |

Seven claims carry an owner/sequence label with no machine-checkable branch or lease clock. The remaining 62 `queued` rows are `pr-repair-*` / planning rows with no owner; they are not leases and cannot collide (context only).

---

## 3. Gates

| Gate | Command | Result | Exit |
|---|---|---|---|
| **G1** truth gate | `python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| **G2** matrix + docs | `pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `121 passed in 0.93s` | 0 |
| **G3** board contract | `pytest -q tests/unit/test_agent_board.py` | `134 passed in 4.04s` | 0 |

Environment: Python `3.13` (`/usr/local/bin/python`), **`PYTHONPATH=src` source checkout** (not an editable install). The sandbox had no `.venv` and none of the project's dependencies; the watchdog installed the closure needed to *run the gates only* (`pytest`, `pytest-asyncio`, `pydantic`, `pydantic-settings`, `sqlmodel`, `sqlalchemy`, `alembic`, `aiosqlite`, `structlog`, `typer`, `httpx`, `python-multipart`, `numpy`, `pillow`, `beautifulsoup4`, `psutil`, `arabic-reshaper`, `python-bidi`, `duckduckgo-search`, `langgraph`, `langgraph-checkpoint-sqlite`). All three gates **GREEN** — no gate regressed vs the 2026-11-05 run.

> **Environment note (F5).** The documented reproduction path is `pip install -e ".[dev]"` (editable install, `AGENTS.md` §3). With a bare `PYTHONPATH=src` the gate suite still runs here because the three gate modules do not spawn `python -m nexus_ai_agent.cli`, but the four migration-race tests and the distribution-version test documented in `docs/architecture/TESTING.md` §5 would fail for purely environmental reasons. No gate in this report depends on those.

---

## 4. CI health — 12 most recently updated open PRs

Canonical required contexts (from `AGENTS.md` §1 and the registry): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status per context on the **exact head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, latest run wins), distinguishing `queued`/`in_progress`/`cancelled`/`failure`/`skipped`/`success`/`absent`:

| PR | Head SHA | Updated (UTC) | `lint` | `test` | `merge-base-guard` |
|---|---|---|---|---|---|
| #322 | `8013704fd7` | 2026-11-06T22:44:53Z | success | success | success |
| #319 | `58deaa097e` | 2026-11-06T22:23:44Z | success | success | success |
| #321 | `6f2e54b8c1` | 2026-11-06T21:55:51Z | success | success | success |
| #320 | `cf037b455d` | 2026-11-06T21:54:16Z | success | success | success |
| #301 | `9c168445d8` | 2026-11-05T02:56:13Z | success | success | success |
| #313 | `ca7f9ea886` | 2026-11-04T20:50:07Z | success | success | success |
| #193 | `d505d80a4e` | 2026-11-04T11:52:33Z | success | success | **skipped** |
| #204 | `13078d7926` | 2026-11-01T20:28:44Z | success | success | success |
| #92 | `40c1163314` | 2026-11-01T20:27:15Z | success | success | success |
| #69 | `7498074867` | 2026-10-31T12:16:59Z | success | success | **absent** |
| #66 | `db18cf66f3` | 2026-10-31T12:16:57Z | success | success | **absent** |
| #297 | `f142f2e923` | 2026-10-31T08:16:47Z | success | success | success |

No `queued`/`in_progress` at snapshot time. Superseded `cancelled` duplicates exist (a re-run cancels the previous attempt) but are never the latest run.

### 4.1 Repository-wide required-context histograms (all 46 PRs)

| Check | Latest-run histogram on exact head |
|---|---|
| `lint (ruff + mypy + version lockstep)` | `{success: 42, failure: 4}` (absent: 0) |
| `test (pytest -m "not slow")` | `{success: 36, failure: 10}` (absent: 0) |
| `merge-base-guard (base == main)` | `{success: 24, skipped: 2, absent: 20}` |

`merge-base-guard` is **absent on 20 legacy heads** (unchanged): #57, #58, #60, #63, #66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #135, #137, #140, #149, #150, #157. The **2 `skipped`** runs are #205 and #193 — a *skipped* required check is not a pass.

### 4.2 Non-required `CircleCI Pipeline` `ERROR` (D7 — worsened)

The combined commit status on the 12 recent heads (`.../commits/<sha>/status`) is `failure` for **8 of 12** — #322, #319, #321, #320, #301, #313, #204, #92 — each solely from the non-required `CircleCI Pipeline` context reading `error`. `#193` is `success`; #69, #66, #297 are `pending`. The repository still has **no `.circleci/config.yml`** (`ls -d .circleci` → *No such file or directory*), while the board carries a `queued` claim `task-nagar-circleci-pilot-01` whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. The status originates from an external CircleCI project not reproducible from the tree.

---

## 5. Findings (engineering debt)

### D1 — `_latest_ci_verdict` still masks a real `FAILURE` behind an absent required context (P0) — **STILL OPEN, LATENT**

The ordering bug is unchanged (`scripts/pr_convergence.py` line ~225): with `require_all_present=True`, a required context that never ran returns `PENDING` **before** the red check runs on the present contexts. Reproduced at function level:

```
$ python3 - <<'PY'
import sys; sys.path.insert(0,'scripts'); import pr_convergence as pc
req=['lint (ruff + mypy + version lockstep)','test (pytest -m "not slow")','merge-base-guard (base == main)']
rec={"statusCheckRollup":{"contexts":[
  {"name":req[0],"conclusion":"FAILURE","completedAt":"2026-10-04T17:39:57Z"},
  {"name":req[1],"conclusion":"FAILURE","completedAt":"2026-10-04T17:43:37Z"}]}}
print(pc._latest_ci_verdict(rec, req, require_all_present=True))   # -> PENDING (should be FAILURE)
print(pc._latest_ci_verdict(rec, req, require_all_present=False))  # -> FAILURE
PY
PENDING
FAILURE
```

This run the bug is **latent, not active**: because branch protection is unreadable, `collect` calls the verdict with `require_all_present=False`, so the registry reports the true `ci_state` (`{SUCCESS: 34, FAILURE: 12}` — the 12 red PRs: #57, #58, #60, #63, #64, #97, #114, #135, #150, #205, #284, #295). The moment protection becomes readable again (or D8 is otherwise resolved), the strict path re-activates and the 6 heads with a failed `lint`/`test` but absent `merge-base-guard` (#57, #58, #60, #63, #135, #150) will read `PENDING` instead of `FAILURE`. Same 6 PRs as the 2026-10-31 … 2026-11-05 reports.

### D2 — `merge-base-guard` absent on 20 heads (P0) — **STILL OPEN (unchanged)**

20 legacy heads have no `merge-base-guard` run; the registry cannot distinguish "never ran" from "still running", and a *skipped* guard (#205, #193) is likewise not surfaced as its own signal. Unchanged 20 → 20.

### D3 — board hygiene: 13 dead-branch claims + 7 owner-labelled rows with no clock (P1) — **STILL OPEN (unchanged)**

The 13 dead-branch claims (§2.2) and the 7 owner-labelled rows (§2.5) persist byte-identically. No new ones this run.

### D5 — `protocol.protocol_version` (P1) — **RESOLVED**

`protocol.protocol_version == 2` is present in the board and enforced by `tests/unit/test_agent_board.py` (134 passing). The 2026-10-31 finding is closed.

### D7 — non-required `CircleCI Pipeline` `ERROR` on 8 of 12 recent heads (P1) — **STILL OPEN, WORSENED**

8 of the 12 recent heads carry a combined status of `failure` from the non-required CircleCI context alone (§4.2). On 2026-11-05 this touched 5 PRs and both READY heads; it now touches the four `PROTECTION_UNREADABLE` PRs (#319–#322) that otherwise clear every required check.

### D8 — branch protection unreadable with BOTH tokens (P0) — **REGRESSED**

On 2026-11-05 the repository PAT (`GITHUB_PERSONAL_ACCESS_TOKEN`) read protection (`protection_state: readable`). This run **both** the ambient `GITHUB_TOKEN` and the PAT return `403 Resource not accessible by integration`; `protection_state: unreadable` and the registry degrades to 15 workflow-derived contexts. Consequences: (a) no PR can be certified `READY` (the four clean PRs #319–#322 are tiered `PROTECTION_UNREADABLE`); (b) the registry uses 15 contexts instead of the 3 canonical ones, so its `ci_state` is not comparable to the pre-2026-11-06 reports (a PR is `FAILURE` if *any* of the 15 fails — e.g. #295 is `FAILURE` solely from `api-gate-mutations`, while its `lint`/`test`/`merge-base-guard` are all green); (c) it re-activates the latent D1 path on the next readable run. Reproduce: `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` with either token → 403.

### N6 — the branch's stale-board finding (N3) is effectively neutralized (P2, informational)

`automation/debt-watchdog` (at `9c168445d8`) still carries the 2026-10-31 board (`sha256 592576dd…`, 79 zones / 175 claims, `updated_at 2026-10-31T15:35:11Z`) while `main` is at `sha256 a0803cd0…`, 89 zones / 189 claims, `updated_at 2026-11-05T10:30:34Z`. N3 is therefore **still open on the branch**, but it no longer pollutes *this* audit because the report is written against the `main` board (working tree == `main` here). The watchdog does not modify `board.json` (read-only outside `.agents/evidence/`), so the branch staleness persists until a human syncs it.

---

## 6. Delta vs previous run (2026-11-05, `main` `cf427874e5c6`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-05.md` (branch-only).

| Metric | 2026-11-05 | 2026-11-07 | Change |
|---|---|---|---|
| `main` SHA | `cf427874e5c6` | `6838c54378d2` | **advanced** |
| Open PRs | 44 | 46 | **+2** |
| Open-PR number set | 44 ids | +4 new, −2 gone | #319–#322 new; #290 closed, #314 merged |
| DRAFT tier | 11 | 11 | 0 |
| CONFLICT tier | 27 | 27 | 0 |
| CI_RED tier | 2 | 3 | **+1** |
| REBASE_FIRST tier | 2 | 1 | −1 |
| PROTECTION_UNREADABLE tier | 0 | 4 | **+4 (new)** |
| READY | 2 | 0 | **−2** |
| Remote branches | 234 | 243 | +9 |
| Board zones / claims (`main`) | 88 / 186 | 89 / 189 | **+1 / +3** |
| Board sha (`main`) | `cdd3f02d…` | `a0803cd0…` | changed |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 0 | 0 | 0 |
| Stale live leases | 0 | 0 | 0 |
| `gates_owner` holders | 0 | 0 | 0 |
| Branch protection | **readable** (PAT) | **unreadable** (both tokens) | **REGRESSED** |
| Gates G1/G2/G3 | green | green | 0 |

**New PRs since last run:** #319 (`PROTECTION_UNREADABLE`, `feat(task-282 slice-2): durable artifact→revision binding`), #320 (`PROTECTION_UNREADABLE`, `docs(nagar-one stage-0): product contract + technical truth matrix`), #321 (`PROTECTION_UNREADABLE`, `fix(nagar-one stage-2): make the boundary-law register unambiguous`), #322 (`PROTECTION_UNREADABLE`, `docs(nagar-one): handoff report for the 2026-11-06 campaign`). All four carry green `lint`/`test`/`merge-base-guard` and `mergeStateStatus: UNSTABLE`.
**Gone since last run:** #314 **merged** (`2026-11-05T04:04:21Z`, `feat(creative): durable, restartable Studio project revisions (task-282)`); #290 **closed** (not merged).
**Tier changes among common PRs:** #313 `READY → CONFLICT` (its head `ca7f9ea886` now `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY`); #301 `REBASE_FIRST` (unchanged tier, head advanced `37e77ed140 → 9c168445d8`).

**Regressions:**
- **READY 2 → 0.** Both 2026-11-05 READY PRs are gone: #314 merged; #313 now conflicts. No new READY because the merge gate is unreadable (D8) — the four clean PRs #319–#322 are tiered `PROTECTION_UNREADABLE`.
- **D8 regressed** (readable → unreadable on both tokens) — the highest-impact change this run.
- **D7 worsened** (5 → 8 of 12 recent heads carry the non-required CircleCI `error`).
- **CI_RED tier 2 → 3** (#295 back in the tier).
- No new gate failure (G1/G2/G3 green both runs). No new ownership mismatch (still 13, byte-identical). No new stale lease (0 both runs).

**Resolved since last run:**
- **N5 (two READY PRs blocked only by the non-required CircleCI `ERROR`)** — resolved by removal: #314 merged, #313 flipped to a real conflict.
- **N3's audit impact** — neutralized: the working-tree board now equals `main`'s, so the audit reads the authoritative board.
- D5 (`protocol_version`) stays resolved.

**Unchanged / still open:** D1 (latent — `PENDING` would mask `FAILURE` once protection is readable again; 6 PRs), D2 (`merge-base-guard` absent on 20 heads), D3 (board hygiene: 13 dead-branch + 7 owner-labelled), D7 (CircleCI `ERROR`), D8 (protection unreadable), N3/N6 (stale board on the automation branch).

---

## 7. Next actions (prioritized)

1. **[P0] Restore branch-protection readability (D8).** Both the ambient `GITHUB_TOKEN` and the repository PAT now return `403`. Without it the registry degrades to 15 contexts, certifies no PR `READY`, and re-activates the latent D1 path. Fix the App/installation permission (`administration: read` / `branches`) or re-scope the automation token; verify with `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` returning `200` with the 3 canonical contexts.
2. **[P0] Fix `_latest_ci_verdict` ordering (D1)** so a real `FAILURE` is never masked by an absent required context: evaluate the `_RED_CONCLUSIONS` verdict over the **present** contexts *before* returning `PENDING` for a missing one. 6 heads (#57, #58, #60, #63, #135, #150) will otherwise read `PENDING` instead of `FAILURE` as soon as protection is readable again.
3. **[P0] Review and land the four clean PRs (#319, #320, #321, #322).** Each clears `lint`/`test`/`merge-base-guard` and is `UNSTABLE` only on the non-required CircleCI context; #320/#321/#322 are already `APPROVED`. They are the nearest green work on `main`.
4. **[P0] Distinguish "required check never ran" from "still running" (D2)**: give the 20 legacy heads missing `merge-base-guard` their own signal, and treat the 2 `skipped` guards (#205, #193) as non-green.
5. **[P1] Resolve the `CircleCI Pipeline` `ERROR` (D7):** commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — it currently marks 8 of 12 recent heads' combined status `failure`.
6. **[P1] Rebase or close #313** — it was a clean READY PR on 2026-11-05 and now conflicts (`DIRTY`) after `main` advanced; rebase is cheap.
7. **[P2] Sweep board hygiene (D3):** remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
8. **[P2] Re-sync the automation branch's board (N3/N6)** so `check`/`next` do not read a 2026-10-31 board on `automation/debt-watchdog`.
9. **[P2] Attack the rebase wall:** 42 of 46 PRs carry `REBASE_FIRST`; triage the 27 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
