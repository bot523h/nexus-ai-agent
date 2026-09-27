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
| `COMPOSITION` | the six builtin packs, in deterministic order (slideshow → caption → edit → motion → audio → delivery) |
| `COMPOSITION_BY_DIRECTORY` / `COMPOSITION_BY_PACKAGE_ID` | O(1) lookups used by the gate and the CLI |
| `build_runtime_registry()` | Wave-1 catalog + every pack's operations (57 operations today) |
| `build_pack_registry()` | the same registry wrapped in a `PackRegistry` (what the CLI consumes) |
| `build_pack_runtime()` | `PackRuntime`: composition + builtin registration + optional activation |
| `PackRuntime.status()` | one `PackStatus` row per pack: capabilities, pending, active, signature, binaries |
| `PackRuntime.activate_all()` | activates every *complete* pack (incomplete packs are skipped, never forced) |
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
5. Write the pack's tests and map them in `PACK_TEST_TARGETS` in
   `continuum/pack_coverage.py` (the pack's directory → its test modules). A pack
   without a mapping is not "0%" any more — the canonical measurement is
   **unverified** (`pack_mapping_issues`), and a test module that imports a pack
   but is neither mapped nor a declared host-layer importer
   (`HOST_LAYER_PACK_IMPORTERS`) is named as an orphan (`pack_test_import_issues`).
6. Run `python scripts/pack_coverage.py` and the architecture gate:
   `pytest -q tests/architecture/test_pack_activation_completeness.py`.

Forbidden: editing any other pack's directory, adding a registrar call somewhere
else in the tree, or relaxing the substrate import allowlist
(`tests/architecture/test_pack_manifest_is_data_only.py`) — the measurement tool
lives in `continuum/` precisely so that allowlist never has to grow.

---

## 5. Measuring what actually runs — the 95% acceptance contract (DECISION_LOG D-0023)

```bash
python scripts/pack_coverage.py                                   # canonical: 27 targets, 95% bar
python scripts/pack_coverage.py --json-out ci-artifacts/pack-coverage.json
python scripts/pack_coverage.py --verify-artifact ci-artifacts/pack-coverage.json
python scripts/pack_coverage.py --list-tests
# diagnostic only — never accepted:
python scripts/pack_coverage.py --pack delivery --json
python scripts/pack_coverage.py --tests tests/unit/test_opgap_wave5.py --pack core
python scripts/pack_coverage.py --threshold 80
```

**The contract.** A report is *accepted* only when every one of these holds:

* the run is **canonical** — exactly the 27 targets of `DEFAULT_TEST_TARGETS`
  (derived from `PACK_TEST_TARGETS`, one entry per composed pack plus the
  substrate), every pack, the default pack root, and the bar at
  `ACCEPTANCE_THRESHOLD = 95.0`; a subset, a respelled duplicate, an unknown or
  partial pack selection, an alternate root or a lowered bar is a diagnostic run;
* the measurement is **verified** — the trace child exited 0, pytest reported
  passing tests with 0 failed / 0 errors / 0 deselected, the trace artifact
  carries the nonce of this run and exactly the expected keys, every measured pack has a
  non-empty executable surface (comment-only modules leave the surface; a pack
  with no surface at all is a measurement issue, never a silently dropped pack),
  and no mapping/orphan issue exists;
* **every pack** (not the total) is at or above 95.00%.

The `core` group is every module directly under `creative/packs/`, measured from
the substrate targets. A substrate module whose tests are not canonical targets is
measured as unexercised; `tests/unit/test_pack_coverage_contract.py` therefore
requires every substrate module imported by any test to be imported by a canonical
target (the trust-root suite of PR #101 was added to `SUBSTRATE_TEST_TARGETS` this way).

The denominator is the compiler's line table of every code object, minus the
lines that are not source (3.11+ emits a synthetic `RESUME` at line 0; 3.10 numbers
a comment-only module's implicit return). The numerator is the intersection of
traced lines with that surface, with integer hit counts ≥ 1 — so executed can
never exceed executable. No third-party dependency: stdlib `trace` + `dis`.

**Exit status:** `0` accepted, `1` rejected (below the bar, unverified,
non-canonical, or `--verify-artifact` did not reproduce the bytes), `2` invalid
request (bad threshold such as `NaN`, `inf`, negative or `true`; unknown pack;
missing target). `--json-out` deletes the old file first and writes atomically, so
a failed run never leaves a previous artifact behind.

**The artifact** (`nexus.pack-coverage/2`) is canonical JSON: sorted keys, no
wall-clock time, targets sorted, so two runs on the same commit and interpreter
are byte-identical (CI proves it with `cmp`). It is bound to its commit
(`provenance.git_commit`, `source_sha256` over the measured sources, the
interpreter identity) and records `accepted` next to the per-pack numbers.

**Measured at `f68757e` (Python 3.11.2, 27 targets, 29 modules, 397 tests passed):**

| pack | modules | executed / executable | cover | status |
|---|---:|---:|---:|---|
| audio | 3 | 615 / 637 | 96.55% | OK |
| caption | 4 | 868 / 894 | 97.09% | OK |
| core (substrate) | 7 | 980 / 1022 | 95.89% | OK |
| delivery | 4 | 566 / 566 | 100.00% | OK |
| edit | 3 | 490 / 508 | 96.46% | OK |
| motion | 3 | 679 / 689 | 98.55% | OK |
| slideshow | 5 | 880 / 899 | 97.89% | OK |
| **TOTAL** | **29** | **5078 / 5215** | **97.37%** | **ACCEPTED** |

`slideshow` was 93.55% under the old 85% bar; it reached the contract through
behavioural tests of its public invariants (`tests/unit/test_slideshow_invariants.py`)
and the existing adapter-boundary tests that had never been mapped
(`tests/architecture/test_slideshow_adapter_boundary.py`) — no line was excluded and no
hard path was removed. The `continuum-evidence` CI job re-measures on 3.10, 3.11
and 3.12 for every push and pull request and uploads the SHA-named artifact; the
numbers of each run live in that artifact, not in this document.

**Self-defence.** `tests/unit/test_pack_coverage_contract.py` pins every clause
above, and `python scripts/continuum_mutations.py` replays the mutation campaign
(`nexus.continuum-mutations/1`): every catalogued weakening of the harness, the
snapshot verifier, the CLI and the gate is applied, its killing tests run in a
fresh interpreter, and the file is restored and checked byte for byte. The run
fails if any applicable mutation survives.

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `packs list` shows `pending=N` | the pack is not in `COMPOSITION`, or its operations were never registered there | add/repair the `COMPOSITION` entry; `composition_issues()` names the directory |
| `packs verify` → `unknown_capability` | the manifest declares an operation the runtime does not know | implement + register the operation, or fix the capability name |
| `packs activate` → `cannot activate — the runtime does not know …` | the pack is genuinely incomplete | the message lists the missing operations verbatim |
| `duplicate operation` at import | two registrars claim the same operation id | one operation belongs to exactly one pack; fix the owner |
| coverage says `unverified` / names a pack without targets | the pack is composed but has no `PACK_TEST_TARGETS` entry, or a mapping names a pack that no longer ships | add or remove the mapping (checklist step 5) |
| coverage names an orphaned pack test | a test module imports a pack but is neither mapped nor a declared host-layer importer | map it under its pack, or add it to `HOST_LAYER_PACK_IMPORTERS` if it tests the host |
| coverage exits `1` with every pack ≥ 95% | the run is diagnostic (`--pack`, `--tests`, `--threshold`) or the trace child/pytest failed | run canonically; read `measurement_issues` in the JSON |
| architecture gate fails on `creative/packs/coverage.py` | the measurement harness was placed in the data-only substrate | keep it in `nexus_ai_agent.continuum` |
