# Master Engineering Truth Report — 2026-09-27

**Repository:** `bot523h/nexus-ai-agent`
**Authoritative `main` at live inspection:** `93c7809ad1bc1cd93212578386159c0a26c5e3f2`
**Candidate branch:** `arena/01a0e2f0-nexus-ai-agent`
**Queue implementation commit:** `260dfc62c1aa9f81ec587e9bd935a54d52566b4b`
**Implementation tree tested locally:** `a33c4f129d459a98d2b1862027909f0b41d40693`
**Candidate PR / remote CI:** not yet opened/available at this report revision; must be updated after submission.
**Decision:** the bounded, process-local queue lifecycle slice is locally verified on the candidate tree. It is not yet merged or remotely CI-verified. The overall product is **not proven production-ready, durable, or globally rate-limited**.

## 1. Scope, truth labels, and evidence boundary

This report refreshes the engineering truth after PR #106 and records the `task-195-llm-queue-lifecycle` implementation. It distinguishes merged-main facts from candidate-branch evidence and from unverified operational claims.

- **PROVEN** — the narrow claim has executable or operational evidence bound to the exact SHA named here.
- **PARTIALLY PROVEN** — a bounded component is evidenced, but the larger domain is incomplete or split across paths.
- **NOT PROVEN** — no sufficient exact-SHA evidence was found for the claim.
- **BLOCKED** — required owner-controlled infrastructure, credentials, or external operation evidence is absent.
- **DEFERRED** — deliberately outside this claim or fenced by another active PR/owner.

A green test or CI job proves only the behavior and environment it exercised. Candidate-PR checks are not main truth; local tests are not remote CI; PostgreSQL migration CI is not a production backup/restore; local coroutine cancellation is not proof that a remote provider stopped processing a sent request.

### Exact repository and GitHub snapshot

| Item | Observed truth | Evidence / consequence |
|---|---|---|
| Main identity | `origin/main` fetched at `93c7809ad1bc1cd93212578386159c0a26c5e3f2`; the session branch was two claim commits ahead before implementation. | PR #106 is a docs-only merge: head `6eeef1d37db1e87ba98c10934943e2df14b459e4`, merged at `2026-09-27T13:01:33Z` as `93c7809...`. Source-level findings in the Phase Zero audit anchored at `f53923d...` remain applicable because #106 changed only `docs/` files and `docs/README.md`. |
| Exact main CI | Run [36320966850](https://github.com/bot523h/nexus-ai-agent/actions/runs/36320966850) completed successfully on the exact merge SHA `93c7809...`: **13/13 jobs succeeded** (CI, lint/type/version, non-slow pytest, Python 3.10/3.11/3.12 parity, extras, trust mutations, PostgreSQL migration, release lineage). | Valid evidence for those jobs on main, not for unrun production operations or this queue change. PR #106: [#106](https://github.com/bot523h/nexus-ai-agent/pull/106). |
| Open PR inventory | **32 open PRs** at the live query. The GraphQL file scan returned at most 99 changed files per PR, so the 100-file query was not truncated. No open PR changed the queue source, this queue test, the mutation script, or the planned root report path. | The `docs/architecture/MODULE_MAP.md` path is changed by #59, #68, #69, #87, #102, #105, and #107; it remains deferred. `.agents/board.json` is a shared coordination file and is also changed by #107, so a later merge may need explicit board reconciliation. |
| Relevant candidate PRs | #105 (`80013e3225c3fd713c86e9d4800cde6032e8f2e7`) and #93 (`ab89319312bcad9995e1cd216ee476863ca4cada`) were `CLEAN` with their displayed checks passing, but neither is merged. #107 (`c39f4984ca7cdb6915d88f001be1ffa42e1ddbf2`) and #102 are `DIRTY`; #96, #97, #100, #94, and #99 are also `DIRTY`. | Candidate work is not promoted to main truth. #105 contains shell hardening; #93 contains typed provider-failure work; #96 is a deployment/restore candidate; #97/#100/#94 are capability/lifecycle candidates; #107 supersedes the Continuum line. |
| Task ownership | Board claim is `task-195-llm-queue-lifecycle`, active on this exact branch, `gates_owner=true`, claimed `2026-09-27T13:02:33Z`. Claim commits: `565c429e84aa8013368786a1dd20ad07e59d38c3` and `1564ba4eeaad5e5e0531ae74bec71300e20a2684`. | Exclusive implementation paths are `request_queue.py`, `test_request_queue.py`, `llm_queue_mutations.py`, and this report. `scripts/agent_board.py check` returned `no overlap — safe to proceed` for the implementation files. The architecture-map exclusion is intentional. |
| Candidate implementation | Commit `260dfc62c1aa9f81ec587e9bd935a54d52566b4b`, tree `a33c4f129d459a98d2b1862027909f0b41d40693`, pushed only to `arena/01a0e2f0-nexus-ai-agent`. | The exact candidate commit is not in main. There is no main proof for this change until a reviewed merge and exact-merge-SHA CI. |

## 2. Baseline failure reproduction

The original queue from exact `origin/main` SHA `93c7809...` was extracted to a temporary directory and executed without changing the checkout. The failures reproduced:

| Scenario | Observed on baseline | Required behavior |
|---|---|---|
| Same user: one provider active, a second queued request times out | `queue_position(user_id)` became **0** while the first request was still pending; timeout cleanup and `finally` both decremented the count. | Keep the first request accounted until its own terminal transition; decrement each request exactly once. |
| Timed-out queued request after backlog release | Its provider callback was invoked (`True`). A caller timeout did not retract the priority-queue item. | A queued timeout/cancellation must prevent provider invocation. |
| Close with active and queued submissions | Both submitter tasks remained unfinished (`False, False`) after `close()`. | Settle all accepted waiters, cancel local in-flight work best-effort, and reject new submissions. |
| Same-priority scheduling | Baseline source sorted by `(priority, monotonic creation time)` and had no per-user rotation. | Rotate users round-robin within a tier; retain and document strict priority between tiers. |

These are local reproductions of the baseline, not a claim that a remote HTTP request can be withdrawn after transmission.

## 3. Current system map and preserved foundations

### LLM and memory paths

- `bot/app.py` constructs a process-local `GeminiRequestQueue` and a `GeminiEngine` configured with it. `GeminiEngine.chat` and one other queued call path use that instance.
- `bot/handlers.py::build_handlers` also constructs a separate `GeminiEngine` without the shared queue or conversation store. Other feature, memory, and knowledge paths can construct provider/engine instances independently. Direct methods such as vision, translate, summarize, and other legacy calls do not all pass through one request gateway.
- `GeminiEngine._call_gemini` returns Persian error strings for HTTP non-200 responses rather than raising a typed HTTP exception. The new queue therefore cannot safely classify those returned strings as retryable provider exceptions; it deliberately does not parse message text.
- No application shutdown call to `request_queue.close()` was found in the bot composition paths inspected. The queue’s close behavior is tested as an API contract, but application-wide lifecycle wiring is not established by this change.
- Two materially different memory paths remain: consent-oriented `AIMemoryEngine` feature storage and graph `LongTermMemory`. The latter stores `(id, thread_id, content, embedding)`, discards its `metadata` argument, performs synchronous SQLite I/O inside async methods, falls back to recency results when vector search is unavailable/fails, and formats retrieved text as prompt context without a provenance/instruction-data trust label.

### Effects, jobs, and operations

- Preserve the merged Nagar trust/effect substrate: data-only capability packs, Ed25519 verification against an external trust root, `TypedCommand`/capability registry/`CommandBus` boundaries, reference resolution, deterministic RenderIR compilation, controlled FFmpeg invocation, and independent artifact verification. These are established foundations, not permission to route generic model output around authorization or evidence checks.
- The graph’s general planner/executor remains a simpler keyword/substring, one-step path; it does not provide a typed multi-step plan, durable approval identity, replay protection, or a general claim/evidence chain.
- The SQLite `InProcessJobQueue` has useful local idempotency, fencing, lifecycle, and verification primitives, but it is not a multi-worker durable scheduler. The webhook path can acknowledge before durable business acceptance.
- The tested local queue is not a global LLM gateway, not a multi-process queue, and not a cost/idempotency boundary for all provider calls.

## 4. Domain-by-domain truth matrix (W0–W8)

| Domain | Status | Exact evidence / current truth | Primary risk and next action |
|---|---|---|---|
| **W0 — Phase Zero evidence** | **PROVEN for the merged assessment/plan deliverable** | PR #106 is merged at `93c7809...`; exact-main CI run `36320966850` is green. | Keep dated findings SHA-bound. The assessment remains an audit, not proof of implementation or operations. |
| **W1 — Canonical runtime composition and DB ownership** | **PARTIALLY PROVEN** | Main has real composition roots, database adapters, and green PostgreSQL migration CI. The source still contains separate provider construction and settings/default database seams described in the merged audit. PRs #99/#89/#66/#63/#60 and related open work touch overlapping runtime paths. | Do not edit those owned/hot paths in this queue claim. Resume after live ownership reconciliation; prove one injected gateway/context identity and one configured database path with negative tests. |
| **W2 — LLM gateway and queue** | **PARTIALLY PROVEN overall; local queue contract PROVEN on candidate tree only** | Queue behavior below passed 15 focused tests and 12/12 local mutants on tree `a33c4f1...`; full local gates passed. Main still has multiple LLM routes, string-returned HTTP errors, no demonstrated global policy, and no app shutdown hook for this queue. | Open/review the candidate PR and obtain exact-head CI; then sequence W1 composition and wire every model call through one policy-owned gateway. Add a blocking mutation job only after the `.github/` path is released. |
| **W3 — Memory provenance, trust, consent, deletion** | **PARTIALLY PROVEN** | Consent/deletion primitives exist in the separate AI-memory feature; graph `LongTermMemory` still has the data/trust and synchronous-I/O limitations above. | Preserve consent controls. After W1, separate thread checkpoints, approved facts, and retrieval corpus; test provenance, tenant scope, instruction-data separation, TTL, and deletion across all stores. |
| **W4 — Typed planner, capability authority, approvals, evidence, repair** | **PARTIALLY PROVEN** | Nagar’s typed command/capability/evidence seam is present and pack trust mutations pass on main. The general graph still selects and executes through simpler private helpers and has no durable approval object or general effect identity. | Integrate the planner through existing authority/evidence contracts; do not replace Nagar or invoke handlers directly. Add actor-bound, expiring, replay-resistant approvals and postcondition evidence. |
| **W5 — Multi-worker jobs and durable ingress** | **PARTIALLY PROVEN locally; production multi-worker/durable ingress NOT PROVEN** | A SQLite in-process job adapter has idempotency/fencing/verification components. Main CI does not prove competing PostgreSQL worker claims or crash-safe webhook acceptance. | Retain the local adapter. Add a PostgreSQL claim/lease protocol and durable inbox/outbox semantics only after W1/W4 identity; test two workers, kill/restart, stale fencing, duplicates, and webhook acknowledgment. |
| **W6 — Deployment, backup, restore, rollback** | **BLOCKED for production claims** | Main’s PostgreSQL migration job passes, but maintenance run [36308867958](https://github.com/bot523h/nexus-ai-agent/actions/runs/36308867958) failed its `backup-db` job on SHA `6624a133...`. Open issue [#85](https://github.com/bot523h/nexus-ai-agent/issues/85) records missing R2 and `NEXUS_DATABASE_URL` configuration; it explicitly refuses to back up the ephemeral local SQLite file. PR #96 is a dirty candidate, not operational proof. | Owner must configure the external DB/R2 environment and produce an actual successful backup plus isolated restore/rollback evidence on named SHAs. Do not label configured backup code as a restored backup. |
| **W7 — Shell grammar and isolation** | **NOT PROVEN for arbitrary-code safety; conditional** | Main has a disabled-by-default allow-listed shell path but no general OS/process isolation proof. PR #105 is clean and its displayed shell/security and CI checks pass, but it is not merged; it does not establish arbitrary-code sandboxing. | Prefer typed capabilities over general shell. If arbitrary code remains required, establish an explicit sandbox, filesystem/egress/resource policy, and adversarial escape tests before exposure. |
| **W8 — CI, governance, docs, Continuum, final readiness** | **PARTIALLY PROVEN** | Exact main CI has 13 green jobs on `93c7809...`; the board protocol and evidence tooling exist. Main `.nexus/continuum.json` remains schema v2 with `test_count_expected: 649` and a next step about v3.12.0 release metadata. `VERSION`/`pyproject.toml` say `3.13.0`, README prose says `v3.12.0`, and GitHub’s latest published release observed is `v3.3.0`; `v3.13.0` was not found. | Reconcile Continuum, release/version prose, board, and CI after the overlapping #107/#102/#105/#59/#68/#69/#87 architecture/governance work is resolved. Main CI success is not a readiness decision. |

### Capability and evidence substrate

| Capability area | Status | What the evidence does and does not establish |
|---|---|---|
| Pack trust root | **PROVEN for the merged trust boundary** | Main contains the Ed25519 verifier and an external trust root with no self-asserted external publisher authority; the `trust-mutations (pack trust plane)` job passes on `93c7809...`. Preserve this boundary. |
| Nagar typed command/render/evidence path | **PARTIALLY PROVEN** | Strong typed commands, capability checks, RenderIR, controlled execution, and artifact verification exist for registered lanes. Open operation-truth/studio PRs (#83/#87/#70/#73/#68/#88) are candidates and are not automatically main evidence. Catalog breadth is not the same as live activation or verified execution. |
| Vision packs | **PARTIALLY PROVEN** | Existing model vision calls are not proof of complete portrait/scene capability packs. PR #97 is `DIRTY` and unmerged. |
| Caption production | **PARTIALLY PROVEN** | Existing caption-related code does not establish the hardened local lane proposed by dirty PR #100. Keep local preview separate from a verified master artifact. |
| Personality/presence | **PARTIALLY PROVEN** | Existing state features do not establish the bounded lifecycle in dirty PR #94. Registration is not activation or execution. |
| Command wiring | **PARTIALLY PROVEN** | Several wiring/hardening improvements are in dirty candidate PR #99 and related open PRs; only merged-main behavior counts as canonical. |

## 5. W2 queue implementation and design decision

Candidate commit `260dfc6...` changes only the owned local queue module, its unit tests, and a temporary-copy mutation harness.

- **Admission and deadline:** validates callable/priority/timeout; one absolute event-loop deadline covers queue wait, rate wait, retries, and provider execution. A deadline/cancellation removes queued work before the provider factory can run.
- **Fairness:** each priority has per-user FIFO lanes and a user rotation ring. One request is selected per user turn. Lower enum values remain strict priority across tiers; continuous higher-tier demand can starve lower tiers by design and is documented/tested.
- **Accounting:** one logical request has a guarded terminal transition and one pending release. Logical submitted/completed/succeeded/failed/cancelled/timed-out/closed counts are separate from provider attempts. Every actual factory invocation is charged against the rolling 60-second and UTC daily quota before invocation; a typed retry passes through the same capacity gate and consumes another attempt.
- **Retry:** only structured exception status attributes (`status_code`, `response.status_code`, or `code`) matching 429/5xx qualify. Error strings and successful strings are not parsed. Retries are capped by `max_retries`, use bounded exponential jitter, honor numeric or HTTP-date `Retry-After`, and decline rather than shorten a provider wait above 30 seconds. `retry_on_429=False` disables the transient-status retry policy (429 and 5xx).
- **Cancellation and close:** local running provider task cancellation is requested; the queue explicitly says remote outcome may be unknown after send. `close()` stops admission, settles queued and active callers with `RequestQueueClosedError`, cancels local active work, and is idempotent. It can wait indefinitely if a provider coroutine deliberately suppresses cancellation; the queue cannot forcibly stop that coroutine or remote work.
- **Status:** exposes process-local depth, rate wait, quota and lifecycle counters. These counters are not distributed metrics and do not imply cross-process coordination.

### Alternatives and research basis

1. **Python asyncio task/cancellation documentation** — [`asyncio` tasks](https://docs.python.org/3/library/asyncio-task.html): `wait_for` cancellation affects awaited work unless shielded; cancellation is cooperative. This is why the caller future is shielded from timeout and local cancellation is tracked separately from remote outcome.
2. **Rate-limiter alternative** — [aiolimiter documentation](https://aiolimiter.readthedocs.io/): a leaky-bucket limiter is event-loop-scoped, but it does not supply queue-item withdrawal, user round-robin, close settlement, or logical/physical accounting. Retaining the existing queue avoided a new runtime dependency and let one local worker own all those transitions.
3. **Retry policy reference** — [Temporal Python failure detection](https://docs.temporal.io/develop/python/failure-detection) and [fixed-count retries](https://docs.temporal.io/design-patterns/fixed-count-retries): explicit attempt caps and backoff are safer than text-driven retries. A durable workflow platform is not justified for this process-local adapter and is not proposed here.
4. **Security and tenancy** — [OWASP AI Agent Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html) and [Multi-Tenant Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html): bound retries, chaining, concurrency, and tenant resource use. This slice improves cancellation/quota accounting but does **not** add a maximum queue depth or per-user admission cap; that remains a resource-exhaustion risk.
5. **Fair scheduling** — [Systems Approach fair queuing](https://book.systemsapproach.org/congestion/queuing.html): per-flow round-robin is a small, testable fairness policy. It was selected within each priority tier; strict cross-tier priority remains for compatibility, with starvation explicit rather than hidden.
6. **Retry amplification and recent practice** — [RetryGuard](https://arxiv.org/html/2511.23278) and the September 2026 [OpenClaw retry/cancellation example](https://github.com/openclaw/openclaw/pull/144543) reinforce that retries need one owner, cancellation-aware waits, and measured attempt budgets. This local queue does not establish a global cost/idempotency budget.

The former timestamp heap was simpler but did not implement its stated user fairness or retract abandoned work. A third-party limiter does not solve request lifecycle or fairness. A broker/Temporal-style durable engine would add operational surface without making this queue durable; durable work remains W5. The chosen change is local and testable, but rollback to the old behavior would reintroduce demonstrated timeout and accounting defects; no schema/data migration is involved.

## 6. Verification ledger

All local gates below ran after the final code/test/mutation-script edits against implementation tree `a33c4f129d459a98d2b1862027909f0b41d40693`, then were committed as `260dfc6...`. Local environment: Python 3.11.2, Ruff 0.16.9, Mypy 2.3.1, pytest 9.1.1.

| Gate | Result | Scope / limitation |
|---|---|---|
| Baseline failure reproduction | **RED as expected** on exact main code from `93c7809...` | Timed-out callback ran, same-user pending count dropped to zero while another item remained, and close left active/queued waiters unfinished. Temporary copy only. |
| Focused queue tests | **15 passed** | `PYTHONPATH=src .venv/bin/python -m pytest -q tests/unit/test_request_queue.py` — lifecycle, queued timeout/cancel, running cancellation, close, round-robin, strict priority/starvation, retries/Retry-After, attempt accounting, UTC rollover, and validation. |
| Mutation harness | **12/12 killed; baseline and restored copy GREEN** | `PYTHONPATH=src .venv/bin/python scripts/llm_queue_mutations.py`. Includes timeout removal, queued cancellation removal, running cancellation, exact pending count, attempt accounting, per-user round-robin, strict priority, close settlement, retry bound/classification/Retry-After, and daily rollover. This harness is local-only; its CI workflow was deliberately not edited because `.github/` is outside the claim and overlaps active PRs. |
| `make lint` | **PASS** | `ruff check .` and `ruff format --check .`; 513 files already formatted. |
| `make types` | **PASS** | `mypy src`; no issues in 243 source files. The first attempt had a stale `.mypy_cache` import result for installed `imageio_ffmpeg`; after invalidating that ignored cache, the unmodified gate passed. No source ignore or type suppression was added. |
| `make test` | **2390 passed, 30 skipped, 16 warnings** | `pytest -q -m "not slow"`, 186.30 s. The 30 skips and non-slow marker remain visible; warnings are existing `httpx2` and SQLModel deprecations. Slow tests were not claimed as run. |
| Main CI | **13/13 PASS** | Exact main run [36320966850](https://github.com/bot523h/nexus-ai-agent/actions/runs/36320966850) on `93c7809...`; proves main baseline only. |
| Queue candidate remote CI | **PENDING** | No PR existed at this report revision. Do not promote local results to exact-PR or main proof. |

## 7. Risks, rollback, and next actions

### Residual risks

1. This is one in-process worker and one event loop. It is neither durable nor cross-process, and the application does not yet prove that every model call uses it.
2. Strict cross-tier priority can starve lower tiers; per-user queue depth is not bounded. A single high-volume user may consume memory before service; add admission limits only with a separately owned policy and tests.
3. The Gemini HTTP adapter returns non-200 errors as strings, so the new typed exception retry policy does not retry those outcomes. Do not parse human-readable text to compensate.
4. Cancelling a local task after network send does not prove remote cancellation. A retried request can duplicate provider cost; there is no remote idempotency or global cost budget here.
5. `close()` settles callers before waiting for worker cleanup, but a coroutine suppressing `CancelledError` can delay shutdown. The provider contract must remain cancellation-cooperative.
6. Mutation evidence is not yet a main CI gate. It was kept outside `.github/` to respect active path ownership.
7. The queue is not wired as the app-wide lifecycle owner; its own `close()` tests do not prove bot shutdown calls it.

### Rollback posture

There is no database migration or persisted queue format to roll back. Reverting the implementation commit is mechanically simple but restores known defects. If rollback becomes necessary, first disable or route affected submissions through an explicit safe path, preserve the regression tests, and avoid claiming that the old timeout behavior is safe. Do not force-push or rewrite this branch.

### Immediate sequence

1. Add this report to the candidate branch, create the PR from `arena/01a0e2f0-nexus-ai-agent`, and record the PR number, exact head SHA, changed paths, and exact PR CI run IDs/results here after they finish.
2. Reconcile the shared `.agents/board.json` state with PR #107 before merge; do not blindly merge/rebase a dirty PR or overwrite another claim. Keep `MODULE_MAP.md` deferred while its seven overlapping PRs remain open.
3. Obtain reviewer approval and green exact-head CI. The queue change can then be merged as a local adapter improvement, but main merge does not complete W2 globally.
4. Reopen W1 only after overlapping runtime owners reconcile: one composition-owned gateway/context, handler identity proof, all direct provider paths accounted for, database path ownership, and application shutdown wiring.
5. Resume W3/W4/W5 in dependency order. Preserve Nagar’s trust/evidence plane; do not add an external durable queue until typed effect identity and ingress semantics are established.
6. Ask the infrastructure owner to resolve issue #85, configure external DB/R2, and produce a successful backup plus restore/rollback drill. Until then production backup/recovery remains blocked.
7. After the `.github/` lease overlap clears, add the queue mutation harness as a CI job and verify it on exact main SHA. Refresh Continuum, version/release lineage, and architecture docs only under their current owners.

## 8. Primary evidence links

- Main PR #106: <https://github.com/bot523h/nexus-ai-agent/pull/106>
- Exact main CI on `93c7809...`: <https://github.com/bot523h/nexus-ai-agent/actions/runs/36320966850>
- Backup failure on `6624a133...`: <https://github.com/bot523h/nexus-ai-agent/actions/runs/36308867958>
- Open backup-configuration issue #85: <https://github.com/bot523h/nexus-ai-agent/issues/85>
- Candidate shell hardening PR #105: <https://github.com/bot523h/nexus-ai-agent/pull/105>
- Candidate typed provider failure PR #93: <https://github.com/bot523h/nexus-ai-agent/pull/93>
- Candidate Continuum successor PR #107: <https://github.com/bot523h/nexus-ai-agent/pull/107>
- Candidate deployment/restore PR #96: <https://github.com/bot523h/nexus-ai-agent/pull/96>

---

## 9. Successor addendum — PR #108 salvage and the Python 3.10 cancellation defect (2026-09-27, `arena/01a0e356-nexus-ai-agent`)

**Successor context.** This report was written for candidate branch `arena/01a0e2f0-nexus-ai-agent` before a PR existed. It became PR #108 (head `5a4cb23b26fab15c7624d3e6c9bfc8f8006f8c35`), which at live re-inspection was **CONFLICTING** with main `05dec617f5c596ef9023cab9c42acc689efec583` (merge-base `93c7809...`; **4 ahead / 30 behind**) and **RED on python-parity (3.10)** on both check runs recorded for that head (check runs `108632462990` and `108632643506`, runs `36323757857` / `36323747749`); all other legs including 3.11/3.12 parity, lint, type, non-slow tests, extras, trust-mutations, migrate-postgres and release-lineage were green. A session is bound to exactly one branch and cannot push to `arena/01a0e2f0`, so — per the PR #102 → PR #107 precedent — the four commits `565c429`, `1564ba4`, `260dfc6`, `5a4cb23` were merged **verbatim** (no rebase, no force-push, authorship and SHAs preserved) into the successor branch `arena/01a0e356-nexus-ai-agent` as merge commit by `arena/01a0e356` onto `05dec617`. Changed-tree equivalence was verified before that commit: the four feature/report paths are byte-identical to `5a4cb23` (`git diff --quiet`); the only conflict was `.agents/board.json`, resolved semantically (main's claim statuses and gc notes are canonical; the `llm-request-queue-lifecycle` zone and the task-195 claim were appended; the claim was released via `takeover_log`; continuation claim `task-195-salvage` registered with `gates_owner=true`).

### 9.1 Root cause of the 3.10 failure (reproduced chain)

| Step | Evidence |
|---|---|
| Runtime witness | python-parity (3.10) FAILURE on head `5a4cb23` in both duplicate check runs; 3.11/3.12 green. |
| Root-cause line | `src/nexus_ai_agent/features/request_queue.py:435` (at `5a4cb23`): `current.cancelling()` in `_execute`'s `except asyncio.CancelledError` handler. `asyncio.Task.cancelling()` exists only from Python 3.11; on 3.10 the first cancellation processed by a worker task raises `AttributeError` inside the handler. The path is exercised by `test_caller_cancellation_propagates_to_running_provider_task` and `test_worker_cancellation_settles_active_work_and_close_is_idempotent`, both shipped by the same PR. |
| Local reproduction tripwire | AST parity guard `test_queue_module_stays_within_python_310_asyncio_api` added in the successor: **RED** on the original `5a4cb23` source (reports `cancelling` at line 435), **GREEN** after the fix. The sandbox has no Python 3.10 interpreter and no package source for it; the remote 3.10 parity leg remains the runtime witness of record (see §9.3 limits). |

### 9.2 Fix design (selected over alternatives)

`_execute` must distinguish three cancellation sources: (1) external cancellation of the *worker task itself* → propagate; (2) request withdrawal / `close()`, which cancel `req.execution_task` after recording `cancel_event`/`_closed` → `_RequestCancelled`; (3) a provider coroutine cancelling itself → typed failure that must not kill the worker.

`Task.cancelling()` answered (1) by inspecting pending cancel requests — an API that does not exist on 3.10, and on 3.10 an externally delivered cancel is consumed by the awaited future and leaves no introspectable remainder. The selected recipe therefore changes the *observable*: the provider task is awaited through `asyncio.shield(task)`. An external worker cancellation then appears as `CancelledError` **with the provider task still running** (`task.done() is False`); the shield does not propagate into the provider task, so the code calls `task.cancel()` explicitly (preserving the pre-existing implicit propagation semantics that the running provider must receive cancellation) and re-raises. Provider-side cancellations (withdrawal, close, self-cancel) all complete the task before the worker observes the exception (`task.done() is True`), and are disambiguated by the already-recorded `cancel_event`/`_closed` state. Rejected: feature-detecting `cancelling()` (cannot recover the distinction on 3.10 at all), and swallowing external cancellation into a typed failure (breaks shutdown semantics and `_process_loop`'s settle-closed invariant). All primitives used exist on 3.8–3.12; `submit()` already used the same `wait_for(shield(future))` pattern, so the recipe is also stylistically canonical for this module.

### 9.3 Proof table (successor head)

| Proof | Result | Scope/limit |
|---|---|---|
| Original 15 queue tests | **PASS** (Python 3.11.2) | identical semantics retained. |
| `test_queue_module_stays_within_python_310_asyncio_api` | **RED→GREEN** | RED on `5a4cb23` source (line 435), GREEN after fix; AST-based, comments may document history. Runtime parity remains the CI 3.10 leg — exact-head run ID recorded in the PR. |
| `test_provider_self_cancellation_is_typed_failure_and_worker_survives` | **PASS (new)** | fills the previously untested self-cancel branch (failure type, accounting, worker survival, strict counters). |
| Mutation campaign `scripts/llm_queue_mutations.py` | **14/14 killed, restored baseline GREEN** (25 s) | 12 original + 2 new mutants: `external_worker_cancellation_is_propagated` (dropping the worker `raise`), `provider_self_cancel_settles_as_typed_failure` (self-cancel climbs into worker). A deliberately unshielded `await task` is *not* a mutant: on 3.11+ all behavior tests pass with it — it is exactly the version-dependent trap class caught by the parity guard + CI leg instead. |
| ruff check + format (repo-wide) | **PASS** (532 files) | successor-owned files explicitly checked. |
| mypy `src` | **PASS** (247 files) | unchanged strictness settings. |
| Board structural tests + `agent_board.py check` | **75 PASS; `no overlap — safe to proceed` (exit 0)** | coordination contract. |
| Full non-slow suite | **2911 passed, 30 skipped, 16 warnings in 160.27 s** (`pytest -q -m "not slow"`, Python 3.11.2, successor tree) | includes the two tests added by the successor; slow-marked tests not claimed as run. |
| Exact-head remote CI (all legs incl. 3.10 parity, continuum, trust-mutations) | recorded in the PR body | remote witness of record for this addendum. |

### 9.4 What is still **not** proven (unchanged from §7, plus)

1. The fix is verified locally on 3.11.2 and on CI 3.10/3.11/3.12; no interpreter matrix beyond CI exists. The sandbox used for the fix could not execute 3.10 (no interpreter, no external package source), so the local leg of the RED→GREEN proof is the AST tripwire plus the historical failing check runs.
2. Out-of-contract direct `processor_task.cancel()` racing provider completion on the same event-loop tick settles as a typed provider failure instead of propagating (single-instruction window; in-contract paths — `close()`/`_cancel_request` — record state first and are unaffected). Documented in the code comment.
3. Everything in report §3/§7 still stands: process-local only, no app-wide shutdown wiring, no single gateway, no remote-cancellation guarantee, strict-priority starvation, GIL-sized fairness window, and no production/durability claim. The mutation harness is still not a CI gate (`.github/` ownership respected).
4. `MODULE_MAP.md` remains deferred to the docs owners, exactly as the original claim scoped itself.

---

## 10. Session-bound continuation — Python 3.10 `wait_for` timeout contract (2026-09-27, `arena/01a0e3b7-nexus-ai-agent`)

**This section supersedes any reading of §9 that treats `cd79bfe` as green on Python 3.10.** §9 recorded the `Task.cancelling()` fix. That fix is still in history. It did not fix the timeout-class split. Remote CI on that head is RED.

### 10.1 Live truth (recomputed, not inherited)

| Item | Value |
|---|---|
| Session branch | `arena/01a0e3b7-nexus-ai-agent` (this session cannot push any other branch) |
| `origin/main` | `05dec617f5c596ef9023cab9c42acc689efec583` |
| Merge-base with main | `05dec617f5c596ef9023cab9c42acc689efec583` (main is an ancestor) |
| Fast-forwarded predecessor | `origin/arena/01a0e356-nexus-ai-agent` = `cd79bfe3b8e42b1943ad8ff0d352646fc54df548` |
| PR #109 | OPEN, head `cd79bfe`, `mergeable=MERGEABLE`, `mergeStateStatus=UNSTABLE` |
| Exact failing CI | run [36329794486](https://github.com/bot523h/nexus-ai-agent/actions/runs/36329794486), job `python-parity (3.10)` id `108649421192`, conclusion **failure**. Duplicate run [36329718482](https://github.com/bot523h/nexus-ai-agent/actions/runs/36329718482) same conclusion. 3.11 and 3.12 parity on that head were success. |
| Board | claim `task-195-salvage` moved to this branch via `takeover_log` action `session-bound-continuation` at `2026-09-27T16:54:20Z`. `agent_board.py check` on the owned paths: **no overlap — safe to proceed**. |
| Working tree before this commit | uncommitted fix on top of the fast-forward. Not pushed. Not CI. |

No history was rewritten. No force-push. `cd79bfe` remains an ancestor.

### 10.2 RED, reproduced on CPython 3.10.16 against `cd79bfe`

Local interpreter: CPython 3.10.16 built from the `v3.10.16` tag. Before the fix, `tests/unit/test_request_queue.py` on that tree:

**3 failed, 14 passed.**

Failed:

- `test_same_user_timeout_releases_exactly_one_pending_slot`
- `test_withdrawn_queued_requests_never_run_after_backlog_clears`
- `test_timeout_during_rate_wait_wakes_worker_without_provider_invocation`

The third failure also logged `queue_processor_error error_type=TimeoutError`. Traceback: `submit` line `await asyncio.wait_for(asyncio.shield(future), timeout=remaining)` raised `asyncio.exceptions.TimeoutError`.

Class relationship measured on that interpreter:

- `TimeoutError is asyncio.TimeoutError` → **False**
- `issubclass(asyncio.TimeoutError, TimeoutError)` → **False**
- `asyncio.TimeoutError` MRO: `Exception`, not `OSError` / builtin `TimeoutError`
- Same check on CPython 3.11.2: the two names **are** the same object

Official contract:

- Python 3.10: [`asyncio.TimeoutError` is different from builtin `TimeoutError`](https://docs.python.org/3.10/library/asyncio-exceptions.html#asyncio.TimeoutError)
- Python 3.11+: [`asyncio.wait_for` changed to raise `TimeoutError` instead of `asyncio.TimeoutError`](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for)

`except TimeoutError` therefore misses the 3.10 timer exception. Because that class still subclasses `Exception`, the worker arm logs `queue_processor_error` and settles a failure. The caller sees `asyncio.TimeoutError`, not the documented builtin `TimeoutError`. Pending accounting is not released on the caller path. Retry backoff with a positive delay hits the same miss (`_retry_delay_seconds` returning `0.0` in older tests never entered `wait_for`, which is why those tests stayed green).

### 10.3 Fix (production, not a test weakening)

`_wait_for_timeout_types()` returns `(TimeoutError, asyncio.TimeoutError)` and is looked up at handler time. Both `submit` and `_wait_for_cancellation` classify with `_is_wait_for_timeout` before any other `Exception` handling. The caller-visible raise remains builtin `TimeoutError`. A concurrent settler's exception is retrieved so it cannot warn "Future exception was never retrieved". Settlement still goes through the existing single-transition accounting (`accounted` flag, state gate). No new dependency. No 3.11+ asyncio API (`timeout()`, `TaskGroup`, `cancelling()`, `uncancel()` remain forbidden).

Rejected: rewriting `wait_for` onto `asyncio.wait` (larger cancellation-semantic change than the defect), and catching only `asyncio.TimeoutError` (misses 3.11+ builtin `TimeoutError` if a future was settled with that class and also fails the public contract on 3.10 if the timer class leaks).

### 10.4 Local proof after the fix (not remote CI)

| Proof | Result |
|---|---|
| Queue tests, CPython 3.10.16 | **21 passed** in 0.39s (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `-p pytest_asyncio.plugin`, `--noconftest`) |
| Queue tests, CPython 3.11.2 | **21 passed** in 0.35s |
| Mutation campaign | **18/18 killed**, baseline GREEN, restored baseline GREEN. New mutants: `wait_for_timeout_includes_py310_asyncio_timeout`, `rate_wait_timeout_is_not_a_processor_error`, `caller_wait_timeout_uses_version_correct_classifier`, `caller_timeout_is_builtin_timeout_error`. A revert of the classifier or of either call site is RED on 3.11 because the tests inject a non-builtin timeout class. |
| `ruff check .` | PASS |
| `ruff format --check .` | PASS (532 files) |
| `mypy src` | PASS (247 files) |
| `pytest -q -m "not slow"` | **2915 passed, 30 skipped, 16 warnings in 174.30s**, CPython 3.11.2, exit 0. Delta vs §9's 2911 is the four new queue tests. |
| `tests/unit/test_agent_board.py` | 18 passed in the focused run; the non-slow suite includes the board tests and passed |
| Remote exact-head CI | **not run yet**. Local green is not PR green and not main green. |

### 10.5 Still not proven

1. Exact-head CI on this continuation, including python-parity 3.10/3.11/3.12, has not completed. Do not merge on this section alone.
2. Process-local queue only. No application shutdown wiring, no global gateway, no remote cancellation proof. W1 is not started in this commit.
3. `MODULE_MAP.md` and `.github/` remain outside this claim.
4. An out-of-contract direct worker cancel racing provider completion on the same loop tick is still the residual documented in §9.4.
