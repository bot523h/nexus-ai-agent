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
