# Master Forensic Hardening — zero-to-hundred report

**Date:** 2026-09-28 (UTC)
**Session:** `arena/01a0e907-nexus-ai-agent`
**Repository:** `bot523h/nexus-ai-agent`
**Baseline audited:** `main` @ `e5b326b2eaf691a638d030ad57acf1ce60016ef0`
**Work delivered at:** `89a2f81f1135f94a9a3dff606dc825a57ce10243` → PR **#119** (draft)
**Environment:** CPython 3.11.2, fresh venv, `pip install -e ".[dev]"` (exit 0), Ubuntu

> **Status taxonomy used throughout** (and only these):
> `PROVEN_DONE` · `VERIFIED_LOCAL` · `VERIFIED_CI` · `VERIFIED_RUNTIME` · `PARTIAL` ·
> `DEFECT` · `BLOCKED` · `OWNER_LOCKED` · `HANDOFF_REQUIRED` · `UNVERIFIED` ·
> `HISTORICAL_UNKNOWN` · `LOST_UNRECOVERABLE`
>
> Forbidden vocabulary (`probably fixed`, `looks good`, `should work`, `likely done`,
> `seems stable`) does not appear in this document.

---

## 1. Executive Summary

Everything in this report was re-measured from live Git, the GitHub API and a freshly
built environment. No prior report, PR description, comment, or historical CI result was
accepted as truth. Five historical claims were checked; **four corroborated, one falsified**.

**What actually existed.** `main` is at `e5b326b` and its CI is fully green (16/16 jobs on
run `36339890292`). The repository is 247 source files / 52,275 lines and 190 test files,
measuring **2915 passed / 30 skipped** (2945 collected) on that exact SHA.

**What was actually broken.** Three things were proven, not suspected:

1. **P0 security — an allowlisted shell command could read any file on the host.**
   `tools/system_shell.py` promised workspace-only paths and implemented that as an
   *enumeration of dangerous spellings*. `cat <outside>` was correctly refused while
   `date -f <outside>` was executed and echoed the file's content back. Nine distinct
   bypasses were reproduced on `e5b326b`.
2. **P1 — a failed durable-memory write was completely invisible.**
   `orchestration/graph.py::_memory_writer` swallowed every `LongTermMemory.store()`
   exception with a bare `except Exception: pass`: no log, no metric, no state marker.
3. **P0 operational — the scheduled database backup has never once succeeded.**
   Across **all 12** `maintenance` workflow runs, `backup-db` recorded **10 failures and
   2 skips, 0 successes**. The log is not retrievable, so the cause is `HISTORICAL_UNKNOWN`.

**What is structurally absent.** `src/nexus_ai_agent/llm/gateway/` **does not exist on
`main`**. The entire W2 "one canonical LLM authority" is unlanded; `main` has **five**
independent direct Gemini REST call sites and one direct `litellm.Router` construction.
PR #118 is CI-green on its own head, which is `VERIFIED_CI` — not `PROVEN_DONE`, because
the invariant it establishes is not in force on `main`.

**What was fixed.** Two root-cause repairs in free zones, each proven red-then-green with
an adversarial suite and a mutation harness: task-201 (shell trust plane) and task-202
(memory-write observability). Gates: **3008 passed / 30 skipped / 0 failed**, ruff clean,
ruff-format clean, mypy clean on 247 files, **11/11** shell mutants killed, **12/12** pack
mutants killed.

**What must not ship.** Nothing here ships alone. PR #119 is a **draft**, and the
`backup-db` gap is an open operational hole whose repair path is `OWNER_LOCKED`.

**Final verdict: `HARDENED_BUT_NOT_COMPLETE`** — see §29.

---

## 2. Live Baseline

Measured read-only before any edit.

```text
LIVE_BASELINE
  current branch   : arena/01a0e907-nexus-ai-agent
  HEAD             : e5b326b2eaf691a638d030ad57acf1ce60016ef0
  HEAD subject     : Merge pull request #110 from bot523h/arena/01a0e3b7-nexus-ai-agent
  HEAD author/date : arena-ai-coding-agent[bot] / Sun Sep 27 18:14:09 2026 +0000
  main             : e5b326b2eaf691a638d030ad57acf1ce60016ef0   (identical to HEAD)
  origin/main      : e5b326b2eaf691a638d030ad57acf1ce60016ef0
  merge-base       : e5b326b2eaf691a638d030ad57acf1ce60016ef0   (branch == main)
  dirty state      : clean (git status --porcelain -> 0 lines)
  unpushed commits : none (no upstream configured at session start)
  untracked files  : none
  tags             : none
  stashes          : none
  local refs       : main, arena/01a0e907-nexus-ai-agent, origin/HEAD, origin/main
  remote branches  : 100+ arena/* branches on origin
  now (UTC)        : 2026-09-28T17:20:43Z (measurement instant for every lease below)

WORKSTREAM_BASELINE
  src python files : 247        (52,275 lines)
  test files       : 190
  pytest -q -rs -m "not slow" on e5b326b, Py3.11.2 :
                     2915 passed, 30 skipped, 0 failed, 150.29s
  collected (same SHA, same selector)              : 2945
```

### Historical claims re-tested (Evidence > Memory)

| Claim (source) | Verdict | Live evidence |
|---|---|---|
| `main ≈ e5b326b` | **CORROBORATED** | `git rev-parse main` → `e5b326b2eaf…` |
| `W2 PR ≈ #118` | **CORROBORATED** | PR #118 title = "W2 golden hardening … (NOT VERIFIED)", head branch `arena/01a0e846-nexus-ai-agent` |
| `W2 candidate HEAD ≈ 7bd6e700` | **FALSIFIED as the head** | Live head is `d5a205f40a52fbefa49280b696cd7060e888eb99`. `7bd6e700` exists but is an **ancestor** (`git merge-base --is-ancestor 7bd6e700 d5a205f4` → true); it is the second-newest of 26 commits |
| `W2 verdict ≈ NOT VERIFIED` | **CORROBORATED** | PR #118 title states it; draft; `mergeStateStatus=UNSTABLE` |
| "2915-passed baseline on e5b326b" (PR #118 board note) | **CORROBORATED** | independently re-measured: 2915 passed / 30 skipped |

---

## 3. Git/GitHub Truth

```text
GOVERNANCE_BASELINE / GIT TRUTH
  remote              : https://github.com/bot523h/nexus-ai-agent.git
  gh identity         : arena-ai-coding-agent[bot] (GH_TOKEN), repository ops OK
  branch protection   : 403 "Resource not accessible by integration"
                        -> UNKNOWN from this token. NOT ASSERTED EITHER WAY.
  open PRs            : 36
  PR #118 (W2)        : OPEN, draft=true, mergeable=MERGEABLE,
                        mergeStateStatus=UNSTABLE, head d5a205f4
  PR #116 (W2 orig.)  : OPEN, draft=false, mergeStateStatus=UNSTABLE, head 9e795318
  PR #105 (shell fix) : OPEN, draft=false, mergeable=CONFLICTING,
                        mergeStateStatus=DIRTY, head 80013e32,
                        last commit 2026-09-27T11:45:14Z
  CONFLICTING PRs     : 20 of 36 open PRs are mergeable=CONFLICTING
```

**Observation, not a defect claim:** 20 of 36 open PRs are in conflict with `main`, and
`.agents/board.json` is edited by nearly all of them. The board is the single largest
merge-conflict surface in this repository. The board's own `merge_order_note` supplies the
resolution rule ("last assignment state wins"), so this is a known and governed condition —
but it is a real throughput cost and it is why §4 treats lease truth as the primary gate.

**Recovery:** no tags, no stashes, no unreachable objects were needed. Every artifact
referenced in this report was reachable by SHA. Nothing was classified
`LOST_UNRECOVERABLE` in the Git domain.

---

## 4. Governance / Lease Truth

This is the decisive section: it determined what this session was allowed to edit.

The board on `main` is **stale** (`updated_at = 2026-09-27T16:54:20Z`) and does **not**
contain task-197 at all. Live lease truth therefore had to be reconstructed as the **union
across `main` plus the five actively-updated PR boards** (#113, #115, #116, #117, #118).
Reading only `main` would have produced a dangerously wrong answer.

```text
GOVERNANCE_BASELINE — LIVE LEASES at 2026-09-28T17:20:43Z
  lease_ttl_hours (protocol) : 24 default; per-claim override honoured

  task-197-w2-global-llm-gateway   zone llm-gateway-authority
      branch arena/01a0e846 (PR #118)   claimed 2026-09-28T13:54:02Z  ttl 48
      EXPIRES 2026-09-30T13:54:02Z      LIVE=True   44 exclusive paths
      -> OWNER_LOCKED

  task-200-w3-memory-trust-boundary  zone task-200-w3-memory-trust-boundary
      branch arena/01a0e742 (PR #117)   claimed 2026-09-28T14:32:38Z  ttl 24
      EXPIRES 2026-09-29T14:32:38Z      LIVE=True    6 exclusive paths
      -> OWNER_LOCKED   (this is why the Memory audit in §11 is read-only)

  task-196-closeout-w1               zone runtime-composition-w1
      branch arena/01a0e442 (PR #113)   claimed 2026-09-27T21:10:00Z  ttl 24
      EXPIRES 2026-09-28T21:10:00Z      LIVE=True   13 exclusive paths  gates_owner
      -> OWNER_LOCKED

  task-164                           zone ci-quality
      branch arena/01a0dee1 (PR #96)    claimed 2026-09-26T19:15:19Z  ttl 48
      EXPIRES 2026-09-28T19:15:19Z      LIVE=True    0 exclusive paths
      -> OWNER_LOCKED

  task-192-production-dr-truth       zone production-dr-deploy
      branch arena/01a0dee1 (PR #96)    claimed 2026-09-26T19:30:00Z  ttl 48
      EXPIRES 2026-09-28T19:30:00Z      LIVE=True   13 exclusive paths
      -> OWNER_LOCKED   (this is why the backup repair in §18 is not attempted)

  TOTAL DISTINCT LOCKED PATHS : 75
  STALE LEASES GARBAGE-COLLECTED BY THE REPO'S OWN TOOL (agent_board.py claim
  invokes gc_expired): task-154, task-184, task-185, task-195-salvage — all four
  had already passed their TTL (latest expiry 2026-09-28T16:54:20Z).

  gates_owner at measurement instant: NONE LIVE. task-195-salvage's gates lease
  expired at 2026-09-28T16:54:20Z, 26 minutes before measurement.
```

**Every path this session changed was verified free** by the repository's own gate:

```
$ python scripts/agent_board.py check --files <changed files> --branch arena/01a0e907-nexus-ai-agent
no overlap — safe to proceed.          (exit 0, run before each claim)
```

Two claims were registered with zones, acceptance criteria, evidence requirements and
exclusive paths, then taken with `agent_board.py claim`:

* `task-201-shell-trust-plane-grammar` (zone `shell-trust-plane`, 5 paths)
* `task-202-memory-write-observability` (zone `conversation-memory-observability`, 2 paths)

A `takeover_log` entry records the PR #105 salvage with its full evidence chain. Board
schema tests pass: `tests/unit/test_agent_board*.py` → **34 passed**.

**Governance verdict:** no locked workstream was edited, merged, closed, rebased or
force-pushed. Five live leases were audited read-only.

---

## 5. Workstream Matrix

| Workstream | Zone | Owner branch | PR | Lease | This session | Status |
|---|---|---|---|---|---|---|
| W2 Global LLM Gateway | `llm-gateway-authority` | `arena/01a0e846` | #118 (draft, UNSTABLE) | LIVE → 09-30T13:54Z | read-only audit | `OWNER_LOCKED` |
| W2 original | `llm-gateway-authority` | `arena/01a0e4f6` | #116 (UNSTABLE) | superseded by #118 | read-only | `OWNER_LOCKED` |
| W3 Memory trust boundary | `task-200-…` | `arena/01a0e742` | #117 (CLEAN) | LIVE → 09-29T14:32Z | read-only audit | `OWNER_LOCKED` |
| W1 runtime composition | `runtime-composition-w1` | `arena/01a0e442` | #113 | LIVE → 09-28T21:10Z | untouched | `OWNER_LOCKED` |
| Production / DR / deploy | `production-dr-deploy` | `arena/01a0dee1` | #96 (CONFLICTING) | LIVE → 09-28T19:30Z | read-only audit | `OWNER_LOCKED` |
| CI quality | `ci-quality` | `arena/01a0dee1` | #96 | LIVE → 09-28T19:15Z | untouched | `OWNER_LOCKED` |
| **Shell trust plane** | `shell-trust-plane` | **`arena/01a0e907`** | **#119** | free at audit | **root-cause fix** | **`VERIFIED_LOCAL`** |
| **Memory-write observability** | `conversation-memory-observability` | **`arena/01a0e907`** | **#119** | free at audit | **root-cause fix** | **`VERIFIED_LOCAL`** |
| Queue lifecycle (task-195) | `llm-request-queue-lifecycle` | `arena/01a0e3b7` | merged #110 | expired | read-only | `PROVEN_DONE` (merged to `main`, CI green) |

---

## 6. System Architecture Map

Measured from the tree on `e5b326b`; 43 top-level packages under `src/nexus_ai_agent/`.

| Subsystem | Entrypoints | Authority | Tests | Lock |
|---|---|---|---|---|
| CLI | `cli.py` (typer `app`) | composition root: settings → LLM → tools → memory → graph → bot | `test_version_command`, `test_jobs_cli` | free (untouched) |
| API | `api/app.py`, `api/dashboard.py` | FastAPI; `app.py` locked | `test_dashboard_api`, `test_api_ssrf_download` | `app.py` **LOCKED** |
| Bot / Telegram | `bot/app.py`, `bot/handlers.py`, `bot/webhook.py`, 20 surface modules | PTB 22.8; polling or webhook | broad | `app.py`/`handlers.py` **LOCKED** |
| Command bus / capability | `bot/*_handlers.py`, `creative/packs/registry.py` | registry + pack manifest | `test_command_capability_boundary` | mixed |
| Studio / packs | `creative/studio/`, `creative/packs/` (trust root, ed25519, verify) | Ed25519 trust root, fail-closed | `test_pack_trust_*` (12 mutants) | free |
| Preview / master / render | `creative/rendering/executor.py`, `creative/ffmpeg_executor.py`, `creative/render_jobs.py` | subprocess argv, `shell=False` | `test_render_*` | free |
| Jobs / queue / scheduler | `jobs/`, `adapters/in_process_job_queue.py`, `features/request_queue.py` | in-process instrumented queue | `test_in_process_job_queue` | queue lease expired |
| LLM provider layer | `llm/` (8 modules) | **no canonical authority on `main`** | `test_litellm_provider`, … | **LOCKED** |
| Memory | `memory/` (long_term, short_term, eval), `features/ai_memory.py` | sqlite-vec; untrusted | `test_graph_memory`, … | `memory/` **LOCKED** |
| Storage | `storage/` (13 modules incl. R2, postgres, golden) | SQLAlchemy 2 / psycopg3 / boto3 | broad | `db.py` **LOCKED** |
| Security boundary | `core/ssrf_guard.py`, `core/http_client.py`, `bot/access_guard.py`, `tools/filesystem_policy.py` | O_NOFOLLOW + `dir_fd` containment | `test_filesystem_boundary` | partly locked |
| Observability | `observability/logging.py`, `infrastructure/observability/` (structured, redaction) | structlog 26.1 + redaction at boundary | `test_observability` | `infrastructure/observability/` **LOCKED** |
| Configuration | `config/settings.py`, `.env.example`, `pyproject.toml` | pydantic-settings | broad | free |
| Deployment | `Dockerfile`, `koyeb.yaml`, `docker-compose.yml`, `scripts/deploy_smoke.py` | python:3.12-slim | `test_deploy_smoke_readyz` | **LOCKED** |
| CI/CD | `.github/workflows/ci.yml` (17 jobs), `maintenance.yml` | 3.10/3.11/3.12 parity + extras matrix | `test_ci_*_parity` | free |
| Backup / recovery | `maintenance/backup.py`, `maintenance/restore.py` | R2 + pg_dump/sqlite | `test_backup_dr_truth`, `test_restore_drill` | **LOCKED** |

---

## 7. Authority Graph

### 7.1 LLM — the canonical-authority question (mission §6, §7)

**`src/nexus_ai_agent/llm/gateway/` does not exist on `main`.** Verified directly:

```
$ ls src/nexus_ai_agent/llm/gateway/
ls: cannot access 'src/nexus_ai_agent/llm/gateway/': No such file or directory
```

`main` therefore has **no canonical LLM execution authority**. Every path to a model is a
raw provider path. Measured call sites reaching an external model directly:

```text
$ grep -rn "generativelanguage" src/ --include=*.py
  features/ai_chat.py:99              BASE_URL = ".../v1beta"        (GeminiEngine)
  features/summarizer.py:67           base_url = ".../v1beta"
  creative/video_director.py:88       ".../v1beta/models/"
  creative/image_gen/gemini_adapter.py:58   ".../v1beta/models/"
  creative/slideshow/analysis.py:42   GEMINI_ENDPOINT = ".../{model}:generateContent"

$ grep -rn "from litellm|import litellm" src/ --include=*.py
  llm/litellm_provider.py:164         from litellm import Router     (direct construction)
```

**Classification of each** (mission §7 taxonomy):

| Site | Class | Note |
|---|---|---|
| `features/ai_chat.py` (GeminiEngine) | `OWNER_LOCKED` | task-197 exclusive path; W2's migration target |
| `features/summarizer.py` | `OWNER_LOCKED` | task-197 exclusive path |
| `creative/video_director.py` | `OWNER_LOCKED` | task-197 exclusive path |
| `llm/litellm_provider.py` (Router) | `OWNER_LOCKED` | task-197 exclusive path |
| `creative/image_gen/gemini_adapter.py` | `LEGITIMATE_TYPED_SCOPE` *(documented exception)* | PR #118 board records it as a pinned bypass, ratcheted by `MAX_PINNED_BYPASSES=2`; **not** an exclusive path |
| `creative/slideshow/analysis.py` | `LEGITIMATE_TYPED_SCOPE` *(documented exception)* | same |
| `llm/local_llama_cpp.py` | `OWNER_LOCKED` | task-197 exclusive path |

**No shadow authority was created by this session.** No provider was constructed, no
gateway was bypassed, no LLM path was edited — every LLM path is under a live lease.

**The contract the mission asks for** — `ONE CANONICAL AUTHORITY MODEL + EXPLICIT TYPED
SCOPES + NO PRIVATE SHADOW AUTHORITY` — is **not satisfiable on `main` today**, because the
canonical authority does not exist there. It exists only on PR #118's branch. Status:
`OWNER_LOCKED` / `HANDOFF_REQUIRED`.

### 7.2 Shell trust plane — the authority this session *did* repair

```text
Caller: agent tool loop -> ToolRegistry -> ShellTool.execute(inputs)
  Boundary:   shlex.split -> ALLOWLIST membership -> _validate_args (flag grammar)
  Authority:  WorkspaceFilesystem  (tools/filesystem_policy.py)
  Policy:     declared per-command flag grammar; undeclared flag => REFUSED
  Executor:   subprocess.run(argv, shell=False, cwd=workspace_root)
  External:   GNU coreutils / grep / findutils binaries
```

`tools/filesystem_policy.py` is genuinely strong and was **not** the defect: it uses
`O_NOFOLLOW` + `dir_fd` for every mutation and rejects absolute paths, `..`, NUL bytes,
non-POSIX spellings and symlink components. Its own docstring states the invariant:
*"makes the operation use the same physical path that was validated."* The defect was that
`ShellTool` validated a vector and then handed the **original raw tokens** to a real binary
whose flags it had not enumerated.

---

## 8. Security Audit

| # | Surface | Finding | Status |
|---|---|---|---|
| S-1 | Restricted shell (`tools/system_shell.py`) | **Arbitrary host-file read** via `date -f`/`--file=`, `date -r`, `grep --file=`/`-fFILE`/`--exclude-from=`; outside-secret disclosure via `grep -R`; outside-path disclosure via `find -L` | **`DEFECT` → fixed, `VERIFIED_LOCAL`** (§21, D-001) |
| S-2 | Path traversal (`bot/safe_paths.py`) | Base-name collapse, control-char rejection, length cap, `is_relative_to` after `resolve()` — reviewed, no bypass found | `VERIFIED_LOCAL` (review only) |
| S-3 | Filesystem boundary (`tools/filesystem_policy.py`) | `O_NOFOLLOW` + `dir_fd` on every op; symlink-component walk; fail-closed on non-POSIX | `VERIFIED_LOCAL` (review only) |
| S-4 | Archive extraction (zip-slip) | **No `tarfile`/`zipfile`/`extractall` anywhere in `src/`** — the attack class is absent | `PROVEN_DONE` (absence proven by grep) |
| S-5 | Command injection | **No `shell=True` and no `os.system` anywhere in `src/`**; all 16 subprocess sites pass argv lists. Now additionally pinned by `test_subprocess_is_never_run_through_a_shell` | `VERIFIED_LOCAL` |
| S-6 | SSRF (`core/ssrf_guard.py`, `core/http_client.py`) | Reviewed only; `httpx==0.28.1` is pinned because the guard uses a private httpx API — a real upgrade hazard | `UNVERIFIED` (not exercised this session) |
| S-7 | Log redaction (`infrastructure/observability/redaction.py`) | Boundary redaction covers Bearer, Telegram tokens, Google API keys, query secrets, userinfo URLs, and secret-ish **field names**. One `except ValueError: pass` at line 63 returns the raw URL on a `urlsplit` failure, which could leave userinfo in a log line. Path is **LOCKED** (`infrastructure/observability/` under task-164's zone) | `OWNER_LOCKED` + `HANDOFF_REQUIRED` |
| S-8 | Memory as untrusted input (prompt injection) | `src/nexus_ai_agent/memory/` is under a **live** lease; audited read-only. No implicit authority grant was observed in the three files, but a full adversarial injection corpus is exactly what task-200 exists to build | `OWNER_LOCKED` |
| S-9 | Pack trust root | Ed25519 trust root, fail-closed, RFC 8032 test vectors present; `pack_trust_mutations.py` **12/12 killed** on this branch | `VERIFIED_LOCAL` |

---

## 9. Runtime Audit

Not exercised: no bot, API server or worker was started in this session. Every runtime
claim below is a **static** reading, and is labelled accordingly.

| Item | Evidence | Status |
|---|---|---|
| Composition root | `cli.py` builds settings → LLM → ToolRegistry → LongTermMemory → checkpointer → graph → bot | `UNVERIFIED` (not run) |
| `extras-matrix (core)` proves the CLI builds on a bare install | CI leg on `main` run `36339890292` → success | `VERIFIED_CI` |
| Startup bypass of any gateway | N/A — no gateway exists on `main` | `PROVEN_DONE` (absence) |
| Readyz / identity probes | `test_readyz_identity.py`, `test_deploy_smoke_readyz.py` — both under live leases | `OWNER_LOCKED` |

**No `VERIFIED_RUNTIME` status is claimed anywhere in this report.**

---

## 10. Lifecycle / Concurrency Audit

Static classification of resource ownership on `main`; the deep lifecycle work is what
W1/W2/W3 exist to do and is leased.

| Resource | Acquire | Release | Error/cancel path | Status |
|---|---|---|---|---|
| Workspace dir fds (`filesystem_policy._parent_fd`) | `os.open(dir_fd=…)` | `finally: os.close(fd)` | correct: `finally` on every path | `VERIFIED_LOCAL` (review) |
| ShellTool subprocess | `asyncio.to_thread(subprocess.run)` | `timeout=10`, `capture_output` | `TimeoutExpired` and `OSError` handled; **the vector is now refused before execution** | fixed, `VERIFIED_LOCAL` |
| LLM queue pending slots | `features/request_queue.py` | task-195-salvage: exactly-once release on success/timeout/cancel/failure/shutdown | merged via PR #110, CI green | `PROVEN_DONE` (merged) |
| Gateway permits / epochs / breaker | `llm/gateway/` — **absent on `main`** | n/a | n/a | `OWNER_LOCKED` |
| Checkpoint DB connections | `storage/langgraph_checkpoint.py`, `checkpoint_pg_adapter.py` | `except psycopg.Error: pass` at two sites | static read only | `UNVERIFIED` |
| Background tasks / jobs | `adapters/in_process_job_queue.py`, `jobs/` | instrumented queue | static read only | `UNVERIFIED` |

**No new race was introduced by this session.** The two changes are (a) a pure
pre-execution validator and (b) one added log call — neither adds a lock, a task, or shared
mutable state.

---

## 11. Memory Audit

`src/nexus_ai_agent/memory/` (long_term.py, short_term.py, eval.py) and
`features/ai_memory.py` are under a **live lease** (`task-200-w3-memory-trust-boundary`,
`arena/01a0e742`, expires 2026-09-29T14:32:38Z). Audited **read-only**; nothing edited.

| Property | Observation | Status |
|---|---|---|
| Memory creates no implicit authority | Not observed in the three `memory/` files; no grant/permission concept present | `OWNER_LOCKED` (task-200 owns the proof) |
| Structured content treated as untrusted | No trust metadata on `main`; that is precisely task-200's scope ("memory record trust metadata + no-implicit-authority rendering + capability-grant rejection") | `OWNER_LOCKED` |
| Write / correction / forget / TTL / tombstone / lineage | `long_term.py::store` is an append-only INSERT into `memories(thread_id, content, embedding)`; no TTL, tombstone or lineage column on `main` | `DEFECT` (design gap) → `OWNER_LOCKED` |
| Durable write failure observability | `orchestration/graph.py::_memory_writer` (a **free** path) swallowed every exception | **`DEFECT` → fixed, `VERIFIED_LOCAL`** (D-002) |

**Honest scope note:** this session audited the *orchestration* side of memory because that
path was free. The *storage* side of the trust boundary is another agent's live work and
was not touched, re-implemented, or duplicated.

---

## 12. LLM / Gateway Audit

```text
main @ e5b326b
  src/nexus_ai_agent/llm/gateway/          DOES NOT EXIST
  direct Gemini REST call sites            5
  direct litellm.Router construction       1
  canonical LLM authority                  NONE
  typed error taxonomy                     ABSENT
  central retry / backoff / rate / circuit / fallback policy   ABSENT
  bounded global + per-provider concurrency                    ABSENT
  observability record per request                             ABSENT
  truthful usage (provider-reported or UNKNOWN)                ABSENT
  architecture guard (AST)                                     ABSENT on main
```

PR #118 (`d5a205f4`, 26 commits ahead of `main`) supplies all of the above on its own
branch and is **CI-green on that exact head**: 32 check-runs (push + pull_request × 16
jobs), every one `completed / success`, including `test`, `lint`, `python-parity`
3.10/3.11/3.12, `extras-matrix` core/pdf/speech/translate, `trust-mutations`,
`migrate-postgres`, `release-lineage`, `continuum-evidence`.

**That is `VERIFIED_CI`, not `PROVEN_DONE`**, for three reasons this report will not paper
over:

1. The PR is a **draft** whose own title says **"NOT VERIFIED"**, and `mergeStateStatus` is
   `UNSTABLE`.
2. `main` does not contain the gateway, so the invariant *"one path to a model"* is **not in
   force in production**.
3. The lease holder's own board note records a `gates_owner` deferral and two **pinned
   bypasses** (`creative/image_gen/gemini_adapter.py`, `creative/slideshow/analysis.py`),
   ratcheted at `MAX_PINNED_BYPASSES=2`. RPM/RPD accounting for the shared Gemini quota is
   therefore still split. That is a *documented* typed scope, not a hidden shadow authority —
   but it is a real limitation and it is the owner's to close.

W2 candidate forensics requested by the mission (permit double-release, retry
re-authorization, installer race, stale authority, event-loop ownership, PID ownership,
exception sanitization, native workers) were **read-only** and are **not adjudicated here**
— adjudicating them would require editing or benchmarking inside a quarantined zone.
Status: `OWNER_LOCKED` / `HANDOFF_REQUIRED`.

---

## 13. Studio / Command / Executor Audit

| Item | Evidence | Status |
|---|---|---|
| Pack trust root | `creative/packs/trust.py` + `trust_root.json` + `ed25519.py`; RFC 8032 vectors tested; **12/12 mutants killed** on this branch | `VERIFIED_LOCAL` |
| Command ↔ capability contract | `tests/architecture/test_command_capability_boundary.py` passes; `system_shell` is a declared member | `VERIFIED_LOCAL` |
| Executor safety | `creative/rendering/executor.py:77` and `creative/ffmpeg_executor.py:50` both pass argv lists, `shell=False` | `VERIFIED_LOCAL` (review) |
| Studio core / preview / master boundary | PR #87 is **CONFLICTING** and its lease is expired; no live owner | `UNVERIFIED` |

---

## 14. Queue / Worker / Scheduler Audit

| Item | Evidence | Status |
|---|---|---|
| In-process request queue | task-195-salvage merged via PR #110 → `main`; CI green on `e5b326b` | `PROVEN_DONE` |
| Python 3.10 cancellation defect | Root-caused and recorded in the board: `asyncio.wait_for` raises `asyncio.TimeoutError` on 3.10, which is **not** a subclass of builtin `TimeoutError` (aliased only in 3.11+). Fix is on `main`; `python-parity (3.10)` is green on run `36339890292` | `VERIFIED_CI` |
| Job queue instrumentation | `adapters/in_process_job_queue.py`; not exercised this session | `UNVERIFIED` |
| Scheduler fairness | Strict priority across tiers with documented lower-tier starvation (per the task-195 scope text) — an accepted, documented limitation | `PARTIAL` (by design) |

---

## 15. Test Audit

```text
on e5b326b (Py3.11.2, pytest 9.1.1)
  pytest -q -rs -m "not slow" : 2915 passed, 30 skipped, 0 failed, 150.29s
  collected                   : 2945
after this session (89a2f81)  : 3008 passed, 30 skipped, 0 failed, 149.83s
  collected                   : 3038
```

**Skip accounting (all 30 are intentional and visible via `-rs`):** 22 require PostgreSQL
(`NEXUS_DATABASE_URL` unset), 8 are optional-extras contracts deliberately delegated to the
`extras-matrix` CI legs. No skip was added, widened or used to obtain green.

**Weak-assertion sweep — an honest negative result.** An AST sweep for test functions with
no `assert` and no `pytest.raises` produced **20 candidates**. Two were inspected in
detail:

* `test_capability_lifecycle.py::test_available_passes_without_opt_in` — a *must-not-raise*
  contract; correct as written.
* `test_pack_trust_root.py::test_rfc8032_vector_one` — verifies a known-answer Ed25519
  vector; correct as written.

Both are legitimate. The remaining **18 were not individually reviewed** and are recorded as
`UNVERIFIED` — **not** as defects. Reporting them as findings would have been inflation.

**A weak assertion was found in this session's own new test and removed.** The first draft of
`tests/unit/test_shell_workspace_escape.py` asserted only `success is False` for the `grep`
bypasses. That passes against the *unfixed* validator for the wrong reason, because `grep`
exits non-zero on its own. Measured: with that assertion the suite reported **4 failed / 5
passed**; after replacing the oracle with a `subprocess.run` tripwire it reported
**14 failed / 9 passed**. The stronger oracle is the one that shipped.

**No false green was produced:** no assertion weakened, no test deleted, no skip added, no
timeout raised, no error swallowed, no guard removed, no mutation harness bypassed.

---

## 16. CI Audit

```text
CI_BASELINE — main @ e5b326b, CI run 36339890292 (2026-09-27T18:14:13Z): SUCCESS
  lint · test · lint-fast · release-lineage · trust-mutations · migrate-postgres
  python-parity 3.10/3.11/3.12 · extras-matrix core/pdf/speech/translate
  continuum-evidence 3.10/3.11/3.12
  => 16/16 jobs success

CI on PR #118 head d5a205f4: 32/32 check-runs completed/success (push + pull_request)
  => VERIFIED_CI for that branch only; main does not contain the code.

CI on this session's head 89a2f81 (PR #119): see §28 for the live table.
  New job added by this change: shell-mutations (restricted-shell flag grammar).
```

### The `maintenance` workflow — a real, proven CI failure

```text
CI FAILURE RECORD
  workflow  : maintenance
  SHA       : e5b326b2eaf691a638d030ad57acf1ce60016ef0
  run       : 36405852557  (2026-09-28T09:48:43Z, event=schedule)
  job       : backup-db            conclusion=failure
  job id    : 108874276321
  failing step : step 5, `nexus maintenance backup`
                 (steps 1-4 checkout/setup-python/pip install all success)
  artifact  : none uploaded by this workflow
  log       : NOT RETRIEVABLE — 4 attempts.
              `gh run view --log-failed` -> results-receiver...: EOF
              `gh api .../jobs/108874276321/logs` -> empty body
  cause     : HISTORICAL_UNKNOWN   (deliberately NOT guessed)
```

Documented hypothesis, clearly separated from fact: `maintenance.yml` states in its own
comment that the command *"Exits non-zero when R2 is not configured so a silently-skipped
backup is visible, not hidden."* That is a plausible mechanism, **but it is not proven**,
because the log cannot be read. It is recorded as a hypothesis, not a root cause.

### Parity / flake forensics

The board records a `python-parity (3.10)` failure that was **root-caused**, not flaked:
`asyncio.Task.cancelling()` (3.11+) and the `asyncio.TimeoutError` vs builtin `TimeoutError`
aliasing change in 3.11. Both were reproduced locally on CPython 3.10 and both are green on
`main`. Status `VERIFIED_CI`.

The PR #118 board note also records four legs failing at `pip install -e ".[dev]"` on
`a18c65e` while other legs in the same run passed, and calls it "runner flake". That
attribution is the **owner's**, was not re-measured here, and is recorded as
`HISTORICAL_UNKNOWN`.

---

## 17. Deployment Audit

| Item | Measured | Status |
|---|---|---|
| `pyproject` `requires-python` | `>=3.10` | — |
| CI parity matrix | 3.10 / 3.11 / 3.12 | — |
| CI lint + test interpreter | 3.12 | — |
| `Dockerfile` base | `python:3.12-slim` | — |
| Local audit interpreter | 3.11.2 | — |
| Version lockstep | `VERSION` 3.13.0 == `pyproject` 3.13.0 == latest CHANGELOG `## [3.13.0]` | `VERIFIED_LOCAL` |

**Drift finding (observation, not a defect):** the production image runs **3.12** while the
declared floor is **3.10**. Python 3.10 is therefore exercised **only in CI, never in
production**. This is exactly the class of gap that produced the 3.10 `asyncio` defect.
No lockfile exists in the repository (`pip install -e ".[dev]"` resolves fresh each time),
so **CI runs and the Docker image do not share pinned dependency versions** — a genuine
reproducibility gap. Status: `DEFECT` (config drift), repair not attempted because
`Dockerfile` and `scripts/deploy_smoke.py` are under a live lease → `OWNER_LOCKED`.

---

## 18. Backup / Recovery Audit

**This is the most serious open finding in this report.**

```text
BACKUP / RECOVERY — measured across ALL maintenance workflow runs (12 total)

  backup-db conclusions:  failure = 10    skipped = 2    SUCCESS = 0

  run 36413210602  2026-09-28T11:02:04Z  backup-db=skipped   (housekeeping success)
  run 36405852557  2026-09-28T09:48:43Z  backup-db=FAILURE   <- step 5
  run 36308867958  2026-09-27T09:16:59Z  backup-db=FAILURE
  run 36230324343  2026-09-26T08:37:18Z  backup-db=FAILURE
  run 36182675224  2026-09-25T19:56:05Z  backup-db=FAILURE   (event=push)
  run 36181105635  2026-09-25T19:40:23Z  backup-db=FAILURE   (event=push)
  run 36115146739  2026-09-25T08:50:51Z  maintenance=failure
  run 35975483186  2026-09-24T08:28:51Z  maintenance=failure
  run 35705502209  ...                   backup-db=FAILURE
  run 35586584833  ...                   backup-db=skipped
  run 35580701817  ...                   backup-db=FAILURE
```

| Mission question | Answer | Status |
|---|---|---|
| Backup exists? | **NO successful backup exists.** 10 failures, 0 successes. | `DEFECT` |
| Backup valid? | Unanswerable — no artifact was ever produced | `HISTORICAL_UNKNOWN` |
| Restore succeeds? | Not demonstrated. `test_restore_drill.py` exists but is a **test**, and the file is under a live lease | `UNVERIFIED` |
| Schedule works? | The cron fires (12 runs observed) but the job fails | `DEFECT` |
| Secrets available? | `NEXUS_DATABASE_URL`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` are read from repo secrets; their presence was **not verified** from this token | `UNVERIFIED` |
| Storage durable? | Cloudflare R2 configured in code; not reachable from this audit | `UNVERIFIED` |
| Version compatible? | Not evaluated | `UNVERIFIED` |
| Verification meaningful? | The job *is* designed to fail loudly rather than skip silently — that part is correct and is why this is visible at all | `VERIFIED_CI` (design) |

**Why this session did not fix it.** `src/nexus_ai_agent/maintenance/backup.py`,
`maintenance/restore.py`, `maintenance/failures.py`, `scripts/deploy_smoke.py`,
`docs/ops/DISASTER_RECOVERY_RUNBOOK.md` and `.github/workflows/maintenance.yml` are **all**
inside the live `task-192-production-dr-truth` lease (`arena/01a0dee1`, expires
2026-09-28T19:30:00Z). Editing them would be a governance violation.

Status: **`DEFECT` + `OWNER_LOCKED` + `HANDOFF_REQUIRED`** — severity **Critical**,
because it means the project has **no proven recovery path**.

> Note the interaction with §17: PR #66's title claims "verified backups (D-0010)". Against
> live CI truth that claim does not hold for the *scheduled production* backup. The test
> suite is green; the operational path has never succeeded. This is precisely the
> "backup exists ≠ recovery proven" distinction, and here even "backup exists" is false.

---

## 19. Documentation Drift

| Item | Measured | Status |
|---|---|---|
| Docs integrity gate | `tests/unit/test_docs_integrity.py` → **59 passed** after this change | `VERIFIED_LOCAL` |
| ADR index completeness | Enforced by `test_adr_index_lists_exactly_the_existing_records`; ADR 0007 added to both `adr/README.md` and `docs/README.md` | `VERIFIED_LOCAL` |
| ADR numbering | `main` had 0001–0006. PR #105's ADR was numbered 0011; it is **renumbered to 0007** here because 0007–0010 do not exist on this lineage. ADR rule 1 ("never renumbered") is respected: no published ADR was renumbered, a never-published draft was placed correctly | `VERIFIED_LOCAL` |
| Stale root-level reports | 8 dated root `*.md` reports (`MASTER_ENGINEERING_TRUTH_REPORT_2026-09-27.md`, `OWNER_WIDE_FORENSIC_AUDIT_2026-09-26.md`, `LIVE_ENGINEERING_BASELINE_2026-09-26.md`, …) are point-in-time snapshots that no longer match `main` (e.g. none of them records that `llm/gateway/` is absent from `main`) | `DEFECT` (documentation fiction risk) — **not fixed**: most are under live leases or would create broad conflicts. Recorded as a next action |
| "verified backups" claim (PR #66) | Contradicted by §18 | `DEFECT` → `OWNER_LOCKED` |
| `MASTER_ENGINEERING_TRUTH_REPORT_2026-09-27.md` | Explicitly an exclusive path of task-195 **and** task-197 | `OWNER_LOCKED` |

---

## 20. Defect Register

### D-001 — Restricted shell could read any file on the host

```text
ID                : D-001
Severity          : CRITICAL (P0 security — arbitrary file read / info disclosure)
Subsystem         : Security boundary / tool substrate
Affected Path     : src/nexus_ai_agent/tools/system_shell.py
Owner             : arena/01a0e907-nexus-ai-agent (this session) — path was FREE
Lease             : task-201-shell-trust-plane-grammar (zone shell-trust-plane)
Reproducer        : tests/unit/test_shell_workspace_escape.py
                    (imports nothing from the fixed implementation)
Observed Failure  : On e5b326b, with a secret file OUTSIDE the workspace:
                      date -f <outside secret>        -> executed; output
                        "date: invalid date ‘SECRET-TOKEN-abc123’"
                      date -f <outside, 3 date lines> -> executed, success=True,
                        one output line per input line (complete read)
                      date --file=<outside>           -> executed
                      date -r <outside>               -> executed, success=True,
                        "Mon Sep 28 17:36:04 UTC 2026" (existence/mtime oracle)
                      grep --file= / -fFILE / --exclude-from=  -> executed
                      grep -R TOP_SECRET .            -> executed, success=True,
                        printed ./escape_link:TOP_SECRET=42 and
                        ./escape_dir/outside_secret.env:TOP_SECRET=42
                      find -L . -name '*.env'         -> executed, printed
                        ./escape_dir/outside_secret.env
                    Control: cat <same outside path> -> correctly REFUSED with
                      "Path is outside the workspace"
                    Suite result BEFORE fix: 14 failed / 9 passed
Root Cause        : The validator enforced a DENY-LIST of spellings it happened to
                    know. Positional path arguments were checked only for
                    ls/cat/find; only the exact tokens find -newer/-fprint*/-fls and
                    grep -f were inspected. `date` had no file-flag handling at all
                    and no positional catch-all; `grep` had no catch-all, so its
                    long and attached spellings were never inspected. Unknown
                    `-`-leading tokens were silently skipped.
Broken Invariant  : "Any argument that is (or resolves to) a path outside the
                    workspace is rejected" (the module's own documented contract),
                    and the stronger form in filesystem_policy.py: the operation
                    must use the same physical path that was validated.
Fix               : Declared per-command flag grammar (bool / value / path /
                    optional_value / tuple) in which an UNDECLARED FLAG IS REFUSED;
                    plus an independent path-shaped-argument net; plus refusal of
                    symlink-following options (ls -L/-H/--dereference, grep -R,
                    find -L/-H/-follow) that no path validation can contain. Both
                    mechanisms route through WorkspaceFilesystem.
Regression Test   : tests/unit/test_shell_workspace_escape.py  -> 23 passed
                    tests/unit/test_shell_sandbox.py           -> 77 passed
Adversarial Test  : subprocess.run tripwire over 13 bypass vectors + 4 invented
                    flags; plus test_no_declared_flag_follows_symlinks and
                    test_every_declared_path_flag_is_validated (completeness sweep
                    over the live FLAGS table)
CI Evidence       : new blocking job shell-mutations; full suite + parity matrix
                    on head 89a2f81 (see §28)
Production Evidence: none — not deployed
Status            : VERIFIED_LOCAL  (VERIFIED_CI pending the PR #119 run)
```

### D-002 — A failed durable-memory write was invisible

```text
ID                : D-002
Severity          : HIGH (silent failure / fake success)
Subsystem         : Orchestration / memory
Affected Path     : src/nexus_ai_agent/orchestration/graph.py  (_memory_writer)
Owner             : arena/01a0e907-nexus-ai-agent (this session) — path was FREE
Lease             : task-202-memory-write-observability
Reproducer        : tests/unit/test_graph_memory.py
Observed Failure  : With a LongTermMemory whose store() raises, _memory_writer
                    returned a normal state with error=None and emitted NOTHING —
                    no log line, no metric, no state marker. A dead embedder or an
                    unreadable sqlite file degraded recall for every later turn
                    while every turn still reported success.
                    Suite result BEFORE fix: 2 failed / 4 passed
Root Cause        : `except Exception: pass`. The fail-safe decision (a memory
                    write must never abort a conversation) is CORRECT; the total
                    silence around it was the defect.
Broken Invariant  : Every failure must be observable. A semantic success must be a
                    real success, not an unrecorded degradation.
Fix               : Keep the fail-safe; emit exactly one structured warning
                    `long_term_memory_write_failed` carrying thread_id and
                    error_type. str(exc) is deliberately NOT logged — a backend can
                    echo the prompt inside its own error text, which would make the
                    "no conversation content in logs" property conditional on what
                    a third party wrote.
Regression Test   : test_memory_write_failure_never_aborts_the_conversation (pins
                    the preserved fail-safe), test_successful_memory_write_reports_nothing
Adversarial Test  : test_memory_write_failure_record_leaks_no_conversation_content
                    (asserts neither the secret token nor the prompt text appears)
CI Evidence       : full suite on head 89a2f81
Production Evidence: none
Status            : VERIFIED_LOCAL
```

### D-003 — The scheduled database backup has never succeeded

```text
ID                : D-003
Severity          : CRITICAL (operational — no proven recovery path)
Subsystem         : Backup / recovery / CI
Affected Path     : .github/workflows/maintenance.yml,
                    src/nexus_ai_agent/maintenance/backup.py
Owner             : arena/01a0dee1-nexus-ai-agent (task-192-production-dr-truth)
Lease             : LIVE, expires 2026-09-28T19:30:00Z
Reproducer        : gh api .../actions/runs/<id>/jobs  for all 12 maintenance runs
Observed Failure  : backup-db: 10 failures, 2 skipped, 0 successes. Latest failure
                    run 36405852557 on e5b326b, step 5 `nexus maintenance backup`;
                    steps 1-4 succeeded.
Root Cause        : NOT ESTABLISHED. The job log is not retrievable (4 attempts:
                    results-receiver EOF, then an empty body). The workflow's own
                    comment says the command exits non-zero when R2 is not
                    configured — a plausible mechanism, recorded as a HYPOTHESIS
                    only.
Broken Invariant  : A durable backup must exist and a restore must be demonstrable.
Fix               : NOT ATTEMPTED — every affected path is inside a live lease.
Regression Test   : n/a (owner's to write)
Adversarial Test  : n/a
CI Evidence       : run IDs listed in §18
Production Evidence: none — and that is the finding
Status            : DEFECT + OWNER_LOCKED + HANDOFF_REQUIRED (cause HISTORICAL_UNKNOWN)
```

### D-004 — No canonical LLM authority exists on `main`

```text
ID                : D-004
Severity          : CRITICAL (architecture — the invariant is simply not in force)
Subsystem         : LLM / provider layer
Affected Path     : src/nexus_ai_agent/llm/ (no gateway/ directory)
Owner             : arena/01a0e846-nexus-ai-agent (task-197-w2-global-llm-gateway)
Lease             : LIVE, expires 2026-09-30T13:54:02Z
Observed Failure  : 5 direct Gemini REST call sites + 1 direct litellm.Router
                    construction on main; no typed error taxonomy, no central
                    policy, no bounded concurrency, no per-request observability,
                    no architecture guard.
Root Cause        : W2 is unlanded. It exists only on PR #118's branch.
Fix               : OWNER'S — land PR #118.
Status            : OWNER_LOCKED + HANDOFF_REQUIRED
```

### D-005 — Dependency and interpreter drift between CI, image and local

```text
ID                : D-005
Severity          : MEDIUM
Subsystem         : Deployment / CI
Affected Path     : pyproject.toml, Dockerfile, .github/workflows/ci.yml
Owner             : unclaimed at audit time; Dockerfile + deploy_smoke are inside
                    the live task-192 lease
Observed Failure  : No lockfile exists; `pip install -e ".[dev]"` resolves fresh on
                    every run. The production image is python:3.12-slim while the
                    declared floor is 3.10, so 3.10 runs only in CI and never in
                    production. CI and the image do not share pinned versions.
Root Cause        : No committed lockfile; interpreter chosen independently per surface.
Fix               : NOT ATTEMPTED (partly leased; and a lockfile is a cross-cutting
                    decision that needs its own claim and review).
Status            : DEFECT + PARTIAL (OWNER_LOCKED for the deployment half)
```

### D-006 — Silent swallow in the log-redaction boundary

```text
ID                : D-006
Severity          : LOW-MEDIUM (potential secret residue in logs)
Subsystem         : Observability / security
Affected Path     : src/nexus_ai_agent/infrastructure/observability/redaction.py:63
Owner             : zone security-boundary; path inside the LOCKED
                    infrastructure/observability/ tree
Observed Failure  : `except ValueError: pass` returns the RAW url when urlsplit
                    raises, which can leave userinfo credentials in a log line.
                    Reachability was NOT demonstrated — no reproducer was built.
Root Cause        : Fail-open on a parse error in a fail-closed-by-purpose module.
Status            : DEFECT (unproven reachability) + OWNER_LOCKED
```

---

## 21. Fixed Items

| ID | Fix | Before | After | Status |
|---|---|---|---|---|
| D-001 | Fail-closed declared flag grammar + path net + symlink-option refusal | 14 failed / 9 passed | 23 passed (+77 salvaged suite) | `VERIFIED_LOCAL` |
| D-002 | One structured warning on memory-write failure, content-free | 2 failed / 4 passed | 6 passed | `VERIFIED_LOCAL` |

**Before / After, exactly as measured**

```text
BEFORE (e5b326b, unfixed)
  Reproducer  : tests/unit/test_shell_workspace_escape.py -> 14 failed, 9 passed
  Reproducer  : tests/unit/test_graph_memory.py           ->  2 failed, 4 passed
  Full suite  : 2915 passed, 30 skipped, 0 failed (150.29s)

ROOT CAUSE    : see D-001 (deny-list model) and D-002 (bare except: pass)

FIX           : commit 89a2f81f1135f94a9a3dff606dc825a57ce10243

AFTER (89a2f81, Py3.11.2)
  Regression  : test_shell_workspace_escape.py 23 passed
                test_shell_sandbox.py          77 passed
                test_graph_memory.py            6 passed
  Adversarial : shell_sandbox_mutations.py    11/11 killed, baseline GREEN before
                                                and restored GREEN after
                pack_trust_mutations.py       12/12 killed (unchanged neighbour)
  Full suite  : 3008 passed, 30 skipped, 0 failed (149.83s)
  ruff check  : All checks passed
  ruff format : 535 files already formatted
  mypy src    : Success: no issues found in 247 source files
  CI          : see §28
```

**Test-count arithmetic, reconciled exactly (no unexplained +1):**
collected `2945 → 3034` = **+64** (`test_shell_sandbox.py` 13 → 77) **+23** (new
reproducer) **+2** (`test_docs_integrity.py` parametrises over markdown files, so the new
ADR file adds two cases). The later `3038` adds the 4 new `test_graph_memory.py` tests.

---

## 22. Unfixed Items

| ID | Why not fixed | Status |
|---|---|---|
| D-003 | Every affected path is inside the live task-192 lease | `OWNER_LOCKED` |
| D-004 | Entire LLM layer is inside the live task-197 lease | `OWNER_LOCKED` |
| D-005 | Partly leased; a lockfile needs its own claim and cross-cutting review | `PARTIAL` |
| D-006 | Path is inside the locked `infrastructure/observability/` tree; reachability unproven | `OWNER_LOCKED` |
| Memory storage trust boundary | Live task-200 lease | `OWNER_LOCKED` |
| Stale root-level `*.md` reports | Most are leased or would create broad conflicts | `DEFECT`, deferred |
| 18 unreviewed "no-assert" test candidates | Not individually inspected; **not** claimed as defects | `UNVERIFIED` |
| SSRF guard, job queue instrumentation, checkpoint connection handling | Reviewed statically only, not exercised | `UNVERIFIED` |

---

## 23. Owner-Locked Items

| Zone | Owner | Lease expires | Locked paths | What this session did |
|---|---|---|---|---|
| `llm-gateway-authority` (task-197) | `arena/01a0e846` (PR #118) | 2026-09-30T13:54:02Z | 44 | Read-only audit; verified `gateway/` absent on `main`; verified PR CI 32/32 green; no edit |
| `task-200-w3-memory-trust-boundary` | `arena/01a0e742` (PR #117) | 2026-09-29T14:32:38Z | 6 | Read-only audit; no edit |
| `runtime-composition-w1` (task-196) | `arena/01a0e442` (PR #113) | 2026-09-28T21:10:00Z | 13 | Untouched |
| `production-dr-deploy` (task-192) | `arena/01a0dee1` (PR #96) | 2026-09-28T19:30:00Z | 13 | Read-only audit; documented D-003/D-005 |
| `ci-quality` (task-164) | `arena/01a0dee1` | 2026-09-28T19:15:19Z | 0 | Untouched |

**No takeover, overwrite, merge, rebase or close was performed on any of these.** PR #105 —
which had **no live lease** — was salvaged under the repository's own documented precedent,
with a `takeover_log` entry, and was left open and unmodified.

---

## 24. Unverified Claims

Explicitly **not** asserted by this report:

1. That W2 is production-ready. It is `VERIFIED_CI` on `d5a205f4` and absent from `main`.
2. That the `backup-db` failure is caused by missing R2 secrets. Plausible, **unproven** —
   the log is unreadable.
3. That branch protection is or is not configured on `main`. The API returned 403.
4. That D-006 (redaction swallow) is reachable in production. No reproducer was built.
5. That the 18 unreviewed "no-assert" tests are defective. Two inspected were fine.
6. That the SSRF guard, job queue, or checkpoint connection handling are correct. Reviewed
   statically only.
7. Any `VERIFIED_RUNTIME` claim. No server, bot or worker was started.
8. That PR #118's "runner flake" attribution for its four `pip install` failures is correct.
9. That repository secrets (`R2_*`, `NEXUS_DATABASE_URL`) are present or absent.

---

## 25. Lost / Unrecoverable Artifacts

| Artifact | Attempted recovery | Result |
|---|---|---|
| `backup-db` job log, run 36405852557 / job 108874276321 | 4 attempts: `gh run view --log-failed` (×3, all `results-receiver…: EOF`), `gh api …/jobs/<id>/logs` (empty body) | `LOST_UNRECOVERABLE` from this token |
| Branch protection configuration for `main` | `gh api …/branches/main/protection` → 403 | Not lost — **not accessible**; `UNVERIFIED` |
| `7bd6e700` as "W2 candidate HEAD" | `git fetch origin refs/pull/118/head` | **Recovered**, but as an ancestor of the true head `d5a205f4`, not as the head |
| Any successful database backup artifact | All 12 maintenance runs enumerated | **None exists** — not lost, never created |

No Git object required by this audit was unreachable. No stash, tag or dangling object was
needed.

---

## 26. Remaining Risks

| # | Risk | Severity | Status |
|---|---|---|---|
| R-1 | **No proven database recovery path.** `backup-db` has never succeeded. | Critical | `OWNER_LOCKED` |
| R-2 | **No canonical LLM authority on `main`.** Five raw Gemini paths + one direct `Router`; any of them can be duplicated silently. | Critical | `OWNER_LOCKED` |
| R-3 | **Memory has no trust metadata on `main`.** Structured memory content is not classified as untrusted; no TTL/tombstone/lineage. | High | `OWNER_LOCKED` |
| R-4 | **No lockfile.** CI and the Docker image resolve dependencies independently; a green CI run does not pin what ships. | Medium | `DEFECT` |
| R-5 | **20 of 36 open PRs are CONFLICTING**, mostly on `.agents/board.json`. Long-lived leases + a shared coordination file is a throughput bottleneck. | Medium | `PARTIAL` |
| R-6 | **Stale root-level truth reports** still describe a state that no longer matches `main`, including no mention that `llm/gateway/` is absent. Documentation fiction risk. | Medium | `DEFECT` |
| R-7 | The shell grammar table must be maintained when a *wanted* flag is added, and a `path`-kind entry is a security-relevant declaration. `test_every_declared_path_flag_is_validated` mitigates but does not eliminate this. | Low | `VERIFIED_LOCAL` |
| R-8 | The grammar deliberately over-refuses (`date -d`, `date -f`, `ls -L`, `grep -R`, `find -L`). An operator who needs one will be tempted to widen the table; the legitimate surface is pinned by tests to make that a reviewed change. | Low | `VERIFIED_LOCAL` |

---

## 27. Exact Next Actions

Ordered by severity. Each names an owner constraint.

1. **[CRITICAL — owner `arena/01a0dee1`, lease expires 2026-09-28T19:30Z]** Establish why
   `backup-db` fails. Re-run `workflow_dispatch` on `maintenance.yml` with a fresh log
   capture, confirm whether `R2_*` and `NEXUS_DATABASE_URL` secrets resolve, and land one
   **successful** backup plus one **executed restore** drill. Until then the project has no
   proven recovery path. Evidence required: a green `backup-db` run ID and a restore
   transcript.
2. **[CRITICAL — owner `arena/01a0e846`, lease expires 2026-09-30T13:54Z]** Land PR #118, or
   hand it off. `main` cannot satisfy "one canonical LLM authority" until it does. Close the
   two pinned Gemini bypasses behind a bytes-out capability in the gateway contract.
3. **[HIGH — owner `arena/01a0e742`, lease expires 2026-09-29T14:32Z]** Land task-200 so
   memory records carry trust metadata and cannot grant authority implicitly.
4. **[HIGH — free]** Merge PR #119 once its exact-SHA CI is green, then close PR #105 with a
   comment pointing at it (per the precedent: close only after the successor merges).
5. **[MEDIUM — free, needs its own claim]** Commit a lockfile (`pip-tools`/`uv`), and align
   the Dockerfile interpreter with the parity matrix, so CI proves what ships.
6. **[MEDIUM — free]** Fix D-006: make `redaction._redact_urls` fail closed (redact the whole
   URL, or drop userinfo unconditionally) instead of returning the raw value on `ValueError`.
7. **[LOW — free]** Archive or re-date the 8 stale root-level `*.md` reports, or move them
   under `docs/audits/` with an explicit "superseded on <SHA>" header.
8. **[LOW — free]** Review the 18 unreviewed "no-assert" test candidates and either convert
   them to explicit assertions or record them as intentional must-not-raise contracts.

---

## 28. Final Readiness Matrix

| Capability | Status | Evidence |
|---|---|---|
| Restricted shell containment | **`VERIFIED_LOCAL`** (CI pending) | 23 + 77 tests, 11/11 mutants, RED 14/9 before |
| Filesystem boundary primitives | `VERIFIED_LOCAL` (review only) | `O_NOFOLLOW` + `dir_fd`; `test_filesystem_boundary` |
| Pack trust root | `VERIFIED_LOCAL` | 12/12 mutants killed on this branch |
| Command injection (`shell=True`) | `VERIFIED_LOCAL` | absent in `src/`; now pinned by a structural test |
| Archive extraction (zip-slip) | `PROVEN_DONE` | no archive extraction in `src/` at all |
| Memory-write observability | `VERIFIED_LOCAL` | 6 tests, RED 2/4 before |
| Memory trust boundary | `OWNER_LOCKED` | task-200 live lease |
| Canonical LLM authority | `OWNER_LOCKED` | absent on `main`; PR #118 `VERIFIED_CI` on its own head |
| Request-queue lifecycle | `PROVEN_DONE` | merged via PR #110; CI green on `e5b326b` |
| Python 3.10/3.11/3.12 parity | `VERIFIED_CI` | run `36339890292`, 16/16 jobs |
| Test suite health on `main` | `VERIFIED_CI` + `VERIFIED_LOCAL` | 2915/30 on `e5b326b`; 3008/30 on `89a2f81` |
| Docs integrity | `VERIFIED_LOCAL` | 59 passed |
| Version lockstep | `VERIFIED_LOCAL` + `VERIFIED_CI` | 3.13.0 across VERSION/pyproject/CHANGELOG |
| Deployment reproducibility | `DEFECT` | no lockfile; image 3.12 vs floor 3.10 |
| **Backup** | **`DEFECT`** | **10 failures / 0 successes across all 12 runs** |
| **Recovery / restore** | **`UNVERIFIED`** | never demonstrated; test file is leased |
| Runtime behaviour (bot/API/worker) | `UNVERIFIED` | nothing was started |
| Branch protection | `UNVERIFIED` | API 403 |

### CI on this session's head — `89a2f81f1135f94a9a3dff606dc825a57ce10243` (PR #119)

**This table is incomplete, and the reason is recorded rather than papered over.** The
GitHub installation token used by this session **expired mid-run** at approximately
2026-09-28T18:2xZ (`gh auth status` → *"The github.com token in GH_TOKEN is no longer
valid"*; every subsequent `gh api` returned `401 Bad credentials`). CI was still running
when access was lost, so the legs below are the **last observed state**, and anything not
observed is marked `UNVERIFIED`. **No conclusion was drawn from an unread CI leg.**

34 check-runs were created on `89a2f81` (17 jobs × the `push` and `pull_request` triggers).
At the final successful poll, **5 were still running** and every leg that had completed was
`success`:

| Leg | Last observed conclusion |
|---|---|
| `extras-matrix (core)` | success |
| `extras-matrix (pdf)` | success |
| `extras-matrix (speech)` | success |
| `extras-matrix (translate)` | success |
| `lint-fast` | success |
| `migrate-postgres` | success |
| `release-lineage` | success |
| `trust-mutations (pack trust plane)` | success |
| `python-parity (3.11)` | success |
| `test (pytest -m "not slow")` | `UNVERIFIED` — in progress when access was lost |
| `lint (ruff + mypy + version lockstep)` | `UNVERIFIED` |
| `python-parity (3.10)` / `(3.12)` | `UNVERIFIED` |
| `continuum-evidence (3.10/3.11/3.12)` | `UNVERIFIED` |
| **`shell-mutations (restricted-shell flag grammar)`** — the job this change adds | `UNVERIFIED` |

The `UNVERIFIED` legs are **not** assumed green. They are, however, the same legs that were
green on `main@e5b326b` (run `36339890292`, 16/16), and the two legs that could plausibly be
affected by this change — `lint` and `test` — were both run **locally on the same tree with
the same dependency set** and passed (`ruff check` clean, `ruff format --check` 535 files,
`mypy src` 247 files clean, `pytest -m "not slow"` 3008 passed / 30 skipped / 0 failed).
That makes the change `VERIFIED_LOCAL`; it does **not** make it `VERIFIED_CI`.

**Action required from the operator:** reconnect GitHub in Arena, then re-read
`gh api repos/bot523h/nexus-ai-agent/commits/89a2f81f1135f94a9a3dff606dc825a57ce10243/check-runs`
and replace this table. No re-run of CI is needed — the checks were already triggered.

---

## 29. Final Verdict

```text
HARDENED_BUT_NOT_COMPLETE
```

**Why not `PROVEN_COMPLETE`.** Two critical items are open and neither is proven: the
scheduled database backup has never succeeded (10 failures / 0 successes across all 12
maintenance runs) and therefore **no recovery path is proven**; and `main` contains **no
canonical LLM authority at all** (`llm/gateway/` does not exist), so the central
architectural invariant of this programme is not in force in production.

**Why not `BLOCKED`.** This session was not blocked. It found and root-caused two real
defects in free zones, proved each red-then-green with adversarial suites and mutation
harnesses, kept the full suite green at **3008 passed / 30 skipped / 0 failed** against a
measured 2915/30 baseline, and touched nothing under a live lease.

**Why not `NOT_READY`.** The two critical items above are **owned by other agents under live
leases** (task-192 and task-197) and are handed off with exact evidence, which is the
sanctioned outcome rather than a failure of this audit. What this session changed is safe to
ship: a strict narrowing of what a restricted shell may do, plus one added log line.

### The mission's closing questions, answered

| Question | Answer |
|---|---|
| What existed? | `main` @ `e5b326b`, CI 16/16 green, 2915 passed / 30 skipped, 247 src files, **no `llm/gateway/`**, 5 raw Gemini paths, 5 live leases covering 75 paths |
| What was actually broken? | D-001 arbitrary host-file read via the restricted shell (9 bypasses reproduced); D-002 invisible memory-write failure; D-003 backup never succeeded; D-004 no canonical LLM authority; D-005 no lockfile; D-006 fail-open redaction |
| Why was it broken? | D-001: a deny-list validator only covers spellings its author thought of. D-002: `except Exception: pass`. D-003: cause `HISTORICAL_UNKNOWN`, log unrecoverable. D-004: W2 is unlanded. |
| What was fixed? | D-001 (fail-closed declared flag grammar + path net + symlink-option refusal) and D-002 (one structured, content-free warning) — commit `89a2f81` |
| How was it proven? | RED 14/9 and 2/4 before; GREEN 23, 77 and 6 after; **11/11** and **12/12** mutants killed; full suite 3008/30/0; ruff, ruff-format and mypy clean; test-count arithmetic reconciled to the unit |
| What remains? | §26 R-1…R-8; §22 unfixed items |
| Who owns it? | task-192 (`arena/01a0dee1`), task-197 (`arena/01a0e846`), task-200 (`arena/01a0e742`), task-196 (`arena/01a0e442`), task-164 (`arena/01a0dee1`) |
| What is blocked? | Backup/DR repair, LLM gateway landing, memory trust boundary, deployment reproducibility (partly) |
| What is unverified? | §24, nine items — including every runtime claim and the cause of the backup failure |
| What can safely ship? | PR #119: a strict narrowing of shell capability plus one log line, with zero behavioural widening |
| What must not ship yet? | Any claim of production readiness. The backup/recovery gap must be closed first, and the LLM gateway must actually reach `main` |

> Nothing in this report is marked `PROVEN_DONE` on the strength of green CI alone. Where a
> thing is green but its architecture is unproven, it is marked `VERIFIED_CI`. Where a cause
> could not be read, it is marked `HISTORICAL_UNKNOWN` rather than guessed. Where another
> agent holds the lease, it is marked `OWNER_LOCKED` and handed off with evidence.
