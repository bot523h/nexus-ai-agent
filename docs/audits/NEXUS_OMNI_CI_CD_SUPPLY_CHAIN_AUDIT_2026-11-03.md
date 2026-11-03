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

## 9. Dependency vulnerability audit (pip-audit + deptry, 2026-11-03)

Tools available in the dev env and already gated by CI: `deptry`, `bandit`,
`pip-audit`. `deptry src` on a **full** `.[dev]` install is clean (the 55
`DEP001` hits reported earlier come from running it against a *minimal* env
without `python-telegram-bot` — an environment artifact, not a finding).

`pip-audit` (full install) surfaced 21 advisories across 5 distributions. Only
two are **directly pinned**; the rest are transitive. Reachability was assessed
against the real call graph, not the advisory text alone.

| Package | Installed | Advisory | Directly pinned? | Reachability in NEXUS | Disposition |
|---|---|---|---|---|---|
| `langgraph-checkpoint-postgres` | 3.1.2 | GHSA-6hmw-9432-67qq | **yes** | affected classes are `PostgresStore`/`SqliteStore` (namespace memory); NEXUS uses `AsyncPostgresSaver`/`SqliteSaver` only → **not reachable** | **FIXED** → 3.1.3 (PR #307) |
| `langgraph-checkpoint-sqlite` | 3.1.1 | GHSA-6hmw-9432-67qq | **yes** | same as above → **not reachable** | **FIXED** → 3.1.2 (PR #307) |
| `filelock` | 3.32.7 | CVE-2026-1129xx (13) | no (transitive) | not imported anywhere in `src/`/`tests/`; the advisories concern `ReadWriteLock`/soft-lock APIs NEXUS never calls | tracked, not bumped (closure risk) |
| `diskcache` | 5.6.3 | PYSEC-2026-2447 | no (transitive) | pickle-based; NEXUS never imports `diskcache` | tracked, not bumped |
| `chromadb` | 1.5.9 | PYSEC-2026-311/3813/3814/3815 | no (transitive) | NEXUS uses the embedded `PersistentClient` (local), never the HTTP server the advisories target (`trust_remote_code` on the REST API) | tracked, not bumped |

**GHSA-6hmw-9432-67qq** (CVSS 4.0 `AV:N/AC:L/AT:P/PR:L/UI:N/VC:H/VI:L/VA:N`): a
namespace label containing `.` reaches another namespace in the Postgres/SQLite
`*Store` classes. Patched in `langgraph-checkpoint-postgres 3.1.3` /
`langgraph-checkpoint-sqlite 3.1.2`. NEXUS does not import `langgraph.store.*`,
so the CVE is **not directly reachable**; the pin bump is defence-in-depth on
the state-persistence path and is delivered in PR #307.

Bumping `filelock`/`diskcache`/`chromadb` would force a re-resolution of the
torch/sentence-transformers/llama-index closure with no reachable benefit, so
they are recorded as **tracked, not actioned** (P3) rather than blindly
upgraded — per the mission rule that a dependency change needs a
compatibility/regression argument, not "latest is better".

## 10. Disposition

| Item | Severity | Status |
|---|---|---|
| `queue-ownership-mutations` disk exhaustion | P1 | **FIXED** — PR #304 (evidence above) |
| `housekeeping` malformed `R2_BUCKET` | P2 | **FIXED (code)** — PR #305; owner must correct the secret |
| `langgraph-checkpoint-*` GHSA-6hmw-9432-67qq pins | P1 | **FIXED** — PR #307 (not reachable; defence-in-depth) |
| `filelock`/`diskcache`/`chromadb` transitive advisories | P3 | **tracked, not actioned** (unreachable; closure risk) |
| Split heavy extras out of default CI installs | P3 | deferred (owner decision; `extras-matrix` already covers 3 legs) |

No test was weakened, skipped, or deleted in this audit.

---

## §11 POST-AUDIT ADDENDUM — the REQUIRED `test` job was disk-flaky (P1)

The first pass scoped `Errno 28` to the advisory `queue-ownership-mutations` job.
That was incomplete. Live check-runs prove the **required** `test (pytest -m
"not slow")` job also fails on the same root cause:

- run `41542383947` job `125610099588` (PR #307 head `0becd39e`) → **failure** at
  `pip install -e ".[dev]"` after pulling `torch-2.15.0` (539.2 MB) +
  `nvidia_cudnn_cu13` (519.2 MB) + `nvidia_cublas` (404.9 MB) + `nvidia_cusolver`
  (227.9 MB) + `nvidia_cufft` (218.3 MB) + `nvidia_nccl_cu13` (216.0 MB) + triton
  → `OSError: [Errno 28] No space left on device`;
- rerun `41544451340` job `125616961754`, **same SHA** → success.

So a required merge gate flips green/red on identical input. **Fixed by PR #308**
(`PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu` at workflow scope in
`ci.yml`/`maintenance.yml`/`langsmith-smoke.yml`, plus a non-vacuous guard test).
This is distinct from #304 (advisory job only) and does not duplicate it.

---

# REMAINDER-CLOSURE-02 — exact advisory inventory (2026-11-03)

`pip-audit` output plus per-advisory reachability. Abbreviated IDs are never
used: each advisory is listed separately.

## filelock 3.32.7 (transitive: `litellm>=1.106` → `filelock>=3.16.1,<4.0`)

All 13 are fixed by `4.2.1` (one by `4.0.9`). Vulnerable classes are the
`*ReadWriteLock` / `StrictSoftFileLock` families and `SoftFileLock` marker
handling; NEXUS instantiates no such lock.

| # | Advisory | Fixed | Affected class | Reachable in NEXUS? |
|---|---|---|---|---|
| 1 | CVE-2026-112907 | 4.2.1 | Windows oplock on a soft-lock marker | No (Linux, no SoftFileLock) |
| 2 | CVE-2026-112908 | 4.2.1 | `StrictSoftFileLock` claim dirs | No |
| 3 | CVE-2026-112909 | 4.2.1 | `UnixFileLock` truncates/chmods a renamed file | No (no UnixFileLock) |
| 4 | CVE-2026-112910 | 4.2.1 | `WindowsFileLock` ignores mode | No (Windows) |
| 5 | CVE-2026-112911 | 4.2.1 | `StrictSoftFileLock`/`SoftReadWriteLock` protocol dir | No |
| 6 | CVE-2026-112912 | 4.2.1 | symlink swap in `SoftFileLock` writability check | No |
| 7 | CVE-2026-112913 | 4.2.1 | `ReadWriteLock` on Windows SQLite side files | No |
| 8 | CVE-2026-112915 | 4.2.1 | `ReadWriteLock` crafted lock DB stalls | No |
| 9 | CVE-2026-112935 | 4.2.1 | `SoftFileLock` stale-lock break on reused inode | No |
| 10 | CVE-2026-112936 | 4.0.9 | `ReadWriteLock` symlink at lock path | No |
| 11 | CVE-2026-112938 | 4.2.1 | `StrictSoftFileLock` symlink swap | No |
| 12 | CVE-2026-112939 | 4.2.1 | `SoftFileLock` forged marker removal | No |
| 13 | CVE-2026-112940 | 4.2.1 | `ReadWriteLock` SQLite reopen by name | No |

**Disposition: TRACKED (upstream-blocked).** A `filelock>=4.2.1` floor is not a
free win: `litellm` pins `filelock<4.0`, so the floor downgrades `litellm`
1.106.2 → 1.103.7. That is a real compatibility trade-off and is not made
silently; the reachable classes are not used, so the residual risk is low.

## chromadb 1.5.9

`CVE-2026-45829`, `CVE-2026-45830`, `CVE-2026-45831`, `CVE-2026-45833` — all
server-side (HTTP server / RBAC). NEXUS uses `chromadb.PersistentClient(path=...)`
only (local, in-process, no `HttpClient`, no server, no listener). **NOT_REACHABLE_WITH_PROOF.**

## diskcache 5.6.3

`CVE-2025-69872` (pickle deserialization). `diskcache` enters via
`llama-cpp-python`; no first-party import and no `Cache(...)` instantiation in
`src/`. **NOT_REACHABLE_WITH_PROOF** (no untrusted cache directory, no
deserialization path reached).
