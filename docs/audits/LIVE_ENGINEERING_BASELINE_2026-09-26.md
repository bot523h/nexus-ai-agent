# Live Engineering Baseline — 2026-09-26

This is a reproducible snapshot of the checkout used for this engineering
session. It deliberately separates code truth from open-PR claims.

## 1. Current main

| Field | Observed value |
|---|---|
| Repository | `bot523h/nexus-ai-agent` |
| Baseline SHA | `2351f099e2f789b80de05c6b382554b5cdcec0f3` |
| Branch at entry | `arena/01a0dc0b-nexus-ai-agent` |
| `origin/main` at entry | `2351f099e2f789b80de05c6b382554b5cdcec0f3` |
| `VERSION` / `pyproject.toml` | `3.13.0` / `3.13.0` |
| README version banner | **stale: `v3.12.0`** |
| Python authoring environment | CPython `3.11.2` |
| Declared runtime floor | `>=3.10` |
| Main exact-SHA CI | run `36176954176`, 12/12 jobs successful |

The README mismatch is a documentation/release-governance defect, not evidence
that the runtime is version `3.12.0`. It was not changed in this hardening
slice because an open prior PR owns broader release/documentation reconciliation.

## 2. Repository shape

Observed at entry:

- 241 Python source files under `src/`.
- 175 Python test files under `tests/`.
- 127 unit-test files, 25 architecture-test files, and 18 integration-test
  files.
- 1,543 test functions/coroutines found by the source-level count.
- 27 top-level package areas under `src/nexus_ai_agent/` (excluding generated
  `__pycache__`).
- No tracked database file, queue database, credentials, or production
  service instance in the checkout.

The repository is a modular monolith with Telegram/API surfaces, an async
orchestration graph, local SQLite and PostgreSQL adapters, a durable
in-process SQLite job queue, creative/rendering packs, LLM providers, and
optional cloud/blob integrations.

## 3. Runtime path map

```text
Telegram/API/CLI surface
  -> validation/auth/rate limiting (surface-dependent)
  -> feature or orchestration command
  -> ToolRegistry or Nagar CommandBus
  -> job queue / worker or synchronous pure operation
  -> filesystem/database/provider operation
  -> independent verification where the job lane declares an artifact
  -> notifier/result
```

Important separate paths found in code:

- `orchestration/graph.py` is the general chat/task graph.
- `creative/studio/bus.py` is the typed Nagar command path.
- `adapters/in_process_job_queue.py` owns durable job transitions and artifact
  verification for registered job types.
- `maintenance/backup.py` and `maintenance/housekeeping.py` are operator-only
  maintenance paths.
- `tools/files.py` and `tools/system_shell.py` are workspace capability paths.

## 4. Data and infrastructure truth

| Area | Code truth at entry | Evidence status |
|---|---|---|
| Primary application DB | `storage/db.py` resolves PostgreSQL from `NEXUS_DATABASE_URL`, otherwise configured SQLite | VERIFIED by existing resolver/migration tests |
| LangGraph checkpoint DB | Separate configured checkpoint path/adapter; intentional separate store | PARTIAL: separation is documented, whole lifecycle is not a single DB |
| Job state | SQLite sidecar from `worker.job_queue_db_path()` | VERIFIED by existing queue tests; capacity is still process-local |
| Temporary/artifact paths | Settings-driven paths, with multiple consumers | PARTIAL before this slice; filesystem boundary was not shared |
| Backups | SQLite online backup or `pg_dump`, R2 upload, local verification and byte round-trip | VERIFIED by existing backup tests; restore against a live target remains unverified |
| External providers | Telegram, LLMs, image/storage providers, optional R2/Postgres | UNVERIFIED without credentials/live services |
| Worker model | In-process asyncio tasks; recovery/fencing exists in queue code | PARTIAL: no external worker/broker or cross-process capacity proof |

## 5. Previous-session/open-PR reconciliation

The following work existed as **open PRs**, not as code on this baseline, and
was not silently merged or counted as delivered here:

| PR | Subject | Entry truth |
|---:|---|---|
| #83 | Nagar Operation Truth gate | OPEN; separate head, not in baseline |
| #84 | Engineering Constitution registration | OPEN; separate head, not in baseline |
| #86 | External-pack verification refusal | OPEN/superseded by later work, not in baseline |
| #87 | Studio Core capability surface | OPEN, not in baseline |
| #88 | Pack security root-cause + artifact proof | OPEN, not in baseline |
| #89 | Task-122 command wiring and moderation closure | OPEN, not in baseline |

Merged baseline work does include PR #82's CI extras matrix, Python parity,
and release-lineage gates. A claim in an open PR is not a claim about this
SHA; each item must be verified after merge and again at the resulting exact
SHA.

## 6. Baseline classification

### Exists and materially implemented

- CI lint/type/test/parity/optional-extra/migration/release-lineage jobs.
- Typed Nagar command/capability model and job lifecycle/failure semantics.
- Artifact verification for the registered creative, slideshow, story, and PDF
  lanes.
- SSRF/path/auth/consent controls covered by existing tests.
- Backup measurement, integrity checks, and remote byte-identical round-trip.

### Partially implemented or deliberately experimental

- Several creative packs remain experimental and/or have spec/state-only
  operations; documentation correctly records this in parts of the tree.
- Job retry eligibility is classified, but no durable retry scheduler exists.
- Backup restore evidence is local/structural for PostgreSQL, not a live
  restore drill in this checkout.
- General orchestration still contains a separate tool-execution lane from
  Nagar's canonical bus.

### Simulated, fake, or unverified

- Any operation explicitly marked `EXPERIMENTAL`, twin/spec-only, unavailable,
  or optional without its provider must not be described as fully implemented.
- External-provider success cannot be established by offline tests alone.
- Open PR bodies and historic audit claims are not runtime evidence for this
  baseline.

## 7. Baseline commands

```text
git rev-parse HEAD
git status --short --branch
gh pr list --state all
gh run view 36176954176
find src tests -name '*.py'
python scripts/agent_board.py check --files <changed-files> --branch <branch>
```

The authoring environment initially had no project dependencies installed;
verification for this session used a repository-local virtual environment and
is recorded in the hardening audit.
