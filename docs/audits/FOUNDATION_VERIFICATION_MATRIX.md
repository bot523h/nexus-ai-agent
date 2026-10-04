# Nagar / Nexus V1 Foundation Verification Matrix (2026-10-04)

**Status:**Dated Audit Record (Phase 9 — Documentation & Truth Alignment)
**Branch:** `arena/01a103a6-nexus-ai-agent`
**Live `origin/main` Baseline:** `e5b326b2eaf691a638d030ad57acf1ce60016ef0` (`v3.13.0`)
**Canonical Verifier:** [`scripts/foundation_gate.py`](../../scripts/foundation_gate.py)

---

## 1. Canonical Execution Backbone & 15 Authority Domains

The V1 foundation enforces a single non-bypassable execution backbone:

`Principal -> Project -> Intent -> Plan / Creative IR -> Authority / Policy -> Typed Command -> Transaction -> Project Revision -> Job -> Attempt / Lease / Fencing -> Artifact -> Independent Verification -> Receipt -> Lineage`

Every responsibility has **one** canonical authority, enforced mechanically by `tests/architecture/test_authority_convergence_matrix.py`:

| # | Responsibility | Canonical Authority | Enforcing Guard / Test |
|---|---|---|---|
| 1 | Creative IR / Semantic Graph (`nagar.creative-ir.v1`) | `nexus_ai_agent.creative.ir.models:CreativeGraph` | `tests/unit/test_creative_ir.py`, `tests/unit/test_creative_ir_determinism.py` |
| 2 | Intent-to-Plan Compilation & Execution Spine | `nexus_ai_agent.creative.spine.compiler:IntentCompiler` | `tests/unit/test_creative_spine.py`, `tests/unit/test_spine_mutations.py` |
| 3 | Multi-Step Atomic Plan Transaction | `nexus_ai_agent.creative.studio.plan_transaction:PlanTransactionExecutor` | `tests/unit/test_plan_transaction.py`, `tests/unit/test_plan_transaction_matrix.py` |
| 4 | Command Dispatch & Fail-Closed Authorization (`STOP-C`) | `nexus_ai_agent.creative.studio.bus:CommandBus` | `tests/architecture/test_command_capability_boundary.py`, `scripts/security_mutations_stop_c.py` |
| 5 | Capability Registry & Pack Trust Root (`STOP-D`) | `nexus_ai_agent.creative.packs.registry:PackRegistry` + `trust.py` | `tests/architecture/test_pack_trust_boundary.py`, `tests/unit/test_pack_trust_root.py` |
| 6 | Undo / Redo Conflict Algebra | `nexus_ai_agent.creative.studio.undo_conflict:evaluate_undo_conflict` | `tests/unit/test_plan_transaction_undo_contract.py`, `tests/unit/test_undo_contract_mutations.py` |
| 7 | Timeline & Rational Temporal Truth (`Timebase`) | `nexus_ai_agent.creative.temporal.core:FrameRateResolver` | `tests/unit/test_temporal_algebra.py`, `tests/unit/test_temporal_exactness.py`, `scripts/temporal_mutations.py` |
| 8 | Studio Read Surface (`ProjectOverview` / `TimelineInspection`) | `nexus_ai_agent.creative.studio.read_api:StudioReadFacade` | `tests/unit/test_studio_read_api.py` |
| 9 | Durable Studio Persistence, Leases & Fencing | `nexus_ai_agent.creative.studio.persistence:DurableStudioStore` | `tests/unit/test_durable_studio_store.py`, `tests/unit/test_crash_recovery_matrix.py` |
| 10 | Artifact Passports & Independent Media Verification | `nexus_ai_agent.creative.studio.passport:ArtifactPassport` | `tests/unit/test_artifact_passport.py` |
| 11 | Causal Provenance Chain & Execution Trace | `nexus_ai_agent.creative.studio.passport:ProvenanceCausalChain` | `tests/unit/test_execution_trace_causality.py`, `scripts/execution_trace_mutations.py` |
| 12 | Render Lane (`RenderIR -> filtergraph -> FFmpeg`) | `nexus_ai_agent.creative.rendering.executor:render_plan_to_video` | `tests/architecture/test_rendering_lane_boundary.py`, `tests/unit/test_creative_render_jobs.py` |
| 13 | Restricted Shell Flag Grammar (`STOP-A`) | `nexus_ai_agent.tools.system_shell:ShellTool` | `tests/unit/test_shell_sandbox.py`, `scripts/shell_sandbox_mutations.py` |
| 14 | Outbound Download SSRF / Redirect / Rebinding Guard (`STOP-B`) | `nexus_ai_agent.api.app:_download_url_bytes` | `tests/unit/test_api_ssrf_download.py`, `scripts/security_mutations_stop_b.py` |
| 15 | Storage Remote-Key Containment | `nexus_ai_agent.storage.providers.local_cache:LocalCacheProvider` | `tests/architecture/test_storage_key_boundary.py`, `scripts/security_mutations_remote_key.py` |

---

## 2. Phase Receipt Chain (`SUBJECT_SHA -> WITNESS_SHA`)

Per [ADR 0013](../architecture/adr/0013-foundation-gate-subject-witness-chain.md), every phase commits its implementation and tests first (`SUBJECT_SHA`) and commits its hash-chained receipt (`previous_receipt_digest`) in a dedicated child commit (`WITNESS_SHA`):

| Phase | Receipt File | `SUBJECT_SHA` | `WITNESS_SHA` | Gate |
|---|---|---|---|---|
| Phase 0 — Live Truth Freeze | [`receipts/phase0-truth-freeze.receipt.json`](receipts/phase0-truth-freeze.receipt.json) | `e5b326b2eaf691a638d030ad57acf1ce60016ef0` | `8d900bad6b6b4b4f84a700c11097fc87e3a2c1b3` | `PHASE_0 = PASS` |
| Phase 1 — Security Closure | [`receipts/phase1-security.receipt.json`](receipts/phase1-security.receipt.json) | `08438f3f458eff197c5c1f3f1795b331281f5fe6` | `18768cece9546b8acd2bfc33873250313558cff3` | `SECURITY_GATE = PASS` |
| Phase 2 — PR & Branch Convergence | [`receipts/phase2-pr-convergence.receipt.json`](receipts/phase2-pr-convergence.receipt.json) | `5706e6844bd0a26fa08b64fd275c438c3625bcea` | `15c99776d37aa6f6be45d17e7a5ca8fa8c9226f0` | `PHASE_2 = PASS` |
| Phase 3 — Canonical Architecture | [`receipts/phase3-architecture.receipt.json`](receipts/phase3-architecture.receipt.json) | `00749b4862e746270083085c1f5e4ef6b7e11d8f` | `bd495698dec29d931bbc4e455c70c5ad1daccfef` | `CONVERGENCE_GATE = PASS` |
| Phase 4 — Durability, Leases & Fencing | [`receipts/phase4-durability.receipt.json`](receipts/phase4-durability.receipt.json) | `ac67cf42a8dc03e076be24454b1bc86c83a5d159` | `77f5c201594b750744731d0c70d66c54e19f83cb` | `DURABILITY_GATE = PASS` |
| Phase 5 — Passports & Provenance | [`receipts/phase5-provenance.receipt.json`](receipts/phase5-provenance.receipt.json) | `cacc3d4dc3f8125e12c36c4ccd4d3bc88826ee54` | `8fb4c19039fd655a8ddadfcc62373d76bf87ba54` | `PROVENANCE_GATE = PASS` |
| Phase 6 — Crash / Recovery Matrix | [`receipts/phase6-recovery.receipt.json`](receipts/phase6-recovery.receipt.json) | `6187a46a562b31de93076f98aef2343d02198139` | `87b9a0a48a434132410e72f8d4d703bc1a21bb9d` | `RECOVERY_GATE = PASS` |
| Phase 7 — L4 & Temporal Truth | [`receipts/phase7-l4.receipt.json`](receipts/phase7-l4.receipt.json) | `247876dd397fac0562e7940da0778487ee67dfa5` | `5cc4671457d42100744065c71bf57d906dae1487` | `L4_GATE = PASS` |
| Phase 8 — Board, Referee & Governance | [`receipts/phase8-governance.receipt.json`](receipts/phase8-governance.receipt.json) | `2d68c7cbd2ff3ecf2628a18d1806521dd5258110` | `321c612f2661ba60ca4d84e190f47f14aadbe9b5` | `GOVERNANCE_GATE = PASS` |
| Phase 9 — Documentation & Truth Alignment | `receipts/phase9-docs.receipt.json` | Recorded in Phase 9 receipt | Recorded in Phase 9 witness commit | `PHASE_9 = PASS` |

Full open-PR classification across all 67 open PRs (`#33`–`#149`) is recorded in [`PR_CONVERGENCE_2026-10-03.md`](PR_CONVERGENCE_2026-10-03.md).

---

## 3. Reproducing the Machine Gate

```bash
PYTHONPATH=src .venv/bin/python scripts/foundation_gate.py --require-phase9 --json
```
