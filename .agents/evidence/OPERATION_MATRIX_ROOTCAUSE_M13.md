# Operation-Matrix root cause — M13 correction and decision record

**Task:** `task-280-operation-matrix-reconciliation` (zone `operation-matrix-reconciliation`)
**Author:** OpenHands, M13 (2026-10-29)
**Supersedes the analysis in:** `.agents/evidence/OPERATION_MATRIX_ROOTCAUSE_M12.md` (PR #281)

## Why this corrects M12

M12 concluded: *"`OPERATION_MATRIX.json` and `RECONCILIATION.json` are hand-authored with **no
generator**."* That is only half true, and the missing half changes the decision.

There are **three** truth artifacts, not two:

| Artifact | Authoring | Consumed by | Gated? |
|---|---|---|---|
| `OPERATION_TRUTH.json` | **generated** — `python -m nexus_ai_agent.nagar [--docs]` | `nagar/truth.py`, `docs_projection.py` | yes — `--check` |
| `OPERATION_CONTRACT_MATRIX.md`, `RECONCILIATION.md`, `OPERATION_WAVE_PLAN.md`, `L0_L4_MATURITY.md` | **generated** (Markdown projections) | docs | yes — `--check` |
| `OPERATION_MATRIX.json`, `RECONCILIATION.json` | **hand-authored** | only `tests/unit/test_operation_matrix_reconciliation.py` | no |

The generator is `src/nexus_ai_agent/nagar/{__main__,truth,sources,docs_projection}.py`. It
recomputes from the **sources**: the product catalogue `docs/NAGAR_70_OPERATIONS_TDD.md`, the live
`build_runtime_registry()`, the executable surface, and the **source-level operation declarations**
scanned by `sources.read_declared_operations()`.

## Root cause (measured, not inferred)

On PR #97 head `3307d397`, with current `main` merged in:

```
$ python -m nexus_ai_agent.nagar --check
JSON DRIFT: [reconciliation_drift] missing_count: projection=23 computed=3
JSON DRIFT: [reconciliation_drift] runtime_count: projection=57 computed=77
JSON DRIFT: [declaration_parity] ... (implied)
FRESH INVARIANT: declaration_parity.registered_not_declared not empty:
  ['portrait.background_blur', 'portrait.correct_gaze', ... 'scene.track_object']   # 20 ids

$ python -m nexus_ai_agent.nagar --docs
FRESH INVARIANT: declaration_parity.registered_not_declared not empty: [ ... 20 ids ]
refusing to write: the recomputation violates structural truth rules
```

Two independent facts follow:

1. **The change is not merely a stale snapshot.** The generated truth file *is* stale relative to
   #97's registry (57→77, missing 23→3), which is the expected, mechanical part.
2. **#97 is structurally incomplete, by the repository's own rule.** The truth engine refuses to
   write because `declaration_parity.registered_not_declared` is non-empty: 20 operations are
   **registered in the runtime registry but have no source-level declaration symbol**.
   `sources._PACK_MODULES` scans only `slideshow, caption, edit, motion, audio, delivery`; the new
   `vision` pack is **not** in the scan set, and its ids are not declared as importable
   `OPERATION_*` constants (the established pattern — see `slideshow/operations.py`).
   `registered_not_declared` must be empty for the truth gate to pass.

So the "number" (#97 registers 20 portrait/scene ops) is the *symptom*; the defect is that the
registration has **no declaration source**. Editing the JSON numbers to 77 would not clear the
invariant.

## What a correct fix must do (and why it is a product decision, not a mechanical edit)

Clearing the invariant requires **both**:

1. **Declaration source** — add `nexus_ai_agent.creative.packs.vision.operations` to
   `sources._PACK_MODULES` and declare the 20 ids as `OPERATION_*` module constants (or otherwise
   give them a resolvable declaration symbol), so `read_declared_operations()` finds them.
2. **Maturity / evidence classification** — `OPERATION_MATRIX.json` (hand-authored) and the
   generated `OPERATION_TRUTH.json` must agree that these 20 operations are no longer
   `EvidenceClass.MISSING`. #97's own docstring states the pack is a *"deterministic, pure vision
   planning substrate; **no pixel/ML execution is claimed**"* — i.e. an L1 (planning) substrate, not
   an L3/L4 executed effect.

Step 2 is a **product/maturity decision**: do we accept a planning substrate as closing a
`product_defined` gap, or do the ops stay `MISSING` (and therefore must **not** be registered)?
Either answer is defensible; the mission's rules forbid choosing it unilaterally to make a test
green, and forbid keeping a registration that the truth engine rejects.

## Decision

- **Not implemented in this shift.** The change cannot be made *minimal, safe, and correct* while
  the product decision in step 2 is open, and it is coupled to #97/#64 (both register the same 20
  ops). Implementing it would either (a) silently reclassify 20 `MISSING` operations as done — a
  capability-truth claim this shift is not authorized to make — or (b) keep a registration the truth
  engine rejects.
- **Recorded precisely** here so the next owner can execute it in one pass, with the invariant named
  and the exact file set listed.
- `task-280-operation-matrix-reconciliation` remains the single-owner vehicle. Claim is held by
  `arena/m13-operation-matrix` during M13 and will be released at shift end.

## Prerequisites status (verified live)

`task-152-portrait-pack` and `task-153-scene-pack` are both `queued` (no owner). #64 and #97 are
both **clean against current `main`** (`git merge-tree --write-tree origin/main <head>` → no
conflicts); their only blocker is this contract. They are `ARCHITECTURAL_BLOCKED` on the decision
above, not on a merge conflict.

## Exact file set for the fix

```
src/nexus_ai_agent/nagar/sources.py                    # _PACK_MODULES += vision pack
src/nexus_ai_agent/creative/packs/vision/operations.py # declare OPERATION_* constants (20)
src/nexus_ai_agent/creative/packs/portrait/operations.py
src/nexus_ai_agent/creative/packs/scene/operations.py
OPERATION_MATRIX.json                                  # reclassify 20 rows (hand-authored)
tests/unit/test_operation_matrix_reconciliation.py     # contract numbers 57→77, gaps 23→3
                                                       # (ONLY if the product decision is "accept")
# then regenerate (generated, do not hand-edit):
#   python -m nexus_ai_agent.nagar --docs
#   OPERATION_TRUTH.json, OPERATION_CONTRACT_MATRIX.md, RECONCILIATION.md,
#   OPERATION_WAVE_PLAN.md, L0_L4_MATURITY.md
```

## Verification required after the fix

```
python -m nexus_ai_agent.nagar --check                     # must be GREEN
pytest -q tests/unit/test_operation_matrix_reconciliation.py
pytest -q tests/architecture/test_vision_pack_boundary.py tests/unit/test_vision_pack.py
pytest -q tests/unit/test_operation_truth.py tests/unit/test_docs_integrity.py
```
