# NEXUS Master Engineering Audit & Hardening — 2026-09-26 (evening session)

Baseline: exact main SHA `6624a133329a22f1b66dce463e0b207385ee8416`
(merge of PR #91). Branch: `arena/01a0dee1-nexus-ai-agent`.
Truth hierarchy applied: Code > Runtime Evidence > Exact-SHA CI > Git
History > Documentation > Claims.

This audit supersedes nothing and merges no open PR. Open-PR claims are
reconciled in §7 and are NOT counted as delivered anywhere in this document.

## 1. LIVE TRUTH

| Field | Observed value | Evidence |
|---|---|---|
| Repository | `bot523h/nexus-ai-agent` | `git remote -v` |
| `origin/main` HEAD | `6624a133329a22f1b66dce463e0b207385ee8416` | `git fetch` + `rev-parse` |
| Version | `3.13.0` (`VERSION` == `pyproject.toml` == CHANGELOG latest) | lockstep test |
| README banner at entry | **stale `v3.12.0`** → fixed + now CI-guarded | this audit F4 |
| Working tree at entry | clean, on session branch, ancestry-verified | `git status`, `merge-base` |
| Exact-SHA CI | run `36221530309` on `6624a13` — **12/12 jobs success** | `gh run view` |
| Exact-SHA maintenance | run `36230324343` — `backup-db` **failure** | step: `nexus maintenance backup` exit 1 |
| Maintenance failure root cause | R2 secrets unset in the repo; the command is *designed* to fail loudly when unconfigured ("a silently skipped backup is worse than a red job") | local repro: exit 1, typed message |
| Open PRs | 16: #93 #92 #89 #88 #87 #86 #84 #83 #73 #70 #69 #68 #67 #66 #64 #63 | `gh pr list` |
| Merged recent PRs | #91 #82 #81 #79 #78 #77 #76 #74 #72 #71 #65 — **every merge commit verified ancestor of main** | `merge-base --is-ancestor` |
| CI log access | job logs via Azure blob unreachable from this sandbox (EOF); step-level evidence used instead | API attempts recorded |

## 2. WHAT I INSPECTED

- 241 Python source files under `src/`; 175 test files under `tests/`
  (132 unit / 25 architecture / 18 integration, 1 bench).
- 13 scripts; 2 workflows (`ci.yml`, `maintenance.yml`); 58 docs md + 11 root md.
- Line-by-line control-flow audit of: `tools/filesystem_policy.py`,
  `tools/files.py`, `tools/system_shell.py`, `tools/registry.py`,
  `orchestration/graph.py`, `agents/executor_agent.py`,
  `bot/access_guard.py`, `maintenance/backup.py`, `maintenance/housekeeping.py`,
  `creative/studio/bus.py`, `creative/studio/lifecycle.py`,
  `creative/packs/verify.py`, `adapters/in_process_job_queue.py` (contracts),
  `storage/db.py`, `api/app.py` + `api/dashboard.py` (gates), `cli.py`.
- Executed: full non-slow suite twice (before/after), live exploit probes
  against `ShellTool` (8 spellings), CLI end-to-end runs, live Nagar dispatch
  probe against all 57 registered operations, 5 kill-mutations.

## 3. FINDINGS

| ID | Severity | File / line | Root cause | Impact | Decision |
|---|---|---|---|---|---|
| F1 | **P0 security** | `tools/system_shell.py` (pre-fix `_validate_args`) | Only `ls`/`cat`/`find` had operand path checks; `date` file options (`-f`, `--file=`, bundled) were untyped. `date` echoes file content in its error output | **Arbitrary file read** of any process-readable path (e.g. `/etc/passwd`, `/proc/self/environ` → credential leak) through a tool documented as workspace-only. PROVEN live pre-fix | **FIXED** (fail-closed option policy; `date` has no path-typed options) |
| F2 | **P1 security** | `tools/system_shell.py` | `-f` matched only exact separate-token spelling; attached (`-fFILE`), long (`--file=FILE`), and bundled (`-if FILE`) forms fell through | Unvalidated pattern-file read outside workspace (content oracle). PROVEN pre-fix | **FIXED** (path-typed option, validated in every spelling) |
| F3 | **P1/P2 security** | `tools/system_shell.py` | Recursive dereference options (`grep -R`/`--dereference-recursive`/`--follow`, `find -L`/`-H`/`-follow`, `ls -L`) were unknown to the blocklist and follow symlinks *below* a validated argument | Outside-workspace content/name leak through a planted symlink. PROVEN pre-fix | **FIXED** (dereference options absent from vocabulary) |
| F4 | **P1 execution truth** | `orchestration/graph.py` executor node | `needs_confirmation` results carry no `output`/`error`, so `output_text or f"Tool {tool_name} executed."` fell through to the success-shaped message; step was marked `failed` | User-visible "Tool write_file executed." while **nothing executed** and the step failed. PROVEN live pre-fix | **FIXED** (pause semantics; honest confirmation message; empty-result branch reports observed truth) |
| F5 | **P2 operator truth** | `cli.py` housekeeping command | CLI printed only mutation result lists (always empty in dry-run); `*_planned` fields never rendered | `--dry-run` answered "what would you delete?" with empty output — the exact question the flag exists for. PROVEN pre-fix | **FIXED** (planned lists in dry-run; mutation lists only after real runs) |
| F6 | **P2 release hygiene** | `README.md` line 5 + version lockstep guard | Lockstep checked VERSION/pyproject/CHANGELOG but not README | Banner `v3.12.0` drifted from release `3.13.0`, surviving all gates; documented as deferred by PR #91 | **FIXED** (banner corrected; guard extended with fixture red-tests) |
| F7 | **P3 robustness** | `tools/filesystem_policy.py` | `read_text(".")`/`write_text(".", …)`/`unlink(".")` passed an empty parts tuple to `_parent_fd`, escaping as bare `AssertionError` | Crash-shaped failure at a security boundary instead of typed `FilesystemBoundaryError` | **FIXED** (typed root-operand refusal) |
| F8 | **P2 reliability** | `tools/system_shell.py` | Unbounded captured stdout/stderr | `cat` of a huge in-workspace file could exhaust process memory | **FIXED** (200k-char cap with truncation marker) |
| F9 | product/ops | `.github/workflows/maintenance.yml` | By-design fail-loud on missing R2 secrets | Scheduled `backup-db` on main is permanently red until secrets are set; noise vs. visibility is a product call | **NEEDS_PRODUCT_DECISION** (either set the repo secrets, or gate the job with an explicit `if: secrets` expression and accept silent-skipping; behavior is correct as coded) |
| F10 | P2 (open-PR-owned) | `llm/fallback_provider.py` | Fallback classified by exception-substring ("429"/"rate limit") | Misclassification risk for provider failures | open PR #93 owns the typed classification; **not fixed here** to avoid collision |
| F11 | P1 (open-PR-owned) | `creative/packs/verify.py` | Signature verification is format-only in main ("no cryptography dependency"); placeholder digests/signatures are warnings | A sufficiently staged unverified external pack can pass main's verifier | open PRs #86/#88 own fail-closed external packs; **not fixed here** to avoid collision |
| F12 | P2 design gap | `orchestration/graph.py` + `tools/registry.py` | GUARDED tools invoked from the graph lane always receive `policy=None` | Confirmation dialogue (`Reply 'confirm'`) is surfaced (now honestly) but no surface translates a later "confirm" into `confirmed=True` — guarded tools cannot complete via the graph lane | NEEDS_PRODUCT_DECISION (confirmation-flow wiring is a feature, not a bug; the fake-success lie is fixed) |

## 4. FIXES

| File | Change | Invariant | Test |
|---|---|---|---|
| `tools/system_shell.py` | Curated per-command option specs (`_OptSpec`) with fail-closed parsing: short bundles, attached args, long `=` forms; path-typed options validated; dereference/time-set/file-echo options absent from vocabulary; NUL typed-failure; output cap 200k | Workspace containment holds for *every* spelling of every option, not only canonical ones | `test_shell_option_policy.py` (22 tests) + legacy `test_shell_sandbox.py` still green |
| `orchestration/graph.py` | `needs_confirmation` → step stays `pending`, no tool result recorded, honest "NOT been executed" message; empty-result branches describe observed outcome | not-executed ⇒ never reported as executed; success ⇒ real execution | `test_execution_truth.py` +2 |
| `cli.py` | `--dry-run` prints `temp_files_planned`/`backups_planned`; real runs print removed/deleted only | preview ⇒ planned, not zero-length mutation lists | `test_maintenance_cli.py` (2) |
| `README.md`, `tests/unit/test_version_command.py` | Banner → `v3.13.0`; lockstep extended to README banner with fixture-based red tests | user-facing banner == release metadata | +4 lockstep tests incl. drift/missing-banner failures |
| `tools/filesystem_policy.py` | Root operand for read/write/unlink → typed `FilesystemBoundaryError` | boundary errors are typed, never bare `AssertionError` | `test_filesystem_boundary.py` +1 |

## 5. UNFIXED

- F9 (maintenance noise) — deliberate product decision, see Findings.
- F12 (confirmation-flow wiring) — needs product decision; honest pause state ships here.

## 6. UNVERIFIED

- Live external providers (Telegram, LLMs, image/storage/R2/Postgres): contract-
  tested only; no live-proven evidence in this checkout.
- Live PostgreSQL **restore** drill: local structural verification only
  (pg_dump footer / SQLite integrity + byte round-trip), by documented design.
- CI job **log bodies** for the maintenance failure (Azure blob blocked);
  step-level + local-repro evidence used instead.
- Distributed queue capacity: in-process queue semantics verified; no
  cross-process capacity proof exists.
- Open-PR CI: not evaluated here (branch-level claims are not main truth).

## 7. OPEN-PR RECONCILIATION

| PR | State | Ancestry of main | Note |
|---|---|---|---|
| #91 | MERGED | ✓ in main | baseline for this audit |
| #82 #81 #79 #78 #77 #76 #74 #72 #71 #65 | MERGED | ✓ all verified ancestors | — |
| #90 #80 #75 | CLOSED | no | superseded work |
| #83 (Operation Truth) | OPEN | no | workers against this SHA must re-verify post-merge |
| #84 (Constitution) | OPEN | no | untouched here |
| #86/#88 (pack fail-closed) | OPEN | no | owns F11; do not double-fix |
| #87 (Studio Core) | OPEN | no | preview/master boundary work; untouched |
| #89 (moderation/command closure) | OPEN | no | owns Task-122 wirings; untouched |
| #92 (storage defaults) | OPEN | no | owns SQLite-defaults item |
| #93 (typed LLM failure) | OPEN | no | owns F10 |
| #63–#73 older gates | OPEN | no | stale/advanced work; rebase before any merge |

## 8. TEST MATRIX (this branch, exact worktree)

| Command | Result |
|---|---|
| `pytest -q -m "not slow"` (pre-fix baseline) | 2323 passed, 30 skipped |
| `pytest -q -m "not slow"` (post-fix) | **2356 passed, 30 skipped** |
| Targeted new/updated (7 files) | 66 passed |
| Mutation M1 (re-allow `grep -R`) | RED as required, restored |
| Mutation M2 (re-add `date -f`) | RED as required, restored |
| Mutation M3 (delete pause branch) | RED as required, restored |
| Mutation M4 (revert README banner) | RED as required, restored |
| Mutation M5 (remove CLI planned lists) | RED as required, restored |
| Live exploit re-probe (8 spellings) | 0 leaks; positive controls intact |
| `ruff check .` / `ruff format --check .` | pass / 503 files formatted |
| `mypy src` | 241 files, no issues |
| `python -m compileall -q src` | pass |
| `scripts/extras_matrix.py check` | pass |
| `pytest tests/unit/test_version_command.py -k lockstep` | 6 passed |
| `git diff --check` | pass |
| Live CLI dry-run/real housekeeping demo | planned listed + zero mutation; real run reports observed removal |

## 9. SECURITY MATRIX

| Threat | Boundary | Mitigation (now) | Test | Residual risk |
|---|---|---|---|---|
| File echo via `date -f/--file` | ShellTool options | option absent from date vocabulary | `test_date_*` | none via this tool |
| Pattern-file outside read (`grep -fF/--file=/--exclude-from/-if`) | ShellTool options | path-typed & validated in all spellings | `[grep_file_options…]` parametrized | none via this tool |
| Symlink-deref recursion (`grep -R`, `find -L/-H/-follow`, `ls -L`) | recursion below validated arg | dereference options absent; `-r` kept (no follow) | dereference parametrized + `-r` negative check | symlink *arg* spelling still rejected by resolve ✓ |
| GNU prefix abbreviation (`--fi=`) | long options | exact names only | abbreviation test | none |
| Traversal/absolute/UNC/NUL | lexical policy | unchanged (#91) + NUL typed | existing boundary tests | none for this API |
| Symlink components | descriptor opens + resolve | unchanged (#91) | existing boundary tests | root's ancestor swap by privileged attacker |
| TOCTOU (parent/leaf swap) | dir_fd + O_NOFOLLOW ops | unchanged (#91) | adversarial tests | hostile same-dir process can DoS, not escape |
| Memory exhaustion via output | subprocess capture | 200k cap + marker | `test_output_is_capped…` | bounded |
| Confirmation-less guarded run | registry policy | pause + honest message; no mutation | graph truth tests | confirmation flow itself is F12 |
| Unsupported operation | executor lanes | typed refusal (#91), kept green | `test_execution_truth.py` | none found |

## 10. FINAL VERDICT

**READY_FOR_NEXT_HARDENING.** The main-baseline invariants verified as
VERIFIED (filesystem containment, dry-run zero-mutation, execution truth,
backup round-trip, access guard, job fencing) re-verified green after this
slice; two demonstrated live bypasses (F1–F4 group: 1 P0, 2 P1, plus one
execution-truth lie) are closed with mutation-killed tests. Not
ENGINEERING-GRADE: live-provider/live-restore evidence, external-pack
cryptographic verification (#86/#88), and the confirmation-flow decision
(F12) remain open by evidence, not by feeling.
