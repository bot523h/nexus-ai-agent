# Character Intelligence / Character Capsule (NAGAR)

Independent NAGAR mission **B**: a versioned character capsule, an independent
continuity verifier, and a measured visual-continuity benchmark — deliberately
outside Arena 1's composition runtime.

| status legend | meaning |
|---|---|
| **VERIFIED** | implemented and demonstrated by an executed command in this workspace |
| **VERIFIED_WITH_LIMITATIONS** | implemented, demonstrated, with a named limitation |
| **EXPERIMENTAL** | implemented but not trustworthy enough to gate anything |
| **MOCK_ONLY** | no real implementation exists |
| **BLOCKED** | cannot proceed in this environment (stated why) |
| **DEFERRED** | out of scope for this mission, by decision |

## What exists right now

| # | artifact | status |
|---|---|---|
| 1 | [CHARACTER_CAPSULE_LIVE_TRUTH.md](CHARACTER_CAPSULE_LIVE_TRUTH.md) | VERIFIED (this workspace, 2026-11-09) |
| 2 | [CHARACTER_INTELLIGENCE_RESEARCH.md](CHARACTER_INTELLIGENCE_RESEARCH.md) | VERIFIED (sourced, with citation reach noted) |
| 3 | [CHARACTER_CAPSULE_ARCHITECTURE.md](CHARACTER_CAPSULE_ARCHITECTURE.md) | VERIFIED |
| 4 | [adr/](adr/) — ADR-0001 … ADR-0005 | VERIFIED |
| 5 | [CAPSULE_SCHEMA.md](CAPSULE_SCHEMA.md) — schema `character-capsule/1` | VERIFIED |
| 6 | [OWNERSHIP_AND_REVISION_MODEL.md](OWNERSHIP_AND_REVISION_MODEL.md) | VERIFIED |
| 7 | [THREAT_MODEL.md](THREAT_MODEL.md) | VERIFIED (controls implemented except where marked) |
| 8 | [PRIVACY_AND_CONSENT.md](PRIVACY_AND_CONSENT.md) | VERIFIED (synthetic only) |
| 9 | [LICENSE_AND_MODEL_WEIGHT_MATRIX.md](LICENSE_AND_MODEL_WEIGHT_MATRIX.md) | VERIFIED (licence facts read from primary sources) |
| 10 | [TOOL_AND_MODEL_COMPARISON.md](TOOL_AND_MODEL_COMPARISON.md) | VERIFIED (comparison; no vendor benchmark re-run) |
| 11 | [VISUAL_CONTINUITY_EVALUATION_DESIGN.md](VISUAL_CONTINUITY_EVALUATION_DESIGN.md) | VERIFIED |
| 12 | [BENCHMARK_DATASET_SPEC.md](BENCHMARK_DATASET_SPEC.md) | VERIFIED |
| 13 | prototype source — `experiments/character_capsule/` (16 modules) | VERIFIED (executed) |
| 14 | automated tests — `tests/character_capsule/` (58 tests) | VERIFIED (executed: 58 passed) |
| 15 | [BENCHMARK_RESULTS.md](BENCHMARK_RESULTS.md) | VERIFIED (numbers from a real run) |
| 16 | [EVIDENCE_BUNDLE.md](EVIDENCE_BUNDLE.md) | VERIFIED (bundle written; signature **UNSIGNED** here) |
| 17 | [INTEGRATION_CONTRACT_PROPOSAL.md](INTEGRATION_CONTRACT_PROPOSAL.md) | proposal only |
| 18 | [ARENA1_COMPATIBILITY_REPORT.md](ARENA1_COMPATIBILITY_REPORT.md) | VERIFIED (zero file overlap) |
| 19 | [COST_AND_HARDWARE_ANALYSIS.md](COST_AND_HARDWARE_ANALYSIS.md) | VERIFIED locally, GPU **NOT_MEASURED** |
| 20 | [ROADMAP_NOW_NEXT_LATER.md](ROADMAP_NOW_NEXT_LATER.md) | VERIFIED (plan) |
| ★ | [FINAL_REPORT_FA.md](FINAL_REPORT_FA.md) — گزارش نهایی فارسی | VERIFIED |

## Reproduce (2 vCPU, ~3 GB, no GPU)

```bash
python -m venv .venv && . .venv/bin/activate
pip install numpy pillow pynacl pytest ruff && pip install -e . --no-deps
PYTHONPATH=. python -m experiments.character_capsule.benchmark \
    --work-dir experiments/character_capsule/.work
PYTHONPATH=. python -m pytest tests/character_capsule --noconftest -q   # 58 passed
```

The generated run artifacts live under
`experiments/character_capsule/.work/results/` (corpus, calibration,
continuity reports, benchmark results, evidence bundle) and are **not** committed:
`.work/` is a regenerated corpus, not source.

## Boundary

Nothing in this mission touches Arena 1's composition runtime, packs, rendering,
job queue or `nagar/ibpc/`. See [ARENA1_COMPATIBILITY_REPORT.md](ARENA1_COMPATIBILITY_REPORT.md)
for the mechanical check.
