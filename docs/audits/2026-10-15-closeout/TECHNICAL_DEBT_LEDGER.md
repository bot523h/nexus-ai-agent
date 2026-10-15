# Technical debt ledger — measured at the #218 code head (`7bfd628`)

Every row is a number produced by a command on this tree, with the guard that stops the item
from growing silently. "Guard: none" means exactly that — the item can regress until someone
enables the rule; those rows are the next ratchet steps.

## 1. Lint rules measured but not yet enabled (`ruff check . --select <rule> --statistics`)

| Rule | Violations | Meaning | Guard | Next step |
|---|---|---|---|---|
| `BLE001` | 59 | `except Exception` without narrowing — every site carries a `# noqa: BLE001` justification kept alive by `external = ["BLE001"]` | reviewer-visible comments only | narrow per subsystem (storage, bot handlers first), then select the rule |
| `S108` | 45 | hard-coded `/tmp` paths — concentrated in tests and the creative FFmpeg lane's fixtures | none | route through `tempfile`/`tmp_path`, then select `S` subset |
| `PERF401` | 44 | manual list building in loops | none (style/perf) | mechanical rewrite, then select |
| `TRY400` | 24 | `logging.error` inside `except` instead of `logging.exception` | none | select and fix in one pass (loses no behaviour) |

Everything currently in `[tool.ruff.lint] select` (E, F, I, B, UP, RUF100, T201, PLW2901,
PLW1510, A002, PGH003, RUF012, RUF015, DTZ, S324, SIM115, SIM300, G004, PIE, RSE102, FURB,
**ASYNC**) is at **zero** violations on the whole repository and pinned by
`tests/unit/test_ci_lint_parity.py` (A-14, A-12).

## 2. Typing

`mypy --strict src` → **0 errors in 283 files**, enforced by `[tool.mypy] strict = true`
(A-13). Remaining looseness is explicit: `# type: ignore[<code>]` is allowed only with a code
(`PGH003`) and unused ignores are errors (`warn_unused_ignores` via strict).
`tests/` and `scripts/` are not under mypy — a known gap, not a hidden one.

## 3. Complexity hotspots (law R22 ratchet, `tests/architecture/test_complexity_ratchet.py`)

21 functions above the ceiling (20 decision points) are pinned at their measured value; each may
only shrink. Largest: `bot/handlers.py::build_handlers` 205,
`bot/feature_handlers.py::build_feature_command_handlers` 77,
`creative/rendering/compiler.py::_compile` 38, `creative/slideshow/service.py::_planned_session`
36. `adapters/in_process_job_queue.py::_process_job` left the list in A-11 (62 → 17 under a
characterisation contract, 17/17 execution-core mutants still killed). The two `build_*`
registries are tables of handlers, not control flow — decomposing them is a readability task,
not a correctness risk.

## 4. Architecture baselines

* `tests/architecture/legacy_baseline.json`: **40** grandfathered legacy framework imports in
  `src/` (`langgraph`, `sqlmodel`, `telegram`, `adapters` outside their layer); the law fails on
  any new one. Reducing the list requires the port/adapter moves described in
  `docs/architecture/MODULE_MAP.md`.
* Orphan-module law (A-10): nine unreachable modules were deleted; the law fails on any new
  unreachable production module.

## 5. Test estate

* Fast suite at `7bfd628`: see `evidence/10_full_suite_at_7bfd628_with_skips.txt` (pass/skip
  counts and every skip reason). Skips are environment gates (PostgreSQL server, optional
  extras such as `translate`/`speech`/`pdf` wheels, FFmpeg presence, Docker) — none hides a
  known failure; a `@pytest.mark.skip` without a reason is rejected by the suite's own checks.
* **35 production modules have no test file naming them** (`module_inventory.csv`,
  `test_files_referencing == 0`; 32 of them above 20 LOC). They remain `DEFERRED`; writing
  tests for them is the main remaining assurance work and should start with the ones that are
  reachable from the bot/API composition roots.
* 139 modules carry Mission 2's `NOT_ASSESSED`/`BLOCKED`/`HARDENED_BUT_NOT_COMPLETE` verdicts
  (`DEFERRED` here). The per-criterion gaps are in `mission2_module_status_6b398565.csv`
  (`reason` column).

## 6. Dependencies (`deptry src` clean; `pip-audit` 2026-10-15)

| Package | Advisories (no fixed release published) | Exposure in this project |
|---|---|---|
| `chromadb 1.5.9` | CVE-2026-45829 (pre-auth code injection, server), CVE-2026-45833, CVE-2026-45831 (RBAC), CVE-2026-45830 | used only as an **embedded** `chromadb.PersistentClient(path=…)` inside `features/rag.py`; the HTTP server and its auth providers are never started by this code base, so the server-side CVEs are not reachable through the bot. Keep the package pinned to the first release that ships fixes once published. |
| `diskcache 5.6.3` | CVE-2025-69872 (pickle deserialisation of cache files) | transitive via `llama-cpp-python`; not imported by `src/`. Risk requires write access to the container's cache directory, i.e. an attacker who already owns the `nexus` user. |

The dependency audit is not yet a CI job (it needs network to PyPI's advisory DB and would
currently fail on the two unfixable items). Recommended: a scheduled, non-blocking workflow
that posts the table, promoted to blocking once the two packages publish fixes.

## 7. Documentation debt

`docs/README.md` indexes every document (enforced by `tests/unit/test_docs_integrity.py`).
Historical audit reports under `docs/audits/` are immutable and may describe states that no
longer hold; this package is the current one for `main` after #218.

## 8. Structural items the audit named that are **not** done (carried as debt, not closed)

| Item | Audit id | What exists today | What is still missing |
|---|---|---|---|
| Board state inside the merge path | A-04 | referee + Gate-5 mutation harnesses attack again and run in CI; lease/generation fencing is tested | `.agents/board.json` (≈ 290 KB) is still a tracked file that 51 of 64 open PRs modify, so every merge re-dirties the rest. Remedy as specified by the audit: per-claim files (`.agents/claims/<task>-<agent>.json`) **or** an orphan `agents/board` branch the referee reads via `git fetch`; `agent_board.py check` reading the board from that source instead of the PR tree; a CI rule that code PRs do not touch the board. Acceptance: the count of code PRs that are `dirty` only because of the board drops to zero. |
| Dependency lockfile | A-05 | declared deps match imports (`deptry`), extras parity in CI, drift pin fixed | no lockfile / hash-pinned install for the image (`pip install .` resolves at build time) |
| Multi-stage container build | A-15 | `USER nexus`, mode-aware `HEALTHCHECK`, `.dockerignore`, CI `container` job | `build-essential` still ships in the runtime image; `COPY . .` precedes the install (cache invalidation on every change) |
| Dependency audit in CI | — | `pip-audit` run locally and recorded (§6) | scheduled non-blocking workflow; promote to blocking when `chromadb`/`diskcache` publish fixes |
