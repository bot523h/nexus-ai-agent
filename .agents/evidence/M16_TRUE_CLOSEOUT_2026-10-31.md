# M16 — true closeout of the M15 spine and the open-PR backlog

Mission: `NEXUS-M16-M15-TRUE-CLOSEOUT-AND-PR-CONVERGENCE-01`
Baseline `main`: `a28479adf2fd774915c1a8454b9dd3fa5048de34` (advanced to `ab56becc` during the run)
Evidence convention: `.agents/evidence/<NAME>_<date>.md`

Every claim below is reproducible from the tree with the exact command shown.

---

## 1. What was already true on main (Phase 1–3)

| Deliverable | PR | Verified |
|---|---|---|
| board reconciliation | #287 | `.agents/board.json` schema 2; `pytest -q tests/unit/test_agent_board.py` green |
| `diagnostics/truth.py` live-lease TTL expiry | #292 | `python -m nexus_ai_agent.diagnostics.truth` → `OK — no findings` |
| PR convergence engine + evidence/scripts | #291 | `pytest -q tests/unit/test_pr_convergence.py` → 51 passed |
| `AGENT_DEBT_WATCHDOG` | #289 | present under `.github/workflows/` |

Board tasks closed through the official CLI only (no hand-edited JSON):
`task-267` (truth041 live-lease alignment) → `done` gen 2;
`task-268` (board-referee mutation campaign in CI) → `done` gen 2.
Reconciliation branch `arena/m16-board-reconciliation` @ `1e17a571` (pushed).

## 2. PR #189 — stale mega-PR, repaired and superseded by #294

`#189` (head `c2ab90d`, base **Oct-7** `48f280c5`) is 25 files. Path-level
reconciliation against current main proved most of it is a **stale revert**:
`ci.yml` hardening, the `AGENTS.md` "NO MANDATORY GATES OWNER" policy,
`MODULE_MAP`/`README`, the #292 TTL predicate, and the #291 M15 fixes.

Repair (`arena/m16-pr189-repair` @ `af8ab80a`, PR **#294**) lands only the
genuinely-new, self-contained deliverable — the durable creative-graph package:

- `src/nexus_ai_agent/provenance/graph.py` (+ export in `provenance/__init__.py`)
- `tests/unit/test_creative_graph.py`, `tests/architecture/test_creative_graph_boundary.py`
- `docs/architecture/CREATIVE_GRAPH.md`, indexed, **law R28** (main's R21 is the
  `selfhood/` zone; #189's `law R21` was stale)
- the **strictly stronger** identity guard: a *mention* of `nexus_causal_journal`
  is not a *write* to it (`_LEDGER_WRITE` regex + declared `_LEDGER_READERS`)
- `OPERATION_TRUTH.json` regenerated (the new test file is now a suite reference)

Root causes fixed during the repair (both reproduced, then proved fixed):
- `test_exactly_one_module_writes_the_durable_ledger` failed because the old
  guard scanned for the *substring* `nexus_causal_journal`; `graph.py` names the
  table in prose while only reading it. The #189 reader/writer split fixes this
  without weakening the guard.
- `test_operation_truth_mutations.py` failed because `OPERATION_TRUTH.json` was
  stale relative to the new suite reference file.

Evidence: `pytest -q -m "not slow"` → **5765 passed, 39 skipped, 0 failed**;
`mypy src` clean (343 files); `ruff check`/`format --check` clean;
`python -m nexus_ai_agent.nagar --check` → `TRUTH GATE: GREEN`;
`agent_board.py check` → exit 0 (45 sources read, disjoint).

`#189` closed as superseded with a full triage comment.

## 3. PR #218 — dirty mega-PR, safe extraction in #295

`#218` (head `2b956ca`, base **Oct-15** `da78666c`) is **305 files / +67823 /
−1893**, `mergeable=false`. A–H triage:

- **stale reverts**: `scripts/pr_convergence.py` deletes 921 lines of #291 work;
  `truth.py` −70; `agent_board.py` +481/−351; `README`/`MODULE_MAP`/`ci.yml` older.
- **already done on main**: 9 "deletions" already absent; `bot/surface/{moderation,viral}.py`
  already present; the 6 root-doc renames already done.
- **genuinely new**: `execution/asset_library.py` (+test), `storage/clock.py` (+test),
  2 migrations, 5 scripts, 64 `docs/audits/*` (historical).

Extraction (`arena/m16-pr218-extract` @ `ebb61361`, PR **#295**) lands the
self-contained, high-value piece only:

- `src/nexus_ai_agent/execution/asset_library.py` — content-addressed blob store
  (atomic finalize via `O_EXCL` + `os.link`, SHA-256 dedup, hostile-source
  refusal, race-safe admission, corruption reported never overwritten, MIME sniff)
- `tests/unit/test_asset_library.py` — 38 tests, all green

Evidence: `mypy` clean; `ruff` clean; truth doctor `OK`;
`test_no_orphan_modules` + `test_docs_integrity` → 106 passed.

`clock.py` (A-12) is **deferred**: the module is self-contained, but its lockstep
test bans `datetime.utcnow()` in **47 modules** across many board zones
(`agent-sdk-build`, `execution-core`, `storage-observability`, `bot/surface/*`,
`features/*`, …). That is a dedicated migration, not a drive-by extract.
`#218` closed as superseded.

## 4. Live PR convergence engine — verified, not asserted

`python scripts/pr_convergence.py collect` reads **42** open PRs live.

- **Defect A (latest-run semantics).** Every `CI_RED` PR in the registry is
  *genuinely* red — the latest run of a required context is `FAILURE` — while
  older `CANCELLED` superseded runs sit alongside. The classifier reads the
  **latest run per required context**, so #287/#289 (older `CANCELLED` + newer
  `SUCCESS`) are not mis-flagged.
- **Defect B (fail-closed).** With no token the tool prints
  `source unreadable` and exits **2** — never a pass. With a token and a real red
  required check, `guard` prints `BLOCKED` and exits **1**.
- Adversarial fixtures cover missing/red/skipped/non-required contexts, moved
  head, stale base, unknown merge state, self-approval deadlock, and
  unknown-never-green. `pytest -q tests/unit/test_pr_convergence.py` → 51 passed.

Artifacts: `.agents/evidence/PR_REGISTRY_2026-10-31.json`,
`.agents/evidence/PR_CONVERGENCE_2026-10-31.md`.

## 5. LangSmith — deployed key verified end-to-end

The user deployed a `LANGSMITH_API_KEY` secret (repo + runtime).

- `python scripts/langsmith_smoke.py` → `RESULT=OK DETAIL=project=nexus-ai-agent-smoke`, exit 0.
- Missing secret → `RESULT=SKIPPED_NO_SECRET`, exit 0 (a visible skip, never a fake pass).
- `sanitize_exception(...)` returns only the exception class name — the URL, the
  key and the payload are never printed.
- CI job `langsmith-smoke.yml` (uses `secrets.LANGSMITH_API_KEY`) → last main run `success`.
- `pytest -q tests/unit/test_langsmith_smoke_safe_output.py tests/unit/test_tracing_bootstrap.py tests/unit/test_optional_extras.py` → 30 passed, 10 skipped.

## 6. Independent verification (two-agent)

A read-only second agent re-derived §2–§5 from the tree and the live API without
access to this narrative; the commands in each section are the ones it ran.

## 7. Honest status

- Merged into main this mission: **none** — both repairs are open PRs (#294, #295)
  pending the required human review; the board/task closure is pushed.
- Deferred and named: `clock.py` A-12 migration (47 modules); `asset_library`
  wiring into the render path (execution-core zone); the remaining 30-PR backlog
  (`CONFLICT 25`, `DRAFT 11`, `REBASE_FIRST 1`).
