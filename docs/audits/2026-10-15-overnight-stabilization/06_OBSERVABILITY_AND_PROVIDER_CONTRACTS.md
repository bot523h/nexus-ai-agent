# 06 — Correlation ids, PII posture, provider contracts (Phase 6)

## Correlation chain as it exists (live read)
| Hop | Identifier | Where |
|---|---|---|
| Telegram request | `correlation_id = uuid4()` bound into structlog contextvars per update | `bot/handlers.py` ≈l.984 |
| Command | `RequestContext.request_id` (≤128 chars), `channel` | `creative/studio/models.py` |
| Job | `request_id = content_id("request", {job_type, idempotency_key})`, `request_fingerprint`, `transaction_id` persisted on the row | `jobs/creative_passport.py request_identity`, queue columns |
| Attempt | `attempt_id(job_id, attempt)`; fencing token = row `attempt` | `jobs/creative_passport.py`, queue |
| Artifact / verification / evidence | passport binds job, attempt, source + output `sha256`, verification block; journal event per durable transition with digests | `jobs/creative_passport.py`, `provenance/models.py` |

Gap: the user-facing `correlation_id` is **log-only** — it is not carried into the job row's `request_id` (which is content-derived by design so redeliveries collapse). Decision: keep `request_id` content-derived (idempotency depends on it) and add `correlation_id` as a *separate* optional, non-identifying field on `RequestContext` and the journal event payload (NEXT; small, test-first). No PII: the chain carries uuids and digests only; the logging boundary masks values (`observability/logging.py` redaction tests).

## OpenTelemetry decision
Not adopted tonight. Reason: the brief asks for *minimal* correlation ids and forbids new libraries without proof; the chain above already exists with structured logging and content ids. Trigger to revisit: a second process (worker) or an external collector requirement → then OTel SDK + OTLP exporter, mapping `correlation_id`→trace id, `attempt_id`→span attributes.

## Provider contract audit (scope: static contract, not live calls)
* LangGraph: `adapters/langgraph/*` with lifecycle recording and checkpoint lifecycle store/adapters (`storage/checkpoint_*`), golden fingerprints under `storage/golden/` (`nexus checkpoints golden update` is the only legal way to regenerate).
* LiteLLM: version pin research recorded in the session (`1.105.1` on PyPI, 2026-10-14; `requires_python <3.15,>=3.10`); rate-limit error classes must be the real `RouterRateLimitError` objects in tests (standing rule).
* Local (`llm/local_llama_cpp.py`, `adapters/native_local_backend.py`): mocked-provider tests only; **no live-provider or real-engine validation occurred tonight** and none is claimed.
* Eval set: not added (would be unexecuted prose); listed in `09_RESEARCH…` item 8 with its trigger.
