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

### 9.4 Defect class #2 — Python 3.10 `wait_for` timeout-class split (found by CI, reproduced executably)

**DISCLOSURE (honesty first):** the first successor head `cd79bfe` fixed defect class #1 only. The exact-head CI it triggered **proved the job incomplete**: `python-parity (3.10)` FAILED again on run `36329718482` (check run `108649212432`) while 3.11/3.12 stayed green. This is CI doing precisely what the mission's LIVE TRUTH law demands — the failure was caught, root-caused and fixed before merge; see the updated exact-head CI in the PR.

| Step | Evidence |
|---|---|
| Runtime witness | `python-parity (3.10)` FAILURE on `cd79bfe` (check run `108649212432`, run `36329718482`); 3.11/3.12 legs green. |
| Root cause | Python 3.10's `asyncio.wait_for` raises `asyncio.TimeoutError` — a class **distinct** from builtin `TimeoutError` (unified only in 3.11; on 3.11+ `asyncio.TimeoutError is TimeoutError` and `concurrent.futures.TimeoutError is TimeoutError` are both `True`, verified on 3.11.2). Five sites in `5a4cb23`'s queue caught `except TimeoutError` (builtin only): on 3.10, wait_for timeouts escape → `submit` leaks raw timeout exceptions without `_TIMEOUT_MESSAGE`/accounting, and a worker-side capacity-wait timeout is misaccounted as a **provider error** (`queue_processor_error error_type=Py310AsyncioTimeoutError` observed in the simulation log), violating LAW 10-style honest accounting. Defect class #1's `AttributeError` cascade had masked this second class in the original run. |
| Executable reproduction on 3.11 | A committed fixture (`py310_timeout_split`) binds `asyncio.TimeoutError` to a fresh distinct class and makes `wait_for` raise it — a faithful emulation (the patched attribute is re-read at every `except` evaluation). On the pre-fix module, **4 guards go RED**: `test_submit_timeout_survives_py310_timeout_class_split`, `test_rate_wait_timeout_survives_py310_timeout_class_split`, `test_capacity_quota_window_wait_survives_py310_timeout_class_split`, and the AST contract (catches both bare `except TimeoutError` sites). An earlier naive emulation (raising `concurrent.futures.TimeoutError`) proved nothing because on 3.11 it is the *same object* as builtin `TimeoutError`; the faithful version was used for all conclusions. |
| Fix design | `_timeout_errors()` resolves the catch pair **per catch** (not at import): evaluates `getattr(asyncio, "TimeoutError", TimeoutError)` fresh and dedupes. Import-time freezing was rejected — an interpreter whose class identity is established at import is fine, but the per-catch form is also correct under the committed post-import emulation and dodges the 3.12 alias-deprecation surface; both `submit`'s caller-deadline catch and `_wait_for_cancellation`'s capacity-wait catch use it. |
| Mutation lesson (kept visible) | The first capacity-wait mutant **SURVIVED**: the initial rate-wait test always let the caller's own deadline win, so the worker-side catch was never load-bearing. A deterministic worker-side test (minute-window opens after ~0.15 s, caller deadline 5 s) made it killable. The mutant catalog now carries two version-independence mutants; the catalog total is **16/16 killed, restored baseline GREEN**, 31 s. |
| Residual honesty | The two original timeout-family tests plus all retry tests pass under the split simulation; the failure set under simulation is exactly the 3 tests above, matching the observed red leg's repair surface. The real 3.10 interpreter remains the CI parity leg — updated run IDs are in the PR body. |

### 9.5 What is still **not** proven (unchanged from §7, plus)

1. The fix is verified locally on 3.11.2 and on CI 3.10/3.11/3.12; no interpreter matrix beyond CI exists. The sandbox used for the fix could not execute 3.10 (no interpreter, no external package source), so the local leg of the RED→GREEN proof is the AST tripwire plus the historical failing check runs.
2. Out-of-contract direct `processor_task.cancel()` racing provider completion on the same event-loop tick settles as a typed provider failure instead of propagating (single-instruction window; in-contract paths — `close()`/`_cancel_request` — record state first and are unaffected). Documented in the code comment.
3. Everything in report §3/§7 still stands: process-local only, no app-wide shutdown wiring, no single gateway, no remote-cancellation guarantee, strict-priority starvation, GIL-sized fairness window, and no production/durability claim. The mutation harness is still not a CI gate (`.github/` ownership respected).
4. `MODULE_MAP.md` remains deferred to the docs owners, exactly as the original claim scoped itself.
5. Defect class #2 note: the 3.10 timeout-class-split fix is verified by faithful simulation on 3.11 plus the CI 3.10 leg — there is still no local 3.10 interpreter run in this sandbox. Everything else in §9.4's list stands, and the "out-of-contract direct worker cancel racing provider completion" residual is unchanged.

---

## §10 — W1: Canonical Runtime Composition (task-196, waves after task-195 ship)

**WAVE STATUS: PROOF COMPLETE locally; awaiting exact-head CI + merged-main proof.**

### A — WHAT CHANGED
- NEW `src/nexus_ai_agent/application/runtime.py`: `DatabaseIdentity`, `resolve_database_identity()` (one canonical backend decision: `NEXUS_DATABASE_URL` → Postgres, else `settings.db_path` → SQLite, with auditable `source` and credential-free `describe()`), `RuntimeContext` (settings + db + request_queue + conversation_store + gemini_engine + llm_provider + summarizer_engine + late-bound job_queue), `aclose()` (ordered, idempotent, per-component guarded: request_queue → job_queue → store engine dispose), and `build_runtime()` — the ONLY approved factory for these components.
- `storage/db.py:get_session(None)`: the no-argument fallback now follows `settings.db_path` (NEXUS_DB_PATH) instead of a hardcoded literal `"data/app.sqlite"`. Default behavior unchanged (field default IS `"data/app.sqlite"`).
- `llm/gemini_provider.py`: optional `engine=` seam — approved path wraps the runtime's shared engine; the legacy no-engine fallback is preserved but now emits `gemini_provider_legacy_engine_fallback` (structured warning) so production split-brains are detectable.
- `bot/handlers.py`: `build_handlers()` accepts `llm_engine=`/`summarizer_engine=`/`llm_provider=`; when absent it keeps legacy self-construction but warns (`handlers_legacy_llm_self_construction`). The free-text agent flow now passes the shared provider to `AgentManager.get_active()`.
- `bot/app.py`: builds the runtime, reuses its components for `engines`/`bot_data` (plus `llm_provider`, `runtime`), late-binds `runtime.job_queue`, wires `post_shutdown` through the new module-level `make_post_shutdown()`, and hands the shared engine/summarizer/provider to `build_handlers()`. Three internal engines sites in `_init_v2_engines` were deleted (ratchet now forbids them).
- `bot/feature_handlers.py`: `build_feature_engines(..., llm_provider=)` forwards into `AIMemoryEngine(gemini_provider=…)` — no more per-start queue-less provider there.
- `bot/knowledge_handlers.py`: resolves `llm_provider` from `bot_data` (defensive `getattr` chain) and injects it into `KnowledgeManager` at all three command sites — WITHOUT touching the knowledge/ zone, which is under an active claim by `arena/01a0df05`.
- `agents/store/agent_manager.py`: `get_active(user_id, gemini_provider=None)` forwards the provider into agent construction.
- Governance: `runtime-composition-w1` zone claim (overlap_ack for stale PRs plus takeover_log), 10 new available roadmap items (task-197..206: W2..W7 + residuals), tests `tests/unit/test_runtime_composition.py` (15 contract tests) + `tests/architecture/test_constructor_ownership.py` (AST ratchet pinning ALL `GeminiEngine`/`GeminiProvider`/`ConversationStore`/`GeminiRequestQueue`/`SummarizerEngine` construction sites).

### B — WHY
Phase-0 forensics + the merged execution plan showed production split-brains: app.py and handlers.py each built their own GeminiEngine + SummarizerEngine; AIMemoryEngine/agents/knowledge handlers spawned queue-less private providers; `get_session(None)` silently ignored configuration; and shutdown closed only reminders (queue/waiters/jobs/DB leaked past application lifetime). W1's owner + ratchet make the drift mechanically detectable instead of convention-based.

### C — PROOF
- Baseline on pre-W1 tree: the 15 contract tests + AST ratchet RED (the default-path regression guard GREEN by design); 16/16 GREEN after implementation.
- Full non-slow suite: **2930 passed, 30 skipped** (was 2914 + 16 new). ruff check + format: clean (440 files). mypy: clean (248 files).
- Mutation/transplant probes (all killed): M1 `aclose` skips `request_queue.close()` ⇒ 3 shutdown tests RED; M2 `build_handlers` ignores the injected engine ⇒ identity test RED; M3 db fallback reverts to literal ⇒ db-identity tests RED. **Lesion recorded:** the first in-place sed transplant was "undone" by restoring the file within the same second — CPython's (mtime, size)-keyed stale pyc kept executing the MUTANT after restore; probes now clear `__pycache__` (and `-p no:cacheprovider` pytest cache) between mutations. Trust requirement: any future transplant campaign MUST invalidate bytecode caches or the evidence is void.
- Gateway identity is proven by object-identity assertions (`provider.engine is runtime.gemini_engine`, `engine._queue is runtime.request_queue`), a zero-construction spy while building every Telegram handler, and closure-level inspection that command callbacks reference the runtime's engine — not type checks.
- Shutdown ownership is proven behaviorally: a stalled in-flight waiter is settled by `aclose()` (RequestQueueClosedError), the close order is observed (`request_queue` before `job_queue`), idempotency holds, and the post_shutdown hook PTB actually calls is invoked and observed closing the queue — plus a structural guard that the builder wires `post_shutdown` through `make_post_shutdown`.

### D — LIMITS (honest residuals)
- `ConversationStore` remains SQLite-at-settings.db_path even on Postgres deployments (historical behavior preserved; task-198). Its engine is released via a private-attribute seam in `aclose()` until a public `close()` lands (task-199).
- Legacy construction seams remain (ratchet-pinned, warn-logged): provider no-engine fallback, handlers self-construction, `base_agent`/`ai_memory` fallbacks, and `knowledge_manager`'s internal fallback whose file is zoned to another agent (task-203 shrink work).
- `ImageGenEngine`/`SpeechEngine`/`UnifiedCloudStorage` are still constructed inside `build_handlers` (task-204 decides ownership and pins either way).
- `bot/agent_handlers.py:myagent_cmd` (display-only) intentionally does not receive a provider — no LLM call happens there.
- W2 typed/policy gateway arrives as task-197; W1 fixed ownership, not policy payloads.
- No production readiness is claimed.

### E — NEXT MOVE
Merge W1 behind task-195 after exact-head CI (must include python-parity 3.10), then start task-197 (W2 typed LLM gateway) on fresh main — design chosen only after re-verifying live truth at that time.
