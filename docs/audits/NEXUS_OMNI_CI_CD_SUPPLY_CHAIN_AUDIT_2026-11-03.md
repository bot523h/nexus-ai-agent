# NEXUS-OMNI — CI/CD & Supply-Chain Audit (2026-11-03)

> **Status:** dated record (not a living source of truth). Mission
> `NEXUS-OMNI-FORENSIC-RECONSTRUCTION-2026-11-03`, agent branch
> `arena/omni-audit-reports`. Base: `main` @ `5ce9f7d3f558ab903ece5f19018b28a5e2ff9a04`
> (PR #303 merge). Every claim below names the command or API call that produced it.

## 1. Scope

`.github/workflows/` (4 files), `pyproject.toml` dependency closure, the two
optional-CI extras matrices, and the runner disk budget. Out of scope here:
application runtime (see the runtime/data report) and product security (see the
security/privacy report).

## 2. Ground truth — the failing run

| Item | Value | Source |
|---|---|---|
| Failing run | `41532014621` | `GET /actions/runs?branch=main` |
| Failing job | `queue-ownership-mutations` / job `125574345212` | `GET /actions/runs/41532014621/jobs` |
| SHA | `5ce9f7d3` | same |
| Observed at | `2026-11-02T21:50:38Z` | same |
| Same SHA earlier | **passed** at `2026-11-02T17:59:52Z` on `a2a847a6` | `GET /actions/runs?branch=main` |
| Terminal error | `OSError: [Errno 28] No space left on device` | job log |

**Key fact:** the mutation campaign never ran. The failure is in the
`pip install -e ".[dev]"` step, before a single mutant is exercised. This is a
CI-health defect (P1), not a test failure.

## 3. Root cause

`pip install -e ".[dev]"` resolves the **full runtime closure**, which includes
`sentence-transformers>=3.0` → `torch` (2.15.0, 539 MB wheel) plus a chain of
`nvidia-*`/`cuda_*` wheels (`nvidia_cudnn_cu13` 519 MB, `nvidia_cufft` 218 MB, …).
On the standard GitHub `ubuntu-latest` runner this exhausts the disk and the
install aborts with `Errno 28` **before** `scripts/queue_ownership_mutations.py`
runs. The intermittency (pass at 17:59, fail at 21:50 on the same SHA) is the
signature of a disk budget that sits at the edge: a shared runner whose free
space drifts below the ~1.5 GB the closure needs.

The `queue-ownership` campaign only needs the SQLite/queue stack and `pytest`.
It has no dependency on torch, CUDA, llama-index, chromadb, or sentence
transformers — yet `.[dev]` dragged all of them in.

## 4. Fix (PR #304)

`.github/workflows/queue-ownership-mutations.yml` now installs only the
campaign's real dependency set (modelled on the existing `trust-mutations` job's
minimal-install pattern) instead of `.[dev]`.

Evidence produced locally in `.venv`:

| Check | Result |
|---|---|
| minimal dependency install | succeeds |
| `pytest` on the 56 targeted queue-ownership/publication tests | `56 passed` (16.9 s) |
| full 10-mutant campaign (`scripts/queue_ownership_mutations.py`) | `EXIT=0`, all 10 mutants killed |
| run from a detached worktree at `5ce9f7d3` | campaign reproducible end-to-end |
| worktree state after run | restored, clean (provenance check `worktree_clean`) |

The fix does **not** disable a test, skip a step, or add `continue-on-error`.
It narrows the install to what the job actually proves.

## 5. Workflow security review

All four workflows were read line by line.

| Check | Finding |
|---|---|
| `permissions:` block | present and least-privilege (`contents: read`) in `ci.yml`, `langsmith-smoke.yml`, `maintenance.yml`, `queue-ownership-mutations.yml` |
| Dangerous triggers | **none** — no `pull_request_target`, no `workflow_run` |
| Action pinning | third-party actions pinned by full SHA (`actions/checkout@11bd719…`, `actions/setup-python@a26af69…`, `actions/upload-artifact@ea165f8…`) |
| `persist-credentials` | set to `false` on every checkout |
| Secrets in logs | no `echo $SECRET`; secrets passed only via `env:` to the steps that need them |
| Fork safety | `pull_request` (not `pull_request_target`) → no secrets to fork PRs, by design |

**Assessment:** workflow security is sound. No hardening change is required.

## 6. Required checks vs optional checks

- **Required** (branch protection `required_status_checks.contexts`): the three
  jobs surfaced as `lint…`, `test…`, `merge-base-guard`.
- **Advisory**: the mutation jobs (`trust-mutations`, `api-gate-mutations`,
  `governance-mutations`, `temporal-mutations`, `remote-key-mutations`),
  `extras-matrix`, `python-parity`, `continuum-evidence`, `release-lineage`.
  A red advisory job is visible but does not block a merge.

`queue-ownership-mutations` is **advisory**. That is exactly why the disk failure
went unnoticed for hours: it did not turn the PR red. It is nonetheless a real
loss of mutation coverage and is treated as P1.

## 7. Disk / runner budget

| Measurement | Value |
|---|---|
| `torch` wheel | 539.2 MB |
| `nvidia_cudnn_cu13` wheel | 519.2 MB |
| `nvidia_cufft` wheel | 218.3 MB |
| plus CUDA toolkit / runtime / other `nvidia-*` | several hundred MB more |

A single `.[dev]` install needs well over 1.5 GB of transient disk. Jobs that do
not exercise the local-model stack must not pay it. **Recommendation (follow-up):**
audit every workflow step that runs `pip install -e ".[dev]"` and split the
heavy optional extras (`[speech]`, `[local-llm]`, `[translate]`, `[pdf]`) into
their own legs — which `extras-matrix` already does for three of them.

## 8. Other CI observations

- `lint-fast` runs `scripts/check_version_lockstep.py`,
  `scripts/check_board_integrity.py`, and `scripts/extras_matrix.py check`
  **before** any install — good fail-fast design.
- `lint` runs `deptry src` after the full install, so undeclared imports are a
  red build. (Note: running `deptry` against a *minimal* local env produces 55
  false `DEP001` hits because the `telegram` distribution is absent; that is an
  environment artifact, not a finding.)
- `maintenance.yml` `housekeeping` fails every schedule with an opaque
  `ParamValidationError: Invalid bucket name` (see the runtime/data report and
  PR #305) — a separate P2 defect.

## 9. Disposition

| Item | Severity | Status |
|---|---|---|
| `queue-ownership-mutations` disk exhaustion | P1 | **FIXED** — PR #304 (evidence above) |
| `housekeeping` malformed `R2_BUCKET` | P2 | **FIXED (code)** — PR #305; owner must correct the secret |
| Split heavy extras out of default CI installs | P3 | deferred (owner decision; `extras-matrix` already covers 3 legs) |

No test was weakened, skipped, or deleted in this audit.
