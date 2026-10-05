---
name: nexus-nagar-pack-operation
description: This skill should be used when adding or changing a Nagar capability-pack operation in the nexus-ai-agent creative substrate, when asked to "add a pack operation", "extend the timeline/edit/motion/audio/caption/delivery pack", "wire a creative command", "add an operation to the capability registry", or when a pack boundary test fails. It encodes the pure-substrate rules, the manifest↔code coherence gate, the CommandBus dispatch pipeline, and the exact six-step recipe with its tests.
---

# NEXUS Nagar Pack Operation

## Purpose

Nagar is the creative studio. Its substrate is a set of **data-only capability packs** under
`src/nexus_ai_agent/creative/packs/`. Every user-visible creative action is a typed operation
dispatched through one `CommandBus`. Adding an operation is a well-defined six-step recipe, but it
touches five files and four boundary laws, and getting any one wrong turns a fitness function red.

## The three invariants

1. **One bus, one registry, one envelope.** Only `creative/studio/bus.py::CommandBus.dispatch`
   mutates state. AI modules may not import pack handlers; in the creative tree only the bus calls an
   operation handler (law R13). The envelope is `TypedCommand` with protocol `nagar.command.v1`; a
   `v2` protocol id is banned from `src/`.
2. **The substrate is pure.** A pack may import only stdlib, `pydantic`, `creative.studio`, and
   `creative.packs`. No `subprocess`/`socket`/`ctypes`/`shutil`/`os.system`, no `storage`/`llm`/`bot`
   reach-through, no ML/CV imports (laws R7, R9). If an operation must reach a real file, it emits an
   IR; the render lane (`creative/rendering/`) is the only place that spawns a process.
3. **Manifest ↔ code coherence.** A pack's declared capabilities must equal the operations its
   registration function actually adds, and the manifest must validate with **zero executable keys
   at any depth** (law R8).

## The six-step recipe

Worked example: add `timeline.deband_denoise` to the edit pack. Read
`src/nexus_ai_agent/creative/packs/edit/` first — it is the reference implementation.

### 1. Typed input model — `creative/packs/<pack>/models.py`

```python
OPERATION_DEBAND_DENOISE = "timeline.deband_denoise"


class DebandDenoiseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    clip_asset_id: str = Field(min_length=1)
    strength: float = Field(ge=0.0, le=1.0)
```

Every input model **must** use `extra="forbid"` (registration refuses non-`forbid` models) and
validate its own invariants in a `model_validator`.

### 2. Pure handler — `creative/packs/<pack>/operations.py`

```python
def _deband_denoise(project: Project, context: OperationContext) -> OperationOutcome:
    payload = DebandDenoiseInput.model_validate(context.input_data)
    # pure: read project, derive new AssetRecord(s), return an OperationOutcome
```

Register it in `register_<pack>_operations(registry)` with an `OperationSpec` (permission level,
schema version, execution modes). The handler must not mutate `project` in place and must not
perform I/O. `timeline.trim` at `operations.py` is the canonical Level-B (REVERSIBLE) example.

### 3. Manifest — `creative/packs/<pack>/pack.manifest.json`

Add the operation id to `capabilities`. Keep every key data-only (no `entrypoint`, `run`, `hooks`,
`install`, ...). `download_size_mb`, `permissions`, and `runtime` are data, not code.

### 4. Unit test — `tests/unit/test_<pack>_pack.py`

Deterministic handler test: build a `Project`, dispatch through a real `CommandBus`, assert the
outcome and the resulting state. No mocks for the bus or registry.

### 5. Boundary test — `tests/architecture/test_<pack>_pack_boundary.py`

The purity gate already exists per pack; add the new operation's id to its assertions if the test
enumerates them. Run it: `python -m pytest -q tests/architecture/test_<pack>_pack_boundary.py`.

### 6. Reach a real file? Extend the render lane, not the handler

If the operation must touch bytes, extend `creative/rendering/ir.py` + `compiler.py` (the
`LaneIR → filtergraph → argv → one FFmpeg` path). Never spawn a process from a pack handler.

Then update the coverage table in `docs/architecture/CREATIVE_STUDIO.md` §5 and the TDD ledger in
`docs/NAGAR_70_OPERATIONS_TDD.md`.

## The dispatch pipeline (what the bus checks)

Every command passes nine stages (plus 4b) inside one lock, each fail-closed; later stages never run
after a refusal: parse → envelope/operation schema → actor/project grant → capability+version+
permissions → **4b pack lifecycle gate** → execution policy A/B/C/D → input/time references →
idempotency reserve/replay/conflict → revision preconditions → pure handler + atomic commit.

The lifecycle gate sits *after* authorization (lifecycle can never grant what auth denied) and
*before* policy/idempotency/handler (a refused pack does zero work and consumes no key).
`allow_experimental` is composition-root state, never an envelope field — a command cannot widen its
own lifecycle.

## Permission ladder

- Level A (IMMEDIATE) — `project:read` baseline.
- Level B (REVERSIBLE) — `project:write`. `timeline.trim` is Level B.
- Level C — requires `confirmed`.
- Level D — always denied.

## Additional Resources

- **`references/contracts.md`** — the full command envelope, error taxonomy, idempotency formulas,
  the permission ladder, and the exact laws (R6–R9, R13, R14) with their enforcing tests.
- **`scripts/scaffold_pack_operation.py`** — prints the five file edits for a new operation from a
  small spec, so none is forgotten.

## Common mistakes

- Importing `subprocess`, `cv2`, `torch`, or `storage`/`llm` inside a pack — R7/R9 go red.
- Forgetting to add the operation id to the manifest — R8 goes red.
- Using an input model without `extra="forbid"` — registration refuses it.
- Spawning a process from the handler instead of emitting lane IR — R9 goes red.
- A new test that imports a pack but is not classified in `pack_coverage.py` — the coverage contract
  fails. Use the pack-free `build_wave1_registry()` in tests that do not need a real pack.
