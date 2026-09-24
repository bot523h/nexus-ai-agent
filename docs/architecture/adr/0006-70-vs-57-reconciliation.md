---
status: accepted
date: 2026-09-24
deciders: Agent 2 (arena/01a0d3a8-nexus-ai-agent) — Gate 2
consulted: src/nexus_ai_agent/creative/packs/runtime.py, docs/NAGAR_70_OPERATIONS_TDD.md
informed: all agents
---

# 0006. 70 vs 57 reconciliation — product catalog ≠ runtime registry ≠ executable surface

## Context and Problem Statement

Mission reports: "70 -20 -3 +10 =57". This must be independently verified from repo, not taken as truth. Gate 2 must prove with evidence that Product Catalog ≠ Runtime Registry ≠ Executable Surface, and that no operation is claimed implemented merely because its name exists.

## Decision Drivers

- Evidence-based reconciliation, not narrative
- Machine-detectable drift
- Zero hallucinated completeness
- Preserve distinction: Defined, Registered, Reduced, Executed, Surfaced, Proven

## Considered Options

1. **Claim 70 implemented** — forbidden by mission rule
2. **Reconcile with explicit tables A-I** — required by mission §10

## Decision Outcome

Chosen option: **2 — explicit reconciliation with evidence**.

Measured on main @ `035a896`:

* **70 catalog:** TDD 7 packs ×10 (see `canonical_70_catalog()`)
* **57 runtime:** `build_runtime_registry().list_operations()` = 57 (5 wave1 + 6 packs: slideshow 6, caption 10, edit 9, motion 10, audio 10, delivery 7)
* **Extra 10:** wave1 4 (media.play, media.pause, timeline.mark, system.undo) + slideshow 6
* **Missing 23:** portrait 10 + scene 10 + color 3 (white_balance, hdr_tonemap, deband_denoise)
* **Formula:** `70 -20 -3 +10 =57` verified
* **Orphan:** 0
* **Duplicates:** 0
* **Legacy/alias:** 0

Tables (see `docs/contracts/RECONCILIATION.md`):

* A. Which 70 in TDD? — 70 listed
* B. Which 70 in registry? — 47 of 70 are in registry (70-23)
* C. Which have domain reducer? — 57 (all runtime ops)
* D. Which have execution proof? — subset with render lane real encode
* E. Which only concept/contract? — 23 missing
* F. Which extra outside 70? — 10 listed
* G. Which ID collision? — none
* H. Which orphan? — none
* I. Which legacy/duplicate/alias? — none

Final proof sentence: **"Product Catalog ≠ Runtime Registry ≠ Executable Surface"** — proven via `reconciliation_summary()`.

### Consequences

- (+) Honest gap reporting — no fake inflation
- (+) Tests can detect drift: if new operation added to TDD but not runtime, test fails; if runtime adds operation without TDD update, extra count changes
- (−) Requires maintaining matrix JSON + reconciliation JSON

## Confirmation

* `PYTHONPATH=src /tmp/venv/bin/python -c "from nexus_ai_agent.creative.contracts.operation_matrix import reconciliation_summary; s=reconciliation_summary(); assert s['catalog_70_count']==70 and s['runtime_57_count']==57"`
* `pytest -q tests/unit/test_contract_matrix.py::test_reconciliation_tables`
* `pytest -q tests/architecture/test_contract_boundaries.py::test_product_catalog_not_equal_runtime_registry`

## Pros and Cons of the Options

### Option 1 — claim 70 implemented

- (+) looks good
- (−) violates Gate 2 rule, hides gaps, breaks evidence model — FORBIDDEN

### Option 2 — explicit reconciliation (chosen)

- (+) evidence-based, testable, honest
- (−) more docs, but required

## More Information

- Matrix: `docs/contracts/OPERATION_CONTRACT_MATRIX.md`
- JSON: `docs/contracts/OPERATION_MATRIX.json`, `RECONCILIATION.json`
- Code: `src/nexus_ai_agent/creative/contracts/operation_matrix.py`
- Tests: `tests/unit/test_contract_matrix.py`
