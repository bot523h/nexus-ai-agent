# Nagar Operation Contract Matrix

> **Corrected by Gate 2.2 (2026-09-24).** This page previously carried a
> hand-written table of 80 rows asserting a maturity level and an evidence class
> per operation. Measurement showed those columns were never recomputed from
> anything — rewriting all nine `L4` rows to `L0` left the guard test green. The
> table is therefore **replaced by a generated projection**
> ([`../OPERATION_TRUTH.json`](../OPERATION_TRUTH.json)); this page keeps the
> vocabulary and points at the machine-readable evidence. Full reasoning:
> [`audits/GATE22_OPERATION_TRUTH_AUDIT_2026-09-24.md`](audits/GATE22_OPERATION_TRUTH_AUDIT_2026-09-24.md).

## 1. Where the per-operation truth lives now

| Question | Answer |
|---|---|
| Per-operation evidence, all 80 operations | `OPERATION_TRUTH.json` → `operations[]` |
| How it is produced | `python -m nexus_ai_agent.nagar --write` |
| How it is enforced | `python -m nexus_ai_agent.nagar --check` (a named CI step) and `tests/architecture/test_operation_truth_gate.py` |

Each node in the projection carries **identity, source, status, evidence and
owner** — and the two fields that no source can supply are `NOT AVAILABLE`
rather than invented:

* `owner` — no repository source names an operation owner. The projection
  reports the **registrar** instead (the pack that declares the operation, or
  the Wave-1 core registry for the five pack-less ones), which *is* sourced.
* `production_like` — no source defines or measures it.

## 2. Layer vocabulary

The single level token is gone. Every operation is described by eight
independent predicates; see
[`L0_L4_MATURITY.md`](L0_L4_MATURITY.md) §2 for the table and §1 for why the
repository's three competing `L0..L4` ladders cannot be used as one.

| Column (old page) | Now |
|---|---|
| `Def` (product-defined) | layer `defined` |
| `Reg` (registered) | layer `registered` |
| `Dom` (domain-ready) | layer `domain_ready` |
| `Exec` (pure executor ready) | layer `domain_ready` — the handler is the executor; a *media* executor is the separate `executor_ready` layer |
| `Surf` (surface mapped) | layer `surface_reachable`, with the derived entrypoint recorded |
| `Contract` (command contracted) | layer `registered` (the `OperationSpec` is the contract) |
| `Test` (tested) | `evidence.test_references` — the test files that literally name the operation |
| `Class` (evidence class) | the five verification states in `L0_L4_MATURITY.md` §3 |
| `Mat` (maturity level) | the eight layers; no bare `Lx` |
| `Owner` | `NOT AVAILABLE` (not sourced) + `registrar_pack` |

## 3. Measured summary (PR #70 baseline, `035a896` + PR #70)

| Metric | Value | How |
|---|---:|---|
| Product catalogue | 70 | seven pack tables of `NAGAR_70_OPERATIONS_TDD.md` |
| Runtime registry | 57 | `build_runtime_registry().list_operations()` |
| Overlap | 47 | `\|C ∩ R\|` |
| Catalogue gaps | 23 | portrait 10, scene 10, color 3 |
| Runtime-only | 10 | 4 Wave-1 core + 6 `slideshow.*` |
| Universe | 80 | `\|C ∪ R\|` |
| **Executable surface** | **12** | 7 `/edit` `/caption` `/grade` + 5 `/slideshow` — derived, not asserted |
| **Surface-reachable** | **12** | layer `surface_reachable` |
| **Executor-ready** | **7** | the render lane's execution branches |
| **Runtime-proven** | **1** | Gate 4 `Runtime = PASS` (`timeline.trim`) |
| **Artifact-proven** | **1** | Gate 4 `Artifact = PASS` (`timeline.trim`) |

**None of these numbers is stored in code.** They are recomputed and compared
against the generated projection; a source change reds the gate until the
projection is regenerated. That is deliberate: this baseline reports `57`
runtime operations, while the open PR #67/#69 report `77` — a hard-coded `57`
would fail on the integrated tree and block a correct change (audit §9).

## 4. Claims this page previously made, and their state

| Claim | State | Note |
|---|---|---|
| "All 9 surface operations verified in CI" | `NOT VERIFIED` | the `9` had no source; the derived surface is 12 and only 1 operation is artifact-proven |
| "100 % runtime readiness, Wave 1" | `NOT VERIFIED` | readiness here meant "a callable reducer exists", not "a runtime artifact was produced" |
| "EvidenceClass `INFERRED` applies to 0 operations" | `INFERRED` | true under the test-naming reading only |
| "Runtime Readiness 0 %" for the vision packs | `MISSING` on this baseline, `VERIFIED` on PR #67/#69 | those packs are implemented on the sibling branches |

## 5. Wave planning, restated honestly

The four waves are kept as *planning intent*; their readiness percentages are
withdrawn because they were derived from the level tokens above.

| Wave | Scope | Measured state on this baseline |
|---|---|---|
| 1 | Green cockpit: registry, bus, `/slideshow`, `/edit`, `/caption`, `/grade` | surface registered and reachable; **1** operation artifact-proven |
| 2 | Timeline and colour maturity | 47 of the catalogue's timeline/colour ids registered; 0 artifact-proven beyond `timeline.trim` |
| 3 | Audio, local speech, diarization | registered and domain-ready; 0 runtime-proven |
| 4 | Vision packs (portrait, scene) | `MISSING` here — **implemented on PR #67/#69** (20 ids), which is why the gap count differs between trees |

## 6. Regenerating

```bash
python -m nexus_ai_agent.nagar --write     # regenerate after an intended change
python -m nexus_ai_agent.nagar --check     # what CI runs
```
