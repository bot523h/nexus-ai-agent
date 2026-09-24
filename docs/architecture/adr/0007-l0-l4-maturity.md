---
status: accepted
date: 2026-09-24
deciders: Agent 2 (arena/01a0d3a8-nexus-ai-agent) — Gate 2
consulted: src/nexus_ai_agent/creative/studio/, src/nexus_ai_agent/creative/packs/, tests/architecture/
informed: all agents
---

# 0007. L0–L4 maturity model — canonical definitions

## Context and Problem Statement

Gate 2 mission requires L0–L4 maturity model but forbids inventing from intuition — must search repo first. Repo scan 2026-09-24: `grep -R "L0|L1|L2|L3|L4|maturity|readiness" src/ docs/` → 0 results before Gate 2. No canonical definition exists. TDD uses permission levels A/B/C/D, not L-levels. Runtime has pure handlers (domain/reducer) + render lane (executor) + surface (bot). Need a model that distinguishes Defined → Registered → Reduced → Executed → Surfaced → Proven.

## Decision Drivers

- Must be evidence-based, not intuitive
- Must map to existing repo layers: L1 studio core, L2 pack substrate, L3 render lane, L4 surface
- Must be testable and have transition criteria
- Must support multi-layer status booleans

## Considered Options

1. **Adopt mission proposal as truth without verification** — L0 concept, L1 contract/registry, L2 domain/state, L3 real execution, L4 end-to-end
2. **Search repo, then adopt proposal if it matches evidence** — required by mission

## Decision Outcome

Chosen option: **2 — search, then adopt proposal because it matches evidence**.

Repo evidence:

* L1 studio core: `TypedCommand`, `CapabilityRegistry`, `CommandBus` — contract and registry presence
* L2 pack substrate: `creative/packs/*/operations.py` pure handlers — domain/reducer ready
* L3 render lane: `creative/rendering/` — real execution/instrument proven (exactly one subprocess site)
* L4 surface: `bot/creative_surface.py`, `worker.py` — end-to-end product proof

Therefore canonical model (matches mission proposal, now anchored):

* **L0** — concept / unimplemented — TDD entry only — DOCUMENTED_ONLY
* **L1** — contract or registry presence — registry knows operation_id, manifest verifies — SUPPORTED
* **L2** — domain/state behavior proven — pure handler + deterministic unit test — OBSERVED
* **L3** — real execution/instrument proven — render lane compiles IR → filtergraph → argv, real encode — VERIFIED
* **L4** — end-to-end product/surface proof — Telegram surface + idempotency + auth + locality — VERIFIED

Multi-layer booleans:

* PRODUCT_DEFINED, REGISTERED, DOMAIN_REDUCER_READY, EXECUTOR_READY, SURFACE_MAPPED, COMMAND_CONTRACTED, TESTED, PROVEN

Computation: `compute_l_level(registered, domain_reducer_ready, executor_ready, surface_mapped, tested, proven)` → LLevel.

Current distribution (2026-09-24):

* L0: 23 (portrait 10 + scene 10 + color 3)
* L1: 0 (all registered have reducer)
* L2: ~30
* L3: ~20
* L4: ~7

### Consequences

- (+) Clear transition criteria — no hallucinated completeness
- (+) Each level has required evidence and allowed claims
- (−) Requires maintaining matrix L-level column

## Confirmation

* `pytest -q tests/unit/test_contract_matrix.py::test_l_level_computation`
* `pytest -q tests/architecture/test_contract_boundaries.py::test_l_levels_are_canonical`
* Code: `src/nexus_ai_agent/creative/contracts/l0_l4.py`

## Pros and Cons of the Options

### Option 1 — adopt without verification

- (+) fast
- (−) violates Gate 2 rule "do not define from intuition"

### Option 2 — search then adopt (chosen)

- (+) evidence-based, matches repo layers, testable
- (−) requires scan, but done

## More Information

- Doc: `docs/contracts/L0_L4_MATURITY.md`
- Code: `src/nexus_ai_agent/creative/contracts/l0_l4.py`
- Matrix: `docs/contracts/OPERATION_CONTRACT_MATRIX.md` (Current L-Level column)
