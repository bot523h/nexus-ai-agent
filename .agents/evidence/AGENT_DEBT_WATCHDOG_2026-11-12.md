# Engineering-Debt Watchdog — 2026-11-12

- **Run timestamp (UTC):** `2026-11-12T02:47Z` (ambient registry `generated_at` `2026-11-12T02:39:33Z`; PAT registry `generated_at` `2026-11-12T02:45:29Z`; report written immediately after)
- **Repo:** `bot523h/nexus-ai-agent`
- **`main` SHA:** `c3aa7c85e4b64400cb8d51b0880ef5e6cafd4e1b` (`c3aa7c85e4b6`, "Merge pull request #332 from bot523h/fix/pr325-py310-pg-cleanup-20261111") — **advanced** from the 2026-11-11 run (`5fe9451228ad`).
- **Observer:** NEXUS engineering-debt watchdog automation — **observe-and-report only**; no product-code edits, no merge, no push to `main`. The only file created by this run is this one, under `.agents/evidence/`.
- **Previous watchdog report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-11.md` (branch `automation/debt-watchdog`, `main` `5fe9451228ad`) — the baseline for the deltas below.

Every number in this report is reproducible from the named command in the same section. Nothing here is estimated or invented. Where a source was unreadable, that is stated explicitly.

> **Credential note (unchanged) — D8 still split.** Branch protection is readable **with the repository PAT** (`GITHUB_PERSONAL_ACCESS_TOKEN` → `200`, 3 canonical contexts, `strict: true`, `enforce_admins: true`) but still `403 Resource not accessible by integration` with the ambient `GITHUB_TOKEN`. The default `collect` (ambient) therefore degrades to 15 workflow-derived contexts and certifies **no PR as READY**; a second `collect` with `GH_TOKEN=$GITHUB_PERSONAL_ACCESS_TOKEN` reads the real gate and certifies **1 PR READY** (#335). Both views are reported below.
>
> **Two earlier `collect` attempts exited `2`** on a transient `gh: HTTP 504` / `unexpected end of JSON input` (GraphQL), then succeeded — the same transient GitHub flakiness the 2026-11-11 run recorded. This is not a source-readability failure of the tool.

---

## 0. Sources read

| Source | Result |
|---|---|
| `git fetch origin --prune` | clean; local `main` == `origin/main` @ `c3aa7c85e4b6` |
| `AGENTS.md`, `docs/MULTI_AGENT_PROTOCOL.md` | read |
| `scripts/agent_board.py`, `scripts/pr_convergence.py` | read |
| `.agents/board.json` (working tree == `main`; sha256 `ed37d685188e79de0bf5fe4647a2c0851b599ece8c74d8f71586b3fb824a2c49`) | schema 2; **89 zones, 190 claims**, 14 `deferred_log`, 19 `takeover_log`, 10 `next_work`, 4 `history`, 5 `completion_records`; `protocol.protocol_version = 2`; `protocol.lease_ttl_hours = 24`; board `updated_at = 2026-11-10T12:08:41Z` |
| Remote branches | **259** (`git ls-remote --heads origin \| wc -l`; **201** are `refs/heads/arena/*`) |
| Open PRs | **51** (`python scripts/pr_convergence.py collect --out /tmp/reg.json`) |
| Branch protection | **unreadable** with the ambient `GITHUB_TOKEN` (403); **readable** with the repository PAT (3 canonical contexts) |

---

## 1. Live PR registry

```
$ GH_TOKEN="$GITHUB_TOKEN" python scripts/pr_convergence.py collect --out /tmp/reg.json
pr-registry: branch protection UNREADABLE (… 403 Resource not accessible by integration);
             degrading to workflow-derived contexts […]; No PR will be certified READY.
pr-registry: 51 open PRs → /tmp/reg.json (main c3aa7c85e4b6)
COLLECT EXIT=0   (two earlier attempts exited 2 on a transient GraphQL 504 / empty body — retried, then OK)

$ python scripts/pr_convergence.py report --in /tmp/reg.json
REPORT EXIT=0
```

- `schema`: `nexus-pr-registry/v1`, `generated_at`: `2026-11-12T02:39:33Z`, `main_sha`: `c3aa7c85e4b64400cb8d51b0880ef5e6cafd4e1b`
- `open_total`: `51`, `protection_state`: `unreadable` (ambient), `protection_source`: `ci.yml workflow (branch-protection API unreadable)`
- `required_contexts` (15, workflow-derived because the ambient token cannot read protection): `["lint (ruff + mypy + version lockstep)", "test (pytest -m \"not slow\")", "extras-matrix (${{ matrix.leg }})", "python-parity (${{ matrix.python-version }})", "continuum-evidence (${{ matrix.python-version }})", "trust-mutations (pack trust plane + IBPC security closure)", "b2-blob-tier-mutations (maintenance blob tier)", "temporal-mutations (temporal truth algebra)", "governance-mutations (board referee 42 + Gate-5 fencing 17)", "execution-core-mutations (V1 execution core 25)", "remote-key-mutations (ingress + cache containment)", "api-gate-mutations (A-06 security surface)", "merge-base-guard (base == main)", "lint-fast (lockstep + pinned ruff, no install)", "dependency-audit (pip-audit vs reviewed allow-list)"]`

**Tier counts** (`tier` = hardest blocker; a PR may carry several). Reproduce: `python scripts/pr_convergence.py report --in /tmp/reg.json`.

| Tier | Count | Δ vs 2026-11-11 |
|---|---|---|
| WRONG_BASE_BRANCH | 0 | 0 |
| DRAFT | 13 | 0 |
| CONFLICT | 31 | **+2** |
| MERGEABILITY_UNKNOWN | 0 | 0 |
| CI_RED | 4 | 0 |
| CI_PENDING | 0 | 0 |
| CHANGES_REQUESTED | 0 | 0 |
| NEEDS_APPROVAL | 0 | 0 |
| REBASE_FIRST | 2 | **+1** |
| PROTECTION_UNREADABLE | 1 | **−1** |
| **READY** | **0** | 0 |

**Blocker occurrence counts** (independent tallies; a PR appears in several): `WRONG_BASE_BRANCH` 0 · `DRAFT` 13 · `CONFLICT` 40 · `MERGEABILITY_UNKNOWN` 0 · `CI_RED` 13 · `CI_PENDING` 0 · `CHANGES_REQUESTED` 11 · `NEEDS_APPROVAL` 0 · `REBASE_FIRST` 49 · `PROTECTION_UNREADABLE` 51.

**All 51 open PRs are non-READY** under the ambient registry. Controlling blocker (first in the ordered list) per PR:

| PR | Tier | CI state (registry) | Title |
|---|---|---|---|
| #33 | `CONFLICT` | `SUCCESS` | v3.13.0 — deliver the P0 security code, wire the dead engines, |
| #57 | `CONFLICT` | `FAILURE` | feat(research-v2): P1 CLAIM+LEASE + P2 INBOX/RECEIPT (task-160 |
| #58 | `DRAFT` | `FAILURE` | Security Boundary hardening — S1/S2/S3/S4/S5 (guard STOP, forc |
| #60 | `CONFLICT` | `FAILURE` | M0 runtime integration: InstrumentedJobQueue + readyz factory  |
| #63 | `CONFLICT` | `FAILURE` | task-163: board-Git truth gate + scale-to-zero state tiers (#1 |
| #64 | `CI_RED` | `FAILURE` | Creative Runtime (task-176 s2): execution semantics, multi-segm |
| #66 | `CONFLICT` | `SUCCESS` | P0 creative integration: one production path, closed legacy la |
| #69 | `CONFLICT` | `SUCCESS` | Gate 4: cross-layer vertical slice verification (T01·T02·T22· |
| #73 | `CONFLICT` | `SUCCESS` | Gate 2.2 — Operation Truth → Executable Evidence Gate (anti-dr |
| #84 | `CONFLICT` | `SUCCESS` | Register the NEXUS Engineering Constitution as an enforced con |
| #89 | `CONFLICT` | `SUCCESS` | task-122: 12 real command wirings + moderation ingress — foren |
| #92 | `DRAFT` | `SUCCESS` | fix(storage): honor SQLite defaults; preserve inherited releas |
| #97 | `CI_RED` | `FAILURE` | feat(vision): complete portrait and scene Vision packs |
| #99 | `CONFLICT` | `SUCCESS` | task-193: command truth — 11 fake/silent commands wired to the |
| #113 | `CONFLICT` | `SUCCESS` | feat(runtime): W1 final close-out — engine-owned schema fast-pa |
| #114 | `DRAFT` | `FAILURE` | feat(cockpit): Persian capability explorer and input-only comma |
| #115 | `CONFLICT` | `SUCCESS` | W1: True Runtime Closure / Lifecycle Ownership / Zero Semantic |
| #117 | `DRAFT` | `SUCCESS` | fix(runtime): close audited session/media contracts and wheel r |
| #118 | `DRAFT` | `SUCCESS` | W2 golden hardening: authority races, probe permits and indepen |
| #121 | `CONFLICT` | `SUCCESS` | feat(memory): retrieval truth — noise-ranked recall (hit@3 0.16 |
| #122 | `CONFLICT` | `SUCCESS` | fix(orchestration): H1 — route chat through the memory reader ( |
| #129 | `CONFLICT` | `SUCCESS` | feat(creative/studio): staged plan transactions and atomic comm |
| #135 | `CONFLICT` | `FAILURE` | feat(creative/studio): canonical PlanTransaction substrate and  |
| #137 | `CONFLICT` | `SUCCESS` | board(task-227,task-228): collision preflight + executable gove |
| #140 | `CONFLICT` | `SUCCESS` | Harden Studio CommandBus: PlanTransaction, Logical Identity, an |
| #149 | `CONFLICT` | `SUCCESS` | Security Wave 1 Verification & Governance Alignment |
| #150 | `DRAFT` | `FAILURE` | feat(agent-intelligence): verified CreativeWork trim path |
| #157 | `CONFLICT` | `SUCCESS` | feat(governance): Nagar agent constitution — persistent, versio |
| #193 | `CONFLICT` | `SUCCESS` | feat(infra): canonical Ubuntu 26.04 + Python 3.14 foundation |
| #204 | `DRAFT` | `SUCCESS` | fix(memory): resolve sqlite-vec extension fallback and add fore |
| #205 | `CONFLICT` | `FAILURE` | feat(nagar): Asset Library V1 — secure content-addressed ingest |
| #230 | `CONFLICT` | `SUCCESS` | Arena/aa451c49 nexus ai agent |
| #247 | `CONFLICT` | `SUCCESS` | feat(creative): perception lane — measured silence/scene/loudnes |
| #257 | `DRAFT` | `SUCCESS` | 🎨 Palette: Improve Telegram inline callback UX for language and |
| #273 | `DRAFT` | `SUCCESS` | feat(nagar): P0 intent NEL semantic time causal contracts |
| #284 | `DRAFT` | `FAILURE` | feat(nagar): add read-only Causal Memory and bounded Director pr |
| #285 | `DRAFT` | `SUCCESS` | chore(board): synchronize task-278 deferred status |
| #294 | `CONFLICT` | `SUCCESS` | repair(#189): land the durable creative-graph package (law R28) |
| #295 | `CI_RED` | `FAILURE` | extract(#218): land the self-contained content-addressed Asset L |
| #297 | `CONFLICT` | `SUCCESS` | evidence(m16): true closeout of the M15 spine + open-PR converge |
| #301 | `REBASE_FIRST` | `SUCCESS` | chore(automation): engineering-debt watchdog 2026-11-10 |
| #313 | `CONFLICT` | `SUCCESS` | chore(board): register task-nagar-preservation-media-profiles-01 |
| #326 | `CONFLICT` | `SUCCESS` | feat(character-capsule): versioned Character Capsule + continuit |
| #327 | `CONFLICT` | `SUCCESS` | feat(composition): integrate the NAGAR Creative Capability Compo |
| #328 | `DRAFT` | `SUCCESS` | feat(studio): NAGAR Studio PWA over the verified read API (Draft |
| #329 | `DRAFT` | `SUCCESS` | chore(board): register NAGAR-IBPC-TRIAD-CLOSURE-01 zones (draft |
| #330 | `CONFLICT` | `SUCCESS` | feat(nagar/ibpc): NAGAR IBPC triad - Delta Dialogue, Shadow Prev |
| #331 | `REBASE_FIRST` | `SUCCESS` | ci(truth): add a local pre-push truth gate and a parity test |
| #333 | `CONFLICT` | `SUCCESS` | feat(provenance): RFC 8785 (JCS) canonicalization foundation w |
| #334 | `CI_RED` | `FAILURE` | feat(connect): controlled agent connectivity foundation |
| #335 | `PROTECTION_UNREADABLE` | `SUCCESS` | fix(maintenance): real PostgreSQL durability — dump/restore bu |

### 1.1 Secondary view — registry with the PAT (protection **readable**, 3 canonical contexts)

```
$ GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN" python scripts/pr_convergence.py collect --out /tmp/reg_pat.json
pr-registry: 51 open PRs → /tmp/reg_pat.json (main c3aa7c85e4b6)
COLLECT EXIT=0   (two earlier attempts exited 2 on a transient GraphQL 504 / empty body)

$ python scripts/pr_convergence.py report --in /tmp/reg_pat.json
REPORT EXIT=0
```

- `protection_state`: `readable`, `protection_source`: `branch-protection API`, `required_contexts` = the **3 canonical** contexts (`lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`), `strict: true`, `enforce_admins: true`.
- Tiers: `DRAFT` 13 · `CONFLICT` 31 · `CI_RED` 3 · `REBASE_FIRST` 3 · `PROTECTION_UNREADABLE` **0** · **`READY` 1** (#335).
- Blocker counts: `CONFLICT` 40 · `CI_RED` 6 · **`CI_PENDING` 20** · `CHANGES_REQUESTED` 11 · `REBASE_FIRST` 49 · `DRAFT` 13.

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

**PRs whose canonical required checks** (`lint`, `test`, `merge-base-guard`) are **all `success`**: **24 of 51** — #33, #92, #99, #115, #122, #204, #230, #247, #257, #273, #285, #294, #295, #297, #301, #313, #326, #327, #328, #329, #330, #331, #333, #335. (A *CI-green* set, not a mergeable set: most also carry `CONFLICT`/`DRAFT`/`CHANGES_REQUESTED`.) Up from 22 on 2026-11-11; the two new heads #333 and #335 are green.

**The one `READY` PR (#335)** — `fix(maintenance): real PostgreSQL durability — dump/restore bugs + production DB policy (mission P0)`, head `fix/neon-runtime-database-recovery` @ `c91c85a5004b`, `mergeable: MERGEABLE`, `merge_state: UNSTABLE`. It is `UNSTABLE` only on the non-required CircleCI context (D7); no reviewer requested; not a draft. It is the nearest clean work on `main`.

### 1.2 Which registry differs from which

The two registries differ on exactly **2 PRs** (reproduce: diff `tier` between `/tmp/reg.json` and `/tmp/reg_pat.json`):

| PR | ambient tier (ci_state) | PAT tier (ci_state) | Why |
|---|---|---|---|
| #335 | `PROTECTION_UNREADABLE` (SUCCESS) | **`READY`** (SUCCESS) | The ambient token cannot read protection, so it cannot certify any PR READY; the PAT can. |
| #295 | `CI_RED` (FAILURE) | **`REBASE_FIRST`** (SUCCESS) | The ambient path folds the non-required `api-gate-mutations` failure into the coarse rollup; the PAT path judges only the 3 required contexts (all green) and falls to the `REBASE_FIRST` blocker. |

---

## 2. Board audit

```
$ PYTHONPATH=src python -m pytest -q tests/unit/test_agent_board.py
134 passed in 3.02s
EXIT=0
```

The authoritative board is the one on **`main`**; the working-tree copy is byte-identical this run (`sha256 ed37d685…`; `git show origin/main:.agents/board.json | sha256sum` → same). No board-state mutation is intended by this audit (see the note in §2.7 on the CLI's `gc` side effect, which was reverted).

- **Claims:** 190 (unchanged). **Zones:** 89 (unchanged). Status histogram: `done` 80, `queued` 61, `expired` 20, `completed_merged` 9, `completed_released` 6, `deferred` 3, `available_sequenced_post_33` 2, `active` 2, `assigned_to_B` 1, `assigned_to_E_pr33` 1, `available_sequenced_post_32_33` 1, `available_sequenced_post_32` 1, `assigned_to_B_next` 1, `superseded_by_PR33` 1, `completed_delivered_via_PR34` 1.
- **`gates_owner` holders:** 0 (consistent with the retired mandatory-gates-owner policy, `AGENTS.md` §1.4).
- **Claims carrying an `agent_branch`:** 23. **Claims carrying `exclusive_paths`:** 127.
- **`protocol.protocol_version`:** present and `== 2` (D5 stays resolved).
- **Board delta vs 2026-11-11:** 0 claims, 0 zones; `updated_at` advanced `2026-11-08T08:42:47Z` → `2026-11-10T12:08:41Z` with **no claim added, removed, or status-changed** (a metadata/evidence refresh only; reproduce: diff `claims` maps between `git show 5fe94512:.agents/board.json` and `git show c3aa7c85:.agents/board.json`).

### 2.1 Active claims — 2 (both **stale** live leases, both for **already-merged** work)

`python3 -c "…sum(1 for c in claims if c['status']=='active')"` → **2**:

| Task | Zone | `agent_branch` | Remote branch | Open-PR head | TTL (h) | `claimed_at` | Lease state |
|---|---|---|---|---|---|---|---|
| `task-282-slice-2-artifact-assetversion-binding` | `durable-project-state-lifecycle` | `fix/binding-ownership-fence-20261107` | yes (`af00b0ad…`) | no (PR #325 **merged** 2026-11-11T09:50:33Z) | 24 | 2026-11-10T12:08:41Z | **STALE** — expired 2026-11-11T12:08:41Z, **~14.6 h past TTL** |
| `task-324-closeout-docs-repair` | `docs-architecture` | `openhands/nagar-one-final-closeout-20261107` | yes (`366033ec…`) | no (PR #324 **merged** 2026-11-10T09:06:20Z) | 24 | 2026-11-08T08:42:47Z | **STALE** — expired 2026-11-09T08:42:47Z, **~66.1 h past TTL** |

- Both leases are **stale** (24 h TTL lapsed; run at 2026-11-12T02:47Z). `task-282` still reserves zone `durable-project-state-lifecycle` (paths include `src/nexus_ai_agent/cli.py` and `src/nexus_ai_agent/creative/project_state/`) for work PR **#325** has **already merged** (merge commit `af00b0ad…`, which the GitHub compare API reports as an ancestor of `main`: `status: behind`, `ahead_by: 0`). `task-324` reserves `docs-architecture` (paths include `.agents/board.json`) for work PR **#324** merged 2026-11-10. This is **landed-but-still-leased** debt.
- **Overlap check:** no cross-zone collision — every `exclusive_paths` entry falls inside its declared zone (§2.5), and there is no third live lease, so no overlap with another agent's live lease. **Note:** `task-324` claims `.agents/board.json`, which this watchdog does **not** author a change to (it writes only under `.agents/evidence/`), so this run creates no overlap with that live lease.

### 2.2 Ownership mismatch (dead `agent_branch`) — 13 claims (unchanged, all `expired`)

Claims whose `agent_branch` has **no matching remote branch and no open-PR head**. All 13 are already `expired` (TTL lapsed) → board hygiene debt, not live collisions. Byte-identical list to the 2026-10-30 … 2026-11-11 reports:

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

Reproduce: compare each claim's `agent_branch` against `git ls-remote --heads origin` (259 refs) and the 51 open-PR heads. No active claim has a dead branch → **no ownership mismatch among live leases**.

### 2.3 Stale live leases — 2 (unchanged in count)

The two `active` claims (§2.1) are both past their 24 h TTL (~14.6 h and ~66.1 h). `task-282` was already stale on 2026-11-11 (then ~77 h; its head advanced and PR #325 has since merged); `task-324` remains stale (~66 h). Both hold zones for work already on `main`.

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

Seven claims carry an owner/sequence label with no machine-checkable branch or lease clock. Unchanged from 2026-11-11.

### 2.5 `exclusive_paths` outside a declared zone — 0

Every claim's `exclusive_paths` falls under its declared zone's `paths` (0 violations, with prefix logic — a zone path like `tests/unit/test_ibpc_` is a prefix that covers `tests/unit/test_ibpc_intent_compiler.py`). No cross-zone overlap. Every claim's `zone` field is a declared zone id (0 orphans). Reproduce: for each claim, assert every `exclusive_paths` entry equals a `zone.paths` entry, is a child of one, or shares a `zone.paths` prefix.

### 2.6 Open PRs with no board claim (informational) — 50 of 51

Only **1** of the 51 open PRs (#33) has a head branch that matches a board claim's `agent_branch`; the other 50 carry no `agent_branch` claim, and **21 are entirely absent from the board** (#64, #92, #204, #230, #257, #284, #285, #294, #295, #297, #301, #313, #326, #327, #328, #329, #330, #331, #333, #334, #335). The three new heads this run — #333 (`nagar-jcs-rfc8785-foundation-01`), #334 (`nagar/connect-foundation-20261111`), #335 (`fix/neon-runtime-database-recovery`) — all fall in the unclaimed set, so the newest work-streams again entered the repo with no lease.

```
$ python3 -c "
import json
b=json.load(open('.agents/board.json')); reg=json.load(open('/tmp/reg_pat.json'))
cb={c.get('agent_branch') for c in b['claims'] if c.get('agent_branch')}
blob=json.dumps(b, ensure_ascii=False)
print([p['number'] for p in reg['prs'] if p['head'] not in cb and p['head'] not in blob])
"
```

### 2.7 CLI side effect observed: `show` rewrites `board.json` (informational)

Running the mandated board read `python scripts/agent_board.py show` **mutated the working tree**: its built-in `gc` auto-released both stale `active` leases, flipping them to `expired`, adding a `release_reason`, and advancing `updated_at`. Output: `[gc] auto-released expired leases: task-282-slice-2-artifact-assetversion-binding, task-324-closeout-docs-repair`. This is documented behaviour (`docs/MULTI_AGENT_PROTOCOL.md` §2: "`gc` runs automatically inside `show`/`next`"), but it means `show` is **not** a read-only command. The watchdog does not own `.agents/board.json`, so the change was reverted (`git checkout -- .agents/board.json`) and no board edit is included in this run's commit. The diff is preserved for the record as `/tmp/board_gc_diff.patch` (50 lines; both claims `active → expired`). Reproduce: `python scripts/agent_board.py show && git --no-pager diff --stat .agents/board.json`.

---

## 3. Gates

| Gate | Command | Result | Exit |
|---|---|---|---|
| **G1** truth gate | `PYTHONPATH=src python -m nexus_ai_agent.nagar --check` | `TRUTH GATE: GREEN — projection and generated docs match the sources` | 0 |
| **G2** matrix + docs | `PYTHONPATH=src pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/unit/test_docs_integrity.py` | `123 passed in 1.01s` | 0 |
| **G3** board contract | `PYTHONPATH=src pytest -q tests/unit/test_agent_board.py` | `134 passed in 3.02s` | 0 |

Environment: Python `3.13.15` (`/usr/local/bin/python`). The sandbox had no `.venv` and none of the project's dependencies; the watchdog ran the repository's own `scripts/ci_install.sh ".[dev]"` (CPU-only torch closure, `pip check` clean) to install the runtime needed to *run the gates only*. All three gates **GREEN** — no gate regressed vs the 2026-11-11 run.

`.agents/board.json` sha256 before/after every gate run: `ed37d685188e79de0bf5fe4647a2c0851b599ece8c74d8f71586b3fb824a2c49` (unchanged by the gate runs).

---

## 4. CI health — 12 most recently updated open PRs

Canonical required contexts (from the live protection API read with the PAT, and `AGENTS.md` §1): `lint (ruff + mypy + version lockstep)`, `test (pytest -m "not slow")`, `merge-base-guard (base == main)`. Status per context on the **exact head SHA** (`gh api repos/bot523h/nexus-ai-agent/commits/<sha>/check-runs?per_page=100`, latest run wins), distinguishing `queued`/`in_progress`/`cancelled`/`failure`/`skipped`/`success`/`absent`:

| PR | Head SHA | Updated (UTC) | `lint` | `test` | `merge-base-guard` | combined status |
|---|---|---|---|---|---|---|
| #334 | `99bd2f6bcc` | 2026-11-11T21:47:31Z | **failure** | **failure** | success | failure (CircleCI) |
| #335 | `c91c85a500` | 2026-11-11T17:47:59Z | success | success | success | failure (CircleCI) |
| #333 | `ec64519f3a` | 2026-11-11T08:45:37Z | success | success | success | failure (CircleCI) |
| #330 | `5a281ffbde` | 2026-11-11T06:44:06Z | success | success | success | failure (CircleCI) |
| #301 | `368431a8a3` | 2026-11-11T02:44:59Z | success | success | success | failure (CircleCI) |
| #331 | `4284876b00` | 2026-11-10T23:53:13Z | success | success | success | failure (CircleCI) |
| #329 | `56f3409416` | 2026-11-10T15:42:12Z | success | success | success | failure (CircleCI) |
| #328 | `4f6ca1d1f8` | 2026-11-10T11:05:28Z | success | success | success | failure (CircleCI) |
| #326 | `c38a811f6f` | 2026-11-10T08:41:48Z | success | success | success | failure (CircleCI) |
| #327 | `d5d5ead38d` | 2026-11-10T08:24:36Z | success | success | success | failure (CircleCI) |
| #294 | `9b60307a14` | 2026-11-08T23:32:36Z | success | success | success | pending (no statuses) |
| #313 | `ca7f9ea886` | 2026-11-04T20:50:07Z | success | success | success | failure (CircleCI) |

No `queued`/`in_progress` at snapshot time. Superseded `cancelled` duplicates exist (a re-run cancels the previous attempt) but are never the latest run. **#325 rolled out of the top-12 window** (merged); #334, #335, #333 rolled in.

**#334 is a new all-red head** (`99bd2f6bcc`): its failing checks include `lint`, `lint-fast`, `test`, `extras-matrix (core)`, `python-parity (3.10/3.11/3.12)`, `continuum-evidence (3.10/3.11/3.12)`, `governance-mutations`, and `migrate-postgres` — 13 failing contexts, so the branch is red on more than the required gate.

### 4.1 Repository-wide required-context histograms (all 51 PRs)

| Check | Latest-run histogram on exact head |
|---|---|
| `lint (ruff + mypy + version lockstep)` | `{success: 46, failure: 5}` (absent: 0) — failures #57, #58, #60, #150, #334 |
| `test (pytest -m "not slow")` | `{success: 40, failure: 11}` (absent: 0) — failures #58, #60, #63, #64, #97, #114, #135, #150, #205, #284, #334 |
| `merge-base-guard (base == main)` | `{success: 29, skipped: 2, absent: 20}` |

`merge-base-guard` is **absent on 20 legacy heads** (unchanged): #57, #58, #60, #63, #66, #69, #73, #84, #89, #113, #117, #118, #121, #129, #135, #137, #140, #149, #150, #157. The **2 `skipped`** runs are #193 and #205 — a *skipped* required check is not a pass.

### 4.2 Non-required `CircleCI Pipeline` `ERROR` (D7 — **REGRESSED**, now 11 of 12)

The combined commit status on the 12 recent heads (`.../commits/<sha>/status`) is `failure` for **11 of 12** — #334, #335, #333, #330, #301, #331, #329, #328, #326, #327, #313 — each **solely** from the non-required `CircleCI Pipeline` context reading `error` (`"No configuration was found in your project"`). #294 is `pending` (no statuses). The repository still has **no `.circleci/config.yml`** (`ls -d .circleci` → *No such file or directory*), while the board carries a `queued` claim `task-nagar-circleci-pilot-01` whose `exclusive_paths` are exactly `.circleci/config.yml` and `docs/engineering/NAGAR_CIRCLECI_PILOT.md`. The status originates from an external CircleCI project not reproducible from the tree. This window was 9 of 12 on 2026-11-11; it is the sole non-required reason #335 (the one `READY` PR) is `UNSTABLE` rather than clean.

---

## 5. Findings (engineering debt)

### D1 — `_latest_ci_verdict` still masks a real `FAILURE` behind an absent required context (P0) — **STILL OPEN (~12th run)**

The ordering bug is unchanged (`scripts/pr_convergence.py`): with `require_all_present=True`, a required context that never ran returns `PENDING` **before** the red check runs on the present contexts. Reproduced at function level in §1.1 (`PENDING` where `FAILURE` is correct). The PAT-based registry reports `CI_PENDING` 20 and only 3 `CI_RED`, whereas the truth is that **6 of those 20 heads carry a real `lint`/`test` `failure`** (#57, #58, #60, #63, #135, #150). Reproduce the 6: for each open PR, require `merge-base-guard == absent` **and** (`lint == failure` or `test == failure`).

### D2 — `merge-base-guard` absent on 20 heads (P0) — **STILL OPEN (unchanged)**

20 legacy heads have no `merge-base-guard` run; the registry cannot distinguish "never ran" from "still running", and a *skipped* guard (#193, #205) is likewise not surfaced as its own signal. 20 → 20.

### D3 — board hygiene: 13 dead-branch claims + 7 owner-labelled rows with no clock (P1) — **STILL OPEN (unchanged)**

The 13 dead-branch claims (§2.2) and the 7 owner-labelled rows (§2.4) persist byte-identically. No new ones this run.

### D5 — `protocol.protocol_version` (P1) — **RESOLVED (stays resolved)**

`protocol.protocol_version == 2` is present in the board and enforced by `tests/unit/test_agent_board.py` (134 passing).

### D6 — PR #325 CI regression (P0) — **RESOLVED**

The 2026-11-11 run's acute finding was that PR #325's `test` + `python-parity` turned **failure** at head `af00b0ad6565`. That PR has since **merged** (`2026-11-11T09:50:33Z`, merge commit `af00b0ad…`; the GitHub compare API reports it as an ancestor of `main`: `status: behind`, `ahead_by: 0`), via the `#325 → #332` stack. The regression is moot: the branch is closed and the offending head is no longer a merge candidate. Resolved.

### D7 — non-required `CircleCI Pipeline` `ERROR` on 11 of 12 recent heads (P1) — **REGRESSED**

11 of the 12 recent heads carry a combined status of `failure` from the non-required CircleCI context alone (§4.2), up from 9 of 12 on 2026-11-11. It is the sole non-required reason #335 (the only `READY` PR) is `UNSTABLE` rather than clean. It is not reproducible from the tree (no `.circleci/config.yml`).

### D8 — branch protection readability (P0) — **UNCHANGED (split)**

The repository PAT (`GITHUB_PERSONAL_ACCESS_TOKEN`) reads protection (`protection_state: readable`, 3 canonical contexts, `strict: true`, `enforce_admins: true`), while the ambient `GITHUB_TOKEN` still returns `403 Resource not accessible by integration` (`protection_state: unreadable`, 15 workflow-derived contexts). Consequence: the **default** `collect` (ambient) certifies **no PR READY** and tiers the one clean PR (#335) `PROTECTION_UNREADABLE`; a `GH_TOKEN=$PAT` collect certifies **1 READY** and exposes D1 as active. Reproduce: `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` (403 ambient) vs `GH_TOKEN="$GITHUB_PERSONAL_ACCESS_TOKEN" gh api …` (200, 3 contexts).

### N7 — the `main` board still carries TWO stale `active` leases for already-shipped work (P1, unchanged)

See §2.1. `task-282-slice-2-artifact-assetversion-binding` (expired 2026-11-11T12:08:41Z, ~14.6 h past TTL; its work merged via PR #325 on 2026-11-11) and `task-324-closeout-docs-repair` (expired 2026-11-09T08:42:47Z, ~66.1 h past TTL; its work merged via PR #324 on 2026-11-10). Both hold zones for work already on `main`. Owner action: `release` (or `gc`) both. (`show` would `gc` them automatically — §2.7.)

### N8 — the automation branch's board is still stale (P2, unchanged)

`automation/debt-watchdog` (at `368431a8…`) still carries a board with **79 zones / 175 claims**, `updated_at 2026-10-31T15:35:11Z` (`sha256 592576dd…`), while `main` is at **89 zones / 190 claims**, `updated_at 2026-11-10T12:08:41Z` (`sha256 ed37d685…`). The watchdog does not modify `board.json`, so the branch staleness persists until a human syncs it.

### N9 — new PRs entered with no board claim (P2, recurring)

#333 (`nagar-jcs-rfc8785-foundation-01`), #334 (`nagar/connect-foundation-20261111`) and #335 (`fix/neon-runtime-database-recovery`) are all new since 2026-11-11 and none appears in `.agents/board.json` — not as a claim's `agent_branch`, and not as text. Under the `AGENTS.md` §1 rule ("claim before you code"), these work-streams bypassed the board. No collision is implied (0 `exclusive_paths` violations, §2.5), but the board no longer reflects the live work set for the newest PRs. The unclaimed set now stands at 50 of 51 open PRs (§2.6).

---

## 6. Delta vs previous run (2026-11-11, `main` `5fe9451228ad`)

**Previous report:** `.agents/evidence/AGENT_DEBT_WATCHDOG_2026-11-11.md` (branch `automation/debt-watchdog`).

| Metric | 2026-11-11 | 2026-11-12 | Change |
|---|---|---|---|
| `main` SHA | `5fe9451228ad` | `c3aa7c85e4b6` | **advanced** (PR #332 merged) |
| Open PRs | 49 | 51 | **+2** |
| Open-PR number set | 49 ids | +3 new (#333, #334, #335), −1 merged (#325) | — |
| DRAFT tier | 13 | 13 | 0 |
| CONFLICT tier | 29 | 31 | **+2** |
| CI_RED tier (ambient / PAT) | 4 / 3 | 4 / 3 | 0 / 0 |
| REBASE_FIRST tier (ambient / PAT) | 1 / 2 | 2 / 3 | **+1 / +1** |
| PROTECTION_UNREADABLE tier (ambient) | 2 | 1 | **−1** |
| READY (ambient / PAT) | 0 / 2 | 0 / 1 | 0 / **−1** |
| Remote branches | 255 | 259 | +4 |
| Board zones / claims (`main`) | 89 / 190 | 89 / 190 | 0 / 0 |
| Board sha (`main`) | `eda3f6ad…` | `ed37d685…` | **advanced** |
| Board `updated_at` (`main`) | `2026-11-08T08:42:47Z` | `2026-11-10T12:08:41Z` | **advanced** (no claim change) |
| `merge-base-guard` absent heads | 20 | 20 | 0 |
| Dead-branch claims | 13 | 13 | 0 |
| Owner-labelled rows (no branch/clock) | 7 | 7 | 0 |
| Active (leased) claims | 2 | 2 | 0 |
| Stale live leases | 2 | 2 | 0 |
| `gates_owner` holders | 0 | 0 | 0 |
| Branch protection | readable via PAT, 403 via ambient | readable via PAT, 403 via ambient | 0 |
| Gates G1/G2/G3 | green | green | 0 |
| CircleCI `error` on 12-recent heads | 9 | 11 | **REGRESSED** |
| PRs with all 3 canonical checks green | 22 | 24 | **+2** |

**New PRs since last run:** #333 (`CONFLICT`, `feat(provenance): RFC 8785 (JCS) canonicalization …`, head `ec64519f3a`), #334 (`CI_RED`, `feat(connect): controlled agent connectivity foundation`, head `99bd2f6bcc`), #335 (`READY`-under-PAT, `fix(maintenance): real PostgreSQL durability …`, head `c91c85a500`).
**Gone since last run:** #325 **merged** into `main` (merge commit `af00b0ad…`; part of the `#325 → #332` stack). It was the last run's acute `CI_RED` regression (D6).
**Heads advanced among common PRs:** #330 `6c3677040abb` → `5a281ffbde`; #301 `64bf39dca25e` → `368431a8a3` (the 2026-11-11 watchdog commit itself, now on the `automation/debt-watchdog` branch head).
**Tier change among common PRs:** #294 `CONFLICT → CONFLICT` (unchanged); #295 stays `CI_RED` (ambient) / `REBASE_FIRST` (PAT).

**Regressions:**
- **D7 regressed 9 → 11** of 12 recent heads carrying the non-required CircleCI `error` context.
- **CONFLICT tier +2** (29 → 31) as #333, #334 entered with conflicts; **REBASE_FIRST +1**.
- **NEW all-red head #334** (13 failing contexts) — the first new fully-red PR since 2026-11-10.
- **N9 recurs:** #333, #334, #335 entered with no board claim.
- No gate failure (G1/G2/G3 green both runs). No new ownership mismatch (still 13, byte-identical). No new stale-lease count. No `exclusive_paths` violation.

**Resolved / improved since last run:**
- **D6 resolved** — PR #325 merged (`#325 → #332` stack), so last run's acute CI regression is moot.
- **PR #324's successor stack landed** and `main` advanced; the `READY` backlog is being consumed (the one remaining `READY` PR is #335, a P0 maintenance fix).
- **The set of PRs with all 3 canonical checks green grew 22 → 24.**
- **D5** (`protocol_version`) stays resolved; `exclusive_paths` violations remain 0; every claim's zone is declared.

**Unchanged / still open:** D1 (active — 6 masked heads), D2 (`merge-base-guard` absent on 20 heads; 2 skipped), D3 (board hygiene: 13 dead-branch + 7 owner-labelled), D8 (ambient token still 403), N7 (2 stale landed-but-leased active claims), N8 (stale board on the automation branch).

---

## 7. Next actions (prioritized)

1. **[P0] `release`/`gc` the two stale `active` leases (N7):** `task-282-slice-2-artifact-assetversion-binding` (expired 2026-11-11T12:08:41Z; its work merged via PR #325) and `task-324-closeout-docs-repair` (expired 2026-11-09T08:42:47Z; its work merged via PR #324). Both reserve zones for shipped work. Note: a single `agent_board.py show` already `gc`s them (§2.7) — committing that board change would resolve N7.
2. **[P0] Fix `_latest_ci_verdict` ordering (D1) — active for ~12 runs.** Evaluate the `_RED_CONCLUSIONS` verdict over the **present** contexts *before* returning `PENDING` for a missing one. Today 6 heads (#57, #58, #60, #63, #135, #150) are mis-tiered `CI_PENDING` instead of `CI_RED` whenever protection is readable.
3. **[P0] Restore the ambient `GITHUB_TOKEN`'s protection read (D8).** The PAT works; the automation's ambient token still returns `403`. Without it the *default* `collect` degrades to 15 contexts and certifies no PR `READY` (masking #335 as `PROTECTION_UNREADABLE`). Grant the automation App/installation `administration: read` (or `branches`), then verify `gh api repos/bot523h/nexus-ai-agent/branches/main/protection` returns `200` with the 3 canonical contexts.
4. **[P0] Review and land the one clean PR (#335)** — real PostgreSQL durability (mission P0). It clears `lint`/`test`/`merge-base-guard`, is `MERGEABLE`, and is `UNSTABLE` only on the non-required CircleCI context. It is the nearest green work on `main`.
5. **[P0] Diagnose the new all-red head #334** (`99bd2f6bcc`): 13 failing contexts (`lint`, `lint-fast`, `test`, `extras-matrix (core)`, `python-parity (3.10/3.11/3.12)`, `continuum-evidence (3.10/3.11/3.12)`, `governance-mutations`, `migrate-postgres`). Either fix the branch or mark it a draft so it stops reading as a live candidate.
6. **[P0] Distinguish "required check never ran" from "still running" (D2):** give the 20 legacy heads missing `merge-base-guard` their own signal, and treat the 2 `skipped` guards (#193, #205) as non-green.
7. **[P1] Resolve the `CircleCI Pipeline` `ERROR` (D7) — now 11 of 12:** commit the `.circleci/config.yml` that the queued `task-nagar-circleci-pilot-01` claim reserves, or remove the external pipeline — it currently marks 11 of 12 recent heads' combined status `failure`.
8. **[P1] Rebase or close #313** — it remains `CONFLICT`/`DIRTY`; rebase is cheap. Also triage #294 and #333 (new `CONFLICT`).
9. **[P2] Register the unclaimed new work-streams (#333, #334, #335) on the board (N9)**, or record them as intentionally out-of-band, so `check`/`next` see the live work set.
10. **[P2] Sweep board hygiene (D3):** remove or re-scope the 13 dead-branch claims and the 7 owner-labelled rows with no branch or clock.
11. **[P2] Re-sync the automation branch's board (N8)** so `check`/`next` do not read a 2026-10-31 board on `automation/debt-watchdog`.
12. **[P2] Attack the rebase wall:** 49 of 51 PRs carry `REBASE_FIRST`; triage the 31 `CONFLICT` PRs into "rebase and land" vs "close as superseded" — the backlog is dominated by staleness, not by green-but-unmerged work.

---

_This report was produced by an AI agent (OpenHands) on behalf of the repository owner. Observe-and-report only; no product code was modified, nothing was merged, and `main` was not touched._
