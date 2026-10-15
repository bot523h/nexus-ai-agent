# 05 — CommandBus audit: Intent ≠ Authority (Phase 5)

Subject: `src/nexus_ai_agent/creative/studio/bus.py` (the one Nagar write path; Gate-2 order D-0013) plus `studio/models.py`, `studio/authorization.py`, `studio/lifecycle.py`. Read live at `7fd4112`.

| Checklist item | Evidence in source | Verdict |
|---|---|---|
| Intent ≠ Authority | `target.project_id` is a *claim*; an injected `authorizer` must bind actor+project; actor claim without authorizer → `AuthorizationError`; legacy schema-1 without actor runs under *implicit local trust* (documented deprecated path) | PASS with a named legacy hole: implicit local trust for claim-less schema-1 commands (in-process call sites only) |
| Typed IO | protocol `nagar.command.v1`, envelope schema 1\|2, operation schema version match, input validated by the operation's Pydantic model; `extra="forbid"`, `frozen=True`; 27 explicit bounds (`max_length`/`ge`/`le`) in `models.py`, `input_refs ≤ 512` | PASS |
| AuthZ | actor grant → capability authorization (registry is authoritative; client snapshot advisory) → pack lifecycle gate (`AVAILABLE`/`EXPERIMENTAL` opt-in at composition root only) → execution policy ladder | PASS |
| Idempotency | `(project_id, operation, idempotency_key)` reservation before the handler; same payload → identical original result; different payload → `IdempotencyConflictError`; concurrent same key → "already executing"; failed command releases reservation | PASS **in-process only** — the docstring states reservations are per-bus/in-memory (no cross-restart durability). Durable idempotency lives in the job queue lane (`nexus_job_queue`), not in the bus |
| Timeouts / cancel | handlers are pure, synchronous, run under a re-entrant lock against an isolated copy; no per-command deadline; cancellation exists only at the job-queue lane (`JOB_REOPENED`) | GAP (P1): a long handler blocks the bus; mitigation today = handlers are CPU-light edits; long work is dispatched to the queue lane |
| Durable status / restart reconciliation | bus state is in-memory by design (D-0025 first slice); durable facts and recovery/takeover live in the queue (`JOB_TAKEOVER`, fenced attempts, journal) | PASS for the queue lane; the bus itself claims no durability (honest) |
| Limits | payload field bounds as above; nested dispatch forbidden; `input_refs` cap | PARTIAL — no total-envelope byte cap at the bus (API layer caps body size) |
| Sandbox | execution mode `local` only; render lane executes through `creative/ffmpeg_executor.py` / staging under the artifact root; the shell tool is a separate grammar-validated boundary (ADR 0011) | PASS for current scope |
| Provenance | every commit records an `EditTransaction` with previous/new state hashes; undo recorded as a transaction; queue lane records journal events + passports | PASS |

## Orchestration-library decision
No Temporal / Ray / Prefect / Redis. Proof of sufficiency, not preference: durability, fencing (per-row attempt tokens), idempotency, recovery/takeover and exactly-once evidence are already provided by `nexus_job_queue` + the causal journal and are mutation-tested (`governance-mutations` CI job: execution core 17/17). A distributed scheduler would add a second authority (the brief's explicit prohibition) without a demonstrated need (single worker process, SQLite/PostgreSQL deployment).

## Actions
* NOW (documented, not coded tonight): none required for safety.
* NEXT: per-command deadline in `_apply_guarded` (handler run in a worker with timeout, state untouched on timeout — a refusal, not a partial apply); total envelope byte cap at parse; retire the implicit-local-trust path once all in-process call sites pass an actor (`grep -n "actor=None"`).
