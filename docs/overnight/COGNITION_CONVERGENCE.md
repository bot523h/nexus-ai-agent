# Cognition convergence — one path from model to execution

> Mission (this phase): move the *production decision path* behind the new
> **model-optional cognition boundary** and eliminate every legacy path under
> `agents/**` where **raw model text** could influence an **execution decision**
> without traversing typed proposal / policy / authorization.
>
> The closing sentence this work must make true:
>
> > There is exactly one reachable path from model cognition to an execution
> > intent, and every operational execution decision passes through typed
> > cognition, deterministic routing, registry/policy authorization, the
> > authorizer, the CommandBus and verification. Legacy raw-model execution
> > cannot cross this boundary.

This document is the engineering record for that phase. It adds **no new
feature** — no new abstraction, no second registry, no redesigned bus or
authorizer, no second execution path, no new endpoint.

## 1. What "bypass" means here (and what is not a bypass)

A **bypass** is a place where model output can reach an *executable decision* —
a command dispatch, a tool run, a shell, a file write — without passing through
the cognition boundary (typed proposal) and the deterministic substrate
(registry → policy → authorizer → CommandBus).

Things that are **not** bypasses:

* **Chat/persona generation.** `agents/{chat,qwen,gemma,phi}_agent.py` and
  `orchestration/graph._chat_agent` turn model text into `state["response"]`,
  which is *prose shown to the user*, not an executed action. Merging these
  into `nagar.cognition` would be a semantic mistake — `proposal_to_command`
  executes typed operations, it does not produce conversational text.
* **`agents/executor_agent.py` / `graph._executor_agent`.** They call
  `tool_registry.run(...)`, but the tool name comes from a **deterministic
  plan**, never from model text (`_planner_agent` is deterministic; `PlannerAgent`
  is deterministic). No `generate()` call exists on these paths, so no raw
  model text can select what runs.
* **`image_fill` / `StoreAgent.respond` / `knowledge_manager` / `ai_memory` /
  `short_term`.** Model output is image bytes, prose, a summary, or stored text
  — consumed as data, never as a selection of executables.

These paths were verified by reading the live tree (see `LOG.md`, evidence
commands). The one *consumed* model verdict that influences behaviour is
`phi_agent.moderate` — see §4.

## 2. The one path (composition)

`nagar.cognition.CognitionGateway` is the single composition-root adapter. It
adds no abstraction; it wires the pieces that already exist:

```
RoutingRequest
  -> DeterministicRouter.route            (pure; no model decides routing)
  -> blocked?  -> typed refusal (stop)    (fail closed, never fall through)
  -> schema.allowed_operations
       = CapabilityRegistry.list_operations()        (authority: the registry)
         ∩ caller's optional narrowing              (caller may narrow, never widen)
  -> CognitionPort.propose  -> TypedProposal | Refusal   (untrusted model text)
  -> proposal_to_command        (pure bridge; caller supplies actor/project)
  -> CommandBus.dispatch        (envelope schema, operation schema, capability
                                 version, pack lifecycle, execution policy,
                                 reference resolution, idempotency, revision,
                                 authorizer, atomic apply)
  -> CommandResult  |  typed refusal
```

Every rejection — router-blocked, empty offered set, producer refusal,
malformed proposal, disallowed operation, authority smuggling, authorization
denial, or any other deterministic bus rejection — becomes a typed
`CognitionRefused` carrying a stable `RefusalReason`. There is deliberately no
fall-back branch: the gateway never executes raw model text.

### Authority gap closed

Previously `ProposalSchema.allowed_operations` was *caller-supplied* at each
call site. The authoritative set now derives from the very registry the bus
consults (`nagar.cognition.capabilities.offered_operations`), and a caller may
only **narrow** it (`offered_operations_within` = `requested ∩ registry`). A
caller — or a model — asking for an operation the runtime does not expose is
dropped before the producer is even asked; if that leaves nothing, the gateway
refuses with `DISALLOWED_OPERATION`.

## 3. Fail-closed feature selection

`build_cognition_gateway(enabled=..., completion=...)` selects the producer,
never the authority:

* `enabled and completion is not None` → `LocalCognition(completion)` (the only
  place a model is ever consulted; `completion` is the canonical `LLMPort`);
* otherwise → `NullCognition` (always refuses).

A disabled flag or a missing provider therefore **cannot** fall back to a
raw-model path; the worst case is an explicit refusal. The selector reads no
global config object — the composition root passes `enabled` and `completion`
explicitly (dependency injection, no singleton). A fake provider is never
accepted as a model: `nagar.composition.build_cognition_provider` reports a
`FakeLLM` fallback as `not_configured`, so it cannot become a proposal source.

## 4. Honest limits (not overclaimed)

* **A production NL→operation caller exists now (`nexus intent`), but the
  applied path has only been driven with a *declared* scripted model** at the
  external `LLMPort` seam. The deterministic chain (router, registry schema,
  policy/authority, bus, verification, receipts, causal observation) is real;
  no paid/cloud model has been exercised here, so the claim is ARTIFACT-PROVEN,
  not PRODUCTION-PROVEN.
* **`phi_agent.moderate` now fails closed** (`agents/phi_agent.py`, commit
  `b257d5f`): an unparseable verdict is `{"safe": False, "reason": "parse_error"}`,
  a non-dict or `safe`-less verdict is `malformed_verdict`, and `safe` is
  normalised so only JSON `true` counts (a truthy `"false"`/`1` cannot read as
  safe). This closes the earlier policy weakness where a parse error was treated
  as "safe"; the verdict still never selects or grants an operation.
* **Chat paths remain raw-model by design** (§1). They are not execution.

## 5. Mechanical enforcement

* `tests/architecture/test_cognition_isolation.py` — the cognition package
  imports only its own package, studio *models* and studio *capabilities*; it
  never imports the bus, the authorizer or an execution primitive; the studio
  core never imports cognition.
* `tests/architecture/test_legacy_agent_no_raw_execution.py` — no module under
  `agents/**` both calls `generate()` and calls an execution sink; no agent
  imports the bus/bridge; the set of model-consuming agent modules is pinned.

## 6. Status

| Area | Status |
|---|---|
| Gateway (router → registry schema → producer → bridge → bus) | **IMPLEMENTED + VERIFIED** |
| `allowed_operations` derived from the registry | **IMPLEMENTED + VERIFIED** |
| Fail-closed flag/completion selection | **IMPLEMENTED + VERIFIED** |
| Legacy raw-model→execution bypass | **ABSENT — mechanically proven** |
| `agents/**` model consumers pinned | **VERIFIED** |
| Production free-text → gateway wiring (`nexus intent`) | **IMPLEMENTED + VERIFIED + ARTIFACT-PROVEN** |
| Durable typed handoff (intent → queue → existing worker) | **IMPLEMENTED + VERIFIED + ARTIFACT-PROVEN + DURABLE-PROVEN** |
| `phi_agent.moderate` fail-open | **RESOLVED — fail-closed (`b257d5f`)** |
| `phi_agent.moderate` → cognition boundary | **DEFERRED (ADR)** |
| Remote delivery / main integration | **DELIVERED — PR #155 fast-forwarded to a converged head** |
