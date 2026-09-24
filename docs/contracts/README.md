# Nagar Contracts — Gate 2

> **Owner:** Agent 2  
> **Status:** Canonical source of truth for Operation ↔ Capability ↔ Command boundary  
> **Baseline:** `035a896dd2ed1293de6accf2ef4309da2fd64c89` (main)  
> **Evidence date:** 2026-09-24

This directory is the **contract-first reconciliation** deliverable for Gate 2.

## Files

| File | Purpose |
|---|---|
| [OPERATION_CONTRACT_MATRIX.md](OPERATION_CONTRACT_MATRIX.md) | Human-readable matrix: 70 product catalog vs 57 runtime reality |
| [OPERATION_MATRIX.json](OPERATION_MATRIX.json) | Machine-readable matrix (for tests) |
| [RECONCILIATION.md](RECONCILIATION.md) | 70 ↔ 57 explicit reconciliation tables |
| [CAPABILITY_CONTRACT.md](CAPABILITY_CONTRACT.md) | Capability Contract — what runtime can execute |
| [COMMAND_ENVELOPE.md](COMMAND_ENVELOPE.md) | Typed Command Envelope — versioned, validated, authorized |
| [L0_L4_MATURITY.md](L0_L4_MATURITY.md) | L0–L4 maturity model with evidence requirements |

## Product Rule

* **70 operations** = contracted product catalog (TDD), not number of buttons, not registry entries, not executors.
* **57 operations** = live `build_runtime_registry()` reality.
* **10 extra** = wave1 (media.play, media.pause, timeline.mark, system.undo) + slideshow (6) — outside 70 but in runtime.
* **23 missing** = portrait 10 + scene 10 + color 3 (white_balance, hdr_tonemap, deband_denoise).

Formula: `70 - 20 - 3 + 10 = 57`

## Verification

```bash
PYTHONPATH=src python -m nexus_ai_agent.creative.contracts.validation
pytest -q tests/unit/test_contract_matrix.py
pytest -q tests/architecture/test_contract_boundaries.py
```

## Ownership Fence

* **Owned:** `docs/contracts/`, `src/nexus_ai_agent/creative/contracts/`, contract tests
* **Read-only:** `src/nexus_ai_agent/creative/studio/`, `src/nexus_ai_agent/creative/packs/`, `docs/NAGAR_70_OPERATIONS_TDD.md`
* **Untouched:** `src/nexus_ai_agent/creative/rendering/`, `src/nexus_ai_agent/creative/slideshow/` (Agent 1), `docs/audits/NAGAR_70_OPERATIONS_RESEARCH_2026-09-24.md` (Agent 3, does not exist on main — treated as input only)

## Evidence Model

Every claim carries:

* **Claim** — what is asserted
* **Source** — file, commit, or runtime probe
* **Evidence Type** — VERIFIED / OBSERVED / SUPPORTED / INFERRED / HYPOTHESIS / NOT_VERIFIED
* **Limitation** — what is not proven

Zero hallucinated completeness:

* "implemented because registered" — FORBIDDEN
* "production-ready because reducer exists" — FORBIDDEN
* "AI-ready because schema exists" — FORBIDDEN
* "end-to-end because one unit test passes" — FORBIDDEN

Distinction is explicit: Defined → Registered → Reduced → Executed → Surfaced → Proven
