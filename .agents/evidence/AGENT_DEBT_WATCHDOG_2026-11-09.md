# Engineering-Debt Watchdog — 2026-11-09

- **Run timestamp (UTC):** `2026-11-09T02:44:34Z` (registry `generated_at` `2026-11-09T02:39:46Z` ambient / `2026-11-09T02:40:59Z` PAT; report written immediately after)
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `5ed6d4e10be55f5baf5e9f2a8aa99775577621db` (`5ed6d4e10be5`, "Merge pull request #322 from bot523h/nagar-one-handoff-report")
- **Observer:** NEXUS engineering-debt watchdog automation — **observe-and-report only**; no product-code edits, no merge, no push to `main`. The only file created is this one under `.agents/evidence/`.
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-08.md` (branch `automation/debt-watchdog`, `main` `6838c54378d2`) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable, that is stated explicitly.

> **Credential note (unchanged from 2026-11-08) — D8 still partially recovered.** Branch protection is readable **with the repository PAT** (`GITHUB_PERSONAL_ACCESS_TOKEN` → `200`, 3 canonical contexts) but still `403 Resource not accessible by integration` with the ambient `GITHUB_TOKEN`. The default `collect` (ambient) therefore degrades to 15 workflow-derived contexts and certifies **no PR as READY**; a second `collect` with `GH_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN` reads the real gate and certifies **2 PRs READY** (#324, #325). Both views are reported below.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `5ed6d4e10be5` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` (working tree == `main`; sha256 `f27bcb44729d142504fcdd060c67eed34cc92c277e6e75b2e88cba0376fb534c`) | schema 2; **89 zones, 189 claims**, 14 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; board `updated_at = 2026-11-06T21:25:39Z` — **changed vs 2026-11-08** (was `a0803cd0…`, `updated_at 2026-11-05T10:30:34Z`); the change is one status flip `queued → active` (see §2.1) |
| Remote branches | **247** (`git ls-remote --heads origin \| wc -l`; **197** are `refs/heads/arena/*`) |
| Open PRs | **44** (`python scripts/pr_convergence.py collect --out /tmp/reg.json`) |
| Branch protection | **unreadable** with the ambient `GITHUB_TOKEN`; **readable** with the repository PAT (3 canonical contexts) |

---

## 1. Live PR registry

```
$ python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: branch protection UNREADABLE (… 403 Resource not accessible by integration);
             degrading to workflow-derived contexts […]; No PR will be certified READY.
pr-registry: 44 open PRs → /tmp/reg.json (main 5ed6d4e10be5)
COLLECT EXIT=0

$ python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-09T02:39:46Z`, `main_sha`: `5ed6d4e10be55f5baf5e9f2a8aa99775577621db`
- `open_total`: `44`, `protection_state`: `unreadable` (ambient), `protection_source`: `ci.yml workflow (branch-protection API unreadable)`
- `required_contexts` (15, workflow-derived because the ambient token cannot read protection): `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "extras-matrix (${{ matrix.leg }})", "python-parity (${{ matrix.python-version }})", "continuum-evidence (${{ matrix.python-version }})", "trust-mutations (pack trust plane + IBPC security closure)", "b2-blob-tier-mutations (maintenance blob tier)", "temporal-mutations (temporal truth algebra)", "governance-mutations (board referee 42 + Gate-5 fencing 17)", "execution-core-mutations (V1 execution core 25)", "remote-key-mutations (ingress + cache containment)", "api-gate-mutations (A-06 security surface)", "merge-base-guard (base == main)", "lint-fast (lockstep + pinned ruff, no install)", "dependency-audit (pip-audit vs reviewed allow-list)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-11-08 |
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
| PROTECTION_UNREADABLE | 2 | **−3** |
| **READY** | **0** | **0** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 11 · `CONFLICT` 34 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 12 · `CI_PENDING` 0 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 42 · `PROTECTION_UNREADABLE` 44.

**All 44 open PRs are non-READY** under the ambient registry. Controlling blocker (first in the ordered list) per PR:

| PR | Tier | Controlling blocker | CI state | Title |
|---|---|---|---|---|
| #33 | CONFLICT | CONFLICT | SUCCESS | v3.13.0 — deliver the P0 security code, wire the dead engines, f |
| #57 | CONFLICT | CONFLICT | FAILURE | feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-160) |
| #58 | DRAFT | DRAFT | FAILURE | Security Boundary hardening — S1/S2/S3/S4/S5 (guard STOP, force- |
| #60 | CONFLICT | CONFLICT | FAILURE | M0 runtime integration: InstrumentedJobQueue + readyz factory +  |
| #63 | CONFLICT | CONFLICT | FAILURE | task-163: board-Git truth gate + scale-to-zero state tiers (#1 + |
| #64 | CI_RED | CI_RED | FAILURE | Creative Runtime (task-176 s2): execution semantics, multi-segme |
| #66 | CONFLICT | CONFLICT | SUCCESS | P0 creative integration: one production path, closed legacy lane |
| #69 | CONFLICT | CONFLICT | SUCCESS | Gate 4: cross-layer vertical slice verification (T01·T02·T22·T31 |
| #73 | CONFLICT | CONFLICT | SUCCESS | Gate 2.2 — Operation Truth → Executable Evidence Gate (anti-drif |
| #84 | CONFLICT | CONFLICT | SUCCESS | Register the NEXUS Engineering Constitution as an enforced contr |
| #89 | CONFLICT | CONFLICT | SUCCESS | task-122: 12 real command wirings + moderation ingress — forensi |
| #92 | DRAFT | DRAFT | SUCCESS | fix(storage): honor SQLite defaults; preserve inherited release  |
| #97 | CI_RED | CI_RED | FAILURE | feat(vision): complete portrait and scene Vision packs |
| #99 | CONFLICT | CONFLICT | SUCCESS | task-193: command truth — 11 fake/silent commands wired to the e |
| #113 | CONFLICT | CONFLICT | SUCCESS | feat(runtime): W1 final close-out — engine-owned schema fast-pat |
| #114 | DRAFT | DRAFT | FAILURE | feat(cockpit): Persian capability explorer and input-only comman |
| #115 | CONFLICT | CONFLICT | SUCCESS | W1: True Runtime Closure / Lifecycle Ownership / Zero Semantic B |
| #117 | DRAFT | DRAFT | SUCCESS | fix(runtime): close audited session/media contracts and wheel re |
| #118 | DRAFT | DRAFT | SUCCESS | W2 golden hardening: authority races, probe permits and independ |
| #121 | CONFLICT | CONFLICT | SUCCESS | feat(memory): retrieval truth — noise-ranked recall (hit@3 0.167 |
| #122 | CONFLICT | CONFLICT | SUCCESS | fix(orchestration): H1 — route chat through the memory reader (t |
| #129 | CONFLICT | CONFLICT | SUCCESS | feat(creative/studio): staged plan transactions and atomic commi |
| #135 | CONFLICT | CONFLICT | FAILURE | feat(creative/studio): canonical PlanTransaction substrate and d |
| #137 | CONFLICT | CONFLICT | SUCCESS | board(task-227,task-228): collision preflight + executable gover |
| #140 | CONFLICT | CONFLICT | SUCCESS | Harden Studio CommandBus: PlanTransaction, Logical Identity, and |
| #149 | CONFLICT | CONFLICT | SUCCESS | Security Wave 1 Verification & Governance Alignment |
| #150 | DRAFT | DRAFT | FAILURE | feat(agent-intelligence): verified CreativeWork trim path |
| #157 | CONFLICT | CONFLICT | SUCCESS | feat(governance): Nagar agent constitution — persistent, version |
| #193 | CONFLICT | CONFLICT | SUCCESS | feat(infra): canonical Ubuntu 26.04 + Python 3.14 foundation |
| #204 | DRAFT | DRAFT | SUCCESS | fix(memory): resolve sqlite-vec extension fallback and add foren |
| #205 | CONFLICT | CONFLICT | FAILURE | feat(nagar): Asset Library V1 — secure content-addressed ingest  |
| #230 | CONFLICT | CONFLICT | SUCCESS | Arena/aa451c49 nexus ai agent |
| #247 | CONFLICT | CONFLICT | SUCCESS | feat(creative): perception lane — measured silence/scene/loudnes |
| #257 | DRAFT | DRAFT | SUCCESS | 🎨 Palette: Improve Telegram inline callback UX for language and  |
| #273 | DRAFT | DRAFT | SUCCESS | feat(nagar): P0 intent NEL semantic time causal contracts |
| #284 | DRAFT | DRAFT | FAILURE | feat(nagar): add read-only Causal Memory and bounded Director pr |
| #285 | DRAFT | DRAFT | SUCCESS | chore(board): synchronize task-278 deferred status |
| #294 | CONFLICT | CONFLICT | SUCCESS | repair(#189): land the durable creative-graph package (law R28)  |
| #295 | CI_RED | CI_RED | FAILURE | extract(#218): land the self-contained content-addressed Asset L |
| #297 | CONFLICT | CONFLICT | SUCCESS | evidence(m16): true closeout of the M15 spine + open-PR converge |
| #301 | REBASE_FIRST | REBASE_FIRST | SUCCESS | chore(automation): engineering-debt watchdog 2026-11-08 |
| #313 | CONFLICT | CONFLICT | SUCCESS | chore(board): register task-nagar-preservation-media-profiles-01 |
| #324 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | docs(closeout): NAGAR-ONE P0 final closeout + PR reconciliation  |
| #325 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | fix(task-282): land the F1 artifact-binding ownership fence on m |

### 1.1 Secondary view — registry with the PAT (protection **readable**, 3 canonical contexts)

```
$ GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN" python scripts/pr_convergence.py collect --out /tmp/reg_pat.json
pr-registry: 44 open PRs → /tmp/reg_pat.json (main 5ed6d4e10be5)
$ python scripts/pr_convergence.py report --in /tmp/reg_pat.json
```

- `protection_state`: `readable`, `required_contexts` = the **3 canonical** contexts (`lint`, `test`, `merge-base-guard`), `required_approvals`: `0`.
- Tiers: `DRAFT` 11 · `CONFLICT` 27 · `CI_RED` 2 · `REBASE_FIRST` 2 · `PROTECTION_UNREADABLE` **0** · **`READY` 2** (#324, #325).
- Blocker counts: `CONFLICT` 34 · `CI_RED` 5 · **`CI_PENDING` 20** · `CHANGES_REQUESTED` 11 · `REBASE_FIRST` 42.

**The 20 `CI_PENDING` under the readable path are the D1 bug, not reality** (see §5, D1): the strict path calls `_latest_ci_verdict(..., require_all_present=True)`, which returns `PENDING` when a required context is **absent** *before* it evaluates the present contexts' verdicts. The 20 split into (a) **6 genuinely-red heads** whose `merge-base-guard` never ran (#57, #58, #60, #63, #135, #150 — each carries a real `lint`/`test` `failure`) that the strict path **masks** as `PENDING`, and (b) **14 heads** (#66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #137, #140, #149, #157) whose `lint`/`test` are both `success` and whose `merge-base-guard` genuinely never ran. Reproduce (a):

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

The six masked heads, verified on the exact head SHA (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, latest run per name):

| PR | Head SHA | `lint` | `test` | `merge-base-guard` | Registry `ci_state` | True verdict |
|---|---|---|---|---|---|---|
| #57 | `2f591618e1` | failure | success | absent | PENDING | **FAILURE** |
| #58 | `bb06da1cb3` | failure | failure | absent | PENDING | **FAILURE** |
| #60 | `efea9bae3e` | failure | failure | absent | PENDING | **FAILURE** |
| #63 | `3e376bea06` | success | failure | absent | PENDING | **FAILURE** |
| #135 | `f83b12835c` | success | failure | absent | PENDING | **FAILURE** |
| #150 | `5f84be4dd2` | failure | failure | absent | PENDING | **FAILURE** |

**PRs whose canonical required checks** (`lint`, `test`, `merge-base-guard`) are **all `success`**: **18 of 44** — #33, #92, #99, #115, #122, #204, #230, #247, #257, #273, #285, #294, #295, #297, #301, #313, #324, #325. (A *CI-green* set, not a mergeable set: most also carry `CONFLICT`/`DRAFT`/`CHANGES_REQUESTED`.)

---

## 2. Board audit

```
$ PYTHONPATH=src python -m pytest -q tests/unit/test_agent_board.py
134 passed in 3.07s
EXIT=0
```

The authoritative board is the one on **`main`**; the working-tree copy is byte-identical this run (`sha256 f27bcb44…`, `git show origin/main:.agents/board.json | sha256sum` → same). No board-state mutation is performed by the audit.

- **Claims:** 189. **Zones:** 89. Status histogram: `done` 80, `queued` 61, `expired` 20, `completed_merged` 9, `completed_released` 6, `deferred` 3, `available_sequenced_post_33` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1, **`active` 1**.
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).
- **Claims carrying an `agent_branch`:** 22.
- **`protocol.protocol_version`:** present and `== 2` (D5 of the 2026-10-31 report stays resolved).
- **Board delta vs 2026-11-08:** the `main` board changed — `sha256 a0803cd0…` → `f27bcb44…`. The only claim-level change is `task-282-slice-2-artifact-assetversion-binding`: `queued → active` (reproduce: diff the `status` of each claim between `git show 6838c54378d2:.agents/board.json` and the working tree). Zone/claim counts are unchanged (89 / 189).

### 2.1 Active claims — 1 (a **stale** live lease, NEW)

`python3 -c "…sum(1 for c in claims if c['status']=='active')"` → **1**:

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-282-slice-2-artifact-assetversion-binding` | `durable-project-state-lifecycle` | `nagar-one-slice2-artifact-revision-binding` | yes (`c5b7da56…`) | **no** (PR #325 head is `fix/binding-ownership-fence-20261107`) | 24 | 2026-11-06T21:25:39Z | **STALE** — expired 2026-11-07T21:25:39Z, **~29 h past TTL** at run time |

- The lease is **stale** (24 h TTL lapsed 2026-11-07T21:25:39Z; run at 2026-11-09T02:44:34Z). It still reserves the zone `durable-project-state-lifecycle` (whose `paths` include `src/nexus_ai_agent/cli.py` and `src/nexus_ai_agent/creative/project_state/`) for work that has **already shipped**: PR **#325** (head `fix/binding-ownership-fence-20261107`, `d022242f80`, CI green, `MERGEABLE`, `UNSTABLE`) is the successor vehicle whose body states it lands "the F1 artifact-binding ownership fence on main". This is **landed-but-still-leased** debt, exactly like the 2026-10-31 finding.
- **Overlap check:** no cross-zone collision — the three `exclusive_paths` (`src/nexus_ai_agent/creative/project_state/`, `tests/integration/test_durable_project_revision_pinning.py`, `docs/architecture/DURABLE_PROJECT_STATE.md`) all fall inside the declared zone, and there is no second live lease, so no overlap with another agent's live lease.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30 … 2026-11-08 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (247 refs) and the 44 open-PR heads.

### 2.3 Stale live leases — 1 (NEW)

The single `active` claim (§2.1) is ~29 h past its 24 h TTL. Last run had **0** active claims and **0** stale leases.

### 2.4 Claims assigned without a lease (D3) — 7 (unchanged)

| Task | Status | `agent_branch` | `claimed_at` |
|---|---|---|---|
| `task-122` | `assigned_to_B` | (absent) | (absent) |
| `task-123` | `assigned_to_E_pr33` | (absent) | (absent) |
| `task-124` | `available_sequenced_post_32_33` | (absent) | (absent) |
| `task-126` | `available_sequenced_post_33` | (absent) | (absent) |
| `task-121` | `available_sequenced_post_33` | (absent) | (absent) |
| `task-128` | `available_sequenced_post_32` | (absent) | (absent) |
| `task-127` | `assigned_to_B_next` | (absent) | (absent) |

Seven claims carry an owner/sequence label with no machine-checkable branch or lease clock. Unchanged from 2026-11-08.

### 2.5 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations, with prefix logic — a zone path like `tests/unit/test_ibpc_` is a prefix that covers `tests/unit/test_ibpc_intent_compiler.py`). No cross-zone overlap. Reproduce: for each claim, assert every `exclusive_paths` entry equals a `zone.paths` entry, is a child of one, or shares a `zone.paths` prefix.

---

## 3. Gates

| Gate | Command | Result | Exit |
|---|---|---|---|
| **G1** truth gate | `PYTHONPATH=src python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| **G2** matrix + docs | `PYTHONPATH=src pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `121 passed in 1.08s` | 0 |
| **G3** board contract | `PYTHONPATH=src pytest -q tests/unit/test_agent_board.py` | `134 passed in 3.07s` | 0 |

Environment: Python `3.13.15` (`/usr/local/bin/python`), **`PYTHONPATH=src` source checkout** (not an editable install). The sandbox had no `.venv` and none of the project's dependencies; the watchdog installed the closure needed to *run the gates only* (`pytest`, `pytest-asyncio`, `pydantic`, `pydantic-settings`, `sqlmodel`, `sqlalchemy`, `alembic`, `aiosqlite`, `structlog`, `typer`, `httpx`, `python-multipart`, `numpy`, `pillow`, `beautifulsoup4`, `psutil`, `arabic-reshaper`, `python-bidi`, `duckduckgo-search`, plus `greenlet` / `sqlalchemy[asyncio]` — without `greenlet`, `test_agent_board.py` errors at import with `ImportError: The SQLAlchemy asyncio module requires that the Python 'greenlet' library is installed`). All three gates **GREEN** — no gate regressed vs the 2026-11-08 run.

---

## 4. CI health — 12 most recently updated open PRs

Canonical required contexts (from the live protection API read with the PAT, and `AGENTS.md` §1): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status per context on the **exact head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, latest run wins), distinguishing `queued`/`in_progress`/`cancelled`/`failure`/`skipped`/`success`/`absent`:

| PR | Head SHA | Updated (UTC) | `lint` | `test` | `merge-base-guard` |
|---|---|---|---|---|---|
| #325 | `d022242f80` | 2026-11-08T23:57:07Z | success | success | success |
| #294 | `9b60307a14` | 2026-11-08T23:32:36Z | success | success | success |
| #324 | `366033ec23` | 2026-11-08T08:49:57Z | success | success | success |
| #301 | `4afdc866b1` | 2026-11-08T02:47:10Z | success | success | success |
| #313 | `ca7f9ea886` | 2026-11-04T20:50:07Z | success | success | success |
| #193 | `d505d80a4e` | 2026-11-04T11:52:33Z | success | success | **skipped** |
| #204 | `13078d7926` | 2026-11-01T20:28:44Z | success | success | success |
| #92 | `40c1163314` | 2026-11-01T20:27:15Z | success | success | success |
| #69 | `7498074867` | 2026-10-31T12:16:59Z | success | success | **absent** |
| #66 | `db18cf66f3` | 2026-10-31T12:16:57Z | success | success | **absent** |
| #297 | `f142f2e923` | 2026-10-31T08:16:47Z | success | success | success |
| #295 | `c641154d19` | 2026-10-31T08:16:02Z | success | success | success |

No `queued`/`in_progress` at snapshot time. Superseded `cancelled` duplicates exist (a re-run cancels the previous attempt) but are never the latest run.

### 4.1 Repository-wide required-context histograms (all 44 PRs)

| Check | Latest-run histogram on exact head |
|---|---|
| `lint (ruff + mypy + version lockstep)` | `{success: 40, failure: 4}` (absent: 0) |
| `test (pytest -m "not slow")` | `{success: 34, failure: 10}` (absent: 0) |
| `merge-base-guard (base == main)` | `{success: 22, skipped: 2, absent: 20}` |

`merge-base-guard` is **absent on 20 legacy heads** (unchanged): #57, #58, #60, #63, #66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #135, #137, #140, #149, #150, #157. The **2 `skipped`** runs are #193 and #205 — a *skipped* required check is not a pass.

### 4.2 Non-required `CircleCI Pipeline` `ERROR` (D7 — improved)

The combined commit status on the 12 recent heads (`.../commits/<sha>/status`) is `failure` for **6 of 12** — #325, #324, #301, #313, #204, #92 — each solely from the non-required `CircleCI Pipeline` context reading `error`. #193 is `success` (from `CodeRabbit`, not CircleCI); #294, #69, #66, #297, #295 are `pending` (no statuses). The repository still has **no `.circleci/config.yml`** (`ls -d .circleci` → *No such file or directory*), while the board carries a `queued` claim `task-nagar-circleci-pilot-01` whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. The status originates from an external CircleCI project not reproducible from the tree. This window shrank from **9 of 12** (2026-11-08) to **6 of 12**.

---

## 5. Findings (engineering debt)

### D1 — `_latest_ci_verdict` still masks a real `FAILURE` behind an absent required context (P0) — **STILL OPEN (active)**

The ordering bug is unchanged (`scripts/pr_convergence.py`, `_latest_ci_verdict`): with `require_all_present=True`, a required context that never ran returns `PENDING` **before** the red check runs on the present contexts. Reproduced at function level in §1.1 (`PENDING` where `FAILURE` is correct). The PAT-based registry reports `CI_PENDING` 20 and only 2 `CI_RED`, whereas the truth is that **6 of those 20 heads carry a real `lint`/`test` `failure`** (#57, #58, #60, #63, #135, #150). Reproduce the 6: for each open PR, require `merge-base-guard == absent` **and** (`lint == failure` or `test == failure`).

### D2 — `merge-base-guard` absent on 20 heads (P0) — **STILL OPEN (unchanged)**

20 legacy heads have no `merge-base-guard` run; the registry cannot distinguish "never ran" from "still running", and a *skipped* guard (#193, #205) is likewise not surfaced as its own signal. 20 → 20.

### D3 — board hygiene: 13 dead-branch claims + 7 owner-labelled rows with no clock (P1) — **STILL OPEN (unchanged)**

The 13 dead-branch claims (§2.2) and the 7 owner-labelled rows (§2.4) persist byte-identically. No new ones this run.

### D5 — `protocol.protocol_version` (P1) — **RESOLVED (stays resolved)**

`protocol.protocol_version == 2` is present in the board and enforced by `tests/unit/test_agent_board.py` (134 passing).

### D7 — non-required `CircleCI Pipeline` `ERROR` on 6 of 12 recent heads (P1) — **STILL OPEN, IMPROVED**

6 of the 12 recent heads carry a combined status of `failure` from the non-required CircleCI context alone (§4.2). It is the sole non-required reason #324 and #325 are `UNSTABLE` rather than clean. Down from 9 of 12 on 2026-11-08 (the older heads in that window rolled out of the 12-most-recent set).

### D8 — branch protection readability (P0) — **IMPROVED (unchanged half)**

As on 2026-11-08, the repository PAT (`GITHUB_PERSONAL_ACCESS_TOKEN`) reads protection (`protection_state: readable`, 3 canonical contexts), while the ambient `GITHUB_TOKEN` still returns `403 Resource not accessible by integration` (`protection_state: unreadable`, 15 workflow-derived contexts). Consequence: the **default** `collect` (ambient) still certifies **no PR READY** and tiers the two clean PRs `PROTECTION_UNREADABLE`; a `GH_TOKEN=$PAT` collect certifies **2 READY** and exposes D1 as active. Reproduce: `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` (403 ambient) vs `GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN" gh api …` (200, 3 contexts).

### N7 — the `main` board now carries a stale `active` lease for already-shipped work (P1, NEW)

See §2.1. The `main` board's status flip `queued → active` (the only board change since 2026-11-08) created a live-but-stale lease on `task-282-slice-2-artifact-assetversion-binding` whose branch (`nagar-one-slice2-artifact-revision-binding`, head `c5b7da56…`) is **not** the head of the PR that actually delivers the work (PR #325 head is `fix/binding-ownership-fence-20261107`). The lease expired 2026-11-07T21:25:39Z (~29 h before this run) and still reserves zone `durable-project-state-lifecycle`. Owner action: `release` the claim (or `gc` it) since #325 already carries the F1 work.

### N8 — the automation branch's board is still stale (P2, informational)

`automation/debt-watchdog` (at `4afdc866b1`) still carries the 2026-10-31 board (`sha256 592576dd…`, 79 zones / 175 claims, `updated_at 2026-10-31T15:35:11Z`) while `main` is at `sha256 f27bcb44…`, 89 zones / 189 claims, `updated_at 2026-11-06T21:25:39Z`. The watchdog does not modify `board.json` (read-only outside `.agents/evidence/`), so the branch staleness persists until a human syncs it.

---

## 6. Delta vs previous run (2026-11-08, `main` `6838c54378d2`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-08.md` (branch-only).

| Metric | 2026-11-08 | 2026-11-09 | Change |
|---|---|---|---|
| `main` SHA | `6838c54378d2` | `5ed6d4e10be5` | **advanced** (24 commits) |
| Open PRs | 47 | 44 | **−3** |
| Open-PR number set | 47 ids | +2 new (#324, #325), −5 gone (#319, #320, #321, #322 merged; #323 closed) | — |
| DRAFT tier | 11 | 11 | 0 |
| CONFLICT tier | 27 | 27 | 0 |
| CI_RED tier (ambient / PAT) | 3 / 2 | 3 / 2 | 0 / 0 |
| REBASE_FIRST tier (ambient / PAT) | 1 / 2 | 1 / 2 | 0 / 0 |
| PROTECTION_UNREADABLE tier (ambient) | 5 | 2 | **−3** |
| READY (ambient / PAT) | 0 / 5 | 0 / 2 | 0 / **−3** |
| Remote branches | 244 | 247 | +3 |
| Board zones / claims (`main`) | 89 / 189 | 89 / 189 | 0 / 0 |
| Board sha (`main`) | `a0803cd0…` | `f27bcb44…` | **CHANGED** (one status flip) |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Owner-labelled rows (no branch/clock) | 7 | 7 | 0 |
| Active (leased) claims | 0 | 1 | **+1** |
| Stale live leases | 0 | 1 | **+1 (NEW)** |
| `gates_owner` holders | 0 | 0 | 0 |
| Branch protection | readable via PAT, 403 via ambient | readable via PAT, 403 via ambient | 0 |
| Gates G1/G2/G3 | green | green | 0 |
| CircleCI `error` on 12-recent heads | 9 | 6 | **improved** |

**New PRs since last run:** #324 (`READY`-under-PAT, `docs(closeout): NAGAR-ONE P0 final closeout + PR reconciliation`, head `366033ec23`), #325 (`READY`-under-PAT, `fix(task-282): land the F1 artifact-binding ownership fence on main`, head `d022242f80`).
**Gone since last run:** #319, #320, #321, #322 **merged** (heads `3d7bbfe510`, `cf037b455d`, `6f2e54b8c1`, `8013704fd7`), #323 **closed** (not merged).
**Heads advanced among common PRs:** #294 `9b60307a14`; #301 `c7dba9f292 → 4afdc866b1`.

**Regressions:**
- **NEW stale live lease (N7):** the `main` board flipped `task-282-slice-2` to `active`; the lease is ~29 h past TTL and reserves a zone for work already delivered by PR #325. Last run had 0 active / 0 stale.
- **Board changed on `main`** (`a0803cd0 → f27bcb44`), which is the substrate for N7.
- No gate failure (G1/G2/G3 green both runs). No new ownership mismatch (still 13, byte-identical). No new conflict beyond the +2/−5 set.

**Resolved / improved since last run:**
- **The 2026-11-08 "next action 3" is DONE:** the five clean PRs it listed (#319–#323) have all left the open set — #319–#322 merged, #323 closed.
- **D7 improved:** CircleCI `error` window shrank 9 → 6 of 12 recent heads.
- **D8** stays partially recovered (PAT readable).
- D5 (`protocol_version`) stays resolved.

**Unchanged / still open:** D1 (active — 6 masked heads), D2 (`merge-base-guard` absent on 20 heads; 2 skipped), D3 (board hygiene: 13 dead-branch + 7 owner-labelled), D7 (CircleCI `ERROR`), D8 (ambient token still 403), N8 (stale board on the automation branch).

---

## 7. Next actions (prioritized)

1. **[P0] `release`/`gc` the stale `active` lease on `task-282-slice-2-artifact-assetversion-binding` (N7).** The board on `main` reserves zone `durable-project-state-lifecycle` for work PR #325 already delivers; the lease expired 2026-11-07T21:25:39Z. `python scripts/agent_board.py release …` or `… gc`.
2. **[P0] Fix `_latest_ci_verdict` ordering (D1) — active.** Evaluate the `_RED_CONCLUSIONS` verdict over the **present** contexts *before* returning `PENDING` for a missing one. Today 6 heads (#57, #58, #60, #63, #135, #150) are mis-tiered `CI_PENDING` instead of `CI_RED` whenever protection is readable.
3. **[P0] Restore the ambient `GITHUB_TOKEN`'s protection read (D8, remaining half).** The PAT works; the automation's ambient token still returns `403`. Without it the *default* `collect` degrades to 15 contexts and certifies no PR `READY`. Grant the automation App/installation `administration: read` (or `branches`), then verify `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` returns `200` with the 3 canonical contexts.
4. **[P0] Review and land the two clean PRs (#324, #325).** Each clears `lint`/`test`/`merge-base-guard`; both are `MERGEABLE` and `UNSTABLE` only on the non-required CircleCI context. They are the nearest green work on `main`.
5. **[P0] Distinguish "required check never ran" from "still running" (D2):** give the 20 legacy heads missing `merge-base-guard` their own signal, and treat the 2 `skipped` guards (#193, #205) as non-green.
6. **[P1] Resolve the `CircleCI Pipeline` `ERROR` (D7):** commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — it currently marks 6 of 12 recent heads' combined status `failure`.
7. **[P1] Rebase or close #313** — it remains `CONFLICT`/`DIRTY` after `main` advanced; rebase is cheap.
8. **[P2] Sweep board hygiene (D3):** remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
9. **[P2] Re-sync the automation branch's board (N8)** so `check`/`next` do not read a 2026-10-31 board on `automation/debt-watchdog`.
10. **[P2] Attack the rebase wall:** 42 of 44 PRs carry `REBASE_FIRST`; triage the 27 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
