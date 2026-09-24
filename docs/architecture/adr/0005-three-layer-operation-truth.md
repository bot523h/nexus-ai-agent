# ADR 0005: Decoupling Product Catalog, Runtime Registry, and Executable Surface

- Status: accepted
- Date: 2026-09-24
- Deciders: Agent B (Architecture & Operation Matrix Stewardship)
- Consulted: Agent 1 (Runtime Owner), Agent A (Contract Alignment)
- Informs: `docs/OPERATION_CONTRACT_MATRIX.md`, `docs/RECONCILIATION.md`, `docs/L0_L4_MATURITY.md`

## Context and Problem Statement

Previous iterations of the Nagar/Nexus documentation and task tracking suffered from semantic conflation between three distinct architectural layers:
1. Operations defined in the aspirational product documentation (`docs/NAGAR_70_OPERATIONS_TDD.md`, 70 operations across 7 capability domains);
2. Operations implemented, typed, and registered within the runtime CapabilityRegistry (`src/nexus_ai_agent/creative/packs/runtime.py`, 57 operations);
3. Operations directly executable from an end-user surface such as Telegram or worker jobs (`src/nexus_ai_agent/bot/creative_surface.py` and `src/nexus_ai_agent/creative/render_jobs.py`, 9 operations).

This conflation led to "dashboard inflation" where unverified stubs or mock registries were proposed to prematurely claim complete implementation of the 70 product operations.

## Decision Drivers

- **Zero Fake Implementations:** Never inflate metrics with empty stubs or mocked registries.
- **Strict Evidence Gating:** Every capability claim must be backed by reproducible test evidence.
- **Boundary Protection:** Strict isolation between Runtime-owned modules (`creative/rendering/*`, `creative/packs/*`, `creative/slideshow/*`) and Agent surfaces.
- **Reproducible Truth:** Every number in documentation must be programmatically verifiable via unit tests and automated inspection scripts.

## Considered Options

1. **Option 1: Merge Catalog into Runtime Registry (Stub Missing 23):** Implement empty placeholder handlers for all 23 missing portrait/scene/color operations to make the registry count equal 70.
2. **Option 2: Three-Layer Decoupled Operation Truth Model (Chosen):** Explicitly isolate Product Catalog (70) != Runtime Registry (57) != Executable Surface (9), preserving the 23 missing gaps as `EvidenceClass.MISSING`, `L0`, `NOT_IMPLEMENTED`.
3. **Option 3: Scope Down Product Catalog to 57:** Alter the historical specification in `NAGAR_70_OPERATIONS_TDD.md` to match current code.

## Evaluation Matrix

| Evaluation Criteria | Weight | Option 1 (Stub Gaps) | Option 2 (Three-Layer Truth) | Option 3 (Scope Down) |
|---|---:|---:|---:|---:|
| Fit to Nagar Architecture | 40 | 10 | 40 | 25 |
| Security (Fail-Closed / IDOR) | 20 | 5 | 20 | 15 |
| Maintainability | 15 | 5 | 15 | 10 |
| Testability & Reproducibility | 15 | 5 | 15 | 10 |
| Operational Cost | 10 | 2 | 10 | 5 |
| **Total Weighted Score** | **100** | **27** | **100** | **65** |

## Decision Outcome

Chosen option: **Option 2: Three-Layer Decoupled Operation Truth Model**.

The three layers are strictly defined as:
1. **Layer 1: Product Catalog (70 Operations)**: Architectural specification of capabilities defined in `docs/NAGAR_70_OPERATIONS_TDD.md`. Being listed in the catalog conveys zero runtime implementation.
2. **Layer 2: Runtime Registry (57 Operations)**: In-memory pure handlers registered in `nexus_ai_agent.creative.packs.runtime.build_runtime_registry()`. Having a registry entry proves domain models and deterministic reducer logic exist, but does not imply user-facing reachability.
3. **Layer 3: Executable Surface (9 Operations)**: Operations wired to durable queues, permission checks, and Telegram entrypoints (`/edit`, `/caption`, `/grade`, `/slideshow`).

### Reconciliation Facts

- **Product Catalog:** 70 operations
- **Runtime Registry:** 57 operations
- **Reconciled Overlap:** 47 operations
- **Missing Gaps:** 23 operations (10 `portrait.*`, 10 `scene.*`, 3 `color.*`)
- **Runtime Extras:** 10 operations (4 Wave 1 transport/undo/marker, 6 `slideshow.*`)
- **Universe Total:** 80 unique operations

### Architectural Boundary Safety

All 23 missing operations are classified as `GAP -> Agent 1 (Runtime Owner)`:
- `creative/rendering/*`
- `creative/packs/*`
- `creative/slideshow/*`
are runtime-owned boundaries. Agent B does NOT fabricate stubs in these paths.

## Confirmation

This decision is enforced and verified by:
1. `tests/unit/test_operation_matrix_reconciliation.py`: Automated assertion verifying the 70/57/23/9 invariant and ensuring no fake implementations exist in the registry.
2. `OPERATION_MATRIX.json` and `RECONCILIATION.json`: Machine-readable schemas synchronizing catalog and registry states.
3. `tests/unit/test_docs_integrity.py`: Confirms ADR indexing and documentation consistency.
