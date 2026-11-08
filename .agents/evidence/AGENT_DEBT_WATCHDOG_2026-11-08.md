# Engineering-Debt Watchdog — 2026-11-08

- **Run timestamp (UTC):** `2026-11-08T02:44:53Z` (registry `generated_at` `2026-11-08T02:39:19Z`; report written immediately after)
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `6838c54378d24f34680cf1d903a1fd04562af046` (`6838c54378d2`, "Merge pull request #317 from bot523h/board-reconcile-282-stale-lease")
- **Observer:** NEXUS engineering-debt watchdog automation — **observe-and-report only**; no product-code edits, no merge, no push to `main`. The only file created is this one under `.agents/evidence/`.
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-07.md` (branch-only; `main` `6838c54378d2` — **same `main` SHA this run**) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable or a computation was ambiguous, that is stated explicitly.

> **Credential note (this run) — D8 improved.** Branch protection is readable **with the repository PAT** (`GITHUB_PERSONAL_ACCESS_TOKEN` → `200`, 3 canonical contexts) but **still 403 with the ambient `GITHUB_TOKEN`** (`Resource not accessible by integration`). The default `collect` (ambient token) therefore still degrades to 15 workflow-derived contexts and certifies **no PR as READY**; a second `collect` with `GH_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN` reads the real gate and certifies **5 PRs READY**. Both views are reported below. This is a **partial recovery** of D8 (on 2026-11-07 *both* tokens returned 403).

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `6838c54378d2` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` (working tree == `main`; sha256 `a0803cd05544dd5576c9e0d45af2d0cab5e008eac20d2e373a5fa45dfaad5f21`) | schema 2; **89 zones, 189 claims**, 14 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; board `updated_at = 2026-11-05T10:30:34Z` — **byte-identical to the 2026-11-07 run** |
| Remote branches | **244** (`git ls-remote --heads origin \| wc -l`; **196** are `refs/heads/arena/*`) |
| Open PRs | **47** (`python scripts/pr_convergence.py collect --out /tmp/reg.json`) |
| Branch protection | **unreadable** with the ambient `GITHUB_TOKEN`; **readable** with the repository PAT (3 canonical contexts) |

---

## 1. Live PR registry

```
$ python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: branch protection UNREADABLE (… 403 Resource not accessible by integration);
             degrading to workflow-derived contexts […]; No PR will be certified READY.
pr-registry: 47 open PRs → /tmp/reg.json (main 6838c54378d2)
COLLECT EXIT=0

$ python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-08T02:39:19Z`, `main_sha`: `6838c54378d24f34680cf1d903a1fd04562af046`
- `open_total`: `47`, `protection_state`: `unreadable` (ambient), `protection_source`: `ci.yml workflow (branch-protection API unreadable)`
- `required_contexts` (15, workflow-derived because the ambient token cannot read protection): `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "extras-matrix (${{ matrix.leg }})", "python-parity (${{ matrix.python-version }})", "continuum-evidence (${{ matrix.python-version }})", "trust-mutations (pack trust plane + IBPC security closure)", "b2-blob-tier-mutations (maintenance blob tier)", "temporal-mutations (temporal truth algebra)", "governance-mutations (board referee 42 + Gate-5 fencing 17)", "execution-core-mutations (V1 execution core 25)", "remote-key-mutations (ingress + cache containment)", "api-gate-mutations (A-06 security surface)", "merge-base-guard (base == main)", "lint-fast (lockstep + pinned ruff, no install)", "dependency-audit (pip-audit vs reviewed allow-list)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-11-07 |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 11 | 0 |
| CONFLICT | 27 | 0 |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 3 | 0 |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 1 | 0 |
| PROTECTION_UNREADABLE | 5 | **+1** |
| **READY** | **0** | **0** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 11 · `CONFLICT` 34 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 12 · `CI_PENDING` 0 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 42 · `PROTECTION_UNREADABLE` 47.

**All 47 open PRs are non-READY** under the ambient registry. Controlling blocker (first in the ordered list) per PR:

| PR | Tier | Controlling blocker | CI state | Head SHA | Title |
|---|---|---|---|---|---|
| #33 | CONFLICT | CONFLICT | SUCCESS | `9942939713` | v3.13.0 — deliver the P0 security code, wire the dead engines, f |
| #57 | CONFLICT | CONFLICT | FAILURE | `2f591618e1` | feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-160) |
| #58 | DRAFT | DRAFT | FAILURE | `bb06da1cb3` | Security Boundary hardening — S1/S2/S3/S4/S5 (guard STOP, force- |
| #60 | CONFLICT | CONFLICT | FAILURE | `efea9bae3e` | M0 runtime integration: InstrumentedJobQueue + readyz factory +  |
| #63 | CONFLICT | CONFLICT | FAILURE | `3e376bea06` | task-163: board-Git truth gate + scale-to-zero state tiers (#1 + |
| #64 | CI_RED | CI_RED | FAILURE | `ab564d7cc7` | Creative Runtime (task-176 s2): execution semantics, multi-segme |
| #66 | CONFLICT | CONFLICT | SUCCESS | `db18cf66f3` | P0 creative integration: one production path, closed legacy lane |
| #69 | CONFLICT | CONFLICT | SUCCESS | `7498074867` | Gate 4: cross-layer vertical slice verification (T01·T02·T22·T31 |
| #73 | CONFLICT | CONFLICT | SUCCESS | `1a35ec0697` | Gate 2.2 — Operation Truth → Executable Evidence Gate (anti-drif |
| #84 | CONFLICT | CONFLICT | SUCCESS | `89e04c6a24` | Register the NEXUS Engineering Constitution as an enforced contr |
| #89 | CONFLICT | CONFLICT | SUCCESS | `2f0df4f11e` | task-122: 12 real command wirings + moderation ingress — forensi |
| #92 | DRAFT | DRAFT | SUCCESS | `40c1163314` | fix(storage): honor SQLite defaults; preserve inherited release  |
| #97 | CI_RED | CI_RED | FAILURE | `3307d3978f` | feat(vision): complete portrait and scene Vision packs |
| #99 | CONFLICT | CONFLICT | SUCCESS | `9a862e26ad` | task-193: command truth — 11 fake/silent commands wired to the e |
| #113 | CONFLICT | CONFLICT | SUCCESS | `6a867a77ca` | feat(runtime): W1 final close-out — engine-owned schema fast-pat |
| #114 | DRAFT | DRAFT | FAILURE | `6ca1b13bfa` | feat(cockpit): Persian capability explorer and input-only comman |
| #115 | CONFLICT | CONFLICT | SUCCESS | `f198734859` | W1: True Runtime Closure / Lifecycle Ownership / Zero Semantic B |
| #117 | DRAFT | DRAFT | SUCCESS | `4a4b30c2d1` | fix(runtime): close audited session/media contracts and wheel re |
| #118 | DRAFT | DRAFT | SUCCESS | `d5a205f40a` | W2 golden hardening: authority races, probe permits and independ |
| #121 | CONFLICT | CONFLICT | SUCCESS | `b4a5965320` | feat(memory): retrieval truth — noise-ranked recall (hit@3 0.167 |
| #122 | CONFLICT | CONFLICT | SUCCESS | `8b8ba31d44` | fix(orchestration): H1 — route chat through the memory reader (t |
| #129 | CONFLICT | CONFLICT | SUCCESS | `75f646a52e` | feat(creative/studio): staged plan transactions and atomic commi |
| #135 | CONFLICT | CONFLICT | FAILURE | `f83b12835c` | feat(creative/studio): canonical PlanTransaction substrate and d |
| #137 | CONFLICT | CONFLICT | SUCCESS | `e2a4614472` | board(task-227,task-228): collision preflight + executable gover |
| #140 | CONFLICT | CONFLICT | SUCCESS | `25da562986` | Harden Studio CommandBus: PlanTransaction, Logical Identity, and |
| #149 | CONFLICT | CONFLICT | SUCCESS | `e865c14650` | Security Wave 1 Verification & Governance Alignment |
| #150 | DRAFT | DRAFT | FAILURE | `5f84be4dd2` | feat(agent-intelligence): verified CreativeWork trim path |
| #157 | CONFLICT | CONFLICT | SUCCESS | `00ccf90326` | feat(governance): Nagar agent constitution — persistent, version |
| #193 | CONFLICT | CONFLICT | SUCCESS | `d505d80a4e` | feat(infra): canonical Ubuntu 26.04 + Python 3.14 foundation |
| #204 | DRAFT | DRAFT | SUCCESS | `13078d7926` | fix(memory): resolve sqlite-vec extension fallback and add foren |
| #205 | CONFLICT | CONFLICT | FAILURE | `ff2792b7ad` | feat(nagar): Asset Library V1 — secure content-addressed ingest  |
| #230 | CONFLICT | CONFLICT | SUCCESS | `9f572aae41` | Arena/aa451c49 nexus ai agent |
| #247 | CONFLICT | CONFLICT | SUCCESS | `d9176f9bcb` | feat(creative): perception lane — measured silence/scene/loudnes |
| #257 | DRAFT | DRAFT | SUCCESS | `b057ef5fda` | 🎨 Palette: Improve Telegram inline callback UX for language and  |
| #273 | DRAFT | DRAFT | SUCCESS | `9d29722e6e` | feat(nagar): P0 intent NEL semantic time causal contracts |
| #284 | DRAFT | DRAFT | FAILURE | `82d9be0182` | feat(nagar): add read-only Causal Memory and bounded Director pr |
| #285 | DRAFT | DRAFT | SUCCESS | `ce1ede4e8d` | chore(board): synchronize task-278 deferred status |
| #294 | CONFLICT | CONFLICT | SUCCESS | `9b60307a14` | repair(#189): land the durable creative-graph package (law R28)  |
| #295 | CI_RED | CI_RED | FAILURE | `c641154d19` | extract(#218): land the self-contained content-addressed Asset L |
| #297 | CONFLICT | CONFLICT | SUCCESS | `f142f2e923` | evidence(m16): true closeout of the M15 spine + open-PR converge |
| #301 | REBASE_FIRST | REBASE_FIRST | SUCCESS | `c7dba9f292` | chore(automation): engineering-debt watchdog 2026-11-07 |
| #313 | CONFLICT | CONFLICT | SUCCESS | `ca7f9ea886` | chore(board): register task-nagar-preservation-media-profiles-01 |
| #319 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `3d7bbfe510` | feat(task-282 slice-2): durable artifact→revision binding (Stage |
| #320 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `cf037b455d` | docs(nagar-one stage-0): product contract + technical truth matr |
| #321 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `6f2e54b8c1` | fix(nagar-one stage-2): make the boundary-law register unambiguo |
| #322 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `8013704fd7` | docs(nagar-one): handoff report for the 2026-11-06 campaign |
| #323 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | `0e7c4f4d16` | docs(process): NAGAR-ONE engineering skill + session memory |

### 1.1 Secondary view — registry with the PAT (protection **readable**, 3 canonical contexts)

```
$ GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN" python scripts/pr_convergence.py collect --out /tmp/reg_pat.json
pr-registry: 47 open PRs → /tmp/reg_pat.json (main 6838c54378d2)
$ python scripts/pr_convergence.py report --in /tmp/reg_pat.json
```

- `protection_state`: `readable`, `required_contexts` = the **3 canonical** contexts (`lint`, `test`, `merge-base-guard`).
- Tiers: `DRAFT` 11 · `CONFLICT` 27 · `CI_RED` 2 · `REBASE_FIRST` 2 · `PROTECTION_UNREADABLE` **0** · **`READY` 5** (#319, #320, #321, #322, #323).
- Blocker counts: `CONFLICT` 34 · `CI_RED` 5 · **`CI_PENDING` 20** · `CHANGES_REQUESTED` 11 · `REBASE_FIRST` 42.

**The 20 `CI_PENDING` under the readable path are the D1 bug, not reality** (see §5, D1): the strict path calls `_latest_ci_verdict(..., require_all_present=True)`, which returns `PENDING` when a required context is **absent** *before* it evaluates the present contexts' verdicts. The 20 split into (a) **6 genuinely-red heads** whose `merge-base-guard` never ran (#57, #58, #60, #63, #135, #150 — all carry a real `lint`/`test` `failure`) that the strict path **masks** as `PENDING`, and (b) **14 heads** (#66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #137, #140, #149, #157) whose `lint`/`test` are both `success` and whose `merge-base-guard` genuinely never ran — these are `PENDING` for a benign reason (an absent guard), but the strict path cannot tell the two groups apart. Reproduce (a):

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

**PRs whose canonical required checks** (`lint`, `test`, `merge-base-guard`) are **all `success`**: **21 of 47** — #33, #92, #99, #115, #122, #204, #230, #247, #257, #273, #285, #294, #295, #297, #301, #313, #319, #320, #321, #322, #323. (This is a *CI-green* set, not a mergeable set: most also carry `CONFLICT`/`DRAFT`/`CHANGES_REQUESTED`.)

---

## 2. Board audit

```
$ PYTHONPATH=src python -m pytest -q tests/unit/test_agent_board.py
134 passed in 2.58s
EXIT=0
```

The authoritative board is the one on **`main`**; the working-tree copy is byte-identical this run (`sha256 a0803cd0…`, `git show origin/main:.agents/board.json | sha256sum` → same). No board-state mutation is performed by the audit.

- **Claims:** 189. **Zones:** 89. Status histogram: `done` 80, `queued` 62, `expired` 20, `completed_merged` 9, `completed_released` 6, `deferred` 3, `available_sequenced_post_33` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1. **`active` 0.**
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).
- **Claims carrying an `agent_branch`:** 21.
- **`protocol.protocol_version`:** present and `== 2` (finding D5 of the 2026-10-31 report stays resolved).
- **Board delta vs 2026-11-07:** **none** — same `sha256 a0803cd0…`, same 89 zones / 189 claims, same `updated_at 2026-11-05T10:30:34Z`.

### 2.1 Active claims — 0 (no live lease)

`python3 -c "…sum(1 for c in claims if c['status']=='active')"` → **0**. No live lease exists, so no zone is reserved.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30 … 2026-11-07 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (244 refs) and the 47 open-PR heads. No **new** mismatch this run.

### 2.3 Stale live leases — 0

No claim is `active`, so no lease is past its 24 h TTL. Reproduce: for each `active` claim, assert `claimed_at + ttl_hours > now` (now = `2026-11-08T02:44Z`).

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
| **G2** matrix + docs | `pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `121 passed in 0.95s` | 0 |
| **G3** board contract | `pytest -q tests/unit/test_agent_board.py` | `134 passed in 2.58s` | 0 |

Environment: Python `3.13` (`/usr/local/bin/python`), **`PYTHONPATH=src` source checkout** (not an editable install). The sandbox had no `.venv` and none of the project's dependencies; the watchdog installed the closure needed to *run the gates only* (`pytest`, `pytest-asyncio`, `pydantic`, `pydantic-settings`, `sqlmodel`, `sqlalchemy`, `alembic`, `aiosqlite`, `structlog`, `typer`, `httpx`, `python-multipart`, `numpy`, `pillow`, `beautifulsoup4`, `psutil`, `arabic-reshaper`, `python-bidi`, `duckduckgo-search`, `langgraph`, `langgraph-checkpoint-sqlite`, plus `greenlet` / `sqlalchemy[asyncio]` — without `greenlet`, `test_agent_board.py` errors at import with `ImportError: The SQLAlchemy asyncio module requires that the Python 'greenlet' library is installed`). All three gates **GREEN** — no gate regressed vs the 2026-11-07 run.

> **Environment note (F5).** The documented reproduction path is `pip install -e ".[dev]"` (editable install, `AGENTS.md` §3). With a bare `PYTHONPATH=src` the gate suite still runs here because the three gate modules do not spawn `python -m nexus_ai_agent.cli`, but the four migration-race tests and the distribution-version test documented in `docs/architecture/TESTING.md` §5 would fail for purely environmental reasons. No gate in this report depends on those.

---

## 4. CI health — 12 most recently updated open PRs

Canonical required contexts (from the live protection API read with the PAT, and `AGENTS.md` §1): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status per context on the **exact head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, latest run wins), distinguishing `queued`/`in_progress`/`cancelled`/`failure`/`skipped`/`success`/`absent`:

| PR | Head SHA | Updated (UTC) | `lint` | `test` | `merge-base-guard` |
|---|---|---|---|---|---|
| #319 | `3d7bbfe510` | 2026-11-07T21:25:38Z | success | success | success |
| #323 | `0e7c4f4d16` | 2026-11-07T19:06:26Z | success | success | success |
| #301 | `c7dba9f292` | 2026-11-07T02:50:05Z | success | success | success |
| #322 | `8013704fd7` | 2026-11-06T22:44:53Z | success | success | success |
| #321 | `6f2e54b8c1` | 2026-11-06T21:55:51Z | success | success | success |
| #320 | `cf037b455d` | 2026-11-06T21:54:16Z | success | success | success |
| #313 | `ca7f9ea886` | 2026-11-04T20:50:07Z | success | success | success |
| #193 | `d505d80a4e` | 2026-11-04T11:52:33Z | success | success | **skipped** |
| #204 | `13078d7926` | 2026-11-01T20:28:44Z | success | success | success |
| #92 | `40c1163314` | 2026-11-01T20:27:15Z | success | success | success |
| #69 | `7498074867` | 2026-10-31T12:16:59Z | success | success | **absent** |
| #66 | `db18cf66f3` | 2026-10-31T12:16:57Z | success | success | **absent** |

No `queued`/`in_progress` at snapshot time. Superseded `cancelled` duplicates exist (a re-run cancels the previous attempt) but are never the latest run.

### 4.1 Repository-wide required-context histograms (all 47 PRs)

| Check | Latest-run histogram on exact head |
|---|---|
| `lint (ruff + mypy + version lockstep)` | `{success: 43, failure: 4}` (absent: 0) |
| `test (pytest -m "not slow")` | `{success: 37, failure: 10}` (absent: 0) |
| `merge-base-guard (base == main)` | `{success: 25, skipped: 2, absent: 20}` |

`merge-base-guard` is **absent on 20 legacy heads** (unchanged): #57, #58, #60, #63, #66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #135, #137, #140, #149, #150, #157. The **2 `skipped`** runs are #193 and #205 — a *skipped* required check is not a pass.

### 4.2 Non-required `CircleCI Pipeline` `ERROR` (D7 — worsened)

The combined commit status on the 12 recent heads (`.../commits/<sha>/status`) is `failure` for **9 of 12** — #319, #323, #301, #322, #321, #320, #313, #204, #92 — each solely from the non-required `CircleCI Pipeline` context reading `error`. `#193` is `success` (from `CodeRabbit`, not CircleCI); #69 and #66 are `pending` (no statuses). The repository still has **no `.circleci/config.yml`** (`ls -d .circleci` → *No such file or directory*), while the board carries a `queued` claim `task-nagar-circleci-pilot-01` whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. The status originates from an external CircleCI project not reproducible from the tree.

---

## 5. Findings (engineering debt)

### D1 — `_latest_ci_verdict` still masks a real `FAILURE` behind an absent required context (P0) — **STILL OPEN**

The ordering bug is unchanged (`scripts/pr_convergence.py`, `_latest_ci_verdict`): with `require_all_present=True`, a required context that never ran returns `PENDING` **before** the red check runs on the present contexts. Reproduced at function level in §1.1. Now that the PAT reads protection (§5, D8), the strict path is **active again**: the PAT-based registry reports `CI_PENDING` 20 and only 2 `CI_RED`, whereas the truth is that **6 of those 20 heads carry a real `lint`/`test` `failure`** (#57, #58, #60, #63, #135, #150) and should read `CI_RED`/`FAILURE`. Reproduce the 6: for each open PR, require `merge-base-guard == absent` **and** (`lint == failure` or `test == failure`). This is the *first* run in which D1 is **active, not latent** — because the PAT works again.

### D2 — `merge-base-guard` absent on 20 heads (P0) — **STILL OPEN (unchanged)**

20 legacy heads have no `merge-base-guard` run; the registry cannot distinguish "never ran" from "still running", and a *skipped* guard (#193, #205) is likewise not surfaced as its own signal. Unchanged 20 → 20.

### D3 — board hygiene: 13 dead-branch claims + 7 owner-labelled rows with no clock (P1) — **STILL OPEN (unchanged)**

The 13 dead-branch claims (§2.2) and the 7 owner-labelled rows (§2.5) persist byte-identically. No new ones this run.

### D5 — `protocol.protocol_version` (P1) — **RESOLVED (stays resolved)**

`protocol.protocol_version == 2` is present in the board and enforced by `tests/unit/test_agent_board.py` (134 passing).

### D7 — non-required `CircleCI Pipeline` `ERROR` on 9 of 12 recent heads (P1) — **STILL OPEN, WORSENED**

9 of the 12 recent heads carry a combined status of `failure` from the non-required CircleCI context alone (§4.2). On 2026-11-07 this touched 8 PRs; it now touches 9 (#323 added to the window). It is the sole non-required reason the five `READY` PRs (#319–#323) are `UNSTABLE` rather than clean.

### D8 — branch protection readability (P0) — **IMPROVED (partially recovered)**

On 2026-11-07 **both** the ambient `GITHUB_TOKEN` and the repository PAT returned `403 Resource not accessible by integration`. This run the repository PAT (`GITHUB_PERSONAL_ACCESS_TOKEN`) reads protection (`protection_state: readable`, 3 canonical contexts), while the ambient `GITHUB_TOKEN` still returns `403` (`protection_state: unreadable`, 15 workflow-derived contexts). Consequence: the **default** `collect` (ambient) still certifies **no PR READY** and tiers the five clean PRs `PROTECTION_UNREADABLE`; a `GH_TOKEN=$PAT` collect certifies **5 READY** and exposes D1 as active. Reproduce: `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` (403 ambient) vs `GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN" gh api …` (200, 3 contexts).

### N6 — the automation branch's board is still stale (P2, informational)

`automation/debt-watchdog` (at `c7dba9f292`) still carries the 2026-10-31 board (`sha256 592576dd…`, 79 zones / 175 claims, `updated_at 2026-10-31T15:35:11Z`) while `main` is at `sha256 a0803cd0…`, 89 zones / 189 claims, `updated_at 2026-11-05T10:30:34Z`. N3 is therefore **still open on the branch**, but it no longer pollutes *this* audit because the report is written against the `main` board (working tree == `main` here). The watchdog does not modify `board.json` (read-only outside `.agents/evidence/`), so the branch staleness persists until a human syncs it.

---

## 6. Delta vs previous run (2026-11-07, `main` `6838c54378d2` — **same `main` SHA**)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-07.md` (branch-only).

| Metric | 2026-11-07 | 2026-11-08 | Change |
|---|---|---|---|
| `main` SHA | `6838c54378d2` | `6838c54378d2` | **unchanged** |
| Open PRs | 46 | 47 | **+1** |
| Open-PR number set | 46 ids | +1 new (#323), −0 gone | #323 new |
| DRAFT tier | 11 | 11 | 0 |
| CONFLICT tier | 27 | 27 | 0 |
| CI_RED tier | 3 | 3 | 0 |
| REBASE_FIRST tier | 1 | 1 | 0 |
| PROTECTION_UNREADABLE tier | 4 | 5 | **+1** |
| READY (ambient) | 0 | 0 | 0 |
| READY (PAT-readable) | 0 (both tokens 403) | 5 | **+5** |
| Remote branches | 243 | 244 | +1 |
| Board zones / claims (`main`) | 89 / 189 | 89 / 189 | **0 / 0 (byte-identical)** |
| Board sha (`main`) | `a0803cd0…` | `a0803cd0…` | **unchanged** |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Active (leased) claims | 0 | 0 | 0 |
| Stale live leases | 0 | 0 | 0 |
| `gates_owner` holders | 0 | 0 | 0 |
| Branch protection | unreadable (both tokens) | **readable via PAT**, unreadable via ambient | **IMPROVED** |
| Gates G1/G2/G3 | green | green | 0 |

**New PR since last run:** #323 (`PROTECTION_UNREADABLE`/`READY`-under-PAT, `docs(process): NAGAR-ONE engineering skill + session memory`, head `0e7c4f4d16`, `APPROVED`, `UNSTABLE`). **Gone since last run:** none.
**Heads advanced among common PRs:** #319 `58deaa097e → 3d7bbfe510`; #301 `9c168445d8 → c7dba9f292`.
**Tier changes among common PRs:** none (all common PRs keep their 2026-11-07 tier under the ambient registry).

**Regressions:**
- **D7 worsened** (8 → 9 of 12 recent heads carry the non-required CircleCI `error`; #323 entered the window).
- **PROTECTION_UNREADABLE tier 4 → 5** (+#323).
- No gate failure (G1/G2/G3 green both runs). No new ownership mismatch (still 13, byte-identical). No new stale lease (0 both runs).

**Resolved / improved since last run:**
- **D8 improved**: the PAT reads branch protection again (was 403 on both tokens on 2026-11-07). The default registry still degrades because the ambient `GITHUB_TOKEN` remains 403 — that half is still open.
- **D1 flipped from latent to active**: because the PAT path is readable again, the strict `require_all_present=True` ordering bug now actually mis-classifies 6 red heads as `PENDING`.
- D5 (`protocol_version`) stays resolved.

**Unchanged / still open:** D1 (now **active** — 6 masked heads), D2 (`merge-base-guard` absent on 20 heads), D3 (board hygiene: 13 dead-branch + 7 owner-labelled), D7 (CircleCI `ERROR`), D8 (ambient token still 403), N3/N6 (stale board on the automation branch).

---

## 7. Next actions (prioritized)

1. **[P0] Restore the ambient `GITHUB_TOKEN`'s protection read (D8, remaining half).** The PAT works; the automation's ambient token still returns `403`. Without it the *default* `collect` degrades to 15 contexts and certifies no PR `READY`. Grant the automation App/installation `administration: read` (or `branches`), then verify `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` returns `200` with the 3 canonical contexts.
2. **[P0] Fix `_latest_ci_verdict` ordering (D1) — now ACTIVE.** Evaluate the `_RED_CONCLUSIONS` verdict over the **present** contexts *before* returning `PENDING` for a missing one. Today 6 heads (#57, #58, #60, #63, #135, #150) are mis-tiered `CI_PENDING` instead of `CI_RED` whenever protection is readable.
3. **[P0] Review and land the five clean PRs (#319, #320, #321, #322, #323).** Each clears `lint`/`test`/`merge-base-guard`; #320/#321/#322/#323 are already `APPROVED`, all five are `UNSTABLE` only on the non-required CircleCI context. They are the nearest green work on `main`.
4. **[P0] Distinguish "required check never ran" from "still running" (D2)**: give the 20 legacy heads missing `merge-base-guard` their own signal, and treat the 2 `skipped` guards (#193, #205) as non-green.
5. **[P1] Resolve the `CircleCI Pipeline` `ERROR` (D7):** commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — it currently marks 9 of 12 recent heads' combined status `failure`.
6. **[P1] Rebase or close #313** — it remains `CONFLICT`/`DIRTY` after `main` advanced; rebase is cheap.
7. **[P2] Sweep board hygiene (D3):** remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
8. **[P2] Re-sync the automation branch's board (N3/N6)** so `check`/`next` do not read a 2026-10-31 board on `automation/debt-watchdog`.
9. **[P2] Attack the rebase wall:** 42 of 47 PRs carry `REBASE_FIRST`; triage the 27 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
