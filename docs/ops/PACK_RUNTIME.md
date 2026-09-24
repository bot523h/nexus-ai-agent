# PACK_RUNTIME — how the Nagar capability packs are composed, activated and measured

> **Scope:** Wave 5 (`wave5-activation-and-gap-closure`, session `arena/01a0c5da`).
> **Audience:** anyone adding a capability pack, touching `nexus_ai_agent.creative.packs`,
> or debugging "why does `nexus packs list` say *pending*?".
> **Code:** `src/nexus_ai_agent/creative/packs/runtime.py` ·
> **Gates:** `tests/architecture/test_pack_activation_completeness.py` ·
> **Measurement:** `src/nexus_ai_agent/continuum/pack_coverage.py`

---

## 1. The rule the runtime enforces

A pack declares capabilities in its `pack.manifest.json`, but it may only be
**activated** once the runtime knows **every** operation of that manifest
(`PackRegistry.activate`). A capability the runtime does not know is reported as
*pending* and blocks activation — the TDD rule *"a pack cannot register an
operation by name alone"*.

That rule is correct. The defect Wave 5 fixed was on the other side of it: the
runtime registry used to be assembled **at each call site**, and only the
slideshow and caption packs were composed. Five of the six packs that ship inside
this repository therefore had *pending* capabilities even though their pure
operations were implemented and tested.

```
# before Wave 5 — nexus packs list
nexus.audio.studio     capabilities=8  · pending=8   ← shipped, importable, unreachable
nexus.edit.timeline    capabilities=8  · pending=8
nexus.motion.graphics  capabilities=7  · pending=7
nexus.color.delivery   capabilities=7  · pending=7
nexus.slideshow.compose capabilities=6               ← only these two were composed
nexus.language.caption  capabilities=10

# after Wave 5
all six: pending=0, and `nexus packs verify` accepts the repository's own manifests
```

---

## 2. One composition, one place

`nexus_ai_agent.creative.packs.runtime` is the **only** place that decides which
builtin packs exist in the runtime:

| Symbol | Meaning |
|---|---|
| `PackComposition` | one pack: `directory`, `package_id`, registrar, summary |
| `COMPOSITION` | the eight builtin packs, in deterministic order (slideshow → caption → edit → motion → audio → delivery → portrait → scene) |
| `COMPOSITION_BY_DIRECTORY` / `COMPOSITION_BY_PACKAGE_ID` | O(1) lookups used by the gate and the CLI |
| `build_runtime_registry()` | Wave-1 catalog + every pack's operations (77 operations today) |
| `build_pack_registry()` | the same registry wrapped in a `PackRegistry` (what the CLI consumes) |
| `build_pack_runtime()` | `PackRuntime`: composition + builtin registration + optional activation |
| `PackRuntime.status()` | one `PackStatus` row per pack: capabilities, pending, active, signature, binaries |
| `PackRuntime.activate_all()` | activates every *complete* pack (incomplete packs are skipped, never forced) |
| `PackRuntime.availability()` | one `PackAvailability` row per pack: `REGISTERED / AVAILABLE / MISSING_DEPENDENCY / MISSING_BINARY / DISABLED / FAILED` (session 2 — registered ≠ runnable; see `CREATIVE_RUNTIME_S3.md` §6) |
| `composition_issues()` | the verifier: a manifest without a builder, a builder without a manifest, or a mismatched `package_id` is a finding |
| `stale_capabilities()` | `package_id → capabilities the runtime cannot execute`; must be `{}` |
| `installed_version()` | the single implementation of the distribution-version lookup |

**Design decisions**

1. **Explicit data, not discovery magic.** A pack whose registrar is missing must
   fail the gate loudly instead of being skipped.
2. **Composition ≠ activation.** `nexus packs list` keeps reporting
   `active: false` until `nexus packs activate <id>` runs. Activation stays an
   explicit, auditable step; what changed is that every pack is now *activatable*.
3. **No import side effects.** Registrars are imported lazily inside
   `build_runtime_registry()`, so importing the package never drags the creative
   packs into memory and no pack can cycle back into the composition module.

---

## 3. Operating the packs

```bash
# what does the runtime know?
nexus packs list                 # human table: capabilities + pending + signature
nexus packs list --json          # machine-readable (package_id, capabilities, pending, active)

# does a manifest fit this runtime? (schema + policy + allow-list)
nexus packs verify src/nexus_ai_agent/creative/packs/motion/pack.manifest.json

# turn a pack on for this process
nexus packs activate nexus.motion.graphics --json
```

Python API:

```python
from nexus_ai_agent.creative.packs.runtime import build_pack_runtime

runtime = build_pack_runtime(activate=True)
runtime.complete  # True: no pending capability anywhere
[r.package_id for r in runtime.status()]  # six packs, composition order
len(runtime.operation_ids())  # 57 = wave-1 (5) + pack operations (52)
```

---

## 4. Adding a capability pack (checklist)

1. Create `src/nexus_ai_agent/creative/packs/<dir>/` with `pack.manifest.json`,
   `models.py` (typed inputs, `extra="forbid"`), `operations.py`
   (`register_<dir>_operations` + `build_<dir>_registry`).
2. Declare every operation as a `*_PACKAGE_ID`-namespaced capability in the
   manifest. A capability outside the package namespace does not validate.
3. Add **one** entry to `COMPOSITION` in `runtime.py` (directory, `package_id`,
   registrar). Nothing else composes packs.
4. `python -c "from nexus_ai_agent.creative.packs.runtime import composition_issues, stale_capabilities; print(composition_issues(), stale_capabilities())"`
   must print `() {}`.
5. Write the pack's tests and add them to `DEFAULT_TEST_TARGETS` in
   `continuum/pack_coverage.py` — a pack with no test target silently reports 0%.
6. Run `python scripts/pack_coverage.py` and the architecture gate:
   `pytest -q tests/architecture/test_pack_activation_completeness.py`.

Forbidden: editing any other pack's directory, adding a registrar call somewhere
else in the tree, or relaxing the substrate import allowlist
(`tests/architecture/test_pack_manifest_is_data_only.py`) — the measurement tool
lives in `continuum/` precisely so that allowlist never has to grow.

---

## 5. Measuring what actually runs

```bash
python scripts/pack_coverage.py                       # 85% bar, 24-module pack test set
python scripts/pack_coverage.py --threshold 95        # show what has not reached the goal
python scripts/pack_coverage.py --pack delivery --json
python scripts/pack_coverage.py --tests tests/unit/test_opgap_wave5.py --pack core
python scripts/pack_coverage.py --list-tests
```

Exit status: `0` every measured unit is at/above the bar, `1` at least one is
below, `2` bad usage. No third-party dependency: the harness uses stdlib
`trace` + `dis`, and the denominator comes from the compiler's line table.

**Wave-5 baseline (measured, not claimed)**

| unit | modules | cover | status |
|---|---:|---:|---|
| core (substrate: manifest/verify/registry/runtime/`__init__`) | 5 | 96.26% | OK |
| slideshow | 5 | 93.03% | OK |
| caption | 4 | 96.66% | OK |
| edit | 3 | 95.89% | OK |
| motion | 3 | 98.12% | OK |
| audio | 3 | 96.09% | OK |
| delivery | 4 | 87.19% | OK (weakest: `signing.py` 68.89%) |
| **TOTAL** | **27** | **94.89%** | OK |

The 95% goal of `wave4-step7` is not met yet, and the tool now says exactly where
the gap is: `delivery/signing.py`, `slideshow/models.py` and the pack `__init__`
re-export blocks.

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `packs list` shows `pending=N` | the pack is not in `COMPOSITION`, or its operations were never registered there | add/repair the `COMPOSITION` entry; `composition_issues()` names the directory |
| `packs verify` → `unknown_capability` | the manifest declares an operation the runtime does not know | implement + register the operation, or fix the capability name |
| `packs activate` → `cannot activate — the runtime does not know …` | the pack is genuinely incomplete | the message lists the missing operations verbatim |
| `duplicate operation` at import | two registrars claim the same operation id | one operation belongs to exactly one pack; fix the owner |
| coverage reports a pack at 0% | its tests are missing from `DEFAULT_TEST_TARGETS` | add them (see checklist step 5) |
| architecture gate fails on `creative/packs/coverage.py` | the measurement harness was placed in the data-only substrate | keep it in `nexus_ai_agent.continuum` |
