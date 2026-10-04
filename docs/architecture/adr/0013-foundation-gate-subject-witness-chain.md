---
status: accepted
date: 2026-10-04
deciders: arena/01a103a6-nexus-ai-agent (foundation master integrator)
consulted: AGENTS.md §2, ADR-0006, ADR-0007, ADR-0012
informed: governance lane, security lane, creative-studio lane, ci-quality lane
---

# 0013. Foundation gate verification uses a SUBJECT_SHA vs WITNESS_SHA hash-chained receipt protocol

## Context and Problem Statement

When a verification receipt is written inside the same commit it claims to verify,
the commit SHA changes the moment the receipt file is staged (`git commit --amend`).
Any static record of `git_sha` inside a self-referential commit either records a
stale pre-amend SHA or omits the commit hash altogether, making offline audit of
which tree state actually ran the verification suite impossible.

At the same time, foundation-wide invariants (`SECURITY_GATE`, `CONVERGENCE_GATE`,
`DURABILITY_GATE`, `PROVENANCE_GATE`, `RECOVERY_GATE`, `L4_GATE`, `GOVERNANCE_GATE`)
must be computed from real command execution and structured test/mutation outputs
rather than hand-typed prose tables.

## Decision Drivers

- **No self-referential SHA paradox.** A receipt must name the exact immutable
  commit (`subject_sha`) whose tree was tested, while the commit that stores the
  receipt (`witness_sha`) is the child commit on the branch.
- **Tamper-evident phase chain.** Each phase receipt (`phase0` through `phase9`)
  must record `previous_receipt_digest` as the SHA-256 of the canonical JSON of
  the immediately preceding receipt.
- **Mechanical gate computation.** `scripts/foundation_gate.py` computes the
  phase gates and the 33-boolean `foundation_gate` acceptance matrix directly
  from repository state, receipt chain integrity, and test/mutation execution.

## Considered Options

1. **Single commit per phase with `HEAD~1` or placeholder SHA** inside the receipt.
2. **Two-commit `SUBJECT_SHA` -> `WITNESS_SHA` protocol** per phase, verified by
   `scripts/foundation_gate.py`.

## Decision Outcome

Chosen option: **2**. Every phase boundary commits the implementation and tests
first (`SUBJECT_SHA`), runs the verification commands against that exact tree,
writes `docs/audits/receipts/phaseN-*.receipt.json` binding `subject_sha` and
`previous_receipt_digest`, and commits the receipt in a dedicated child commit
(`WITNESS_SHA`).

### Consequences

- (+) `git rev-parse WITNESS_SHA^` equals `SUBJECT_SHA` for every phase receipt
  after Phase 0, and `git diff SUBJECT_SHA WITNESS_SHA` touches only the receipt
  artifact.
- (+) `scripts/foundation_gate.py` can verify the entire receipt chain offline
  and recompute all 33 foundation booleans deterministically.
- (−) Each phase produces two commits (`subject` + `witness`) instead of one.
