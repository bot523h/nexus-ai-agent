# W2 Golden Hardening & Final Proof — 2026-09-28

> **Verdict: W2 NOT VERIFIED.**
> This is an independent successor audit, not a renewal of PR #116's verdict.
> **Evidence collection is in progress in this draft.** Pending rows are not passes.

## 1. Identity, exact revisions and evidence boundary

- Repository: `bot523h/nexus-ai-agent`.
- Session branch: `arena/01a0e846-nexus-ai-agent`; no other branch was checked out or pushed by this session.
- Implementation HEAD being evaluated: **`84e9a3f37cec9b91810c7b37e14777df7feff76d`**.
- Main, independently read from the remote: **`e5b326b2eaf691a638d030ad57acf1ce60016ef0`**.
- Owner-authorized predecessor: PR [#116](https://github.com/bot523h/nexus-ai-agent/pull/116), HEAD **`9e795318fa7651ed35f3dfa373bb0ce49d21fcc7`**. Its exact history was retained, not rewritten or merged on GitHub.
- Successor: draft PR [#118](https://github.com/bot523h/nexus-ai-agent/pull/118).
- A subsequent report-only publication commit is distinct from the implementation revision above. Its hash cannot be embedded in its own content. Execution evidence remains bound to the explicit implementation SHA and source fingerprint; publication CI is separately checked.
- Local interpreter: Python **3.11.2**. Installed dependency snapshot: `ci-artifacts/w2/freeze.txt`. No new production dependency, queue, broker, breaker stack or telemetry backend was introduced.

The predecessor's claimed main baseline of 2915 passes is **historical, not a fresh local measurement of main by this session**. A main baseline, predecessor tests, and successor tests are not interchangeable.

## 2. Governance, lease and concurrent work

Task `task-197-w2-global-llm-gateway` was transferred with explicit owner authorization to this session; the local Board records its 48-hour lease starting `2026-09-28T13:54:02Z`. `gates_owner=false` was retained. The task-195 gates claim was not taken over. Local repetitions are isolated diagnostics; they do not confer ownership of main-bound release gates.

Before edits/pushes, `.agents/board.json`, remote branches, open PRs and path overlaps were checked with `scripts/agent_board.py check --files … --branch arena/01a0e846-nexus-ai-agent`. Remote PR #117 and #113 Boards were also fetched when extending the proof to the PDF recovery test. The new test path was claimed and pushed **before** editing it. Shared indexes, W1 composition roots, `pyproject.toml`, CI workflows and another agent's PR were not rewritten or merged.

The inherited Board note contains historical VERIFIED/CI statements. They remain historical text; the successor note and this report explicitly do not inherit them. PR #117's runtime/publication work and W1 PRs #115/#113/#112 are not integrated here. Integration conflict resolution is still required before combining those branches.

## 3. Actual architecture and authority scope

```text
canonical provider factory / deployment-key callers
  → registry.get_llm_gateway / gateway_for_credentials
  → GatewayLLMProvider or an explicit LLMRequest
  → LLMGateway.execute
  → policy → bounded admission → owned breaker permit → rate gate
  → one raw provider adapter attempt
  → typed result/error → retry or policy fallback → observation
```

This is a **process-local deployment authority**, bound to its first execution event loop and creating PID. It is neither fleet-wide coordination nor a promise that only one Python gateway object can ever be constructed. Construction for isolated tests/explicit embedding is different from installing the deployment authority.

Separate non-deployment credential/model scopes and an explicitly constructed legacy Router bridge still have separate policy state. Two image/vision egress exceptions remain. Those limitations must not be hidden behind the word “Global”.

## 4. Full caller inventory and architectural guards

| Entry/role | Actual path and remaining boundary |
|---|---|
| Canonical `llm/provider.py` factory | Registry-backed facade; preserves local provider pin and local hash embedding compatibility. No private/nested Router gateway on this path. |
| `features/ai_chat.py`, `features/summarizer.py`, `creative/video_director.py` | Credential resolver → request → gateway. Summarizer's URL-fetch client is SSRF-controlled document fetching, not LLM execution. |
| `llm/gemini_provider.py` / Gemini engine compatibility | Exposes and executes through the gateway. |
| `features/ai_memory.py`, `agents/store/base_agent.py`, downstream knowledge callers | Gateway-capable provider when composed canonically; plain injected provider protocol remains an explicit embedding/test seam. |
| `llm/local_server_provider.py`, `llm/local_llama_cpp.py` | Provider implementations wrapped by a gateway adapter, not newly installed authorities. |
| Raw LiteLLM Router adapter | Canonical composition disables SDK retries/fallbacks so gateway policy is not bypassed. |
| Explicit `LiteLLMRoutingProvider(...)` | Legacy bridge retains a private gateway and SDK routing chain; not the canonical factory and not proof of shared deployment policy. |
| `FallbackProvider` | Typed compatibility shim. Deprecated keyword argument is now inert; backup cancellation propagates. It is not the canonical authority/factory. |
| `creative/image_gen/gemini_adapter.py` | **Real pinned egress exception:** binary image output, paid-tier guard and separate image resilience/accounting. |
| `creative/slideshow/analysis.py` | **Real pinned egress exception:** opt-in synchronous hosted vision; local heuristic is the default. |
| Fake providers / local hash embeddings / settings endpoint constants | No remote model execution. |

`tests/architecture/test_llm_gateway_authority.py` discovers endpoint literals, provider wire paths, SDK imports and construction aliases; checks roles and construction inventory; ratchets the two pinned egress exceptions; and tests detector liveness. A new explicit guard forbids membership-based classification in the legacy fallback module, including variable needles that escaped the original literal detector.

These AST guards cover known source patterns, **not arbitrary dynamic Python or all transitive SDK internals**. Provider implementations may own transport I/O; callers must not independently own execution policy. “Wrapped somewhere” alone is not evidence that every possible direct invocation is canonical.

## 5. Authority lifecycle, workers and import order

All registry writers use the same authority lock. A warm getter may avoid the lock; execution/retirement share an execution lock, so a stale returned reference cannot resume execution after revocation. A conflicting installer is rejected and its idle candidate retired, rather than silently replacing the installed authority. Reset of active or abandoned work is rejected. Reset of an idle authority revokes old references; the owner still must close transports.

The credential cache is bounded at 16 live scopes and does not evict a live scope to create overlapping authority. Deployment credentials resolve to the installed authority. Different `(credential, model)` scopes deliberately remain independent, including their quota state.

Golden tests cover concurrent installers, reset during active work, stale references, foreign event loops, inherited PID rejection, and fresh subprocess import order without eager installation. The PID regression simulates a changed PID; it is not a fleet deployment test. Fresh workers own their own authority. A process-local singleton cannot enforce a fleet-wide provider quota.

## 6. Defect #23 — multiple singleton builds

**Root:** the original getter's check-then-build race produced multiple gateways under concurrent first use. The predecessor added getter locking, but independent successor tests found installer/reset writers could still replace or detach authority outside the same protocol.

**Fix:** serialized publication and all writers; explicit conflicting-install rejection; idle revocation; active-reset refusal; execution lock, PID/loop ownership checks. Logical authority is the installed execution right, not merely the identity returned by one getter.

**Proof:** `test_llm_gateway_registry_race.py` plus golden installer/reset tests; original hardening RED log `golden-red.log` includes installer and stale-reference failures. Mutation removal of singleton/writer protection must fail the behavioral oracle. Repetition results are recorded in §21, not inferred from one pass.

## 7. Defect #25 — half-open double release

**Root:** outcome accounting decremented a shared probe count and cleanup decremented it again. A completed/non-probe attempt could release another in-flight probe, falsely creating recovery capacity. Checking only the final count missed the overlap.

**Fix:** an owned `CircuitPermit` records probe ownership and generation. Outcome recording is separate from release; release is idempotent in `finally`; each retry reacquires permission. Stale success cannot heal a newer trip.

**Deterministic oracle:** two probes are admitted and parked. Finish/cancel/fail A while B is still parked: count must be exactly **1**, not 0. Settle B: count must be **0**. Parameterized traces cover success, typed failure, cancellation, raw exception, invalid structured output and timeout. Additional tests cover a non-probe completing after a later probe, failed scheduler cleanup and generation changes.

`golden-red.log`: **8 failed / 2 passed** against the earlier implementation, including four overlapping-probe outcomes. Golden regressions and mutation tests enforce acquire/release ownership rather than merely a nonnegative counter.

## 8. Historical unknown full-suite failure and same-SHA CI discrepancy

The historical failed run is still **not root-caused**:

- Predecessor HEAD: `9e795318fa7651ed35f3dfa373bb0ce49d21fcc7`.
- Push run [36417970680](https://github.com/bot523h/nexus-ai-agent/actions/runs/36417970680): success, independently previously queried.
- PR run [36417975197](https://github.com/bot523h/nexus-ai-agent/actions/runs/36417975197): failure in `python-parity (3.12)`, job **108913756977**.
- Live job metadata confirms dependency installation **succeeded**; the test shell step (`Run set -o pipefail`) failed with exit 1.
- PR merge commit `ffe8685b7eccbe723c8e0578d5f89b5561add367` and predecessor HEAD have the same tree `58c14b77f5fe57cdb915fd318b82c4ec2c5ca6e2`.
- Artifact **10968803297**, `parity-log-3.12`, is listed and unexpired. Job-log/artifact downloads hit TLS EOF at the Actions/Azure redirect in this environment. GitHub repository authentication itself works.
- Fresh annotations contain exit 1 and runner notices, **not the failed test identity**.

Therefore no claim of “flaky”, “environmental”, “already fixed”, or “the same failure as below” is justified. The missing test log is an external evidence blocker. Required closure: obtain the artifact, name/minimize the failure, and compare its schedule/environment with the fixed candidate. A fresh green run does not explain a historical red run.

## 9. Independently reproduced full-suite failures — kept separate

| Failure | Evidence/root | Fix and proof |
|---|---|---|
| Successor Board-zone diagnostic | Initial `baseline-full.log`: 1 failed, 3570 passed, 31 skipped. Newly claimed proof paths absent from zone paths. | Zone declaration corrected in `5befbc7`; not evidence about PR #116's unknown test. |
| `test_summarizer_sends_key_in_header` | `full-01.log`: 1 failed, 3590 passed, 31 skipped. Cached gateway crossed pytest event-loop lifetime. | Module-local gateway lifecycle teardown; transport repetitions and compatibility tests. |
| Three clean-clone runner failures | Runner placed `--basetemp` **inside** the source/Git root. Pack coverage projected fixture paths relative to that root; Git discovery saw the enclosing clone. | Exact three tests: **3 RED inside / 3 GREEN outside** (`runner-tmp-{red,green}.log`). Temporary roots moved outside each clone. No production/test assertions relaxed. |
| PDF recovery T11 residue assertion | One of the first three isolated full suites saw `.prev` after durable `COMPLETED`. Queue contract deliberately finalizes backup **after** durable commit. | Barrier probe proves status is observable before finalization. Test now joins the actual task before asserting residue absence. Deterministic companion test rejects omission of that join; scratch mutant fails. No production queue edit. |

**Further final-head finding:** `84e9a3f` run 04 failed `test_delivers_to_originating_chat_not_user_id`: a sent-message observation preceded the async database status commit. A paused `_mark_status` reproducer proves the ordering (`reminder-deterministic-red.log`). The test-only task-join correction and negative oracle are being prepared; this is not attributed to the unknown historical CI failure.

The failed `9415153` clean-clone batch is preserved: one run had four failures, two had three. It was stopped, not counted as clean proof. Four subsequent `5a4e8fc` clean runs passed; that campaign was then stopped to restart on the final implementation SHA. Incomplete runs are not passes.

## 10. Additional evidence-driven fixes

Every extension followed a concrete defect → oracle → minimal change, rather than a new execution framework:

- Shared idempotent waiters ignored their own deadline; equal-length binary inputs collided in request fingerprints.
- Live idempotency/scoped entries could be detached; WAIT callers could grow an unbounded outer backlog.
- A cancellation-resistant provider outlived timeout cleanup while replacement work was admitted.
- **Second cleanup defect:** cancellation *during* timeout settlement escaped abandoned-task ownership. `additional-probes-red.log` reproduces the incorrectly admitted second execution. `_settle` now records still-live tasks before propagating cancellation.
- Retry could bypass an open breaker; stale success could heal a later breaker generation.
- Retry-After could be shortened by a local backoff cap.
- Raw exception chains and plaintext idempotency values reached telemetry; cached logical callers lost records; invalid structured output could be recorded twice; sink/cardinality collections were not all bounded.
- SDK exception classification could trigger import-time remote pricing I/O.
- Model-name-based free pricing and malformed SDK token coercion invented accounting (§17).
- Legacy opt-in keyword fallback still reclassified success and logged answer/keyword text; typed backup cancellation was swallowed (§16).

Relevant RED logs are preserved in `ci-artifacts/w2/`; named permanent tests are in the golden, observability, adapters, engine and architecture suites.

## 11. Composed execution policy and retry

A request is planned once, then each physical attempt crosses cancellation/deadline checks, admission, an owned breaker permit, rate gating and the adapter boundary. Retry is selected by typed `LLMErrorKind`, never provider prose. Authentication, invalid input, content blocks and cancellation do not retry the same route.

Attempt count, jitter/backoff and total budget are bounded. Retry reacquires breaker permission, so opening after the first failure actually stops later attempts. HTTP Retry-After delta/date parsing is explicit; a wait above the available budget is refused rather than silently shortened. Fallback uses the remaining total budget, not a fresh deadline.

A classification SDK import sets offline pricing before importing types. The regression proves that ordering; it does not claim arbitrary explicitly overridden SDK environment settings can never enable network I/O.

## 12. Timeout and deadline proof

Tests separately exercise queue wait, rate wait, per-attempt timeout, retry sleep and caller total deadline. Coalesced waiters retain independent deadlines. The provider race does not rely solely on `asyncio.wait_for`, which can wait indefinitely for a coroutine suppressing cancellation.

Settlement has a finite grace period. Tasks that refuse cancellation remain tracked, and further execution is quarantined until they settle. This is a fail-closed capacity guarantee, **not** the ability to forcibly stop an arbitrary coroutine or undo an already accepted remote provider request.

## 13. Cancellation proof

| Boundary | Enforced result |
|---|---|
| Before admission | Typed withdrawal refuses without a provider call. |
| After entry guard, before admission | Deterministic event schedule still produces zero calls. This strengthened the previously surviving retry-loop mutant. |
| Queued/rate-waiting/backoff | Stop promptly; do not retry or fallback; release owned admission. |
| Provider execution | Native `CancelledError` propagates; typed token withdrawal stays typed. |
| Timeout-cleanup cancellation | Still-live provider/wait tasks are retained before propagation; no replacement execution while abandoned work lives. |
| Shared idempotent waiter | Its cancellation does not cancel the owner. |
| Legacy backup provider | Both native and typed cancellation propagate instead of becoming “both unavailable”. |

Exactly-once release means **release of each locally acquired resource**. It is not an exactly-once remote execution or billing guarantee. Native cancellation is not converted into a successful response.

## 14. Bounded resources

| Owned resource | Bound/evidence |
|---|---|
| Admission | Default 4 global / 2 provider / 2 tenant; queue 32. Ingress cap is global in-flight + queue, including WAIT mode. |
| Abandoned work | New execution refused while tracked abandoned tasks remain. Cleanup interruption preserves ownership. |
| Idempotency | Default capacity 128; active entries are not evicted; completion cache bounded, TTL 300 seconds. |
| Credential/model authorities | Maximum 16 live scopes; no live LRU eviction. |
| Observation sinks | Maximum 128, with identity-distinct saturation regression. |
| Recent records | Ring buffer 256. |
| Provider-error cardinality | 128 distinct keys plus one overflow bucket; bounded labels. |
| Metadata | At most 8 entries, bounded value length 128; pre-sink redaction. |
| Breaker/rate maps | Keyed by configured routes/providers, not caller text; stress checks growth with traffic. |

These are bounds on gateway-owned state under the exercised valid policies. They are not a byte-level RSS ceiling for arbitrary request/response payloads, arbitrary injected Python code, or custom blocking sinks. Load tests measure owned tasks/counters and bounded collections; no universal fleet or memory-allocation theorem is asserted.

## 15. Circuit-breaker behavior

Closed → open follows typed health failures and threshold; open rejects without an adapter call; recovery admits a finite number of owned half-open permits. Non-health errors do not manufacture provider sickness. Each retry must reacquire. A late old-generation success cannot clear a newer trip. Release and outcome are different operations.

The concurrency oracle inspects state while a sibling probe is still running, not only after all tasks finish. This is why the double-release mutation is meaningfully killed.

## 16. Fallback and transitional compatibility

Canonical fallback is a policy decision: eligibility, capability/privacy, hop limit, remaining deadline and degraded-route rules. It is recorded as fallback/degraded and preserves caller opt-out. Content-block and cancellation are never laundered into another provider attempt. The raw canonical Router cannot run an independent hidden chain.

The explicit legacy `FallbackProvider` previously violated these rules even with the canonical path fixed. `legacy-fallback-red.log` records two failed probes: a successful article containing “quota” was replaced; a typed cancelled backup became a string error. The keyword parameter is now **accepted but inert**, with a count-only warning. Raw keyword/answer previews are removed; error labels are redacted. Both forms of cancellation propagate. Functional and architecture regressions, plus five new mutants, cover this path.

Other legacy Router/private-scope state remains explicitly outside the single deployment-authority promise; preserving a constructor's API is not proof that it shares one scheduler.

## 17. Truthful usage and cost

Provider-reported counts and known prices are separate facts. `Usage.source=PROVIDER` does not prove the billing tier. The former default zero prices for Gemini, Groq and `local-model` inferred a contract from a name; this was incorrect. They now remain **UNKNOWN** unless the operator pins prices. Only the explicit OpenRouter `:free` entry retains a default zero. Table version: **2026-09-28**.

Pinned prices reject negative/NaN/Infinity values. LiteLLM usage no longer coerces booleans, negative values, strings, fractional floats or non-finite values into reported integer token counts. The initial scratch oracle recorded **18 failures** across pricing, aggregation and malformed SDK counts; permanent regressions retain those distinctions. Free and paid explicit operator pins both have positive controls.

Metrics expose a known-cost subtotal separately from unknown-cost request count. A complete `estimated_cost_usd` is null if any physical request has unknown cost, including earlier failed attempts whose usage was not reported. Cache/coalescing records do not double-charge token usage. This is an estimate with provenance, not an invoice or all infrastructure expense.

## 18. Observability and logical request identity

Logical callers get distinct correlation IDs and one terminal record each, even when they reuse one physical execution. Reused records do not invent a second provider attempt or spend. Structured-output failure produces one failure record, not a preceding success plus failure.

Records carry bounded caller/purpose/provider/model, attempt outcomes, timings, typed error kind, fallback/degraded decisions and usage provenance. Idempotency values are hashed. Raw prompt, model answer and exception traceback chains are not event payloads. Sinks receive sanitized records, not only the default logger's filtered projection.

Rejected lifecycle misuse before admission (retired gateway, wrong PID/loop) is distinct from an admitted logical request; the report does not claim those programming errors all receive ordinary execution records.

## 19. Security proof and limits

Regression canaries cover exception chains, API-key/bearer patterns, metadata, idempotency tokens, hostile provider labels and legacy fallback diagnostics. Public compatibility messages do not expose raw provider detail. Cancellation and policy/content refusal do not trigger unsafe alternate-provider execution.

Redaction recognizes bounded patterns; it does **not** recognize every possible semantic secret embedded in arbitrary text. AST architecture checks are regression guards, not a Python sandbox. No live provider credentials or production model calls were required or claimed. The two named image/vision lanes still have separate egress/privacy/accounting boundaries.

## 20. Test, type and compatibility evidence

| Diagnostic on final implementation | Result |
|---|---|
| Test collection | **3666 tests collected** (`inventory-84e9.log`). Collection is not execution. |
| Ruff check | **PASS** (`final-lint.log`). |
| Ruff format | **564 files already formatted**. |
| Mypy | **259 source files clean** (`final-types.log`). |
| Architecture + Gemini key transport + LiteLLM + local-server compatibility selection | **52 passed** (`final-compat.log`). |
| Final full-suite execution | `84e9a3f`: three runs **3635 passed / 31 skipped**; one **1 failed / 3634 passed / 31 skipped**, reminder status race. |

Earlier intermediate proofs are retained but not promoted: broad preproof 688 passed / 1 skipped; cost/completion diagnostic 321 passed including scratch probes; legacy focused diagnostic 72 passed including scratch probes. A style error in intermediate `071652d` was corrected by `84e9a3f`; final lint was rerun.

Full means the repository CI selection `pytest -m 'not slow'`, not execution of all optional external services. Local PostgreSQL, optional extras and deployment-key skips remain gaps; CI parity/extras/database jobs must be checked separately.

## 21. Stress and repeated clean execution

The runner varies `PYTHONHASHSEED`, uses fresh processes, records exit status, elapsed time and log SHA-256, and rejects root-tree drift. Full isolation mode snapshots a clean immutable seed and creates a fresh clone on the **same session branch name** for each run, with an external per-run temporary root and import-origin check. Installed dependencies and immutable Git objects are shared, not test working trees. Up to four workers run in finite batches; a failing batch prevents the next batch.

| Campaign | Exact revision | Outcome |
|---|---|---|
| Historical race / engine / transport / load | `70bdc7d` | 40 / 40 / 20 / 40 green; full 3 green then deliberately stopped for a real cleanup bug. |
| Historical race / engine / transport / load | `9415153` | 40 / 40 / 20 / 40 green; isolated full batch failed as diagnosed in §9. |
| Transitional clean full | `5a4e8fc` | 4 green, then deliberately stopped to move proof to final code. Partial later runs not counted. |
| Final race | `84e9a3f` | **40/40 green** |
| Final engine | `84e9a3f` | **40/40 green** |
| Final transport | `84e9a3f` | **20/20 green** |
| Final load | `84e9a3f` | **40/40 green** |
| Final independent clean full suites | `84e9a3f` | **3 green / 1 failed**, stopped after first batch; requested 20 not achieved |

Load scenarios include 200-caller bursts, global/provider/tenant limits, saturation rejection, sustained contention and priority, 429/local quota pressure, transient failures, fallback behavior in engine tests, cancellation/timeout/mixed storms, 1000-request record bounds, idempotency flooding, repeated close/open and task-count stability. These are deterministic/local adapter workloads, not a live provider or distributed load test.

## 22. Mutation proof and blind spots resolved

Final inventory: **82 mutants**, each applied to a scratch source copy; only an actual pytest test failure counts as a kill. Harness requires a green baseline and green restored baseline. Timeout/import/harness errors do not count as killed mutations.

**Final implementation battery: 82/82 killed**, baseline and restored baseline GREEN (`mutations-84e9.log`). The earlier `9415153` battery was **71/71 killed**, baseline and restoration green. A prior 69/70 run is preserved as a real survivor, not relabeled green: the original retry-loop cancellation mutation survived because an earlier entry guard masked the missing loop guard. A new event schedule sets withdrawal between entry and admission, demands zero provider calls, and killed the unchanged mutation (targeted 1/1, baseline/restoration green).

Coverage includes singleton/second installation, stale authority, probe double release/generation, nonretryable classification, cancellation swallowing, deadlines, global/provider/queue bounds, breaker/gateway bypass, raw errors/secrets/request IDs, fallback and Retry-After. New accounting mutants cover inferred free pricing, invalid pins, aggregate unknown cost, earlier retry spend and malformed SDK counts. New legacy mutants cover variable-keyword classification, typed cancellation and secret labels.

Separate T11 synchronization mutant: removing the join fails the deterministic companion test (`t11-mutant-killed.log`). This is reported separately, not added to the gateway 82 count.

## 23. Exact-head GitHub CI and main reconciliation

Implementation SHA: `84e9a3f37cec9b91810c7b37e14777df7feff76d`.

- Push workflow: [36443911061](https://github.com/bot523h/nexus-ai-agent/actions/runs/36443911061).
- PR workflow: [36443916439](https://github.com/bot523h/nexus-ai-agent/actions/runs/36443916439).
- **Current final status: PENDING.** Absence of failed checks is not completed green CI.
- Superseded successor workflows were cancelled by workflow concurrency; cancelled runs are not passes. Historical predecessor success does not establish this SHA's result.
- Fresh rollups/jobs and remote refs are preserved in `ci-artifacts/w2/`. Final publication checks must be bound to their own head, not substituted for implementation execution evidence.

No merge to main was performed. Independent local diagnostics and a candidate's CI do not authorize release, override another agent's gates lease, or prove behavior after a future W1/W2 merge.

## 24. Residual risks, blockers, verdict and direct answer

| Item | Root/evidence | Fixed here? | Remaining / why unresolved |
|---|---|---|---|
| Historical Python 3.12 full-suite failure | Same-tree push success / PR failure, test-step exit 1; failed test log unavailable through TLS redirect | **No** | Obtain artifact and minimize exact failure. Fresh green tests cannot supply missing historical identity. |
| Two real provider egress exceptions | Image generation and synchronous hosted vision execute outside gateway | **No** | Binary-output/sync-entry integration and ownership coordination are separate scoped work; shared credential quota is not globally centralized. |
| Non-deployment scopes and explicit legacy Router | Separate policy objects are reachable by deliberate construction | **Partially** | Canonical factory no longer enters a private/nested path; explicit bridges/scopes remain separate. Do not claim one scheduler for every credential/model/object. |
| Runtime lifecycle composition | Revocation is not asynchronous transport cleanup | **Locally hardened, not integrated globally** | W1 composition-root integration/merge needs the owner and its lifecycle proof. This PR does not rewrite leased roots. |
| External services and fleet behavior | Local skips; process-local coordination; cancellation-resistant remote work | **Not claimed** | Requires actual service/deployment proof. No fleet quota or remote exactly-once guarantee. |
| Arbitrary secret/payload/custom-code behavior | Pattern redaction and bounded owned collections are not universal semantic/byte-level isolation | **Known limits** | Do not turn tested local invariants into an unlimited security or memory guarantee. |
| Final repetitions / mutation / exact-head CI | Pending at draft time | **Pending** | Replace only with completed, SHA-bound evidence. |

**Verdict: W2 NOT VERIFIED.** Concrete authority/resource/accounting defects were reproduced and hardened, but the missing historical failure identity and explicitly remaining authority/egress boundaries prevent the unqualified final W2 claim.

**«آیا NEXUS اکنون در محدوده‌ای که معماری تعریف کرده یک Global LLM Authority واقعی دارد، یا هنوز یک execution/provider bypass یا split-brain authority باقی مانده است؟»**

پاسخ دقیق: مسیر canonical با کلید deployment، یک مرجع منطقی process-local دارد و regressionهای رقابت نصب، reset و مالکیت probe از آن حفاظت می‌کنند؛ این به‌معنای یک مرجع fleet-wide یا یک scheduler برای تمام credentialها نیست. در کل NEXUS هنوز دو provider bypass صریح و scope/bridgeهای دارای state مستقل باقی مانده‌اند. آن‌ها را با رفع race اصلی، «حل‌شده» یا «بدون bypass» اعلام نمی‌کنم. همچنین تا هویت شکست تاریخی روشن نشود، تأیید نهایی W2 قابل دفاع نیست.
