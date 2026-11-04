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

---

# PYTHON-MATRIX-CLOSURE-03 — CPU torch pin covers the supported matrix (2026-11-03)

The #308 package-scoped install removed the global `PIP_EXTRA_INDEX_URL`, but it
pinned `torch==2.15.0+cpu`, which has **no CPython 3.10 wheel**. Live check-runs
on head `b94b155f` red exactly the two 3.10 legs, both at
`bash scripts/ci_install.sh ".[dev]"`:

| Failing check | Head | Failing command |
|---|---|---|
| `python-parity (3.10)` | `b94b155f` | `bash scripts/ci_install.sh ".[dev]"` |
| `continuum-evidence (3.10)` | `b94b155f` | `bash scripts/ci_install.sh ".[dev]"` |

## Root cause (not a disk problem)

PyTorch **2.15 dropped CPython 3.10 wheels**; the repository's
`requires-python = ">=3.10"` and CI run a 3.10/3.11/3.12 parity matrix. Verified
against the official index (`GET https://download.pytorch.org/whl/cpu/torch/`,
2026-11-03):

| torch | cp310 wheels | cp311 wheels | cp312 wheels |
|---|---|---|---|
| `2.15.0+cpu` | **0** | yes | yes |
| `2.14.1+cpu` | yes | yes | yes |
| `2.14.0+cpu` | yes | yes | yes |

Reproduction (baseline RED, CPython 3.10.22):

```
$ pip install --index-url https://download.pytorch.org/whl/cpu --only-binary=:all: torch==2.15.0+cpu
ERROR: Could not find a version that satisfies the requirement torch==2.15.0+cpu
       (from versions: ... 2.13.0+cpu, 2.14.0+cpu, 2.14.1+cpu)
ERROR: No matching distribution found for torch==2.15.0+cpu
```

## Decision (option B — one pin for the whole supported matrix)

`torch==2.14.1+cpu` — the newest release whose CPU index publishes `cp310`,
`cp311` **and** `cp312` wheels, so a single constraint covers every interpreter
`requires-python` supports. Option A (per-interpreter pins) was rejected: two
pins, two provenance paths, no benefit, and it invites silent drift.

## Source-provenance boundary (exact, not approximate)

The #308 comment claimed "the CPU index is used ONLY for torch", but the first
install step did **not** pass `--no-deps`, so the CPU index also resolved
torch's transitive requirements (`filelock`, `typing-extensions`, `setuptools`,
`sympy`, `networkx`, `jinja2`, `fsspec`) — a wider provenance surface than
stated. Corrected in `scripts/ci_install.sh`:

1. **step 1** — `--index-url <CPU>` + `--only-binary=:all:` + `--no-deps` +
   `-c constraints/ci-cpu-torch.txt`, requirement `torch` **only**;
2. **step 2** — `--index-url https://pypi.org/simple` + `-c <constraint>` for the
   editable project and every other distribution;
3. **step 3** — fail closed unless `pip check` is clean, no `nvidia-*`/`triton`
   is installed, and `torch` is still the `+cpu` build.

### Provenance proof (CPython 3.10.22 run of `ci_install.sh ".[dev]"`)

```
Looking in indexes: https://download.pytorch.org/whl/cpu
Collecting torch
  Using cached torch-2.14.1%2Bcpu-cp310-cp310-manylinux_2_28_x86_64.whl (196.2 MB)
Installing collected packages: torch
...
Collecting llama-index-core==0.14.25 (from nexus-ai-agent==3.13.0)   # step 2, PyPI
...
ci_install: torch==2.14.1+cpu, pip check clean, no CUDA packages
```

Only one `Looking in indexes` line appears (the CPU index), and every
`download.pytorch.org` reference in the log is the `torch` wheel — zero non-torch
downloads from the CPU index.

## Matrix verification (all three supported interpreters)

Each environment was created clean, ran `scripts/ci_install.sh ".[dev]"`, and
reported `ci_install: torch==2.14.1+cpu, pip check clean, no CUDA packages`:

| Interpreter | torch | nvidia/triton | pip check |
|---|---|---|---|
| CPython 3.10.22 | `2.14.1+cpu` | none | clean |
| CPython 3.11.17 | `2.14.1+cpu` | none | clean |
| CPython 3.12.15 | `2.14.1+cpu` | none | clean |

Full suite (`pytest -q -m "not slow"`): CPython 3.10.22 → **6173 passed, 39
skipped, 1 failed** (16m00s). The single failure,
`tests/integration/test_execution_native_backend.py::test_stale_attempt_cannot_complete_after_takeover`,
is a known timing flake unrelated to torch: it passed 4/4 on immediate reruns on
the same interpreter. 3.11/3.12 full suites run in CI (`python-parity`,
`continuum-evidence`).

## Guard + mutation evidence

`tests/unit/test_ci_torch_disk_budget.py` now asserts on the **comment-stripped
code** (a comment that says `--no-deps` no longer satisfies the test) and pins:
the exact `+cpu` pin, that the parity matrix equals the interpreter set the pin
was verified against, no global extra index, `--no-deps` on the torch step,
PyPI-only remainder, and the fail-closed postcondition. A new CI step runs
`scripts/ci_torch_mutations.py`, which kills **10/10** mutants (source isolation,
exact pin, interpreter coverage, parity drift, `--no-deps` removed, constraint
removed, CUDA check removed, `+cpu` check removed, `pip check` removed, workflow
bypasses the script) and restores the tree byte-identically.

## Residual advisory defects (unchanged)

The `filelock` / `chromadb` / `diskcache` advisories remain `TRACKED` /
`NOT_REACHABLE_WITH_PROOF` (see above) — this mission does not touch them.

---

## §12 POST-AUDIT ADDENDUM — CLOSURE-04: behavioral hardening (P1)

Mission `NEXUS-OMNI-PYTHON-MATRIX-CLOSURE-04`, continuation of PR #308. Head
`574da675` (base == `main` `5ce9f7d3`). The independent audit of PR #308 reported
ten findings; the two that actually threaten the install contract (H1, M3) were
reproduced first, then fixed and re-proven behaviorally.

### H1 — the guard was textual, not behavioral (HIGH, reproduced)

The pre-revision `tests/unit/test_ci_torch_disk_budget.py` grepped the script
text. Reproduced three behavioral mutants that all left the asserted strings in
place and therefore **survived**:

| mutant | change | old guard | new guard |
|---|---|---|---|
| A | `problems.append("CUDA…")` → `pass` | **SURVIVED** (1 passed) | KILLED |
| B | drop the PEP 503 name normalization | **SURVIVED** (1 passed) | KILLED |
| C | add `--find-links https://evil.invalid/wheels` | **SURVIVED** (8 passed) | KILLED |

The guard now **executes the real postcondition** (extracted from the
`python - <<'PY'` heredoc) against synthetic `*.dist-info` metadata, and runs the
**real installer through a `python` shim** on `PATH`:

* clean CPU torch → exit 0;
* `nvidia-cublas-cu12`, `nvidia_nccl_cu12`, `nvidia.cublas.cu12`, `triton`,
  non-`+cpu` torch, absent/unreadable metadata → all exit non-zero;
* installer argv: step 1 is `--isolated --index-url <cpu> --only-binary=:all:
  --no-deps -c … torch`; step 2 is `--isolated --index-url https://pypi.org/simple
  -c … -e ".[dev]"`; a hostile `PIP_*` environment never reaches the argv; a
  failing postcondition propagates a non-zero exit.

### M3 — pip environment isolation (MEDIUM, reproduced)

Reproduced on pip 26.3: with `PIP_EXTRA_INDEX_URL` set, an explicit
`--index-url https://pypi.org/simple` still resolves against **both** indexes
(`Looking in indexes: https://pypi.org/simple, https://example.invalid/extra`).
Fix: both install steps now pass `--isolated`, which makes pip ignore
`PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` / `PIP_FIND_LINKS` / `pip.conf`
(`pip config list` → empty under `--isolated`).

### L1 — package-name normalization (LOW, fixed)

Detection now normalizes names (PEP 503: lowercase, `-`/`_`/`.` collapsed to `-`),
so `nvidia_nccl_cu12` and `nvidia.cublas.cu12` are caught. An unreadable/absent
torch distribution fails closed with a clear message.

### M4 / L3 — dynamic discovery and a single matrix contract

Workflow coverage is discovered via `.github/workflows/*.yml|yaml` (never a fixed
list). A workflow that resolves the full closure without `scripts/ci_install.sh`
is flagged unless it is in a bounded exception registry carrying a reason and an
owning PR. `queue-ownership-mutations.yml` is the one exception, **owned by PR
#304** (its fix is not duplicated here). `python-parity` and
`continuum-evidence` matrices are asserted equal to the interpreters the pin was
proven for (3.10/3.11/3.12).

### Mutation evidence

`scripts/ci_torch_mutations.py` now carries the ten behavioral mutants X1–X10 plus
four contract probes C1–C4:

```
claude-x-summary: planned=10 killed=10 survived=0 invalid=0
summary: planned=14 killed=14 survived=0 invalid=0
```

The tree is restored byte-identically (`git diff --exit-code` in the
`trust-mutations` job).

### M2 / M1 / M6 — board, PR description, integration branch

* **M2**: the board claim `task-omni-ci-cpu-torch-01` was rewritten through the
  official serializer; the superseded criteria are kept as
  `superseded_acceptance_criteria` / `superseded_evidence_required`, so the record
  states the `2.15.0+cpu` failure, why `2.14.1+cpu` was chosen, and what proves it.
* **M1**: the PR #308 description was corrected (exact pin, `--isolated` policy,
  behavioral/mutation evidence, accurate test counts, links to the run + SHA).
* **M6**: the integration branch `arena/omni-integration-304-307-308` board is
  **non-canonical** — it is not re-serialized by the board serializer (it uses
  `\uXXXX` escaping). Recorded here; the branch is provenance-only and was not
  regenerated.

### M5 — maintenance workflow

`maintenance.yml` is schedule/`workflow_dispatch`-only (not on `push`/`pull_request`),
so it does not run on a PR by design. Its **local restore-drill leg** was proven
end-to-end in an isolated scratch directory: build a real app-schema SQLite DB →
`_dump_sqlite` → `nexus_ai_agent.maintenance.restore --apply --expect-app-schema`
→ `ok: true`. The production `backup-db` leg needs owner R2/Neon secrets and is
**NOT_PROVEN** from here (it is not dispatched, to avoid any production side
effect).

### Remaining

* L2 (hash-pinning the torch wheel) — proposed as NEXT, not done (needs
  per-interpreter constraints and a compatibility review).
* L4 — the claim "2.14.1 is the newest release with a cp310 wheel" is only
  asserted as "the pinned build installs for cp310/cp311/cp312 in CI"; no
  upstream-dated source is cited.
