# Tracing (opt-in LangSmith)

**Status:** Living document
**Scope:** the opt-in LangSmith trace export — the switch, the fail-closed bootstrap, the connectivity smoke, and the honest limits
**Verified against:** `main` @ `cf8b7f6`

The runtime is built on LangGraph, and LangGraph/LangChain already know how to emit
traces to LangSmith through the `LANGSMITH_*` environment variables. This document
describes the **one** place that decides whether those traces leave the process, so
the decision is explicit, testable, and impossible to turn on by accident.

Tracing here follows the same three rules as the rest of observability
(§[`OBSERVABILITY.md`](OBSERVABILITY.md)): **dependency-free by default**
(the SDK ships with the core install, but core code never imports it at module
top level), **off unless asked** (enabling it is an explicit egress decision),
and **never fatal** (a tracing failure can never break a request).

---

## 1. The switch

| Property | Value |
|---|---|
| Enable | `NEXUS_TRACING_ENABLED=true` (default `false`) |
| Key | `NEXUS_TRACING_API_KEY` (falls back to `LANGSMITH_API_KEY`) |
| Project | `NEXUS_TRACING_PROJECT` (default `nexus-ai-agent`; falls back to `LANGSMITH_PROJECT`) |
| Endpoint | `NEXUS_TRACING_ENDPOINT` (default `https://api.smith.langchain.com`) |
| SDK | `langsmith` (ships with the core install: `langgraph` → `langchain-core`) |

Tracing is **off** unless the enable flag is set, and `NEXUS_TRACING_ENABLED` is the
*only* name that turns it on — unlike the key/project/endpoint, there is no
`TRACING_ENABLED` fallback, because an accidental second switch would defeat the
default-off guarantee. Turning it on is a deliberate choice because it exports user
prompts to a third party; the default must never do that silently.

## 2. The bootstrap

`observability/tracing.py::configure_tracing` is deterministic with respect to its
arguments: the environment is mutated on **every** path and the SDK's process-global
override is forced to match — `False` on every non-active path, `True` on the active
path. Because the SDK reads its env flags through a `functools.lru_cache`, clearing
env vars alone cannot undo a read that already happened; the override (checked *before*
the env) is the cache-independent switch that makes the outcome authoritative in both
directions — a stale `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2` cannot smuggle egress
past the switch, and a primed SDK cannot ignore an enable. It returns a typed
`TracingState` (`active`, `reason`, `project`) rather than a bare boolean, so callers
and tests can branch on *why*.

```mermaid
flowchart TD
    start["bootstrap_tracing(settings)"] --> enabled{"NEXUS_TRACING_ENABLED?"}
    enabled -- no --> off["clear SDK flags + override off<br/>TracingState(disabled)"]
    enabled -- yes --> sdk{"langsmith SDK importable?"}
    sdk -- no --> nosdk["clear SDK flags + override off<br/>TracingState(sdk_missing)<br/>logged, not fatal"]
    sdk -- yes --> key{"api key present?"}
    key -- no --> nokey["clear SDK flags + override off<br/>TracingState(key_missing)<br/>logged, not fatal"]
    key -- yes --> on["set LANGSMITH_TRACING/API_KEY/PROJECT/ENDPOINT<br/>clear legacy LANGCHAIN_* flags + override on<br/>TracingState(active)"]
    on --> anyfail{"any exception?"}
    anyfail -- yes --> off2["clear SDK flags + override off<br/>TracingState(disabled)<br/>warning logged"]
```

The SDK resolves its enable flags through a `functools.lru_cache`, so the first read
wins: `bootstrap_tracing` is a *startup* contract and must run once, early — before
the first LangGraph/LangChain call. (The override above still makes a later
*disabled* bootstrap effective, so a re-run is safe, not silently ignored.)

`bootstrap_tracing(settings)` wraps `configure_tracing` in a broad guard: a tracing
failure is an observability failure, and observability failures are never fatal here.
The bot calls it once, right after logging exists (`cli.py::run_bot`), so the outcome
is visible in the log stream.

## 3. The connectivity smoke

`scripts/langsmith_smoke.py` proves the configured key can actually accept a trace.
It is a *connectivity* check, not a unit test, so it fails **visible**, never green:

| Situation | Output | Exit |
|---|---|---|
| `LANGSMITH_API_KEY` unset | `RESULT=SKIPPED_NO_SECRET` | 0 |
| SDK missing | `RESULT=FAILED DETAIL=...` | 2 |
| Endpoint refuses the key | `RESULT=FAILED DETAIL=<ClassName> [status=<code>]` | 1 |
| Trace accepted | `RESULT=OK` | 0 |

A missing secret is a visible skip; a fabricated pass would be the exact lie this
check exists to prevent. The failure `DETAIL` is *allow-listed* — the exception
class name and an optional status code only (`observability.tracing.sanitize_exception`),
never `str(exc)`: SDK/HTTP error text can embed the key, the request URL (with
credentials or query parameters) and the payload, so it is never echoed
(`tests/unit/test_langsmith_smoke_safe_output.py` proves a sentinel-laden exception
cannot leak). Because `langsmith.Client` batches in the background, the smoke registers
the SDK's `tracing_error_callback` and `flush()`es before trusting the result, so a
collector that rejects the batch during ingestion fails the smoke instead of printing
a false `RESULT=OK`. The workflow `.github/workflows/langsmith-smoke.yml` runs it
on `workflow_dispatch` and on changes to the tracing module, script, or workflow.
(It replaces the orphan `langsmith-connectivity-smoke-20261026.yml` registration whose
branch was deleted, leaving a workflow with no source file.)

## 4. Honest limits

- **Tracing is not metrics or alerting.** A trace is a debugging aid; the alert list
  stays in §[`OBSERVABILITY.md`](OBSERVABILITY.md) §6. Nothing here is a signal to
  page on.
- **Traces contain prompts.** This is exactly why the switch defaults off and why the
  tracing key must be a *tracing-only* key (LangSmith scopes let such a key write runs
  but not read projects/settings), so a leaked key cannot exfiltrate other tenants' data.
- **The smoke proves ingest, not correctness.** `RESULT=OK` means the endpoint accepted
  a run; it says nothing about whether a given graph emitted the traces you wanted.
- **No CI leg needs the secret.** The unit suite (`tests/unit/test_tracing_bootstrap.py`)
  proves every non-egress branch without a key; only the smoke job needs the secret, and
  it degrades to a visible skip without one.

## 5. Adding a traced surface

1. Never import `langsmith` at module top level in core code (deptry fails the lint
   gate); the SDK is present, but the runtime reaches it only through environment
   variables that `bootstrap_tracing` sets.
2. Call `bootstrap_tracing(settings)` once, at startup, after `configure_logging`.
3. Do not read `TracingState` to gate business logic — tracing is observation only.
4. If you add a traced surface worth verifying, extend `scripts/langsmith_smoke.py`
   rather than adding a new workflow.
