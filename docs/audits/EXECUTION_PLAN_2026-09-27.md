# NEXUS Architectural Execution Plan — 2026-09-27

**Plan anchor:** `f53923d47f4b6b362181f0c0f420d8c6797ab093`
**Companion assessment:** [`MASTER_ARCHITECTURAL_ASSESSMENT_2026-09-27.md`](MASTER_ARCHITECTURAL_ASSESSMENT_2026-09-27.md)
**Plan status:** Phase Zero complete; implementation waves are ordered and not started
**Branch rule:** all work remains on `arena/01a0e2c3-nexus-ai-agent`; do not create, switch, or push another branch
**Readiness rule:** a wave is not complete because code exists; it is complete only when its regression, negative, mutation/architecture, CI, documentation, and operational evidence are bound to the exact merged SHA.

This is the least-complex correction plan for the gaps found in the live repository. It preserves the proven Nagar trust/evidence plane and existing modular-monolith constraint. It does not authorize a broker, a workflow platform, arbitrary code execution, or a large rewrite without a later decision record.

---

## 1. Goals, non-goals, and invariants

### 1.1 Goals

1. Establish one runtime composition root for settings, database, provider gateway, queue, memory stores, tools, jobs, and surfaces.
2. Make every external model call subject to one typed request policy for consent, privacy, rate, cost, correlation, timeout, retry, and cancellation.
3. Separate thread checkpoints, approved facts, and retrieval corpus; make memory provenance and deletion machine-checkable.
4. Replace substring-selected one-step execution with a typed plan and capability authority pipeline.
5. Make long work durable across process crashes and multiple workers without adding a broker prematurely.
6. Make webhook acceptance, backups, restore, rollback, and deployment mode honest and testable.
7. Preserve and ratchet the existing trust root, command bus, filesystem controls, artifact verification, job fencing, and evidence infrastructure.

### 1.2 Non-goals for this plan

- No deletion of Nagar, Continuum, Trust Root, artifact verification, or existing hardening.
- No force-push, history rewrite, threshold reduction, hidden exclusion, or test deletion.
- No claim that a local SQLite queue is a distributed queue.
- No full workflow engine unless measured requirements show long waits, approvals, replay, timers, or cross-service ownership that the DB job table cannot satisfy.
- No vector/graph memory expansion before provenance, admission, tenant scope, retention, and deletion are correct.
- No arbitrary-code capability merely because a shell allow-list can be made stricter.
- No fixed performance/SLO number before workload and failure measurements are collected.

### 1.3 Invariants that must remain true

| Invariant | Existing protection to preserve | New proof required |
|---|---|---|
| External capability packs cannot self-authorize | ADR 0006 trust root, Ed25519 verifier, activation gate, trust mutation harness | Any new registry/gateway must not bypass pack trust. |
| Effectful Nagar commands are typed, authorized, and evidenced | `TypedCommand`, `CapabilityRegistry`, `CommandBus`, RenderIR, artifact verifier | AI planner must enter this authority path, not call render handlers directly. |
| Unauthorized Telegram users cannot reach normal handlers | group `-1` access guard and real dispatcher tests | New webhook/API/surface routes must use equivalent complete mediation. |
| Filesystem operations remain contained and no-follow | descriptor-relative `WorkspaceFilesystem` | Shell parser and trusted handoff tests must cover all option forms and swaps. |
| A failed artifact is never reported as success | six-state job lifecycle, independent verification, notifier source-of-truth | New job backends must implement the same contract tests. |
| Release and evidence claims name a real SHA | version lockstep, release-lineage, dated audits | Continuum/docs refresh must be release-cut and exact-SHA bound. |

---

## 2. Dependency graph and wave order

```text
W0  Phase Zero evidence + this plan (complete)
 |
 v
W1  Canonical composition + one database path + ownership guards
 |
 +--------------------+
 |                    |
 v                    v
W2  LLM gateway/queue  W3  Memory provenance and trust boundary
 |                    |
 +---------+----------+
           v
W4  Typed planner, capability authority, approvals, evidence, repair
           |
           v
W5  PostgreSQL claims/leases, durable ingress, retry/idempotency semantics
           |
           v
W6  External state, backups, restore, rollback, deployment readiness
           |
           v
W7  Complete shell grammar and isolated arbitrary-code boundary, if needed
           |
           v
W8  Main CI/governance/docs/Continuum closure and production decision
```

W2 and W3 may proceed in parallel after W1’s object ownership and database seam are accepted. W4 consumes both. W5 can begin its repository work after W1, but production claims wait for W4’s typed effect identity because a durable queue cannot make an unsafe effect safe. W6 depends on W5 for durable job/ingress semantics. W7 is conditional: typed capabilities may remove the need for general shell; if arbitrary code remains a requirement, isolation is mandatory before exposure. W8 is the final evidence reconciliation wave, not a substitute for any earlier proof.

---

## 3. Waves, deliverables, and exit gates

### W0 — Phase Zero evidence and plan

**State:** complete in this session through the companion assessment and this plan; no runtime implementation performed.

**Deliverables**

- exact-SHA repository/GitHub truth ledger;
- actual composition/data/trust/Continuum/Nagar/queue/memory/deployment map;
- five-angle research comparison and selected least-complex corrections;
- gap/dependency map, risk register, verification strategy, CI strategy, and Definition of Done.

**Exit gate**

- assessment and plan indexed in `docs/README.md`;
- docs integrity gate passes in a dependency-complete environment;
- implementation claims remain absent until a board claim is made.

### W1 — Canonical runtime composition and database ownership

**Priority:** P0
**Prerequisite:** W0
**Primary zones:** `src/nexus_ai_agent/bot/`, `api/`, `cli.py`, `storage/`, `application/`, `features/`, `llm/`, relevant architecture/unit tests

**Design**

Create one application-owned runtime context/container. It owns the settings snapshot, database/session factory, LLM gateway, request policy, memory stores, tool registry, job repository, feature engines, and graph. Composition roots may adapt framework objects, but feature modules must receive typed ports or the context-owned instances. Direct constructors for `GeminiEngine`, `GeminiProvider`, `ConversationStore`, and configured database sessions are forbidden outside the approved factories.

Make the configured SQLite path and PostgreSQL URL the same decision everywhere. `get_session()` must receive the composition-owned backend or a settings-aware factory; no production call site may silently choose literal `data/app.sqlite`. Preserve explicit test injection and `:memory:` support.

Inventory and classify every synchronous store. Offload a deliberately synchronous adapter at its boundary or convert it to an async port; do not hide event-loop blocking inside an async method.

**Required proof**

- runtime identity test: graph, handlers, active agents, memory, knowledge, and queue observe the same gateway/context instance;
- AST/import architecture guard: no direct provider/database construction outside approved factories;
- configured temporary SQLite path end-to-end test, including negative assertion that the legacy literal path is untouched;
- PostgreSQL URL composition test still passes; real migration CI remains green;
- event-loop blocking test or explicit adapter contract for each retained sync store;
- no change to pack trust/render/job evidence gates.

**Exit condition**

A reviewer can point to one constructor/factory for each stateful engine and one configured backend decision. A mutation that reintroduces a handler-local provider or literal database fallback turns the suite red.

### W2 — One LLM gateway, fair queue, and global usage policy

**Priority:** P0
**Prerequisite:** W1
**Primary zones:** `llm/`, `features/request_queue.py`, `features/ai_chat.py`, `bot/app.py`, `bot/handlers.py`, provider tests and architecture guards

**Design**

Introduce a typed `LLMRequest`/gateway boundary carrying at least request ID, actor/user/tenant, purpose, data classification, consent/policy decision, model policy, timeout, cancellation handle, idempotency/effect key where applicable, and correlation ID. The gateway owns provider selection, queueing, rate/daily/cost accounting, bounded retry classification, redaction-safe metrics, and cancellation semantics. The existing LiteLLM route can be an adapter behind it; the local/fake provider remains explicit.

Retain the current in-process queue as a local adapter but correct its contracts:

- use per-user ready queues or another tested round-robin scheduler within priority tiers;
- remove a timed-out/cancelled request before execution, or persist a state transition that makes later execution impossible;
- decrement pending state exactly once on every path;
- handle processor cancellation and queued futures explicitly;
- classify provider errors by typed status/code, not broad string prefixes;
- count actual attempts and logical requests separately;
- impose a request/run retry and cost budget;
- expose queue depth, wait time, cancellation, timeout, provider, retry, and fallback metrics.

All direct legacy methods (`chat`, one-shot, code, translate, summarize, vision, store, knowledge, AI memory) must go through the gateway or explicitly declare a local-only/no-egress policy. The application-owned queue and conversation store must be consumed by handlers rather than recreated.

**Required proof**

- queue unit tests for fairness, exact counters, timeout/cancellation, retry classification, processor shutdown, and no post-timeout execution;
- integration tests with a fake provider that records calls, request IDs, and cancellation;
- all direct AI handler paths use one injected gateway identity;
- mutation guard that bypasses the gateway or restores a local `GeminiEngine` constructor and turns tests red;
- adversarial budget test for nested retries and provider 429/5xx;
- no prompt/API key in metrics or logs;
- system test preserves graph fallback and explicit local-only behavior.

**Exit condition**

One policy decision is observable for every model call. A caller timeout cannot create a later model call, and a retry cannot create an untracked external effect.

### W3 — Provenance-aware memory and data trust separation

**Priority:** P0
**Prerequisite:** W1; may proceed beside W2 for schema design
**Primary zones:** `memory/`, `features/ai_memory.py`, `storage/models.py`, migrations, graph memory tests, security tests

**Design**

Define separate ports and stores for:

- thread checkpoint state;
- user-approved long-term facts;
- retrieval documents/chunks and derived embeddings.

The minimum fact/event record must retain stable identity, tenant/user scope, thread/source reference, actor that proposed/admitted it, observed/created/updated timestamps, confidence, policy/consent state, retention/TTL, deletion/tombstone state, content hash, and audit correlation. Metadata passed to memory may not be silently discarded.

Use an admission pipeline: classify source trust, validate schema, require the appropriate user/policy consent, refuse instruction-like authority claims, and record the decision. Retrieval returns structured records with provenance and trust labels. Prompt construction must delimit retrieved content as untrusted data and must not grant it capability, approval, or system authority.

Keep `AIMemoryEngine`’s default-deny consent and `/forget_me` semantics. Extend deletion to all derived indices and copies covered by policy. Do not use recency retrieval as a silent substitute for semantic retrieval; if the vector index is unavailable, return an explicit degraded result or use a documented deterministic fallback with evidence.

**Required proof**

- migration and round-trip tests for every provenance field;
- source/actor/tenant/TTL/deletion tests;
- query-only poisoning and instruction-in-memory negative tests;
- retrieval-screen and counterfactual-removal test;
- no-egress tests for unset/denied consent and no external provider construction in local-only paths;
- graph write/read round trip with structured evidence;
- mutation that drops provenance or inserts retrieved text as trusted instructions turns the suite red;
- performance measurement of synchronous versus async storage before changing the index.

**Exit condition**

A memory record can be answered with “who supplied this, when, under what policy, until when, and how can it be deleted?” A retrieved record cannot authorize a tool.

### W4 — Typed planner, capability authority, approvals, and evidence

**Priority:** P0
**Prerequisite:** W1, W2, W3; preserve Nagar contracts
**Primary zones:** `orchestration/`, `agents/`, `tools/`, `creative/studio/`, graph state, planner/negative/mutation tests

**Design**

Replace substring tool selection with versioned typed objects:

- `Plan`: plan ID, goal reference, actor/tenant, policy snapshot, input versions, plan hash, bounded repair budget, and status;
- `PlanStep`: step ID, capability ID, typed input, dependency IDs, resource/effect scope, preconditions, postconditions, approval requirement, idempotency/effect key, and evidence requirements;
- `StepResult`: status, structured output, evidence claims, provenance, error class, and artifact/result identity.

Resolve user intent through a deterministic capability registry. An LLM may propose a candidate plan, but schema validation, capability authorization, dependency cycle checks, preconditions, approval, and postconditions are code-owned. Unsupported or ambiguous operations must refuse explicitly. The planner must be able to decompose independent work, run only proven-safe independent steps in parallel, replan only within a bounded budget, and stop on evidence failure.

Approvals must be bound to actor, plan hash, step ID, normalized parameters, policy/version, expiry, and one-use nonce. A replayed or altered approval is refused. High-risk Nagar operations must still enter `CommandBus` and the existing permission/evidence path.

**Required proof**

- plan schema/property tests: dependencies, cycles, ordering, independent parallelism, deterministic hash;
- capability negative tests for unknown operation, wrong actor, malformed inputs, precondition/postcondition failure, missing/expired/replayed/altered approval;
- repair/replan budget and refusal tests;
- deterministic router fallback tests for unsupported language/intent;
- integration test from Telegram/CLI command through plan, capability, job, artifact, verifier, and result;
- mutation removing authorization, evidence check, or postcondition must fail;
- record/replay test shows a side effect is not repeated under the same effect key.

**Exit condition**

There is no path from raw model text or retrieved memory directly to a side effect. Every effect has a typed capability, actor, approval policy, pre/postcondition, idempotency identity, and evidence result.

### W5 — Multi-worker durable jobs and durable ingress

**Priority:** P0 for production mode
**Prerequisite:** W1 and W4; W2 for external calls
**Primary zones:** `application/ports/`, `adapters/`, `jobs/`, `worker.py`, `api/app.py`, migrations, queue/integration tests

**Design**

Keep `InProcessJobQueue` as the bounded local adapter. Add a PostgreSQL-backed repository for production with:

- unique idempotency/effect keys and a payload fingerprint;
- atomic ready-row claims using a transaction and row locks/`SKIP LOCKED` or an equivalent proven claim protocol;
- owner, attempt/fencing token, lease expiry, heartbeat, and explicit recovery state;
- cancellation requested/accepted/refused semantics;
- typed retryable/terminal classification and a scheduler only when a real retry policy is approved;
- durable result, artifact verification, publication, and notifier outbox/effect identity;
- metrics for age, claim latency, lease expiry, recovery, duplicate suppression, retry, and terminal failure.

For webhooks, choose and document one of two safe semantics:

1. authenticate, persist an inbox row/update ID and payload, and return 200 only after durable acceptance; or
2. return a non-2xx response until a durable application/job handoff is complete, allowing Telegram redelivery.

Do not acknowledge merely because an in-process PTB queue accepted the object. Deduplicate Telegram update IDs and bind them to a durable job/effect identity.

**Required proof**

- two-process competing claim test against real PostgreSQL;
- kill after external effect and before commit, lease expiry/recovery, stale worker fencing, duplicate enqueue, altered-payload idempotency conflict;
- cancellation tests at every lifecycle state;
- webhook crash-after-authentication and duplicate-delivery test;
- result/notifier outbox retry test;
- parity contract runs for SQLite local adapter and PostgreSQL production adapter;
- mutation removing the claim condition or fencing predicate turns tests red.

**Exit condition**

A process crash cannot create an unbounded duplicate effect or silently lose an accepted webhook. A second worker can claim only an unowned lease and stale work cannot publish or notify.

### W6 — Production state, backup, restore, and rollback

**Priority:** P0
**Prerequisite:** W5; owner-side infrastructure configuration required
**Primary zones:** `storage/`, `maintenance/`, `.github/workflows/maintenance.yml`, `Dockerfile`, `koyeb.yaml`, deployment scripts/runbooks

**Design**

Define explicit deployment modes:

- **local-single-process:** SQLite files and in-process queue are allowed; no multi-worker/durability claim;
- **production-webhook:** external PostgreSQL, external object storage, durable job/inbox repository, required secrets, and no reliance on local disk for state;
- **batch/operator:** uses the same configured backend and safe idempotent jobs.

Make preflight reject a production mode that lacks the required external DB, R2/object-store policy, webhook secret, access policy, and backup configuration. Preserve DB-free liveness, but add readiness/identity diagnostics that do not expose secrets or PII.

Make backup evidence include source backend/identity, exact source SHA, timestamp, object key, size, hash, verified status, schema head, and retention classification. Restore into an isolated temporary target, run integrity/schema/entity checks, compare known sentinel records, and record measured duration. Add a rollback matrix for code before/after migrations and a documented forward-fix path when rollback is unsafe. Configure immutable/isolate backup retention where the operator policy requires it.

**Required proof**

- manual `workflow_dispatch` backup success on configured infrastructure;
- scheduled backup success after the manual run;
- downloaded-byte and restore-into-isolated-DB proof;
- migration upgrade/downgrade or backward-compatible rollback drill;
- Koyeb/staging deploy smoke against the exact commit, including webhook authentication and durable state identity;
- restart/scale-to-zero test proving intended state and job recovery;
- no production readiness statement until owner-side evidence is attached.

**Exit condition**

An operator can lose the process or host and recover the declared state and jobs within measured, documented limits. If that cannot be proven, the deployment is labelled ephemeral/single-process.

### W7 — Complete shell grammar and conditional isolation

**Priority:** P1 for disabled shell; P0 before arbitrary-code exposure
**Prerequisite:** W4 capability authority; W6 resource/egress policy if sandboxed
**Primary zones:** `tools/system_shell.py`, `tools/filesystem_policy.py`, shell tests/mutation scripts, deployment images

**Design**

First decide whether a typed file/media capability can remove the need for generic shell. If shell remains, parse each permitted utility’s complete grammar rather than scanning selected strings. Reject follow-symlink flags, file-output forms, attached/long option forms, environment/configuration overrides, unbounded recursion/output, and any command separator or substitution. Pin a minimal environment, resource limits, working directory, network policy, and child-process behavior.

If arbitrary or adversarial code is a supported capability, run it inside a separately owned gVisor or microVM boundary with read-only inputs, controlled outputs, no host secrets, explicit network egress, resource/time limits, and artifact/provenance handoff checks. A workspace allow-list alone is not sufficient.

**Required proof**

- grammar unit tests and mutation tests for every blocked option family;
- symlink/TOCTOU and output containment tests;
- environment/egress/resource/timeout tests;
- black-box escape and trusted-handoff tests for the selected sandbox;
- architecture guard for no new `shell=True`, direct process spawns, or pack reach-through;
- exact main CI mutation job, not only a candidate PR check.

**Exit condition**

The supported capability is either typed and non-shell, or its complete execution boundary is independently isolated and evidence-producing. No “safe command name” claim is accepted as a sandbox claim.

### W8 — CI, governance, docs, and final readiness reconciliation

**Priority:** P0 evidence closure
**Prerequisite:** all applicable waves
**Primary zones:** `.github/workflows/`, `tests/architecture/`, `tests/unit/`, scripts, `docs/`, `.nexus/`, `.agents/`

**Design**

Bring accepted candidate rails into main only after review and exact-SHA verification. Add the canonical runtime composition, planner, memory, queue, ingress, shell, backup/restore, and deployment guards to blocking CI. Refresh living architecture pages to the current merged SHA; keep dated audit records immutable; update the Continuum only at a controlled state/release cut; reconcile board claims and PR status with live GitHub.

**Required proof**

- full non-slow suite, lint, format, types, Python parity, extras matrix, real PostgreSQL migration, release lineage;
- architecture/import/constructor guards;
- trust, shell, planner, memory, queue, and job mutation harnesses;
- docs integrity, stale-SHA/version scan, and Continuum verification;
- deployment contract plus owner-side backup/restore evidence;
- collect-only before/after inventory proves no test was removed, hidden, renamed, or newly skipped without an explicit reason;
- all artifacts name the exact final SHA.

**Exit condition**

The Definition of Done below is machine-checkable or has attached operator evidence. The readiness decision is either “production-ready for the named topology” or “not ready, with explicit remaining risks”; never a generic green badge.

---

## 4. CI and verification strategy

### 4.1 Current main CI baseline

At the plan anchor, main run `36316279654` passed:

- `lint` with Ruff, format, mypy, and version lockstep;
- `lint-fast`;
- `test` non-slow suite;
- Python 3.10, 3.11, and 3.12 parity;
- `extras-matrix` core/pdf/speech/translate;
- real PostgreSQL `migrate-postgres`;
- `release-lineage`;
- `trust-mutations`.

This is the baseline to preserve. It does not include every candidate rail currently visible on open PRs: the shell/docs mutation rail is on PR #105, and the Continuum evidence rail is on PR #102, whose test/parity checks are red at its observed head.

### 4.2 Target blocking jobs

| Job | Purpose | Blocking evidence |
|---|---|---|
| `lint` / `lint-fast` / `types` | Syntax, style, types, version lockstep | Exact SHA output and no hidden exclusions. |
| `architecture-fast` | Import boundaries, constructor ownership, capability/pack contracts, shell spawn sites, planner/memory authority guards | AST/JSON tests plus targeted mutation. |
| `test` | Full non-slow unit/integration selection | `-rs`, explicit skip reasons, collect-only inventory. |
| `python-parity` | Declared Python floor | 3.10/3.11/3.12, blocking. |
| `extras-matrix` | Every declared extra and core-only absence behavior | Install each leg, audit skip inflation, freeze/provenance artifact. |
| `migrate-postgres` | Real schema/migration/idempotency/adapter contracts | Service container, second migration, head assertion. |
| `queue-memory-planner-adversarial` | Negative, cancellation, poisoning, approval, replay, cross-tenant tests | Deterministic fixtures, no live provider. |
| `trust-mutations` / `shell-mutations` / job/planner mutations | Prove controls are load-bearing | Baseline green, mutant red, restoration clean. |
| `continuum-evidence` | Coverage/evidence snapshot and replayable pack/agent proofs | Exact SHA, no threshold lowering, artifacted report. |
| `deploy-contract` | Offline manifest, required config, health/webhook gates | Strict smoke, no production write. |
| `release-lineage` | VERSION → commit → CI → tag/release → changelog | Full history, gaps visible, contradictions red. |
| `backup-restore` | Real configured backup and recovery | Manual/scheduled owner-side job with immutable evidence. |

### 4.3 Gate ownership and board protocol

Before changing an exclusive path:

1. read the live board and open PRs again;
2. add/claim a task with acceptance criteria, evidence required, prerequisites, exclusive paths, branch, and TTL;
3. run `python scripts/agent_board.py check --files <changed files> --branch arena/01a0e2c3-nexus-ai-agent`;
4. identify the single gates owner for main-bound full gates;
5. do not merge stale heads or rely on a candidate branch as current truth;
6. release the claim and record exact evidence after completion.

This assessment and plan do not claim a board lease for implementation.

---

## 5. Operational measurement before optimization

No latency or performance target is invented here. The first implementation of W2, W3, W5, and W6 must collect a baseline under representative synthetic load:

- model request arrival, queue wait, provider time, retries, cancellations, output size, and cost estimate;
- graph route and plan-step duration, repair/replan counts, tool wait, approval wait, and evidence verification time;
- memory admission/retrieval latency, hit/miss, blocked/expired/deleted records, and context size;
- job age, claim/lease latency, execution/verification/publication duration, duplicate suppression, and recovery;
- database lock/transaction time, checkpoint size, migration duration, backup/restore duration, and measured RPO/RTO;
- shell rejection categories, resource use, egress attempts, and sandbox startup/teardown if applicable.

Logs and metrics must be low-cardinality and redacted. Prompt text, tokens, raw memory, credentials, and user PII do not become metric labels.

---

## 6. Definition of Done

The whole architectural correction is done only when every applicable line below is true and evidence is attached to the exact merged SHA.

### Runtime and authority

- [ ] One application composition root owns settings, DB/session factory, provider gateway, request policy, memory stores, tools, jobs, and graph.
- [ ] No handler/store/feature creates a second configured LLM engine, queue, conversation store, or DB path outside an approved factory.
- [ ] All effectful AI actions resolve to typed capabilities and, for Nagar operations, enter the existing command bus and permission/evidence path.
- [ ] Unsupported, ambiguous, unauthorized, malformed, stale, or unapproved operations refuse explicitly and produce no side effect.

### LLM and queue

- [ ] Every external model call carries actor/purpose/data-classification/policy/correlation identity and is accounted by the gateway.
- [ ] Queue fairness, cancellation, timeout, retry, rate, daily, cost, and fallback semantics are tested; an abandoned caller cannot cause an untracked later call.
- [ ] Idempotency/effect keys and payload mismatch behavior are explicit for calls that can cause external effects.
- [ ] Provider and queue logs/metrics are redacted and low-cardinality.

### Memory and privacy

- [ ] Checkpoints, approved facts, and retrieval corpus have separate ownership and contracts.
- [ ] Memory records retain provenance, scope, actor, timestamps, confidence/policy, TTL, deletion state, and audit identity.
- [ ] Admission and retrieval screens reject instruction authority from untrusted content.
- [ ] Consent is default-deny where required; deletion propagates to derived indices; tenant isolation and cross-user negative tests pass.
- [ ] No claim says “memory safe” without poisoning and counterfactual-removal evidence.

### Planner and execution

- [ ] Plans are typed, versioned, hashed, dependency-checked, and persisted at the correct durability boundary.
- [ ] Steps declare capability, typed inputs, preconditions, postconditions, approval, idempotency/effect identity, and evidence requirements.
- [ ] Repair/replan is bounded; parallelism is only used for proven independent effects; replay does not repeat completed effects.
- [ ] Tool results and retrieved memory are data, not authority; approvals are actor/plan/parameter/expiry/nonce bound.

### Jobs and ingress

- [ ] Local SQLite queue remains contract-compatible and explicitly single-process.
- [ ] Production queue uses atomic claims, leases, fencing, recovery, cancellation, idempotency, typed retry policy, durable results, and notifier/effect protection.
- [ ] Two-worker, kill/restart, lease expiry, duplicate delivery, altered payload, and stale execution tests pass.
- [ ] Webhook 200 means durable acceptance, or non-2xx causes safe redelivery; update IDs are deduplicated.

### Storage and operations

- [ ] Every store honors the configured database backend/path through one composition-owned policy.
- [ ] Production topology requires external durable DB/object storage and does not rely on ephemeral local state.
- [ ] Backup success includes hash/size/schema/object identity and a successful isolated restore; immutable/isolated retention is configured where required.
- [ ] Migration compatibility, rollback/forward-fix, restart, scale-to-zero recovery, readiness, and identity probes are demonstrated.
- [ ] RPO/RTO are measured and named; if they are not measured, no recovery target is claimed.

### Security

- [ ] Existing access guard, SSRF, no-follow filesystem, pack trust root, CommandBus, artifact verification, and redaction controls remain green.
- [ ] Shell grammar is complete for every supported utility, or generic shell is removed.
- [ ] Arbitrary/untrusted code, if supported, runs inside a separately tested gVisor/microVM boundary with constrained inputs, outputs, resources, and egress.
- [ ] Trusted handoffs preserve provenance and cannot turn agent-created configuration into unreviewed host execution.

### Quality, CI, and governance

- [ ] Ruff, format, mypy, full non-slow tests, parity, extras, PostgreSQL migration, release lineage, architecture guards, and relevant mutation jobs pass on the exact SHA.
- [ ] Test collection before/after proves no test was removed, hidden, renamed, or newly skipped to obtain green.
- [ ] Every important failure path has unit, integration, adversarial/negative, system/CI, and mutation/architecture proof as appropriate.
- [ ] Living docs, audits, README/version statements, board claims, PR status, and Continuum are reconciled with live Git/GitHub truth.
- [ ] Production readiness is stated only for a named topology with owner-side deployment, backup, restore, and rollback evidence.

### Final decision record

- [ ] A final exact-SHA matrix marks each requirement `PASS`, `MISSING`, `BLOCKED`, or `NOT_APPLICABLE` with a command/job/artifact reference.
- [ ] Any remaining risk has an owner, trigger, mitigation, and explicit non-readiness consequence.
- [ ] No “green” result is used to imply evidence that was not actually collected.

---

## 7. Immediate next actions after plan approval

1. Re-read the live board and open PRs; claim W1’s non-overlapping composition/database zone.
2. Add targeted reproduction tests for G-01 through G-05 before changing runtime code.
3. Implement W1 only; do not combine planner, memory, queue, and deployment refactors in one diff.
4. Run targeted diagnostics locally if dependencies permit; defer full gates to the board’s gates owner and record the exact CI job.
5. Reconcile this plan’s evidence references after each merged wave; never edit a dated audit to make a stale claim appear current.
6. Start W2 and W3 in separate claims only after W1’s identity/path guards are green.

**No implementation is included in this artifact.** The next code change requires a board claim, a reproduction test, an owner, and a wave-specific proof plan.
