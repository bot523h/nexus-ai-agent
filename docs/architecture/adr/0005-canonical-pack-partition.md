---
status: accepted
date: 2026-09-24
deciders: Agent 2 (arena/01a0d3a8-nexus-ai-agent) — Gate 2
consulted: docs/NAGAR_70_OPERATIONS_TDD.md, src/nexus_ai_agent/creative/packs/runtime.py
informed: all agents
---

# 0005. Canonical capability pack partition for the 70-operation contract

## Context and Problem Statement

TDD `docs/NAGAR_70_OPERATIONS_TDD.md` defines 7 packs ×10 =70 operations. Runtime `build_runtime_registry()` reports 57 operations from 6 packs + 5 wave1 ops. Agent 3 research report (not present on main, referenced in mission) reportedly uses 8 packs. Three truths coexist, breaking the contract: product catalog ≠ registry reality ≠ executable surface. Gate 2 must resolve to one canonical partition with evidence.

## Decision Drivers

- Single source of truth for product vs runtime vs surface
- No silent drift — machine-detectable reconciliation
- No fake implementation to reach 70
- Existing runtime is 6 packs (edit, motion, audio, caption, delivery, slideshow) — portrait and scene are MISSING

## Considered Options

1. **7 packs = 70 (TDD)** — keep TDD as canonical product catalog, runtime is 57 with gaps
2. **6 packs = 52 + 5 wave1 =57 (runtime)** — keep runtime as truth, ignore TDD
3. **8 packs (Agent 3 report)** — adopt 8-pack research, re-partition 70 into 8

## Decision Outcome

Chosen option: **1 + explicit reconciliation**.

* **Canonical product catalog:** 7 packs ×10 =70 operations as defined in TDD (source: `docs/NAGAR_70_OPERATIONS_TDD.md` §1.2-1.8). This is the product contract — WHAT Nagar should do.
* **Canonical runtime reality:** 6 packs + wave1 =57 operations as measured by `build_runtime_registry()` (source: `src/nexus_ai_agent/creative/packs/runtime.py` COMPOSITION). This is the runtime reality — what is actually executable.
* **Canonical pack count for product:** 7
* **Canonical pack count for runtime today:** 6 (edit, motion, audio, caption, delivery, slideshow) + wave1 (media/system)
* **8-pack report:** treated as RESEARCH input, not canonical — if it exists, it likely splits `color.delivery` (10) into `color` (7) + `delivery` (3) or adds `slideshow` as 8th. That split is DOCUMENTED_ONLY until verified.

Pack partition:

| Pack ID | TDD Ops | Runtime Ops | Status |
|---|---|---|---|
| nexus.edit.timeline | 10 | 9 + 1 wave1 split =10 effectively | 1 missing from manifest but present as wave1 |
| nexus.vision.portrait | 10 | 0 | MISSING |
| nexus.vision.scene | 10 | 0 | MISSING |
| nexus.motion.graphics | 10 | 10 | AVAILABLE |
| nexus.audio.studio | 10 | 10 | AVAILABLE |
| nexus.language.caption | 10 | 10 | AVAILABLE |
| nexus.color.delivery | 10 | 7 | 3 missing (white_balance, hdr_tonemap, deband_denoise) |
| nexus.slideshow.compose | 0 in TDD, 6 extra | 6 | Extra outside 70 |
| media/system | 0 in TDD, 4 extra | 4 | Extra outside 70 (play, pause, mark, undo) |

Formula: `70 - 20 (portrait+scene) -3 (color) +10 (extra) =57`

### Consequences

- (+) Product catalog stays 70, runtime stays 57 — both truths preserved, no fake inflation
- (+) Reconciliation is machine-detectable via `reconciliation_summary()` and tests
- (+) Future Gate can implement portrait/scene to close gap, without rewriting history
- (−) Two numbers coexist — mitigated by explicit matrix and ADR

## Confirmation

* `PYTHONPATH=src python -c "from nexus_ai_agent.creative.contracts.operation_matrix import reconciliation_summary; print(reconciliation_summary())"` must print `70 - 20 - 3 + 10 =57`
* `pytest -q tests/unit/test_contract_matrix.py::test_70_vs_57_formula` — asserts formula
* `docs/contracts/RECONCILIATION.md` documents A-I tables

## Pros and Cons of the Options

### Option 1 — 7 packs =70 TDD canonical (chosen, with reconciliation)

- (+) matches TDD source of truth
- (+) allows honest gap reporting
- (−) requires reconciliation table

### Option 2 — 6 packs =57 runtime canonical

- (+) matches live code
- (−) loses product intent, cannot explain 70

### Option 3 — 8 packs

- (+) might match research
- (−) research file not on main, cannot be verified; would be HYPOTHESIS

## More Information

- TDD: `docs/NAGAR_70_OPERATIONS_TDD.md`
- Runtime: `src/nexus_ai_agent/creative/packs/runtime.py`
- Matrix: `docs/contracts/OPERATION_CONTRACT_MATRIX.md`
- Reconciliation: `docs/contracts/RECONCILIATION.md`
- Capability contract: `docs/contracts/CAPABILITY_CONTRACT.md`
