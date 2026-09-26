# Owner-Wide Forensic Audit — 2026-09-26

## 1. Scope and truth boundary

This audit started from exact main SHA
`2351f099e2f789b80de05c6b382554b5cdcec0f3`. Open PRs #83, #84, #86, #87,
#88, and #89 were inspected but not merged into this branch and are not
counted as delivered. The audit follows the repository's truth hierarchy:
code, runtime tests, exact-SHA CI, history, then documentation/claims.

## 2. Findings and root causes

| Finding | Root cause | Impact | Decision |
|---|---|---|---|
| `housekeeping --dry-run` deleted stale local files | `_clean_temp_dir()` was called without a dry-run guard before the R2 branch | Violated zero-mutation simulation contract | **FIXED** |
| Workspace file tools used resolve-then-operate paths | Lexical checks were duplicated in each tool; no descriptor/no-follow operation boundary | Symlink and parent-directory TOCTOU could redirect read/write/delete | **FIXED** |
| Tool registry changed `NEXUS_WORKSPACE_ROOT` globally | Composition passed configuration through process environment mutation | One registry/instance could redirect another instance | **FIXED** |
| Shell path validation used a separate string/path implementation | The shell and file tools did not share the physical containment policy | Security behavior could drift between capabilities | **FIXED** |
| General graph executor returned `success=True, output=noop` | Unsupported/no-tool branch treated lack of execution as an MVP success | Fake success made failure invisible to callers and observability | **FIXED** |
| `agents.ExecutorAgent` had the same fake-success fallback | Duplicate executor behavior escaped the graph fix | A second entry point could still violate the failure contract | **FIXED** |
| Dry-run summary reported candidates as `removed`/`deleted` | Result field names described the requested mutation, not observed mutation | Operator output could claim a change that did not happen | **FIXED** |

## 3. Changes delivered in this PR

### 3.1 Physical filesystem boundary

`tools/filesystem_policy.py` is now the shared policy for workspace tools. It:

1. rejects NUL, absolute, drive/UNC, alternate-separator traversal, and `..`;
2. resolves and verifies physical containment;
3. rejects existing symlink components;
4. uses POSIX directory descriptors and `O_NOFOLLOW` for read/write/list and
   unlink operations;
5. creates parent directories only below the already-opened root;
6. enumerates cleanup files without following symlinks;
7. fails closed where secure directory operations are unavailable.

This is intentionally smaller than a general filesystem abstraction: it solves
the boundary invariant without becoming a second storage system.

### 3.2 Dry-run contract

`run_housekeeping()` now returns separate `*_planned` and mutation-result
fields. In dry-run mode:

- local files are inspected but not unlinked;
- R2 keys are listed but `delete_objects()` is never called;
- `temp_files_removed` and `backups_deleted` stay empty;
- the structured result distinguishes planning from observed mutation. The
  CLI file was intentionally not changed because it is held by the active
  `task-181-gate5-closure` board lease; its legacy labels remain a documented
  follow-up and must be changed by that owner without claiming a mutation.

A symlinked temporary root is refused rather than treated as an implicit
permission to clean an arbitrary resolved directory.

### 3.3 Execution truth

Both tool-execution lanes now persist a typed refusal with
`error_code=unsupported_operation`, mark the pending step as `failed`, and
populate the state error. They no longer emit `success=True` for a missing
registry or unselected tool. A successful result is reserved for a tool that
actually ran and returned success.

## 4. Threat model and mitigations

| Threat | Boundary | Mitigation | Regression evidence | Residual risk |
|---|---|---|---|---|
| `../` traversal | Workspace input | lexical rejection | `test_policy_rejects_absolute_windows_and_traversal_spellings` | none for this API |
| absolute/Windows/UNC spelling | Workspace input | both POSIX and NT path checks | same test | none for this API |
| file symlink escape | read/write/unlink | no-follow component checks and `O_NOFOLLOW` | `test_tools_reject_symlink_escape...`, leaf unlink test | root owner must still be trusted |
| directory symlink escape | nested workspace path | no-follow parent opens; cleanup does not traverse links | symlink boundary + housekeeping tests | subprocess tools are not a kernel sandbox |
| parent swap / TOCTOU | destructive unlink/write | open parent directory descriptors and operate relative to them | code contract; covered by no-follow adversarial tests | a hostile privileged process can still deny service |
| dry-run mutation | maintenance operation | explicit branch before all mutations, mutation-result fields | `test_dry_run_does_not_delete...` | remote provider `list` can still be unavailable |
| unsupported operation success | execution result | typed refusal on both executor paths | `tests/unit/test_execution_truth.py` | other historical feature surfaces need separate review |
| cross-instance workspace leakage | composition/config | instance-owned workspace objects; no registry env mutation | `test_registry_workspace_is_instance_local` | legacy direct callers may still intentionally use env fallback |

## 5. Verification evidence

Local commands on the changed tree:

```text
ruff check .                         -> All checks passed!
ruff format --check .                -> 499 files already formatted
mypy src                             -> Success: no issues found in 241 source files
pytest targeted security/execution   -> 26 passed
pytest graph regression selection    -> 8 passed
pytest -q -rs -m 'not slow'         -> 2323 passed, 30 skipped
python -m compileall -q src          -> success
git diff --check                     -> success
```

The virtual environment used for these commands was `.venv/`, ignored by the
repository and not part of the patch. It was installed from `pyproject.toml`
with the `[dev]` extra.

Full suite, architecture suite, and exact new-commit CI are release-gate work;
they are not inferred from this targeted evidence.

## 6. Domain evidence table

| Domain | Invariant | Implementation | Test | CI | Exact SHA | Status |
|---|---|---|---|---|---|---|
| Security | Workspace operations remain physically contained | `tools/filesystem_policy.py` | 4 new adversarial tests + existing shell sandbox | pending for this PR | `8e7e5e5` | VERIFIED locally / CI pending |
| Filesystem | Symlink and alternate path escapes are rejected | `WorkspaceFilesystem` | `test_filesystem_boundary.py` | pending | `8e7e5e5` | VERIFIED locally / CI pending |
| Database | Backup path is measured and round-trip verified | existing `maintenance/backup.py` | existing backup suite | main CI 36176954176 | `2351f09` baseline | VERIFIED baseline |
| Queue | Fenced lifecycle and artifact verification remain intact | existing `InProcessJobQueue` | existing queue/integration suites | main CI 36176954176 | `2351f09` baseline | VERIFIED baseline; capacity remains bounded only in-process |
| Executor | Unsupported work is a failure, never fake success | graph + `ExecutorAgent` | `test_execution_truth.py` | pending | `8e7e5e5` | VERIFIED locally / CI pending |
| LLM | Provider/runtime success requires provider evidence | existing provider contracts | existing provider tests | main CI 36176954176 | `2351f09` baseline | PARTIAL; live providers unverified |
| Registry | Workspace config is instance-owned | `ToolRegistry` + tool constructors | isolation test | pending | `8e7e5e5` | VERIFIED locally / CI pending |
| Packs | External/unverified pack behavior is fail-closed | not changed here; open PR work remains separate | existing registry tests | main CI 36176954176 | `2351f09` baseline | PARTIAL until open PRs land and are rechecked |
| CommandBus | Nagar typed path retains its existing gate | not changed here | existing studio suite | main CI 36176954176 | `2351f09` baseline | VERIFIED baseline |
| Nagar | Operation Truth PR #83 is not on this SHA | open PR only | no baseline claim | no baseline claim | `2351f09` | UNVERIFIED on this branch |
| Storage | Maintenance dry-run is non-mutating | `housekeeping.py` | housekeeping contract tests | pending | `8e7e5e5` | VERIFIED locally / CI pending |
| Backup | Restore is not claimed without a live restore drill | existing backup verifier | backup suite | main CI 36176954176 | `2351f09` | PARTIAL |
| CI/CD | Baseline gates are exact-SHA and blocking | `.github/workflows/ci.yml` | existing CI parity tests | run 36176954176 | `2351f09` | VERIFIED baseline |

## 7. Remaining risks — only real items

1. This PR does not merge or supersede the open Nagar, Constitution, pack, or
   Task-122 PRs. Their code must be ancestry-checked and revalidated after
   landing; open-PR evidence is not main evidence.
2. `ShellTool` remains a guarded subprocess capability. Its allowlist and
   workspace validation reduce risk, but a kernel-level sandbox/resource
   policy is still required for hostile multi-tenant execution.
3. Job concurrency is bounded by the current in-process queue contract, not by
   a distributed admission controller. Durability does not prove capacity.
4. Live PostgreSQL restore, live external providers, and production deployment
   rollback remain unverified in this offline engineering checkout.
5. The README version banner is stale (`3.12.0`) while the release metadata is
   `3.13.0`; this must be reconciled by the release/documentation owner.

## 8. Verdict at audit time

**NEEDS_HARDENING** for the repository as a whole. This PR's narrow invariants
are locally verified, but the complete NEXUS master constitution cannot be
honestly marked `ENGINEERING-GRADE` while open critical PRs are unmerged and
live restore/provider/capacity evidence is absent.
