# ADR 0005: Decoupling Product Catalog, Runtime Registry, and Executable Surface

- Status: accepted, **superseded in part by Gate 2.2** (2026-09-24) — see "Gate 2.2 supersession" below
- Date: 2026-09-24
- Deciders: Agent B (Architecture & Operation Matrix Stewardship)
- Consulted: Agent 1 (Runtime Owner), Agent A (Contract Alignment)
- Informs: `docs/OPERATION_CONTRACT_MATRIX.md`, `docs/RECONCILIATION.md`, `docs/L0_L4_MATURITY.md`, `OPERATION_TRUTH.json`

## Context and Problem Statement

Previous iterations of the Nagar/Nexus documentation and task tracking suffered from semantic conflation between three distinct architectural layers:
1. Operations defined in the aspirational product documentation (`docs/NAGAR_70_OPERATIONS_TDD.md`, 70 operations across 7 capability domains);
2. Operations implemented, typed, and registered within the runtime CapabilityRegistry (`src/nexus_ai_agent/creative/packs/runtime.py`, 57 operations);
3. Operations directly executable from an end-user surface such as Telegram or worker jobs (`src/nexus_ai_agent/bot/creative_surface.py` and `src/nexus_ai_agent/creative/render_jobs.py`, **12** operations once derived rather than counted by hand — Gate 2.2 correction).

This conflation led to "dashboard inflation" where unverified stubs or mock registries were proposed to prematurely claim complete implementation of the 70 product operations.

## Decision Drivers

- **Zero Fake Implementations:** Never inflate metrics with empty stubs or mocked registries.
- **Strict Evidence Gating:** Every capability claim must be backed by reproducible test evidence.
- **Boundary Protection:** Strict isolation between Runtime-owned modules (`creative/rendering/*`, `creative/packs/*`, `creative/slideshow/*`) and Agent surfaces.
- **Reproducible Truth:** Every number in documentation must be programmatically verifiable via unit tests and automated inspection scripts.

## Considered Options

1. **Option 1: Merge Catalog into Runtime Registry (Stub Missing 23):** Implement empty placeholder handlers for all 23 missing portrait/scene/color operations to make the registry count equal 70.
2. **Option 2: Three-Layer Decoupled Operation Truth Model (Chosen):** Explicitly isolate Product Catalog (70) != Runtime Registry (57) != Executable Surface (12), preserving the 23 missing gaps as unimplemented — never stubbed. (The original text said `9` and `L0`; see the Gate 2.2 supersession.)
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
3. **Layer 3: Executable Surface (12 Operations)**: Operations wired to durable queues, permission checks, and Telegram entrypoints (`/edit`, `/caption`, `/grade`, `/slideshow`). Gate 2.2 derives this set from three independent probes rather than counting it: 7 via the surface allow-list and the worker's closed map (which must agree exactly), plus 5 reached by the `/slideshow` handler through the `CommandBus`. The originally published `9` was not derivable from any source; see the audit §5.

### Reconciliation Facts

Measured **on this baseline** (`035a896` + PR #70). These are not product
constants: the same measurement on PR #67/#69 returns 77/67/3, because those
branches implement twenty of the declared gaps.

- **Product Catalog:** 70 operations
- **Runtime Registry:** 57 operations
- **Reconciled Overlap:** 47 operations
- **Missing Gaps:** 23 operations (10 `portrait.*`, 10 `scene.*`, 3 `color.*`)
- **Runtime Extras:** 10 operations (4 Wave 1 transport/undo/marker, 6 `slideshow.*`)
- **Universe Total:** 80 unique operations
- **Executable Surface:** 12 operations (derived; §5 of the Gate 2.2 audit)
- **Runtime-proven / artifact-proven:** 1 operation (`timeline.trim`), per Gate 4

### Architectural Boundary Safety

All 23 missing operations are classified as `GAP -> Agent 1 (Runtime Owner)`:
- `creative/rendering/*`
- `creative/packs/*`
- `creative/slideshow/*`
are runtime-owned boundaries. Agent B does NOT fabricate stubs in these paths.

## Confirmation

This decision is enforced and verified by:
1. `tests/architecture/test_operation_truth_gate.py`: recomputes the three sources and fails when the generated projection disagrees; asserts the layers stay un-collapsed.
2. `tests/unit/test_operation_truth_mutations.py`: sixteen adversarial probes (a removed runtime operation, an invented one, a removed and an invented surface mapping, a changed catalogue, a rewritten maturity ladder, mutated evidence, a fabricated owner, a broken provenance chain).
3. `tests/unit/test_operation_truth_sources.py` and `tests/unit/test_operation_truth_runtime_confirmation.py`: the derivations, and a real execution that must agree with the static ones.
4. `tests/unit/test_docs_integrity.py`: confirms ADR indexing and documentation consistency.

## Gate 2.2 supersession (2026-09-24)

Gate 2.2 kept this decision's **core** — three decoupled layers, zero fake
implementations, no stub to close a gap — and corrected three things that
measurement showed this ADR could not support:

1. **Superseded evidence mechanism.** `tests/unit/test_operation_matrix_reconciliation.py`
   read its expected values from the documents it was checking, and
   `OPERATION_MATRIX.json` / `RECONCILIATION.json` were hand-written. Four of the
   six mutation classes expressible against that suite left it green, including
   deleting a catalogue row outright. Both JSON files and that test are removed;
   the truth is now the generated `OPERATION_TRUTH.json`, and the gate is
   `python -m nexus_ai_agent.nagar --check`.
2. **Corrected surface count.** 9 → 12, derived from three probes instead of
   written as a literal.
3. **Corrected maturity claim.** The ADR's `L0..L4` tokens inherit PR #70's
   ladder, which conflicts with PR #68's definition of the same symbols. Gate 2.2
   emits eight evidence layers instead of a bare level, and fingerprints all
   three ladders so none can change silently. The conflict is recorded, not
   resolved — see the audit §8.

Full record: [`docs/audits/GATE22_OPERATION_TRUTH_AUDIT_2026-09-24.md`](../../audits/GATE22_OPERATION_TRUTH_AUDIT_2026-09-24.md).
