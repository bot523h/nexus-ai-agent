# Nagar Overnight — ARENA HANDOFF

Specialist follow-ups that are architecture- or security-sensitive, with
reproduction, evidence, affected lines, risk, confidence, and why an overnight
patch would be unsafe.

---

## A-1 — Bind a real model provider behind `CognitionPort` (adapter IMPLEMENTED; live binding deferred)

- **Reproduction:** `python -c "from nexus_ai_agent.nagar.cognition import LocalCognition"`
  — the adapter exists now; what is still missing is a composition root that
  constructs a live `LLMProvider` (Gemini/local-server/fake) and binds it.
- **Evidence:** `src/nexus_ai_agent/nagar/cognition/adapter.py` (`LocalCognition`,
  `TextGenerator`), `src/nexus_ai_agent/llm/provider.py:6` (the existing
  `LLMProvider` ABC it satisfies structurally).
- **Status:** the adapter is **IMPLEMENTED + VERIFIED** (unit + end-to-end with a
  fake provider). Live-provider binding in the bot/worker runtime is deferred.
- **Affected files:** the composition root that constructs providers (runtime
  wiring), not the adapter.
- **Risk:** a provider adapter is the first place untrusted model text becomes a
  proposal. `LocalCognition` already routes every response through
  `parse_proposal` and cannot reach the bus directly; the remaining risk is in
  *choosing* a provider via the deterministic router, which must not let a model
  influence routing or privilege.
- **Confidence:** high that the adapter is correct (27 unit + 11 e2e tests, and
  the full suite green); medium on the runtime binding because it touches the
  highest-conflict `bot/handlers.py` surface.
- **Why not patched overnight:** binding a live provider adds an external
  dependency and touches the bot surface; doing it without a token/latency
  metric story (the mission's §31 metrics) and without touching
  `bot/handlers.py` would risk scope creep and conflict.
- **Recommended investigation:** construct `LocalCognition(provider)` in the
  worker composition root behind a feature flag, select it with
  `DeterministicRouter`, and add token/latency instrumentation to `LLMProvider`
  (the contract currently exposes no usage field, so the adapter deliberately
  reports none).

## A-2 — Causal Project Graph + Artifact Passport (NOT-FOUND on main)

- **Reproduction:** `grep -rn -iE "artifact.passport|project.graph|causal" src` →
  no matches (only unrelated checkpoint `verify_lineage`).
- **Evidence:** `docs/overnight/RECON.md` §"FOUND/NOT-FOUND".
- **Risk:** high — this is where "why does this artifact exist?" becomes durable
  truth. Getting the canonical/derived/immutable distinction wrong would bake a
  second source of truth into the product.
- **Why not patched overnight:** the mission explicitly says treat the causal
  graph as one of the most important assets and define which facts are canonical
  / derived / immutable / superseded *before* implementing. That is a design +
  storage-migration task, not a one-night change.
- **Recommended investigation:** design the fact taxonomy against the existing
  `Job`/`Attempt`/`EditTransaction`/`AssetRecord` records, then implement a
  projection (passport) over canonical evidence only.

## A-3 — Legacy chat/agent paths can treat model text as a decision

- **Reproduction:** `grep -rn "LLMProvider" src/nexus_ai_agent/agents` →
  `chat_agent.py:13`, `qwen_agent.py:26`, `gemma_agent.py:32`, `phi_agent.py:27`
  call `generate(prompt)` and consume free text directly.
- **Evidence:** `docs/overnight/RECON.md` §B and §F.
- **Risk:** high — this is the "hidden authority" surface: on these paths there
  is no typed proposal and no bus gate, so a caller *could* act on model text.
  The studio path is safe (everything goes through `CommandBus`); these legacy
  paths are not.
- **Why not patched overnight:** replacing them touches working capability and
  the `bot/handlers.py` highest-conflict file; the mission forbids destroying
  working capability to impose a preferred architecture.
- **Recommended investigation:** migrate these call sites to `CognitionPort`
  incrementally, one agent at a time, with a contract test per migration.
