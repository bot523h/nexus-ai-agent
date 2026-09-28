# NEXUS AI Agent — System Architecture Map

**Date:** 2026-09-28 (UTC)
**Mapped from:** `main` @ `e5b326b2eaf691a638d030ad57acf1ce60016ef0`
**Mapped by:** session `arena/01a0e907-nexus-ai-agent`
**Method:** every entry below was derived by reading the tree on that SHA, not from
`docs/architecture/MODULE_MAP.md` and not from any prior report. Where a subsystem is
under a live lease it is marked `OWNER_LOCKED` and was mapped read-only.

Companion document: [`2026-09-28-master-forensic-hardening-v2.md`](2026-09-28-master-forensic-hardening-v2.md)

---

## 0. Shape of the system

```
247 Python source files · 52,275 lines · 190 test files · 43 packages
console entrypoint : nexus = nexus_ai_agent.cli:app   (typer, 4 @app.command)
HTTP entrypoint    : api/app.py -> FastAPI("NEXUS AI Dashboard") + api/dashboard.py APIRouter
Telegram entrypoint: bot/app.py -> build_application() -> application.run_polling() / run_webhook()
runtime            : python:3.12-slim (Dockerfile); declared floor >=3.10; CI parity 3.10/3.11/3.12
```

Three independent entrypoints converge on one graph:

```
   CLI ──┐
   API ──┼──> ToolRegistry ──> compile_graph(llm, checkpointer, long_term, registry) ──> LangGraph
   Bot ──┘                              │
                                        ├── planner_node → executor_node → _memory_writer
                                        └── persona cores: PhiAgent / QwenAgent / GemmaAgent
```

---

## 1. Subsystem table

For each subsystem: entrypoints · authority · resource ownership · failure paths · tests ·
lock. "Authority" means *the component that is allowed to decide*, not merely the one that
happens to be called.

### 1.1 CLI — `cli.py`
- **Entrypoint:** `nexus` console script; `typer` app, 4 commands (`run-bot`, `migrate`, `smoke`, + schema).
- **Authority:** composition root. Owns settings → LLM → tools → memory → checkpointer → graph → bot wiring.
- **State:** none of its own; delegates to `config/settings.py` (pydantic-settings).
- **Failure paths:** fail-closed on missing schema; `build_llm_provider` falls back through a documented chain.
- **Resource ownership:** sets `NEXUS_WORKSPACE_ROOT` for the tool substrate.
- **Tests:** `test_version_command.py` (version lockstep), `test_jobs_cli.py`.
- **Lock:** free.

### 1.2 API — `api/app.py`, `api/dashboard.py`
- **Entrypoint:** FastAPI app + one `APIRouter`.
- **Authority:** `api/app.py` for the app and the SSRF-guarded download path.
- **Failure paths:** `HTTPException`; download path routed through `core/ssrf_guard`.
- **Tests:** `test_dashboard_api.py`, `test_api_ssrf_download.py`.
- **Lock:** **`api/app.py` OWNER_LOCKED** (task-196); `dashboard.py` free.

### 1.3 Telegram / application — `bot/` (26 modules)
- **Entrypoint:** `bot/app.py::build_application`, polling or `bot/webhook.py`.
- **Authority:** `bot/app.py` (engine wiring), `bot/access_guard.py` (authorization),
  `bot/rate_limiter.py` (throttle), `bot/middleware.py`.
- **State:** conversation rows via `features/conversation_store.py`; cloud files via `storage/unified_cloud.py`.
- **Failure paths:** handler-level; surface modules return user-facing error strings.
- **Resource ownership:** PTB `Application` lifecycle; per-job workspaces (`creative/render_jobs.py`).
- **Tests:** `test_access_guard.py`, `test_rate_limiter.py`, `test_feature_wiring.py`, `test_surface_*`.
- **Lock:** **`bot/app.py`, `bot/handlers.py`, `bot/feature_handlers.py`, `bot/knowledge_handlers.py` OWNER_LOCKED** (task-196/197).

### 1.4 Command bus / capability registry — `creative/packs/registry.py`, `creative/job_registry.py`
- **Authority:** pack manifest + `registry.py`; a command must resolve to a registered capability.
- **Guards:** `tests/architecture/test_command_capability_boundary.py` enforces the boundary.
- **Lock:** free.

### 1.5 Studio core / preview / master — `creative/studio/`, `creative/packs/`
- **Authority:** Ed25519 **trust root** (`creative/packs/trust.py` + `trust_root.json` + `ed25519.py`).
  A pack's own claim is never its own authority — that is ADR 0006.
- **Failure paths:** fail-closed verification.
- **Tests:** `test_pack_trust_root.py` (RFC 8032 known-answer vectors), `test_pack_trust_boundary.py`,
  `scripts/pack_trust_mutations.py` → **12/12 killed** on this branch.
- **Lock:** free.

### 1.6 Executor / render lane — `creative/rendering/executor.py`, `creative/ffmpeg_executor.py`, `creative/slideshow/ffmpeg.py`
- **Authority:** argv-list `subprocess.run`, `shell=False`, allow-listed binary.
- **Resource ownership:** per-job workspace directory; `cleanup_workspace()` is documented best-effort and never raises.
- **Lock:** free.

### 1.7 Jobs / queue / scheduler — `jobs/`, `adapters/in_process_job_queue.py`, `features/request_queue.py`
- **Authority:** in-process instrumented job queue; `request_queue.py` owns LLM request admission,
  per-user round-robin within a priority tier, and per-user slot accounting.
- **Invariant (merged, proven):** a per-user slot is released **exactly once** on success, timeout,
  cancellation, provider failure and shutdown.
- **Known limitation (documented, by design):** strict priority across tiers can starve lower tiers.
- **Lock:** free at audit time (task-195 lease expired); the code is merged on `main` via PR #110.

### 1.8 Memory — `memory/` (long_term, short_term, eval), `features/ai_memory.py`
- **Authority:** sqlite-vec store; `long_term.py::store` is an **append-only** INSERT into
  `memories(thread_id, content, embedding)`.
- **Missing on `main`:** no TTL, no tombstone, no lineage, no trust metadata. Memory records
  are therefore **not** classified as untrusted data — which is exactly what task-200 exists to add.
- **Trust boundary:** no implicit authority grant was observed in the three `memory/` files,
  but that is an observation, not a proof; the adversarial injection corpus is the owner's deliverable.
- **Lock:** **`memory/`, `features/ai_memory.py` OWNER_LOCKED** (task-200 to 2026-09-29T14:32Z; task-197 for `ai_memory.py`).

### 1.9 LLM / providers — `llm/` (8 modules)
- **`src/nexus_ai_agent/llm/gateway/` DOES NOT EXIST on `main`.** Verified directly.
- **Consequence:** there is **no canonical LLM authority** on `main`. Egress map in §2.
- **Lock:** **entire `llm/` tree OWNER_LOCKED** (task-197, 44 paths, to 2026-09-30T13:54Z).

### 1.10 Storage — `storage/` (13 modules)
- **Authority:** SQLAlchemy 2.0.54 / SQLModel 0.0.42 for SQL; `psycopg[binary,pool]` 3.3.5 +
  `langgraph-checkpoint-postgres` for PG; `boto3` for the R2 blob tier.
- **Resource ownership:** engine/connection per store; `storage/db.py` is the shared lifecycle owner.
- **Failure paths:** several `except psycopg.Error: pass` and `except sqlite3.OperationalError: pass`
  handlers in the checkpoint adapters — reviewed statically only, not exercised.
- **Lock:** **`storage/db.py` OWNER_LOCKED** (task-196); the rest free.

### 1.11 Security boundary
| Component | Mechanism | Status |
|---|---|---|
| `tools/filesystem_policy.py` | `O_NOFOLLOW` + `dir_fd` on every op; rejects absolute paths, `..`, NUL, non-POSIX spellings, symlink components | strong; reviewed, no bypass found |
| `bot/safe_paths.py` | base-name collapse, control-char rejection, length cap, `is_relative_to` after `resolve()` | reviewed, no bypass found |
| `tools/system_shell.py` | **was** a deny-list of spellings; now a declared fail-closed flag grammar | **was `DEFECT` → fixed, `VERIFIED_CI`** |
| `core/ssrf_guard.py` | `validate_url`, `SafeAsyncTransport`, `is_public_ip`, private-IP refusal | reviewed only, `UNVERIFIED` |
| `infrastructure/observability/redaction.py` | log-boundary redaction | **was `DEFECT` (fail-open) → fixed, `VERIFIED_LOCAL`** |
| `creative/packs/trust.py` | Ed25519 trust root, fail-closed | `VERIFIED_LOCAL` (12/12 mutants) |
| `features/calculator.py` | AST whitelist; no attribute/subscript/string/lambda nodes; bounded length/nodes/depth; pow + factorial guards; magnitude check | reviewed and attacked, **no escape found** |

### 1.12 Observability — `observability/logging.py`, `infrastructure/observability/`
- **Authority:** structlog 26.1 with a redaction processor; `log_lifecycle_event` routes every
  field through `redact_fields`.
- **Invariant (now enforced):** a redaction boundary never emits input it has not vetted.
- **Lock:** free (the `security-boundary` *zone* declares these paths, but no live *claim* locks them).

### 1.13 Configuration / deployment
- `pyproject.toml` (`>=3.10`), `Dockerfile` (`python:3.12-slim`), `koyeb.yaml`, `docker-compose.yml`,
  `.env.example`, `config/settings.py`.
- **Drift:** **no lockfile exists** — `pip install -e ".[dev]"` resolves fresh each run, so CI and the
  image do not share pinned versions. The image runs 3.12 while 3.10 is only exercised in CI.
- **Lock:** **`Dockerfile`, `scripts/deploy_smoke.py` OWNER_LOCKED** (task-192).

### 1.14 CI/CD — `.github/workflows/ci.yml`, `maintenance.yml`
- 17 jobs: `lint`, `lint-fast`, `test`, `extras-matrix` ×4, `python-parity` ×3,
  `continuum-evidence` ×3, `trust-mutations`, **`shell-mutations`** (added by task-201),
  `migrate-postgres`, `release-lineage`.
- `maintenance.yml`: nightly `backup-db` (cron `17 3 * * *`) and weekly `housekeeping` (`23 4 * * 1`).
- **Lock:** `maintenance.yml` **OWNER_LOCKED** (task-192); `ci.yml` free.

### 1.15 Backup / recovery — `maintenance/backup.py`, `maintenance/restore.py`
- **`backup-db` has never succeeded**: 10 failures / 2 skipped / 0 successes across all 12 runs.
- **Lock:** **OWNER_LOCKED** (task-192, to 2026-09-28T19:30Z).

---

## 2. Authority graph — external egress

Every module that performs HTTP egress, and whether the URL is **fixed** (hardcoded host, no
SSRF surface) or **caller-supplied** (must be guarded).

| Module | Destination | URL origin | Guard |
|---|---|---|---|
| `features/ai_chat.py` | `generativelanguage.googleapis.com` | fixed | n/a |
| `features/summarizer.py` | user-supplied page URL | **caller-supplied** | **`ssrf_guard` ✔** |
| `integrations/external.py` | fetched external content | **caller-supplied** | **`ssrf_guard` ✔** |
| `api/app.py` | download endpoint | **caller-supplied** | **`ssrf_guard` ✔** |
| `features/tools.py` | `api.mymemory.translated.net` | fixed (text is a query param) | n/a |
| `creative/video_director.py` | `generativelanguage.googleapis.com` | fixed | n/a |
| `creative/image_gen/gemini_adapter.py` | `generativelanguage.googleapis.com` | fixed | n/a |
| `creative/image_gen/pollinations_adapter.py` | `image.pollinations.ai` | fixed (prompt in path) | n/a |
| `creative/slideshow/analysis.py` | `generativelanguage.googleapis.com` | fixed | n/a |
| `llm/local_server_provider.py` | configured local base URL | operator-configured | n/a |
| `storage/unified_cloud.py` | dropbox / pcloud / internxt APIs | fixed | n/a |
| `storage/providers/github_releases.py`, `huggingface.py` | `api.github.com`, `huggingface.co` | fixed | n/a |
| `agent/updater.py` | `git pull` + `pip install .` subprocesses | n/a | see note |

**Negative result, recorded honestly:** the SSRF guard is used by only 4 of 18 httpx modules,
which *looks* like a coverage gap. It is not: the guard is applied exactly where the URL is
caller-supplied, and every other module contacts a hardcoded host. No SSRF coverage defect was
found.

**Observation, not a defect claim:** `agent/updater.py` runs `git pull` and `pip install .` as
subprocesses — a self-update path. It is not reachable from any command or handler found in this
scan, and it was not exercised. Status `UNVERIFIED`.

---

## 3. LLM authority — the central architectural gap

```
                        on main @ e5b326b
   ┌──────────────────────────────────────────────────────────┐
   │  src/nexus_ai_agent/llm/gateway/   DOES NOT EXIST        │
   │  canonical authority               NONE                  │
   │  typed error taxonomy              ABSENT                │
   │  central retry/backoff/rate/circuit/fallback policy  ABSENT │
   │  bounded global + per-provider concurrency           ABSENT │
   │  per-request observability record                    ABSENT │
   │  truthful usage (provider-reported or UNKNOWN)       ABSENT │
   │  AST architecture guard                              ABSENT │
   └──────────────────────────────────────────────────────────┘

   Actual paths to a model on main:

   features/ai_chat.py:99                 GeminiEngine      -> generativelanguage REST
   features/summarizer.py:67              SummarizerEngine  -> generativelanguage REST
   creative/video_director.py:88          video director    -> generativelanguage REST
   creative/image_gen/gemini_adapter.py:58 image gen        -> generativelanguage REST   [pinned bypass]
   creative/slideshow/analysis.py:42      slideshow scoring -> generativelanguage REST   [pinned bypass]
   llm/litellm_provider.py:164            from litellm import Router  (direct construction)
```

Classification per the mission taxonomy:

| Site | Class |
|---|---|
| `features/ai_chat.py`, `features/summarizer.py`, `creative/video_director.py`, `llm/litellm_provider.py`, `llm/local_llama_cpp.py` | `OWNER_LOCKED` (task-197 migration targets) |
| `creative/image_gen/gemini_adapter.py`, `creative/slideshow/analysis.py` | `LEGITIMATE_TYPED_SCOPE` — documented pinned bypasses, ratcheted at `MAX_PINNED_BYPASSES=2`; not exclusive paths |

**No shadow authority was created by this session.** No provider was constructed and no LLM path
was edited; the whole tree is under a live lease.

---

## 4. Resource / lifecycle ownership

| Resource | Created by | Released by | Error / cancel / timeout | Status |
|---|---|---|---|---|
| Workspace dir fds | `filesystem_policy._parent_fd` | `finally: os.close(fd)` | correct on every path | `VERIFIED_LOCAL` (review) |
| Shell subprocess | `ShellTool.execute` → `asyncio.to_thread` | `timeout=10`, `capture_output` | now refused **before** execution | fixed, `VERIFIED_CI` |
| LLM queue user slots | `request_queue.py` | exactly-once on success/timeout/cancel/failure/shutdown | merged via PR #110 | `PROVEN_DONE` |
| Job workspaces | `creative/render_jobs.py` | `cleanup_workspace()`, best-effort, never raises | documented | `VERIFIED_LOCAL` (review) |
| Checkpoint DB connections | `storage/langgraph_checkpoint.py`, `checkpoint_pg_adapter.py` | two `except psycopg.Error: pass` sites | static read only | `UNVERIFIED` |
| Un-awaited coroutines | `lifecycle_recording._close_quietly` | `close()` in a guarded try | correct: close must not raise | `VERIFIED_LOCAL` (review) |
| Gateway permits / epochs / breaker | `llm/gateway/` — **absent on `main`** | n/a | n/a | `OWNER_LOCKED` |

**No new lock, task or shared mutable state was introduced by any fix in this programme.** The
three fixes are (a) a pre-execution argument validator, (b) a pure-function redaction rule,
(c) one added log call.

---

## 5. What is proven vs. merely present

| Layer | Proven by | Status |
|---|---|---|
| Test suite health | `pytest -m "not slow"` + CI `test` job on exact SHA | `VERIFIED_CI` |
| Python 3.10/3.11/3.12 parity | `python-parity` ×3 on exact SHA | `VERIFIED_CI` |
| Optional-extras install matrix | `extras-matrix` ×4 on exact SHA | `VERIFIED_CI` |
| Pack trust root | 12/12 mutants + RFC 8032 vectors | `VERIFIED_LOCAL` |
| Shell trust plane | 11/12 mutants + 100 tests + `shell-mutations` CI job | `VERIFIED_CI` |
| Redaction boundary | 53 tests, RED 16/37 before | `VERIFIED_LOCAL` |
| Calculator sandbox | reviewed and attacked, no escape | `VERIFIED_LOCAL` (review) |
| SSRF guard | reviewed only, never exercised here | `UNVERIFIED` |
| Runtime behaviour (bot / API / worker) | nothing was started | `UNVERIFIED` |
| Backup / restore | **never succeeded** | `DEFECT` + `OWNER_LOCKED` |
| Canonical LLM authority | absent from `main` | `OWNER_LOCKED` |

> **No `VERIFIED_RUNTIME` status is claimed anywhere in this map.** No bot, API server or
> worker process was started during this audit.
