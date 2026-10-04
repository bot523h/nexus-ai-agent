# Nagar Overnight — ARENA HANDOFF

Specialist follow-ups that are architecture- or security-sensitive, with
reproduction, evidence, affected lines, risk, confidence, and why an overnight
patch would be unsafe.

---

## A-1 — Wire a real model provider behind `CognitionPort` (DESIGNED, not built)

- **Reproduction:** `python -c "from nexus_ai_agent.nagar.cognition import NullCognition"`
  — only the null provider exists.
- **Evidence:** `src/nexus_ai_agent/nagar/cognition/port.py:33` (the protocol),
  `src/nexus_ai_agent/llm/provider.py:6` (the existing `LLMProvider` ABC that an
  adapter would wrap).
- **Affected files:** new `src/nexus_ai_agent/nagar/cognition/providers/local.py`
  and `.../cloud.py` (to be added); callers in the composition root.
- **Risk:** a provider adapter is the first place untrusted model text becomes a
  proposal. It must go through `parse_proposal` and must never touch the bus
  directly.
- **Confidence:** high that the boundary is correct; medium that the adapter can
  be added without a new observability story (model calls / tokens / escalation
  rate — the mission's §31 metrics).
- **Why not patched overnight:** it needs a model-quality evaluation harness and
  live-provider configuration; doing it blind would risk a second execution path.
- **Recommended investigation:** add `LocalCognition` wrapping an injected
  `LLMProvider`, a contract test that it always returns through
  `parse_proposal`, and a cost/latency metric hook — as its own PR.

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
