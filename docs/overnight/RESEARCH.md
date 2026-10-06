# Deep research used — overnight/nagar-20261004

Dated record. Each entry is `Source → Finding → Decision → Nagar impact`.
Research was used only to resolve a design decision, never for decoration.

## R-1 — Governed execution must be built *around* the model

- **Source:** "Separation of Duties for Privileged LLM Agents: A Governed
  Execution Architecture with Measured Security–Utility Trade-offs"
  (arXiv, 2025) — <https://arxiv.org/html/2609.38224v1>
- **Finding (quoted):** *"If the model cannot be the boundary, the boundary must
  be built around it. … a planner that drafts actions, a policy gate that
  adjudicates them, a privileged executor that performs only affirmatively
  adjudicated actions, and an independent auditor that reviews the ledger …
  actions may be submitted as structured intents (typed operation, absolute
  paths, typed arguments), so adjudication never parses shell syntax; and an
  approval is a credential bound to the exact bytes that will run and
  consumable exactly once."*
- **Decision:** the cognition layer only *proposes* a typed intent; the existing
  `CommandBus` is the policy gate + privileged executor; the existing job
  verifier is the independent auditor. `parse_proposal` refuses any
  shell/argv/env/authority field by name.
- **Nagar impact:** `src/nexus_ai_agent/nagar/cognition/proposal.py`
  (`_AUTHORITY_FIELDS`), `bridge.py` (candidate command only), and the
  integration test that proves a bridged command still needs the authorizer.

## R-2 — The LLM may propose; the runtime must authorize

- **Source:** "AI Agent Architecture: The Trust Boundary Model" (aakashx.com,
  secondary) — <https://www.aakashx.com/blog/agent-trust-boundary-model-ai-agent-architecture>
- **Finding (quoted):** *"The LLM may propose. The runtime must authorize. The
  model should not decide its own permissions, approve its own tool calls, widen
  its own scope, or treat retrieved content as authority."*
- **Decision:** adopt the exact rule "model proposes, runtime authorizes" as the
  boundary's contract, and make it executable rather than aspirational.
- **Nagar impact:** `port.py` docstring; `tests/unit/test_cognition_bus_integration.py`
  (`test_bridged_command_without_authorizer_is_refused_by_the_bus`).

## R-3 — Control-flow integrity requires the plan to be fixed before untrusted data

- **Source:** "Planner-Executor Agentic Framework" (emergentmind, secondary
  survey) — <https://www.emergentmind.com/topics/planner-executor-agentic-framework>
- **Finding (quoted):** *"The separation prevents untrusted tool outputs (which
  Executors handle) from directly influencing the Planner's subsequent plan —
  formally, the Planner's plan is fixed before any external data is ingested …
  the Planner holds minimal or no tool permissions; Executors are provisioned
  only with the tool required for each step."*
- **Decision:** keep the cognition layer dependency-light (no execution
  primitives — enforced by an architecture fitness test) and give the producer
  no tool access; it receives a bounded, caller-projected context only.
- **Nagar impact:** `tests/architecture/test_cognition_isolation.py` (import
  allowlist + forbidden-call scan); `context.py` bounded JSON facts.

## R-4 — Blackboard lineage: contributions carry evidence and confidence

- **Source:** "The Blackboard Is Back" (tianpan.co, secondary) —
  <https://tianpan.co/blog/2026/07/02/the-blackboard-is-back> and "LLM-based
  Multi-Agent Blackboard System for Information Discovery in Data Science"
  (arXiv 2510.01285) — <https://arxiv.org/html/2510.01285v1>
- **Finding (quoted):** *"structure your shared state into levels, make every
  contribution carry its evidence and confidence"*; classical blackboard =
  global hierarchical state + independent knowledge sources + an explicit
  control mechanism.
- **Decision:** every proposal carries `provenance` (producer identity, created
  time, source) and `confidence`; the future causal project graph should adopt
  the "levels + evidence + confidence" shape rather than a flat log.
- **Nagar impact:** `proposal.py::ProposalProvenance`; the design note for the
  causal graph in `ARENA_HANDOFF.md` A-2.

## Honest note on source quality

R-1 and R-4 include a primary arXiv source; R-2 and R-3 are secondary surveys
and are labelled as such. The decisions above do **not** depend on any single
source: they are also the repo's own already-merged invariant (the `CommandBus`
docstring at `studio/bus.py:1-40` states "no command mutates state except
through the bus" and "identity must never be self-asserted"). Research
confirmed the design; it did not invent it.
