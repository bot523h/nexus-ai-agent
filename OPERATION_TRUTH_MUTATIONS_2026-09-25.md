# Operation Truth — Mutation Evidence Log (2026-09-25)

**Branch:** `arena/01a0d708-nexus-ai-agent` · **base:** `main @ fe95cf0`
**Suite:** `pytest tests/unit/test_operation_truth_mutations.py -v` → **23/23 PASSED** (~25 s)
**Protocol:** every probe executes **GREEN → MUTANT → RED → RESTORE → GREEN** inside one
test: `_assert_green` before the mutation, `_assert_red` after it, `_assert_green`
again after the undo. A probe that stays green under mutation fails the test — the
gate cannot be self-certifying.

Red is accepted in exactly four shapes, never as a stored number:

1. `truth.compare(stored, fresh)` returns drift findings (kind + detail);
2. `truth.invariants(...)` reports a structural-rule violation;
3. `sources.SourceError` — the measurement itself refuses (duplicate/empty/corrupt);
4. a generated page differs from a fresh rendering (`doc-drift`).

## Probe → guarded fraud class

| # | Probe (test) | Mutation | Red marker (expected class) | Fraud class guarded |
|---|---|---|---|---|
| 1 | `test_probe_drop_a_pack_is_red` | remove `nexus.color.delivery` from the live composition | `reconciliation_drift`, `declared_not_registered`, `missing_set_added` | registry shrink hidden by stale counts; **implementation without registry** |
| 2 | `test_probe_fake_registration_is_red` | inject `timeline.fake_inflation` into the registry | `operation_added`, `registered_not_declared`, `declaration_parity_drift` | **fake/registry count inflation**; registry without declaration |
| 3 | `test_probe_remove_worker_mapping_is_red` | delete `(edit, trim)` from `SURFACE_TO_CANONICAL` | `surface_contract_drift`, `surface_probe_drift` | **capability mismatch**; stale matrix |
| 4 | `test_probe_invent_worker_mapping_is_red` | add `(edit, deess) → audio.deess` | `surface_contract_drift`, `contract_drift_changed` | **surface operation without contract** |
| 5 | `test_probe_surface_accepts_unmappable_operation_is_red` | allow-list advertises `ghost` | `surface accepts requests nothing can execute` | surface claims nothing can serve |
| 6 | `test_probe_delete_catalogue_row_is_red` | delete the `timeline.trim` row | `reconciliation_drift`, `identity_drift` | **docs/catalog count drift** (row PR #70 ignored) |
| 7 | `test_probe_rename_operation_with_stale_id_is_red` | `timeline.trim` → `timeline.trim_renamed` | `operation_added`, `missing_set_added` | **renamed operation with stale ID** |
| 8 | `test_probe_duplicate_operation_id_is_refused` | duplicate a catalogue row | `SourceError: duplicate operation id` | **duplicated operation ID** (refused, not deduped) |
| 9 | `test_probe_empty_catalogue_is_refused_not_zero` | strip every catalogue row | `SourceError: no pack tables` | empty-source → silent 0=0 |
| 10 | `test_probe_remove_the_only_evidence_file_is_red` | delete the single test module executing an operation | `evidence_drift`, `maturity_drift`, `proof_drift` | **L3 without executable evidence** after evidence vanishes |
| 11 | `test_probe_fabricated_proof_claim_is_red` | stored claims `proof=PROVEN` with no evidence | `proof PROVEN without executable evidence` | **registry without executable evidence** |
| 12 | `test_probe_missing_marked_implemented_is_red` | `portrait.smooth_skin` flipped to registry `PRESENT`, L1 | `missing and implemented`, `pipeline_drift` | **missing silently marked implemented** |
| 13 | `test_probe_l4_without_artifact_proof_is_red` | any L1 node rewritten to L4 | `L4 emitted` | **L4 without artifact proof** |
| 14 | `test_probe_l2_for_a_catalogue_only_operation_is_red` | `scene.replace_sky` rewritten to L2 | `without a surface-reachable entrypoint` | reachable-without-source claim |
| 15 | `test_probe_hidden_runtime_only_operation_is_red` | drop `system.undo` from `runtime_only` | `runtime_only` membership drift | **runtime-only operation hidden** |
| 16 | `test_probe_duplicated_projection_id_is_red` | append a duplicate node | `duplicated operation ids` | duplicated ID inside the artifact |
| 17 | `test_probe_ladder_rewrite_is_red` | rewrite ladder `L3` in place | `ladder_drift` / ladder invariant | silent maturity-vocabulary redefinition |
| 18 | `test_probe_stale_maturity_vocabulary_is_red` | falsify the ladder fingerprints | `ladder_fingerprint_drift` / `ladder_vocabulary_changed` | **stale matrix** of definitions |
| 19–22 | `test_probe_any_generated_doc_edit_is_red[×4]` | hand-edit a count (or any byte) in each generated page | `doc-drift` | **docs count drift** on Matrix / L0-L4 / Reconciliation / Wave Plan |
| 23 | `test_probe_proof_registry_tamper_is_refused` | corrupt `OPERATION_PROOF_REGISTRY.json` | `SourceError: … JSON` | forged proof registry |

## Complementary static guard

`tests/architecture/test_operation_truth_gate.py::test_engine_source_carries_no_hardcoded_counts`
parses every integer literal in `src/nexus_ai_agent/nagar/*.py` (≥ 10, with two
documented semantic exemptions: the git timeout and the digest hex width) and fails
if any equals a *freshly computed* count. Probes 1–2 prove the counts move with the
sources; this scan proves the engine never froze them.

## Honest-scope notes (recorded, not hidden)

* Probes 1–5 mutate **live code** via `monkeypatch` and restore via
  `monkeypatch.undo()` inside the test; file probes rewrite the sandbox copy and
  restore from the original bytes. The final `_assert_green` in each probe is the
  RESTORE→GREEN half — it fails if any mutation leaked.
* `production_like` and the human `owner` field are deliberate **exemptions**:
  no source measures them, so the engine records `NOT_AVAILABLE` and no mutation
  can fabricate them (probe 13 additionally proves L4 stays unreachable).
* The truth engine's own test files are excluded from the suite-execution probe
  (they observe the engine; they do not evidence operations), otherwise citing an
  id while mutating a projection would masquerade as proof.

## Reproduce

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m nexus_ai_agent.nagar --check          # TRUTH GATE: GREEN
pytest tests/unit/test_operation_truth_mutations.py -v   # 23 passed
pytest tests/architecture/test_operation_truth_gate.py tests/unit/test_operation_truth_sources.py -q  # 35 passed
```
