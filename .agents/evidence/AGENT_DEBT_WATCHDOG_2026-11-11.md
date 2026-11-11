# Engineering-Debt Watchdog — 2026-11-11

- **Run timestamp (UTC):** `2026-11-11T02:42:59Z` (registry `generated_at` `2026-11-11T02:40:19Z` ambient / `2026-11-11T02:40:31Z` PAT; report written immediately after)
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `5fe9451228ad6e4d5ac067aaa4cbd2df8c633cd4` (`5fe9451228ad`, "Merge pull request #324 from bot523h/openhands/nagar-one-final-closeout-20261107") — **advanced** from the 2026-11-10 run (`5ed6d4e10be5`); **PR #324 landed**.
- **Observer:** NEXUS engineering-debt watchdog automation — **observe-and-report only**; no product-code edits, no merge, no push to `main`. The only file created is this one under `.agents/evidence/`.
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-10.md` (branch `automation/debt-watchdog`, `main` `5ed6d4e10be5`) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable, that is stated explicitly.

> **Credential note (unchanged from 2026-11-10) — D8 still partially recovered.** Branch protection is readable **with the repository PAT** (`GITHUB_PERSONAL_ACCESS_TOKEN` → `200`, 3 canonical contexts, `strict: true`) but still `403 Resource not accessible by integration` with the ambient `GITHUB_TOKEN`. The default `collect` (ambient) therefore degrades to 15 workflow-derived contexts and certifies **no PR as READY**; a second `collect` with `GH_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN` reads the real gate and certifies **2 PRs READY** (#330, #331). Both views are reported below.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `5fe9451228ad` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` (working tree == `main`; sha256 `eda3f6ad023eb36533b2c2d48abc31079209b024e572b311aa67604125568847`) | schema 2; **89 zones, 190 claims**, 14 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; board `updated_at = 2026-11-08T08:42:47Z` (**advanced** from 2026-11-06; **190 claims** vs 189) |
| Remote branches | **255** (`git ls-remote --heads origin \| wc -l`; **201** are `refs/heads/arena/*`) |
| Open PRs | **49** (`python scripts/pr_convergence.py collect --out /tmp/reg_ambient.json`) |
| Branch protection | **unreadable** with the ambient `GITHUB_TOKEN` (403); **readable** with the repository PAT (3 canonical contexts) |

---

## 1. Live PR registry

```
$ GH_TOKEN="$GITHUB_TOKEN" python scripts/pr_convergence.py collect --out /tmp/reg_ambient.json
pr-registry: branch protection UNREADABLE (… 403 Resource not accessible by integration);
             degrading to workflow-derived contexts […]; No PR will be certified READY.
pr-registry: 49 open PRs → /tmp/reg_ambient.json (main 5fe9451228ad)
COLLECT EXIT=0   (two earlier attempts exited 2 on a transient GraphQL 504 / empty body — retried, then OK)

$ python scripts/pr_convergence.py report --in /tmp/reg_ambient.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-11T02:40:19Z`, `main_sha`: `5fe9451228ad6e4d5ac067aaa4cbd2df8c633cd4`
- `open_total`: `49`, `protection_state`: `unreadable` (ambient), `protection_source`: `ci.yml workflow (branch-protection API unreadable)`
- `required_contexts` (15, workflow-derived because the ambient token cannot read protection): `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "extras-matrix (${{ matrix.leg }})", "python-parity (${{ matrix.python-version }})", "continuum-evidence (${{ matrix.python-version }})", "trust-mutations (pack trust plane + IBPC security closure)", "b2-blob-tier-mutations (maintenance blob tier)", "temporal-mutations (temporal truth algebra)", "governance-mutations (board referee 42 + Gate-5 fencing 17)", "execution-core-mutations (V1 execution core 25)", "remote-key-mutations (ingress + cache containment)", "api-gate-mutations (A-06 security surface)", "merge-base-guard (base == main)", "lint-fast (lockstep + pinned ruff, no install)", "dependency-audit (pip-audit vs reviewed allow-list)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg_ambient.json`.

| Tier | Count | Δ vs 2026-11-10 |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 13 | **+2** |
| CONFLICT | 29 | **+2** |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 4 | **+1** |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 1 | 0 |
| PROTECTION_UNREADABLE | 2 | −2 |
| **READY** | **0** | **0** |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 13 · `CONFLICT` 36 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 13 · `CI_PENDING` 0 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 44 · `PROTECTION_UNREADABLE` 49.

**All 49 open PRs are non-READY** under the ambient registry. Controlling blocker (first in the ordered list) per PR:

| PR | Tier | Controlling blocker | CI state | Title |
|---|---|---|---|---|
| #58 | DRAFT | DRAFT | FAILURE | Security Boundary hardening — S1/S2/S3/S4/S5 (guard STOP, force- |
| #92 | DRAFT | DRAFT | SUCCESS | fix(storage): honor SQLite defaults; preserve inherited release |
| #114 | DRAFT | DRAFT | FAILURE | feat(cockpit): Persian capability explorer and input-only comman |
| #117 | DRAFT | DRAFT | SUCCESS | fix(runtime): close audited session/media contracts and wheel re |
| #118 | DRAFT | DRAFT | SUCCESS | W2 golden hardening: authority races, probe permits and independ |
| #150 | DRAFT | DRAFT | FAILURE | feat(agent-intelligence): verified CreativeWork trim path |
| #204 | DRAFT | DRAFT | SUCCESS | fix(memory): resolve sqlite-vec extension fallback and add foren |
| #257 | DRAFT | DRAFT | SUCCESS | 🎨 Palette: Improve Telegram inline callback UX for language and |
| #273 | DRAFT | DRAFT | SUCCESS | feat(nagar): P0 intent NEL semantic time causal contracts |
| #284 | DRAFT | DRAFT | FAILURE | feat(nagar): add read-only Causal Memory and bounded Director pr |
| #285 | DRAFT | DRAFT | SUCCESS | chore(board): synchronize task-278 deferred status |
| #328 | DRAFT | DRAFT | SUCCESS | feat(studio): NAGAR Studio PWA over the verified read API (Draft |
| #329 | DRAFT | DRAFT | SUCCESS | chore(board): register NAGAR-IBPC-TRIAD-CLOSURE-01 zones (draft |
| #33 | CONFLICT | CONFLICT | SUCCESS | v3.13.0 — deliver the P0 security code, wire the dead engines, f |
| #57 | CONFLICT | CONFLICT | FAILURE | feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-160) |
| #60 | CONFLICT | CONFLICT | FAILURE | M0 runtime integration: InstrumentedJobQueue + readyz factory + |
| #63 | CONFLICT | CONFLICT | FAILURE | task-163: board-Git truth gate + scale-to-zero state tiers (#1 + |
| #66 | CONFLICT | CONFLICT | SUCCESS | P0 creative integration: one production path, closed legacy lane |
| #69 | CONFLICT | CONFLICT | SUCCESS | Gate 4: cross-layer vertical slice verification (T01·T02·T22·T31 |
| #73 | CONFLICT | CONFLICT | SUCCESS | Gate 2.2 — Operation Truth → Executable Evidence Gate (anti-drif |
| #84 | CONFLICT | CONFLICT | SUCCESS | Register the NEXUS Engineering Constitution as an enforced contr |
| #89 | CONFLICT | CONFLICT | SUCCESS | task-122: 12 real command wirings + moderation ingress — forensi |
| #99 | CONFLICT | CONFLICT | SUCCESS | task-193: command truth — 11 fake/silent commands wired to the e |
| #113 | CONFLICT | CONFLICT | SUCCESS | feat(runtime): W1 final close-out — engine-owned schema fast-pat |
| #115 | CONFLICT | CONFLICT | SUCCESS | W1: True Runtime Closure / Lifecycle Ownership / Zero Semantic B |
| #121 | CONFLICT | CONFLICT | SUCCESS | feat(memory): retrieval truth — noise-ranked recall (hit@3 0.167 |
| #122 | CONFLICT | CONFLICT | SUCCESS | fix(orchestration): H1 — route chat through the memory reader (t |
| #129 | CONFLICT | CONFLICT | SUCCESS | feat(creative/studio): staged plan transactions and atomic commi |
| #135 | CONFLICT | CONFLICT | FAILURE | feat(creative/studio): canonical PlanTransaction substrate and d |
| #137 | CONFLICT | CONFLICT | SUCCESS | board(task-227,task-228): collision preflight + executable gover |
| #140 | CONFLICT | CONFLICT | SUCCESS | Harden Studio CommandBus: PlanTransaction, Logical Identity, and |
| #149 | CONFLICT | CONFLICT | SUCCESS | Security Wave 1 Verification & Governance Alignment |
| #157 | CONFLICT | CONFLICT | SUCCESS | feat(governance): Nagar agent constitution — persistent, version |
| #193 | CONFLICT | CONFLICT | SUCCESS | feat(infra): canonical Ubuntu 26.04 + Python 3.14 foundation |
| #205 | CONFLICT | CONFLICT | FAILURE | feat(nagar): Asset Library V1 — secure content-addressed ingest |
| #230 | CONFLICT | CONFLICT | SUCCESS | Arena/aa451c49 nexus ai agent |
| #247 | CONFLICT | CONFLICT | SUCCESS | feat(creative): perception lane — measured silence/scene/loudnes |
| #294 | CONFLICT | CONFLICT | SUCCESS | repair(#189): land the durable creative-graph package (law R28) |
| #297 | CONFLICT | CONFLICT | SUCCESS | evidence(m16): true closeout of the M15 spine + open-PR converge |
| #313 | CONFLICT | CONFLICT | SUCCESS | chore(board): register task-nagar-preservation-media-profiles-01 |
| #326 | CONFLICT | CONFLICT | SUCCESS | feat(character-capsule): versioned Character Capsule + continuit |
| #327 | CONFLICT | CONFLICT | SUCCESS | feat(composition): integrate the NAGAR Creative Capability Compo |
| #64 | CI_RED | CI_RED | FAILURE | Creative Runtime (task-176 s2): execution semantics, multi-segme |
| #97 | CI_RED | CI_RED | FAILURE | feat(vision): complete portrait and scene Vision packs |
| #295 | CI_RED | CI_RED | FAILURE | extract(#218): land the self-contained content-addressed Asset L |
| #325 | CI_RED | CI_RED | FAILURE | fix(task-282): land the F1 artifact-binding ownership fence on m |
| #301 | REBASE_FIRST | REBASE_FIRST | SUCCESS | chore(automation): engineering-debt watchdog 2026-11-10 |
| #330 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | feat(nagar/ibpc): NAGAR IBPC triad - Delta Dialogue, Shadow Prev |
| #331 | PROTECTION_UNREADABLE | PROTECTION_UNREADABLE | SUCCESS | ci(truth): add a local pre-push truth gate and a parity test tha |

### 1.1 Secondary view — registry with the PAT (protection **readable**, 3 canonical contexts)

```
$ GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN" python scripts/pr_convergence.py collect --out /tmp/reg_pat.json
pr-registry: 49 open PRs → /tmp/reg_pat.json (main 5fe9451228ad)
$ python scripts/pr_convergence.py report --in /tmp/reg_pat.json
```

- `protection_state`: `readable`, `required_contexts` = the **3 canonical** contexts (`lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`), `required_approvals`: `0`, `strict: true`.
- Tiers: `DRAFT` 13 · `CONFLICT` 29 · `CI_RED` 3 · `REBASE_FIRST` 2 · `PROTECTION_UNREADABLE` **0** · **`READY` 2** (#330, #331).
- Blocker counts: `CONFLICT` 36 · `CI_RED` 6 · **`CI_PENDING` 20** · `CHANGES_REQUESTED` 11 · `REBASE_FIRST` 44.

**The 20 `CI_PENDING` under the readable path are the D1 bug, not reality** (see §5, D1): the strict path calls `_latest_ci_verdict(..., require_all_present=True)`, which returns `PENDING` when a required context is **absent** *before* it evaluates the present contexts' verdicts. Reproduce at function level:

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
| #57 | `2f591618e13b` | failure | success | absent | PENDING | **FAILURE** |
| #58 | `bb06da1cb304` | failure | failure | absent | PENDING | **FAILURE** |
| #60 | `efea9bae3ebd` | failure | failure | absent | PENDING | **FAILURE** |
| #63 | `3e376bea067f` | success | failure | absent | PENDING | **FAILURE** |
| #135 | `f83b12835cd9` | success | failure | absent | PENDING | **FAILURE** |
| #150 | `5f84be4dd22f` | failure | failure | absent | PENDING | **FAILURE** |

**PRs whose canonical required checks** (`lint`, `test`, `merge-base-guard`) are **all `success`**: **22 of 49** — #33, #92, #99, #115, #122, #204, #230, #247, #257, #273, #285, #294, #295, #297, #301, #313, #326, #327, #328, #329, #330, #331. (A *CI-green* set, not a mergeable set: most also carry `CONFLICT`/`DRAFT`/`CHANGES_REQUESTED`.) Up from 20 on 2026-11-10; the four new heads (#328, #329, #330, #331) are all green.

---

## 2. Board audit

```
$ PYTHONPATH=src python -m pytest -q tests/unit/test_agent_board.py
134 passed in 2.94s
EXIT=0
```

The authoritative board is the one on **`main`**; the working-tree copy is byte-identical this run (`sha256 eda3f6ad…`, `git show origin/main:.agents/board.json | sha256sum` → same). No board-state mutation is performed by the audit.

- **Claims:** 190 (was 189). **Zones:** 89 (unchanged). Status histogram: `done` 80, `queued` 61, `expired` 20, `completed_merged` 9, `completed_released` 6, `deferred` 3, `available_sequenced_post_33` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1, **`active` 2**.
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).
- **Claims carrying an `agent_branch`:** 23.
- **`protocol.protocol_version`:** present and `== 2` (D5 of the 2026-10-31 report stays resolved).
- **Board delta vs 2026-11-10:** +1 claim (189 → 190), +1 `active` (1 → 2), `updated_at` advanced to `2026-11-08T08:42:47Z`.

### 2.1 Active claims — 2 (both **stale** live leases, both for **already-merged** work)

`python3 -c "…sum(1 for c in claims if c['status']=='active')"` → **2**:

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-282-slice-2-artifact-assetversion-binding` | `durable-project-state-lifecycle` | `nagar-one-slice2-artifact-revision-binding` | yes (`c5b7da56…`) | no (PR #325 head is `fix/binding-ownership-fence-20261107`) | 24 | 2026-11-06T21:25:39Z | **STALE** — expired 2026-11-07T21:25:39Z, **~77 h past TTL** |
| `task-324-closeout-docs-repair` | `docs-architecture` | `openhands/nagar-one-final-closeout-20261107` | yes (`366033ec…`) | no (PR #324 head, **merged**) | 24 | 2026-11-08T08:42:47Z | **STALE** — expired 2026-11-09T08:42:47Z, **~42 h past TTL** |

- Both leases are **stale** (24 h TTL lapsed; run at 2026-11-11T02:42:59Z). `task-282` still reserves zone `durable-project-state-lifecycle` (paths include `src/nexus_ai_agent/cli.py` and `src/nexus_ai_agent/creative/project_state/`) for work PR **#325** is the successor vehicle for; `task-324` reserves `docs-architecture` (paths include `.agents/board.json`) for work PR **#324** has **already merged into `main`** (`merged_at 2026-11-10T09:06:20Z`, merge commit `5fe9451228ad` = current `main`). This is **landed-but-still-leased** debt.
- **Overlap check:** no cross-zone collision — every `exclusive_paths` entry falls inside its declared zone (§2.5), and there is no third live lease, so no overlap with another agent's live lease. **Note:** `task-324` claims `.agents/board.json`, which this watchdog does **not** touch (it writes only under `.agents/evidence/`), so this run creates no overlap with that live lease.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30 … 2026-11-10 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (255 refs) and the 49 open-PR heads.

### 2.3 Stale live leases — 2 (was 1)

The two `active` claims (§2.1) are both past their 24 h TTL (~77 h and ~42 h). `task-282` was already stale on 2026-11-10; `task-324` is **NEW** this run — its claim (`claimed_at 2026-11-08T08:42:47Z`) has aged past TTL and its work has **merged** (PR #324).

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

Seven claims carry an owner/sequence label with no machine-checkable branch or lease clock. Unchanged from 2026-11-10.

### 2.5 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations, with prefix logic — a zone path like `tests/unit/test_ibpc_` is a prefix that covers `tests/unit/test_ibpc_intent_compiler.py`). No cross-zone overlap. Every claim's `zone` field is a declared zone id (0 orphans). Reproduce: for each claim, assert every `exclusive_paths` entry equals a `zone.paths` entry, is a child of one, or shares a `zone.paths` prefix.

### 2.6 Open PRs with no board claim (informational) — 48 of 49

Only **1** of the 49 open PRs (#33) has a head branch that matches a board claim's `agent_branch`; the other 48 carry no `agent_branch` claim (29 are mentioned somewhere in board *text* as historical references; **19 are entirely absent from the board**: #64, #92, #204, #230, #257, #284, #285, #294, #295, #297, #301, #313, #325, #326, #327, #328, #329, #330, #331 — see the reproduce command below). This is the same long-standing gap as earlier runs; the **new** heads #328 (`nagar-studio-app-20261110`), #329 (`arena/nagar-triad-closure-governance`), #330 (`arena/nagar-delta-dialogue-vs1`) and #331 (`fix/local-truth-gate-rail-20261111`) all fall in the unclaimed set, so the four newest work-streams entered the repo with no lease.

```
$ python3 -c "
import json
b=json.load(open('.agents/board.json')); reg=json.load(open('/tmp/reg_pat.json'))
cb={c.get('agent_branch') for c in b['claims'] if c.get('agent_branch')}
blob=json.dumps(b)
print([p['number'] for p in reg['prs'] if p['head'] not in cb and p['head'] not in blob])
"
```

---

## 3. Gates

| Gate | Command | Result | Exit |
|---|---|---|---|
| **G1** truth gate | `PYTHONPATH=src python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| **G2** matrix + docs | `PYTHONPATH=src pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `121 passed in 0.89s` | 0 |
| **G3** board contract | `PYTHONPATH=src pytest -q tests/unit/test_agent_board.py` | `134 passed in 2.94s` | 0 |

Environment: Python `3.13.15` (`/usr/local/bin/python`), **`PYTHONPATH=src` source checkout** (not an editable install). The sandbox had no `.venv` and none of the project's dependencies; the watchdog installed the closure needed to *run the gates only* (`pytest`, `pytest-asyncio`, `pydantic`, `pydantic-settings`, `sqlmodel`, `sqlalchemy`, `alembic`, `aiosqlite`, `structlog`, `typer`, `httpx`, `python-multipart`, `numpy`, `pillow`, `beautifulsoup4`, `psutil`, `arabic-reshaper`, `python-bidi`, `duckduckgo-search`, plus `greenlet` — without `greenlet`, `test_agent_board.py` errors at import with `ImportError: The SQLAlchemy asyncio module requires that the Python 'greenlet' library is installed`). All three gates **GREEN** — no gate regressed vs the 2026-11-10 run.

---

## 4. CI health — 12 most recently updated open PRs

Canonical required contexts (from the live protection API read with the PAT, and `AGENTS.md` §1): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status per context on the **exact head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, latest run wins), distinguishing `queued`/`in_progress`/`cancelled`/`failure`/`skipped`/`success`/`absent`:

| PR | Head SHA | Updated (UTC) | `lint` | `test` | `merge-base-guard` |
|---|---|---|---|---|---|
| #331 | `4284876b00c8` | 2026-11-10T23:53:13Z | success | success | success |
| #330 | `6c3677040abb` | 2026-11-10T22:01:32Z | success | success | success |
| #325 | `af00b0ad6565` | 2026-11-10T17:14:17Z | success | **failure** | success |
| #329 | `56f340941694` | 2026-11-10T15:42:12Z | success | success | success |
| #328 | `4f6ca1d1f896` | 2026-11-10T11:05:28Z | success | success | success |
| #326 | `c38a811f6f66` | 2026-11-10T08:41:48Z | success | success | success |
| #327 | `d5d5ead38db2` | 2026-11-10T08:24:36Z | success | success | success |
| #301 | `64bf39dca25e` | 2026-11-10T02:45:40Z | success | success | success |
| #294 | `9b60307a14d0` | 2026-11-08T23:32:36Z | success | success | success |
| #313 | `ca7f9ea88689` | 2026-11-04T20:50:07Z | success | success | success |
| #193 | `d505d80a4e02` | 2026-11-04T11:52:33Z | success | success | **skipped** |
| #204 | `13078d792619` | 2026-11-01T20:28:44Z | success | success | success |

No `queued`/`in_progress` at snapshot time. Superseded `cancelled` duplicates exist (a re-run cancels the previous attempt) but are never the latest run. #295 and #326/#327 rolled out of the top-12 window by update time; #328, #329, #330, #331 rolled in.

**Head advance:** #325 moved `1d8783759866` → `af00b0ad6565`; its `test` (and `python-parity (3.10/3.11/3.12)`) turned **failure** at the new head — a real CI regression (D6). #301 is unchanged at `64bf39dca25e`.

### 4.1 Repository-wide required-context histograms (all 49 PRs)

| Check | Latest-run histogram on exact head |
|---|---|
| `lint (ruff + mypy + version lockstep)` | `{success: 45, failure: 4}` (absent: 0) |
| `test (pytest -m "not slow")` | `{success: 38, failure: 11}` (absent: 0) |
| `merge-base-guard (base == main)` | `{success: 27, skipped: 2, absent: 20}` |

`lint` failures: #57, #58, #60, #150. `test` failures: #58, #60, #63, #64, #97, #114, #135, #150, #205, #284, #325.

`merge-base-guard` is **absent on 20 legacy heads** (unchanged): #57, #58, #60, #63, #66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #135, #137, #140, #149, #150, #157. The **2 `skipped`** runs are #193 and #205 — a *skipped* required check is not a pass.

### 4.2 Non-required `CircleCI Pipeline` `ERROR` (D7 — **unchanged**, now 9 of 12)

The combined commit status on the 12 recent heads (`.../commits/<sha>/status`) is `failure` for **9 of 12** — #331, #330, #325, #329, #328, #326, #327, #301, #313, #204 each solely from the non-required `CircleCI Pipeline` context reading `error` (`"No configuration was found in your project"`). #193 is `success` (from `CodeRabbit`, not CircleCI); #294 is `pending` (no statuses). The repository still has **no `.circleci/config.yml`** (`ls -d .circleci` → *No such file or directory*), while the board carries a `queued` claim `task-nagar-circleci-pilot-01` whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. The status originates from an external CircleCI project not reproducible from the tree. This window was 8 of 12 on 2026-11-10 and 6 of 12 on 2026-11-09; it is the sole non-required reason #330 and #331 are `UNSTABLE` rather than clean.

---

## 5. Findings (engineering debt)

### D1 — `_latest_ci_verdict` still masks a real `FAILURE` behind an absent required context (P0) — **STILL OPEN (active, ~11th run)**

The ordering bug is unchanged (`scripts/pr_convergence.py`, lines 222–224): with `require_all_present=True`, a required context that never ran returns `PENDING` **before** the red check runs on the present contexts. Reproduced at function level in §1.1 (`PENDING` where `FAILURE` is correct). The PAT-based registry reports `CI_PENDING` 20 and only 3 `CI_RED`, whereas the truth is that **6 of those 20 heads carry a real `lint`/`test` `failure`** (#57, #58, #60, #63, #135, #150). Reproduce the 6: for each open PR, require `merge-base-guard == absent` **and** (`lint == failure` or `test == failure`).

### D2 — `merge-base-guard` absent on 20 heads (P0) — **STILL OPEN (unchanged)**

20 legacy heads have no `merge-base-guard` run; the registry cannot distinguish "never ran" from "still running", and a *skipped* guard (#193, #205) is likewise not surfaced as its own signal. 20 → 20.

### D3 — board hygiene: 13 dead-branch claims + 7 owner-labelled rows with no clock (P1) — **STILL OPEN (unchanged)**

The 13 dead-branch claims (§2.2) and the 7 owner-labelled rows (§2.4) persist byte-identically. No new ones this run.

### D5 — `protocol.protocol_version` (P1) — **RESOLVED (stays resolved)**

`protocol.protocol_version == 2` is present in the board and enforced by `tests/unit/test_agent_board.py` (134 passing).

### D6 — NEW CI regression on PR #325 (P0) — **NEW**

PR #325 (`fix(task-282): land the F1 artifact-binding ownership fence on main`) advanced its head `1d8783759866` → `af00b0ad6565` and, at the new head, `test (pytest -m "not slow")` and `python-parity (3.10/3.11/3.12)` all report **failure** (verified on the exact head SHA; §4). On 2026-11-10 #325 was one of the four clean, `READY` PRs (all 3 canonical checks green). It is now `CI_RED` and no longer `READY` — the single most acute regression this run. Reproduce: `gh api repos/bot523h/nexus-ai-agent/commits/af00b0ad65654b4fcc06af14aba87c6f2aad135e/check-runs?per_page=100`.

### D7 — non-required `CircleCI Pipeline` `ERROR` on 9 of 12 recent heads (P1) — **STILL OPEN**

9 of the 12 recent heads carry a combined status of `failure` from the non-required CircleCI context alone (§4.2). It is the sole non-required reason #330 and #331 (both otherwise clean, `READY` under the PAT registry) are `UNSTABLE` rather than clean. It is not reproducible from the tree (no `.circleci/config.yml`). Unchanged in character from 2026-11-10; the window is one head larger.

### D8 — branch protection readability (P0) — **IMPROVED (unchanged half)**

As on 2026-11-09/10, the repository PAT (`GITHUB_PERSONAL_ACCESS_TOKEN`) reads protection (`protection_state: readable`, 3 canonical contexts, `strict: true`, `enforce_admins: true`), while the ambient `GITHUB_TOKEN` still returns `403 Resource not accessible by integration` (`protection_state: unreadable`, 15 workflow-derived contexts). Consequence: the **default** `collect` (ambient) still certifies **no PR READY** and tiers the two clean PRs (#330, #331) `PROTECTION_UNREADABLE`; a `GH_TOKEN=$PAT` collect certifies **2 READY** and exposes D1 as active. Reproduce: `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` (403 ambient) vs `GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN" gh api …` (200, 3 contexts).

### N7 — the `main` board now carries TWO stale `active` leases for already-shipped work (P1, **WORSENED**)

See §2.1. On 2026-11-10 there was one landed-but-leased `active` claim; this run there are **two**: `task-282-slice-2-artifact-assetversion-binding` (expired 2026-11-07T21:25:39Z, ~77 h past TTL) and the **newly stale** `task-324-closeout-docs-repair` (expired 2026-11-09T08:42:47Z, ~42 h past TTL; its work merged into `main` via PR #324 on 2026-11-10). Both hold zones (`durable-project-state-lifecycle`, `docs-architecture`) for work already on `main`. Owner action: `release` (or `gc`) both.

### N8 — the automation branch's board is still stale (P2, unchanged)

`automation/debt-watchdog` (at `64bf39dca25e`) still carries the 2026-10-31 board (`sha256 592576dd…`) while `main` is at `sha256 eda3f6ad…`, 89 zones / 190 claims, `updated_at 2026-11-08T08:42:47Z`. The watchdog does not modify `board.json` (read-only outside `.agents/evidence/`), so the branch staleness persists until a human syncs it.

### N9 — four new PRs entered with no board claim (P2, NEW)

#328 (`nagar-studio-app-20261110`, DRAFT), #329 (`arena/nagar-triad-closure-governance`, DRAFT), #330 (`arena/nagar-delta-dialogue-vs1`, `READY` under the PAT registry) and #331 (`fix/local-truth-gate-rail-20261111`, `READY` under the PAT registry) are all new since 2026-11-10, all CI-green, and none appears in `.agents/board.json` — not as a claim's `agent_branch`, and not as text. Under the `AGENTS.md` §1 rule ("claim before you code"), these four work-streams bypassed the board. No collision is implied (0 `exclusive_paths` violations, §2.5), but the board no longer reflects the live work set for the newest four PRs.

---

## 6. Delta vs previous run (2026-11-10, `main` `5ed6d4e10be5`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-10.md` (branch-only).

| Metric | 2026-11-10 | 2026-11-11 | Change |
|---|---|---|---|
| `main` SHA | `5ed6d4e10be5` | `5fe9451228ad` | **advanced** (PR #324 merged) |
| Open PRs | 46 | 49 | **+3** |
| Open-PR number set | 46 ids | +4 new (#328, #329, #330, #331), −1 merged (#324) | — |
| DRAFT tier | 11 | 13 | **+2** |
| CONFLICT tier | 27 | 29 | **+2** |
| CI_RED tier (ambient / PAT) | 3 / 2 | 4 / 3 | **+1 / +1** |
| REBASE_FIRST tier (ambient / PAT) | 1 / 2 | 1 / 2 | 0 / 0 |
| PROTECTION_UNREADABLE tier (ambient) | 4 | 2 | −2 |
| READY (ambient / PAT) | 0 / 4 | 0 / 2 | 0 / **−2** |
| Remote branches | 251 | 255 | +4 |
| Board zones / claims (`main`) | 89 / 189 | 89 / 190 | 0 / **+1** |
| Board sha (`main`) | `f27bcb44…` | `eda3f6ad…` | **advanced** |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Owner-labelled rows (no branch/clock) | 7 | 7 | 0 |
| Active (leased) claims | 1 | 2 | **+1** |
| Stale live leases | 1 | 2 | **+1** |
| `gates_owner` holders | 0 | 0 | 0 |
| Branch protection | readable via PAT, 403 via ambient | readable via PAT, 403 via ambient | 0 |
| Gates G1/G2/G3 | green | green | 0 |
| CircleCI `error` on 12-recent heads | 8 | 9 | **REGRESSED** |
| PRs with all 3 canonical checks green | 20 | 22 | **+2** |

**New PRs since last run:** #328 (`DRAFT`, `feat(studio): NAGAR Studio PWA …`, head `4f6ca1d1f896`), #329 (`DRAFT`, `chore(board): register NAGAR-IBPC-TRIAD-CLOSURE-01 zones …`, head `56f340941694`), #330 (`READY`-under-PAT, `feat(nagar/ibpc): NAGAR IBPC triad …`, head `6c3677040abb`), #331 (`READY`-under-PAT, `ci(truth): add a local pre-push truth gate …`, head `4284876b00c8`).
**Gone since last run:** #324 **merged** into `main` (merge commit `5fe9451228ad`) — it was one of the four `READY` PRs on 2026-11-10.
**Heads advanced among common PRs:** #325 `1d8783759866 → af00b0ad6565`.

**Regressions:**
- **D6 (NEW):** PR #325's `test` + `python-parity` turned **failure** at its new head — a real CI regression; the `READY` set shrank 4 → 2 as a direct result (plus #324 merging away).
- **D7 regressed 8 → 9** of 12 recent heads carrying the non-required CircleCI `error` context.
- **N7 worsened:** a second stale landed-but-leased `active` claim (`task-324`) joined the board.
- **N9 (NEW):** four new PRs (#328, #329, #330, #331) entered with no board claim.
- No gate failure (G1/G2/G3 green both runs). No new ownership mismatch (still 13, byte-identical). No new conflict class.

**Resolved / improved since last run:**
- **PR #324 landed** (the watchdog's prior action 4, at least in part) — the `READY` backlog is being consumed, not only grown.
- **The set of PRs with all 3 canonical checks green grew 20 → 22.**
- **D8** stays partially recovered (PAT readable).
- D5 (`protocol_version`) stays resolved.
- `exclusive_paths` violations remain 0; every claim's zone is declared.

**Unchanged / still open:** D1 (active — 6 masked heads), D2 (`merge-base-guard` absent on 20 heads; 2 skipped), D3 (board hygiene: 13 dead-branch + 7 owner-labelled), D7 (CircleCI `ERROR`, now 9 of 12), D8 (ambient token still 403), N8 (stale board on the automation branch).

---

## 7. Next actions (prioritized)

1. **[P0] Investigate and fix the #325 CI regression (D6).** #325 was green on 2026-11-10 and is now `test` + `python-parity` red at head `af00b0ad6565`; it is the F1 artifact-binding fence the `task-282` lease (N7) is still holding. `gh api repos/bot523h/nexus-ai-agent/commits/af00b0ad6565/check-runs?per_page=100` reproduces it.
2. **[P0] `release`/`gc` the two stale `active` leases (N7):** `task-282-slice-2-artifact-assetversion-binding` (expired 2026-11-07, ~77 h ago) and `task-324-closeout-docs-repair` (expired 2026-11-09, ~42 h ago; its work already merged as PR #324). Both reserve zones for shipped work.
3. **[P0] Fix `_latest_ci_verdict` ordering (D1) — active for ~11 runs.** Evaluate the `_RED_CONCLUSIONS` verdict over the **present** contexts *before* returning `PENDING` for a missing one. Today 6 heads (#57, #58, #60, #63, #135, #150) are mis-tiered `CI_PENDING` instead of `CI_RED` whenever protection is readable.
4. **[P0] Restore the ambient `GITHUB_TOKEN`'s protection read (D8, remaining half).** The PAT works; the automation's ambient token still returns `403`. Without it the *default* `collect` degrades to 15 contexts and certifies no PR `READY` (masking #330/#331 as `PROTECTION_UNREADABLE`). Grant the automation App/installation `administration: read` (or `branches`), then verify `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` returns `200` with the 3 canonical contexts.
5. **[P0] Review and land the two clean PRs (#330, #331).** Each clears `lint`/`test`/`merge-base-guard`; both are `MERGEABLE` and `UNSTABLE` only on the non-required CircleCI context. They are the nearest green work on `main`.
6. **[P0] Distinguish "required check never ran" from "still running" (D2):** give the 20 legacy heads missing `merge-base-guard` their own signal, and treat the 2 `skipped` guards (#193, #205) as non-green.
7. **[P1] Resolve the `CircleCI Pipeline` `ERROR` (D7) — now 9 of 12:** commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — it currently marks 9 of 12 recent heads' combined status `failure`.
8. **[P1] Rebase or close #313** — it remains `CONFLICT`/`DIRTY`; rebase is cheap.
9. **[P2] Register the four unclaimed new work-streams (#328, #329, #330, #331) on the board (N9)**, or record them as intentionally out-of-band, so `check`/`next` see the live work set.
10. **[P2] Sweep board hygiene (D3):** remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
11. **[P2] Re-sync the automation branch's board (N8)** so `check`/`next` do not read a 2026-10-31 board on `automation/debt-watchdog`.
12. **[P2] Attack the rebase wall:** 44 of 49 PRs carry `REBASE_FIRST`; triage the 29 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.
