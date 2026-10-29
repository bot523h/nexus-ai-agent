# Operation-Matrix blocker — root-cause dossier (M12, 2026-10-29)

Blocks: PR **#64**, PR **#97** (both `CI_RED`).
Failing suite: `tests/unit/test_operation_matrix_reconciliation.py`.

## 1. Reproduction (main `78809509`)

- `pytest -q tests/unit/test_operation_matrix_reconciliation.py` on `main` → **14 passed** (contract holds today).
- On PR #97 head `3307d397` → **3 failed, 11 passed**:
  - `test_reconciliation_numbers_invariant` → `Expected 57 registered operations, got 77`
  - `test_three_layers_are_strictly_decoupled` → `assert 77 == 57`
  - `test_no_fake_implementations_for_23_gaps` → `Gap operation portrait.relight_face was illegally registered!`
- Runtime registry on #97: **77** ops, adding all 10 `portrait.*` + 10 `scene.*`.
- On `main`: `build_runtime_registry().list_operations()` → **57** ops.

## 2. Root cause

Two hand-authored truth artifacts assert an *architectural* invariant that PR #97 (and #64) intentionally violates by completing the vision packs:

- `OPERATION_MATRIX.json`: 80 rows, `product_defined=70` (10 audio, 10 caption, 10 motion, 10 portrait, 10 scene, 10 timeline, 7 color, 3 delivery).
- `RECONCILIATION.json`: `missing_gaps` = **23** (10 portrait + 10 scene + 3 color) with the rule that none may be registered.
- The test hard-asserts: runtime registry == **57**, overlap == 47, gaps == 23.

PR #97 completes the portrait/scene packs, registering 20 previously-declared "gap" operations → 77 ops, so the reconciliation test fails. The registration is **not a stub**: `src/nexus_ai_agent/creative/packs/vision/operations.py` is a deterministic planning substrate (`PROCESSOR = "nagar.vision.plan.v1"`, typed `Definition` per op, permission levels) — it registers real typed operations, it does not claim pixel/ML execution.

The suite is not a stale snapshot: it is self-consistent with the two truth files and green on `main`. The divergence is between two *product contracts*:
1. "portrait/scene are deliberately unimplemented gaps (keep 57)" — `OPERATION_MATRIX.json` + `RECONCILIATION.json`.
2. "portrait/scene are implemented (register them)" — PR #97's packs.

## 3. Source of truth

- Mechanical runtime truth: `nexus_ai_agent.creative.packs.runtime.build_runtime_registry()`.
- Declared product truth: `OPERATION_MATRIX.json` (`product_defined`) + `RECONCILIATION.json` (`missing_gaps`).
- Derived human docs: `OPERATION_CONTRACT_MATRIX.md`, `RECONCILIATION.md`, `OPERATION_WAVE_PLAN.md`, `L0_L4_MATURITY.md` (rendered by `nexus_ai_agent.nagar.docs_projection`).
- **No generator exists for the JSON files.** `grep -rln "OPERATION_MATRIX.json\|RECONCILIATION.json" --include=*.py` returns only the test that reads them; the JSON is hand-edited. So "regenerate the derived artifact" is not available — the reconciliation is a **deliberate governance decision**, not a mechanical regeneration.

## 4. Correct repair (owner decision required — do NOT self-authorize)

Completing the packs is the higher-value direction (it does not discard working code and it matches actual capability). The canonical, non-weakening resolution is:

1. Reclassify the 20 now-implemented ops in `OPERATION_MATRIX.json` (`implementation_status`) and remove them from `RECONCILIATION.json::missing_gaps` (23 → 3 color-only).
2. Update `tests/unit/test_operation_matrix_reconciliation.py` invariants to the new agreed numbers (57 → 77 registry; overlap; gaps 23 → 3) — this changes the *contract value* because the contract genuinely changed, and must be recorded as such.
3. Regenerate the derived `.md` docs via `python -m nexus_ai_agent.nagar --docs` and keep the truth gate GREEN.

Rejected alternatives (must not be used):
- Editing the expected count to 77 **without** the governance decision and the truth-file update — weakens the anti-drift gate.
- Deleting the 20 real ops from #97 to force 57 — discards legitimate work.

## 5. Ownership boundary

The reconciliation touches the operation matrix, its truth files, the reconciliation test, and derived docs — an architectural scope shared by #64 and #97. It must have **one owner**. Proposed registration (via a separate board PR, per §C8): `task-280-operation-matrix-reconciliation`, zone `operation-matrix-reconciliation`, exclusive paths: `OPERATION_MATRIX.json`, `RECONCILIATION.json`, `OPERATION_CONTRACT_MATRIX.md`, `RECONCILIATION.md`, `OPERATION_WAVE_PLAN.md`, `L0_L4_MATURITY.md`, `tests/unit/test_operation_matrix_reconciliation.py`, `src/nexus_ai_agent/nagar/docs_projection.py`.

## 6. Status

Diagnosed and bounded; **not fixed** (owner decision + registration pending). #64 and #97 remain `ARCHITECTURAL_BLOCKED` and must not be merged until the contract is reconciled.

## 7. Next test (after repair)

`pytest -q tests/unit/test_operation_matrix_reconciliation.py tests/architecture/test_vision_pack_boundary.py tests/unit/test_vision_pack.py` must be green with the new invariants, and `python -m nexus_ai_agent.nagar --check` must stay `TRUTH GATE: GREEN`.
