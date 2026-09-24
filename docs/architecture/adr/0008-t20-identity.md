---
status: accepted
date: 2026-09-24
deciders: Agent 2 (arena/01a0d3a8-nexus-ai-agent) — Gate 2
consulted: docs/NAGAR_70_OPERATIONS_TDD.md, src/nexus_ai_agent/creative/packs/, grep for T20
informed: all agents
---

# 0008. T20 identity — canonical meaning and drift prevention

## Context and Problem Statement

Mission reports known conflict: "T20 = Delete Range but in part of documentation attributed to something else." Must find all references to T20, define source of truth, fix collision, add integrity gate to prevent return.

Repo scan 2026-09-24:

* `grep -R "T20|Delete Range|delete_range" src/ docs/ --include="*.py" --include="*.md" --include="*.json"` → 0 results
* `grep -R "T[0-9][0-9]" src/ docs/` → only STRIDE T1-T14 in SECURITY.md, not operation IDs
* No T20 reference exists in codebase before Gate 2

TDD defines T20 as:

* **T20 = portrait.stabilize_face** (Pack 2, vision.portrait, 10th operation)

Mission's reported "T20 = Delete Range" would correspond to timeline operations, but timeline pack already has:

* T01 = split_at_playhead
* T02 = trim
* T03 = ripple_delete (this is "delete range" semantically)
* So "Delete Range" would be T03, not T20

Therefore collision is DOCUMENTED_ONLY (exists in some external doc or research report not on main), not in repo code. Must resolve to TDD source of truth.

## Decision Drivers

- Single canonical truth for T20
- Prevent future drift
- Integrity gate

## Considered Options

1. **T20 = Delete Range** — matches mission's reported conflict but contradicts TDD
2. **T20 = portrait.stabilize_face** — matches TDD canonical 70 list

## Decision Outcome

Chosen option: **2 — T20 = portrait.stabilize_face**, because TDD is source of truth for product catalog.

* **Canonical meaning:** T20 = `portrait.stabilize_face` (Pack: nexus.vision.portrait, Product ID T20)
* **Wrong references:** None in repo (0 results). If external doc says "Delete Range", that is drift — Delete Range is T03 `timeline.ripple_delete`.
* **Fixed references:** Matrix and reconciliation explicitly list T20 as `portrait.stabilize_face` with evidence class MISSING (not in runtime 57)
* **Integrity gate:** `test_t20_identity` in `tests/unit/test_contract_matrix.py` asserts T20 == portrait.stabilize_face and fails if drift returns

### Consequences

- (+) Canonical T20 defined, non-drifting
- (+) Test prevents future collision
- (−) If some external doc still says Delete Range, it must be updated to T03

## Confirmation

* `grep -R "T20" src/ docs/ --include="*.py" --include="*.md"` → only this ADR and matrix (no drift)
* `pytest -q tests/unit/test_contract_matrix.py::test_t20_canonical`
* `PYTHONPATH=src python -c "from nexus_ai_agent.creative.contracts.operation_matrix import CANONICAL_70; print([x for x in CANONICAL_70 if x[0]=='T20'])"` → `[('T20', 'portrait.stabilize_face', ...)]`

## Pros and Cons of the Options

### Option 1 — T20 = Delete Range

- (+) matches reported conflict text
- (−) contradicts TDD, would make timeline pack have two delete ops, loses portrait op — violates product contract

### Option 2 — T20 = portrait.stabilize_face (chosen)

- (+) matches TDD source of truth, preserves 7×10=70 partition
- (−) requires acknowledging that "Delete Range" conflict was external doc drift

## More Information

- TDD: `docs/NAGAR_70_OPERATIONS_TDD.md` §1.3 (portrait pack)
- Matrix: `docs/contracts/OPERATION_CONTRACT_MATRIX.md` (T20 row)
- Test: `tests/unit/test_contract_matrix.py::test_t20_canonical`
- Reconciliation: `docs/contracts/RECONCILIATION.md`
