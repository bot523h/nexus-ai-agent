# W2 — Global LLM Gateway Authority: truth report

**Date:** 2026-09-28 · **Branch:** `arena/01a0e4f6-nexus-ai-agent` · **Base:** `e5b326b` (`origin/main`)
**Board claim:** `task-197-w2-global-llm-gateway` (zone `llm-gateway-authority`, active, TTL 48h from 2026-09-27T22:39:01Z, `gates_owner: false`)
**Verdict:** **W2 VERIFIED**

This report records what was built, what was measured, and what is still not true.
Every number in it was produced by a command named next to it, on this branch, in
this sandbox. Nothing is estimated and nothing is carried over from a claim made
earlier in the mission.

---

## 1. Mission and acceptance criteria

Build a real Global LLM Gateway Authority: one path from any caller to any model,
with an explicit contract, typed semantic errors (no substring scanning),
centralized policy (retry/backoff, timeout budget, concurrency, rate limiting,
fallback), cancellation safety, quota awareness, observability that never leaks
secrets or prompts, truthful usage/cost accounting, and adversarial + mutation
testing in which every mutation is killed by a named test.

The board claim's acceptance criteria are reproduced verbatim in
`.agents/board.json` (`claims[task-197-w2-global-llm-gateway].acceptance_criteria`);
§20 maps each one to the evidence that satisfies it.

## 2. Method

Phase 0 traced the real call graph (static AST + grep, then runtime reasoning about
who constructs what). Phase 2 researched gateway patterns *before* designing:
Gemini's own retry guidance (retry only 408/429/5xx and network timeouts; never
400/402/403), Gemini's rate-limit model (RPM/TPM/RPD, any one exceeded → 429
`RESOURCE_EXHAUSTED`, RPD resets midnight Pacific), Gemini's error shapes
(`{error:{code,message,status}}`, generation error codes, safety block reasons),
RFC 9110 on `Retry-After` and idempotent-method retry gating, Brooker's
"Exponential Backoff And Jitter" (full jitter minimises server load; breaker +
bulkhead patterns), and the OpenTelemetry GenAI semantic conventions (required and
recommended attributes; prompt/completion content as *opt-in events*, never
attributes). Phase 1/3 produced the target architecture and the inventory in §19.
Phase 4 migrated caller by caller behind a compatibility facade — no big-bang
rewrite. Phase 5 wrote the adversarial, security, load and mutation suites, and
then used the survivors to strengthen tests rather than to delete mutations.

## 3. Phase 0 — the call graph as found

Before W2 the repository reached a model through at least six independent paths:
`features/ai_chat.py` (its own httpx POST to Gemini), `features/summarizer.py`
(its own httpx POST), `llm/gemini_provider.py`, `llm/litellm_provider.py` (a
`litellm.Router` chain with its own cooldowns), `llm/fallback_provider.py`
(provider hopping with substring-matched `error_keywords`),
`llm/local_server_provider.py` / `llm/local_llama_cpp.py` (local servers), plus
`creative/image_gen/gemini_adapter.py` and `creative/slideshow/analysis.py` on the
creative side, and callers in `agents/store/base_agent.py`,
`features/ai_memory.py`, `creative/video_director.py` consuming whichever provider
object they were handed. Retry, timeout, concurrency and classification decisions
were duplicated at each site and differed.

## 4. Research → decision mapping

| Decision | Source | Consequence in code |
|---|---|---|
| retry only on rate-limit/timeout/5xx/network; never on 400/402/403 | Gemini troubleshooting guidance | `RETRYABLE_KINDS` = {rate_limited, transient_provider_failure, upstream_timeout, network_failure}; `retry.allows(kind)` gates every attempt |
| honour `Retry-After` exactly, seconds or HTTP-date; cap the total wait, not just the attempt count | RFC 9110 §10.2.3 | `parse_retry_after`, `RetryPolicy.max_retry_after_seconds` (30s default), `TimeoutBudget.max_backoff_seconds`, backoff declined when it cannot fit the remaining budget |
| full jitter `uniform(0, min(cap, base·2^n))` | Brooker, AWS Architecture Blog | `JitterMode.FULL` default in `compute_backoff`, clamped at ≥ 0 |
| bulkhead = concurrency semaphore; breaker = closed/open/half-open probe | Brooker | `BoundedScheduler` (global/per-provider/per-tenant), `CircuitBreaker` with an explicit half-open probe that keeps an open route in the plan so it can recover |
| RPM + RPD windows per provider; 429 when any is exceeded; RPD resets at midnight Pacific | Gemini rate limits | `WindowRateLimiter` (rolling minute + UTC day), `ProviderRateLimit`, `QuotaExhaustedError` when the day is gone |
| span/record attributes: operation, request model, provider, usage, finish reasons, `error.type`; content opt-in and *not* an attribute | OTel GenAI semantic conventions | `RequestRecord.otel_attributes()`; prompt/completion never recorded (§15) |
| classify from the declared machine-readable status, not the message | Gemini error shape (`error.status`, snake_case codes) | `_declared_error_status` reads `error.status` only; the substring shape is banned structurally (§7) |

## 5. Target architecture

```
Caller (features/, agents/, creative/, llm/ providers)
   └─> facade.GatewayLLMProvider  (pre-W2 generate/complete/embed surface)
   └─> registry.gateway_for_credentials / get_llm_gateway  (which authority)
        └─> engine.LLMGateway.execute(LLMRequest)
             ├─ policy.plan()      routes · budget · retry · fallback · privacy · idempotency key
             ├─ scheduler          global / per-provider / per-tenant bulkheads, bounded queue
             ├─ rate gate          local minute + UTC-day windows, Retry-After, quota exhaustion
             ├─ circuit breaker    per route health, half-open probe
             ├─ adapters           GeminiHttpAdapter · LitellmRouterAdapter · LegacyProviderAdapter
             │     └─> External LLM (Gemini REST · litellm Router chain · local server)
             ├─ usage              provider-reported tokens → pinned price table → cost or UNKNOWN
             └─ observability      exactly one RequestRecord per request → sinks → metrics
```

Implemented in `src/nexus_ai_agent/llm/gateway/` (11 modules) plus
`src/nexus_ai_agent/llm/errors.py`: 7,189 lines total. Module sizes: engine 1,632 ·
adapters 1,249 · policy 824 · scheduler 536 · contract 503 · registry 491 ·
observability 473 · errors 463 · resilience 393 · facade 319 · `__init__` 191 ·
usage 115.

## 6. The contract

`LLMRequest` (caller, purpose, operation, prompt/system/messages/parts, provider and
model pins, priority, `deadline_seconds`, `allow_fallback`, `generation`,
`output_validator`, `idempotency_key`, `cancellation`, metadata) →
`LLMResponse` (request_id, text, provider/model actually used, operation, caller,
purpose, usage, finish_reason, `PolicyOutcome`, `Timings`, per-attempt records,
`structured`, `embedding`) or a typed `LLMError`. `GenerationParams` validates in
`__post_init__`, so an out-of-range temperature or a negative token cap is refused
at construction instead of becoming a provider 400. `payload_bytes()` counts binary
parts only. `LLMPriority` values match `features/request_queue.Priority` so a
request crosses either scheduler untranslated. A caller can never receive an
error-shaped string: failures raise, they do not return.

## 7. Typed errors and the substring ban

18 kinds; `retryable` and `fallback_eligible` are derived from the kind
(`RETRYABLE_KINDS`, `FALLBACK_ELIGIBLE_KINDS`). `content_blocked` and `cancelled`
are never fallback-eligible, and the engine enforces that even when a deployment
misconfigures `FallbackPolicy.eligible_kinds` to include them — a typed fact
outranks configuration (killed mutation `engine_falls_back_on_a_blocked_answer`,
test `test_a_misconfigured_fallback_policy_cannot_launder_a_content_block`).

The ban is structural, not a convention: `substring_classifications()` in
`tests/architecture/test_llm_gateway_authority.py` walks every file in `src/` and
fails on `"<error keyword>" in str(...)` (also `f"{...}"` and `.lower()` variants).
Current result: **0 findings in 259 source files**, gateway included. A liveness
test proves the detector still fires on five hostile snippets and stays silent on
four typed-lookalikes, so the gate cannot pass because the detector broke.
`llm/fallback_provider.py` keeps `error_keywords` only as a deprecated constructor
opt-in, empty by default, logging a deprecation warning when used.

## 8. Retry, backoff, `Retry-After`

One place decides (`policy.plan` + the engine's retry loop). Attempts are gated by
kind and by `max_attempts`; backoff is full-jitter, clamped by
`RetryPolicy.max_delay_seconds` and `TimeoutBudget.max_backoff_seconds`, and never
negative; a provider's `Retry-After` is honoured up to `max_retry_after_seconds`
and is *not* shortened by the jitter ceiling (a wait we invented is capped by the
jitter ceiling; a wait the provider asked for is capped by its own ceiling); a
`Retry-After` beyond the daily window becomes `quota_exhausted`, not a throttle;
and a backoff that cannot fit in the remaining budget is **declined**, so the chain
stops and reports `deadline_exceeded` instead of sleeping past the caller's
deadline (killed mutation `engine_backoff_ignores_the_caller_deadline`, test
`test_a_backoff_that_would_outlive_the_budget_is_declined_not_slept`, asserted on a
virtual clock: the declined sleep is never slept, not even partly).

## 9. Timeout budget

`TimeoutBudget` layers are mutually consistent by construction: `validate()` rejects
`connect+read > per_attempt`, `per_attempt > total`, `queue_wait > total`,
`max_backoff > total`. `with_total()` scales the inner layers down to a caller's
shorter deadline and never up past policy. Per-attempt caps are enforced by racing
the provider task against the budget, and a *late* answer that lands after the
budget expired is reported as `deadline_exceeded` rather than returned as a kept
promise. Timings are clamped at ≥ 0 so a monotonic-clock surprise cannot produce a
negative duration in a record.

## 10. Concurrency, bulkheads, the bounded queue

`BoundedScheduler`: global, per-provider and per-tenant inflight bounds; a bounded
queue; `REJECT` sheds with a typed `gateway_overloaded` (and counts it), `WAIT`
blocks for a queue slot and **re-checks the slot in a loop**, so a thundering-herd
wakeup cannot push the queue past `max_queued` (killed mutation
`scheduler_gives_up_after_one_queue_slot_wakeup`, test
`test_a_thundering_herd_wakeup_never_overfills_the_bounded_queue`). Joining the
queue itself pumps for a grant, so a waiter can never strand beside idle capacity
(defect #22, found by the mutation harness; test
`test_a_waiter_joining_an_empty_queue_is_not_stranded_beside_idle_capacity`, proven
to fail without the fix). Grants won during cancellation are released, task
cancellations are counted, and every scheduler test ends by asserting the
accounting returned to zero.

## 11. Rate limiting and quota awareness

`WindowRateLimiter` keeps a rolling minute window and a UTC-day counter per
provider, charges exactly one unit per served request, and refuses *before*
spending quota. Refusal ordering is deliberate: an infinite wait (day budget gone)
is `quota_exhausted`; a wait longer than `max_wait_seconds` is `rate_limited`; a
wait longer than the caller's remaining budget, when policy would have allowed it,
is `deadline_exceeded`. A policy refusal is counted as a throttle even though no
charge happened (`record_throttled()`, defect #21), so `throttled` in
`gateway.status()` is the truth rather than an under-count. And a rate-limit
refusal stays a rate limit even when the caller's deadline is short (killed
mutation `engine_reports_a_policy_refusal_to_wait_as_a_deadline`, test
`test_a_rate_limit_refusal_stays_a_rate_limit_under_a_short_deadline`: 15 refusals,
0 relabelled as deadlines, each carrying `detail="local_rate_limit"` and a
`retry_after`).

## 12. Circuit breaking and route health

Per-route `CircuitBreaker`: closed → open after `failure_threshold` counted
failures → half-open probe after `recovery_seconds` → closed after
`success_threshold`. Only provider-health kinds count (`counted_kinds`): a content
block or a caller cancellation says nothing about provider health, so neither opens
the circuit. An open route stays in the plan — removing it would make recovery
impossible — and only affects ordering.

## 13. Fallback as policy

`plan()` orders routes by rank, health and privacy; a hop happens only when the
typed error is fallback-eligible, `FallbackPolicy.allows(kind)`, hops remain, the
next route can actually serve the request (`require_capability_match`), and the
caller did not veto it (`request.allow_fallback`). Degraded (local/fake) routes are
allowed only as a last hop and are visible on the response: `degraded =
fallback_used OR served_by_degraded_route` (defect #9), and the facade appends
`DEGRADED_DISCLAIMER` so a user is never told a local fallback was the real model.
`features/summarizer.py` sets `allow_fallback=False`: a summary from a different
provider would be a fabricated summary.

## 14. Cancellation safety

Three signals, three outcomes, all proven: a cancelled task propagates
`asyncio.CancelledError` untouched (never converted, never retried, never fallen
back from, no orphan task, slot released); a withdrawal token yields typed
`CancelledByCallerError` while the caller's task stays alive; shutdown yields typed
`GatewayClosedError`, wakes every waiter instead of hanging it, refuses new work at
the door (no plan, no route attribution, no record), and stops an in-flight retry
chain at its own boundary. Every sleep is cancellation-aware, so a withdrawal
during a 2-second backoff lands in milliseconds — asserted as
`elapsed < 1.0` in `test_a_withdrawal_during_backoff_stops_the_retry_chain`, which
is what kills the mutation that drops the token from the sleep.
`KeyboardInterrupt` / `SystemExit` / `GeneratorExit` are re-raised verbatim; the
engine's own guard is proven with a settled future carrying the signal, because
`asyncio.Task` re-raises it into the loop before the engine can see it
(`test_a_signal_carried_by_the_provider_future_is_never_converted`).

## 15. Observability, and what is never recorded

Exactly one `RequestRecord` per request, for every outcome (`success`,
`degraded_success`, `error`, `cancelled`, `deadline_exceeded`, `overloaded`,
`policy_refusal`, `gateway_closed`). Recorded: request id, caller
(category/name/tenant), purpose, operation, outcome, provider and model that
actually served it, error kind + status code + retryable, usage, finish reason, the
five timings, `PolicyOutcome`, one `AttemptRecord` per attempt, `prompt_chars`,
`payload_bytes`, sanitized metadata, idempotency key.

Never recorded: prompt text, system text, message content, binary parts, the
provider's answer, credentials — and **`str(error)`**, because a provider message
can echo a prompt fragment or a key. `build_error_record` copies typed fields only.
Metadata passes `sanitize_metadata` (blocked keys dropped case-insensitively; 8
fields × 128 characters max) *at the builder's call site*, which is now tested at
the call site and not only in the helper
(`test_the_record_builder_itself_sanitizes_caller_metadata`). `redact_secrets`
catches `x-goog-api-key`, `Bearer`, `api_key=` pairs and URL userinfo. Metrics and
the record buffer are bounded (256 records).

## 16. Usage and cost truth

`Usage.source` is `provider` only when the provider reported counts; otherwise
`unknown` with `None` fields — never `0`, never an estimate derived from prompt
length (`is_known` is derived from the source, so an absent report cannot be read as
a free call). Garbage usage metadata is ignored rather than believed. Cost is
attached only when usage is provider-reported **and** the model has a pinned price
in `DEFAULT_PRICE_TABLE` (`PRICE_TABLE_VERSION = "2026-09-27"`); an unpriced model
reports no cost, and tokens the provider never reported are never costed. The table
holds the models this repository actually configures, priced at zero because that is
what the free tiers and local inference cost.

## 17. Adapters — provider is an adapter, and nothing more

`ProviderAdapter` = `name`, `operations`, `modalities`, `execute(...) ->
AdapterResult`, `aclose()`. An adapter translates: no retry, no fallback, no
metrics, no policy. `GeminiHttpAdapter` (REST, classifies from status code +
declared `error.status`, treats a blocking `finishReason` as `content_blocked`
rather than an empty answer, validates `base_url` — https unless loopback, no
userinfo, host required; `None` means the default endpoint, an explicit empty
string is refused), `LitellmRouterAdapter` (the Router stays a *deployment*
selector inside one route, registered `max_attempts=1` so no retry nests inside the
chain's own cooldown logic), `LegacyProviderAdapter` (wraps pre-W2 providers with an
explicit `{ExceptionType: kind}` map). A wrong return type at the adapter boundary
is a typed `internal_gateway_failure`, never a guessed answer.

## 18. Registry, process authority, compatibility

`install_llm_gateway` / `get_llm_gateway` / `set_llm_gateway` /
`reset_llm_gateway` hold one process authority, built by
`build_gateway_from_settings` from `config/settings.py`.
`gateway_for_credentials(key, model, …)` resolves three cases: the deployment key →
the process authority; a *different* key → a scoped single-route gateway
(LRU-capped at 16, fallback disabled, so one caller's credentials cannot borrow
another's routes); an empty key → a typed refusal rather than a silent anonymous
call. `facade.GatewayLLMProvider` keeps the pre-W2 surface — `generate`,
`complete`, `embed` — and the user-facing failure strings byte-for-byte
(`UNAVAILABLE_MESSAGE`, `DEGRADED_DISCLAIMER`, the busy message), because they are
product copy; provider detail never reaches a chat surface. `embed` raises a typed
error instead of returning `None` or an invented vector.

## 19. Migration status — the real inventory

Every path from a caller to a model, as verified mechanically by
`tests/architecture/test_llm_gateway_authority.py` (discovery = provider endpoint
literal, provider wire path, or provider SDK import; the inventory is a ratchet in
both directions — an undocumented path fails, and a stale permission fails too):

| Caller / module | Path | Mechanism |
|---|---|---|
| `features/ai_chat.py` | gateway | `gateway_for_credentials(..., base_url=BASE_URL)` → `execute` |
| `features/summarizer.py` | gateway | `gateway_for_credentials(...)`, `allow_fallback=False`; its own httpx client fetches *user URLs* under the SSRF guard (pinned by `tests/unit/test_summarizer_ssrf.py`) — not LLM egress |
| `features/ai_memory.py` | gateway | duck-typed `provider.gateway` → `execute` with `json_object_validator`; legacy `generate()` path only for injected doubles |
| `agents/store/base_agent.py` | gateway | duck-typed `gemini.gateway` → `execute` with gateway `Message` turns |
| `creative/video_director.py` | gateway | `gateway_for_credentials(api_key, model)` → `execute` |
| `llm/gemini_provider.py` | gateway | `GeminiProvider` executes through the engine's gateway and exposes `.gateway` |
| `llm/litellm_provider.py` | gateway | builds a lazily-owned `LLMGateway` around `LitellmRouterAdapter`, `max_attempts=1` |
| `llm/local_server_provider.py` | gateway | wrapped by `LegacyProviderAdapter` (`registry._build_llama_server_adapter`) |
| `llm/local_llama_cpp.py` | gateway | wrapped by `LegacyProviderAdapter` (`registry._build_llama_cpp_adapter`) |
| `llm/fallback_provider.py` | gateway-typed | compatibility wrapper; hops on typed `exc.fallback_eligible`; substring matching deprecated and off by default |
| `llm/fake_llm.py` | none | in-process test double, no I/O |
| `config/settings.py` | none | endpoint/credential configuration as data; performs no I/O (gated) |

**Pinned bypasses: 2, ratcheted (`MAX_PINNED_BYPASSES = 2`, may only fall).**

| File | Why it is still outside | What migration needs |
|---|---|---|
| `creative/image_gen/gemini_adapter.py` | Gemini **image generation**: `:generateContent` with `responseModalities: [TEXT, IMAGE]`, inline base64 image parts, aspect-ratio validation, paid-tier fail-closed guard, per-image cost estimate, prompt-hash cache. The gateway contract is text/modality-scoped for chat and embeddings | a binary-output adapter contract (`AdapterResult` carrying image parts) plus per-image cost accounting |
| `creative/slideshow/analysis.py` | **synchronous** httpx vision scoring for slide ordering; default provider is `local_heuristic` (Pillow + numpy, no network); the hosted leg is fail-closed behind `NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD` and uploads downscaled copies only | a sync entry into an async engine (or a job-queue hop) plus image-input support in the contract |

Both raise their own typed errors (`ImageGenerationError`, `AnalysisError`) and
neither classifies by substring, so LAW 3 holds everywhere even where LAW 1 does not
yet. Both spend the same Gemini key, so RPM/RPD accounting is still split across the
authority and these two files — recorded as forward work in the board claim, because
unifying it needs the bytes-out capability above and not a speculative abstraction.

## 20. Verification — commands and results

All on this branch, in this sandbox, Python 3.11.2, full dependency set installed
from `pip install -e ".[dev]"` (torch, transformers, litellm, langgraph, chromadb,
sentence-transformers, llama-cpp-python, python-telegram-bot, sqlmodel/alembic/
asyncpg/psycopg all present).

| Gate | Command | Result |
|---|---|---|
| architecture (structure) | `pytest tests/architecture/test_llm_gateway_authority.py` | **12 passed** |
| gateway behaviour | `pytest tests/unit -k llm_gateway` | **637 passed, 1 skipped** (13 files) |
| mutation campaign | `python scripts/llm_gateway_mutations.py` | **49/49 mutants killed**; baseline GREEN before, restored copy GREEN after; exit 0 |
| full suite | `pytest -q -rs -m "not slow"` | **3564 passed, 31 skipped, 0 failed** in 177.56s |
| baseline parity | vs. the recorded pre-W2 baseline 2915 passed / 30 skipped | **+649 passed, +1 skipped = +650 = 638 gateway tests + 12 architecture tests.** Nothing pre-existing regressed and the delta is fully accounted for |
| lint | `ruff check src tests scripts` | All checks passed! |
| format | `ruff format --check src tests scripts` | 479 files already formatted |
| types | `mypy src` | Success: no issues found in 259 source files |

Test volume added: 13 behaviour files (9,385 lines) + the architecture gate (762
lines) + the mutation harness (802 lines) = 10,949 lines.

**Mutation campaign shape.** 49 mutations across 8 modules (engine 20, scheduler 7,
adapters 7, policy 4, resilience 4, observability 3, facade 2, usage 2), grouped by
law: LAW 7 retry/bound, LAW 8 fallback, LAW 5 cancellation, LAW 6 bounds, LAW 4
single policy surface, LAW 3 typed truth, LAW 10/11 observability and truthful
accounting, LAW 3 resilience primitives, LAW 1/12 one authority + compatible
surface. The harness copies `src/nexus_ai_agent` to a temporary directory, refuses
to mutate a red baseline, runs each mutant against its designated test, restores the
file, and re-verifies the restored copy. It never edits the working tree.

**Ten survivors were diagnosed, not deleted.** The first run killed 36/49. Each
survivor was reproduced against a scratch copy and its cause established: a second
guard produced the same typed error (closed-gateway refusal, shutdown during a
retry chain), the mutated branch was unreachable in that test (backoff vs. budget,
rate-limit ordering), the mutant was behaviourally equivalent (post-sleep
cancellation raise, `error.get("message")` on a body whose message is not a status
token), or the invariant had only been tested at the helper and not at the wiring
(metadata sanitization, queue-slot loop). The answer was always a stronger test or a
mutation that states the law observably — eight new or strengthened tests, three
retargeted mutations, three new mutations. The interpreter-signal guard needed a
settled future rather than a task, because `asyncio.Task.__step` re-raises
`KeyboardInterrupt`/`SystemExit` into the loop before the engine can see them.

**22 real defects in the authority were found and fixed by this programme** (21 by
the adversarial/security/load suites during construction, the 22nd — a waiter
stranded beside idle capacity because `_pump` only ran on release — by the mutation
harness). They are listed with their killing tests in §21 of the PR description.

**Governance.** `gates_owner` is *not* held by this claim: `task-195-salvage`
(`arena/01a0e3b7-nexus-ai-agent`) holds the active gates lease until
2026-09-28T16:54Z. Per the board's `gates_rule`, the runs above are read-only
diagnostics inside an isolated sandbox, and the authoritative main-bound gate
verdict is deferred to the gates owner and to this branch's own CI on the exact head
SHA. No other agent's branch, PR or file was touched; `bot/app.py`,
`bot/handlers.py` (PR #113) and `features/request_queue.py` (task-195-salvage) were
left alone by design.

## 21. Honest limits and the remaining blocker

1. **Two files still perform LLM egress outside the authority** (§19). They are
   named, justified, typed, ratcheted and counted — but they are bypasses, and the
   shared Gemini quota is therefore accounted in three places, not one.
2. **Bounds are per-process.** The scheduler, rate windows, breaker state and
   idempotency cache are in-memory. Two processes get two sets of bounds; a
   fleet-wide limit needs a shared store, which no current deployment requires and
   which was not built on speculation.
3. **The price table is pinned, not live.** An unpriced model reports no cost rather
   than a guess; adding a model means editing `DEFAULT_PRICE_TABLE` and its version.
4. **The engine is async.** A synchronous caller cannot enter it directly — the
   concrete reason `creative/slideshow/analysis.py` is still outside.
5. **Idempotency coalesces, it does not persist.** A bounded in-memory window
   (`idempotency_ttl_seconds`, default 300s); a restart forgets, and a duplicate
   across processes is not detected.
6. **No cross-process rate limiting against the provider's own quota.** The local
   windows are configured from settings; if another process spends the same key, the
   provider's 429 remains the source of truth (and is classified typed).
7. **The authority is not yet the only composition root.** `gateway_for_credentials`
   builds a scoped gateway for non-deployment credentials by design; a single
   installed authority per process is the intended end state and depends on the
   composition roots being wired in one place, which is runtime work owned by
   another zone.

None of these is a hidden bypass: each is either pinned by a gate that fails on
growth, or a stated absence of a capability nobody has needed yet.

---

### One-sentence answer

NEXUS now has a true Global LLM Authority for text generation and embeddings — one
contract, one policy surface, typed truth, bounded everything, proven cancellation,
truthful usage, 49/49 mutations killed, and a structural ratchet that fails CI on
any new bypass — with exactly two named, justified, ratcheted exceptions
(`creative/image_gen/gemini_adapter.py` for image generation and
`creative/slideshow/analysis.py` for synchronous vision scoring) that still reach a
provider directly and are the only remaining caller/provider bypasses.
