# NAGAR Platform Stabilization and Merge Ledger

**Branch:** `arena/ad22cc3c-nexus-ai-agent`
**Baseline:** `c3aa7c85e4b64400cb8d51b0880ef5e6cafd4e1b`
**Release:** `3.13.0`

## Exact-branch delivery record

This ledger records changes actually present on this branch. A PR head or source commit being fetched is not evidence that the complete PR was merged, wired, or production-verified. At the time of writing, the branch ends at the Stage 5 commit identified in the delivery report; the Stage 6 ledger is a documentation artifact, not a claim that every requested stage is complete.

| PR | Audited source SHA | Disposition on this branch |
|---|---|---|
| #335 | `fc18b14b8142249fb99c09f1d9833f56c4836656` | Integrated through `8ed5927c`: database restore/recovery, readiness and redaction hardening. Verification included Ruff, mypy on `src`, and 441 focused + architecture tests. |
| #336 | `c174523f7b897aee38a2b8c205c5684139f81709` | Integrated through `2b929b8c`: execution staging/fencing controls. Verification included Ruff, mypy on `src`, and 444 focused + architecture tests. |
| #334 | `99bd2f6bcc7d37634b3480b8da76181e2f6ad70b` | Connect foundation, encrypted credential storage, migration, tests, and docs were integrated in `1dde6906`. The concrete SQLAlchemy persistence adapter was moved to `storage/connect_persistence.py`; `connect/persistence.py` now exposes a storage-independent port/model. `StrEnum` was replaced with Python 3.10-compatible `str, Enum`; Connect docs were indexed. Connect and architecture suites passed. |
| #333 | `ec64519f3af8666ebf00c68621cc54a9535b44cc` | RFC 8785 API/tests and pinned `rfc8785==0.1.4` were integrated in `1dde6906`. `canonical_digest` makes `legacy-json-v1` the default and JCS opt-in. Existing provenance serializers and record hash call sites were not migrated. |
| #331 | `4284876b00c8d54c7f1f3ccac0c6250ca0745897` | Truth-gate parity test and `make truth-check` target were added in `1dde6906`. This is a local check target; it does not claim a pushed Git hook is automatically installed. |
| #330 | `5a281ffbde475e637e84f0f923396a78c0bef989` | Delta, Shadow, and Signature primitives/tests were extracted in `82bd7428`; `OPERATION_TRUTH.json` was regenerated. These are **not fully wired into the canonical service approval/dispatch boundary**. The visual `ScopeSignature` primitive is not a substitute for signing the complete approved execution scope. End-to-end production lifecycle closure remains outstanding. |
| #328 | `4f6ca1d1f896a86b682dc235754fd04e0eae44d0` | Audited, not integrated. The Studio web/API scaffolding and the five deferred production contracts were not completed by this stabilization pass. |

## Other PR dispositions

- **#326 — `RESEARCH_ONLY`:** do not promote the procedural character-capsule prototype (reported EER 0.325) to production identity authority. A research result is not a production identity contract; require independent quality, determinism, and adversarial evidence before reconsideration. The requested claim that a specific competing Chronos implementation supersedes it is not asserted here because that parallel branch was not merged or verified on this branch.
- **#327 — pending convergence review:** do not merge a stale-base composition runtime without rebasing and validating its authority and file-overlap boundaries against the current composition implementation. No merge of Agent OpenHands' composition branch is claimed by this ledger.

## Phase-2 merge sequence (proposed; not executed here)

The four creative-engine branches are isolated and were not fetched/verified as part of this stabilization delivery. Therefore this is a safe-order proposal, not a guarantee of zero conflicts:

1. Freeze and record exact SHAs for the stabilization branch and all four creative branches; ensure each branch is based on the same current `main`.
2. Rebase each creative branch onto the selected integration base independently. Preserve the declared ownership fences: audio (Alpha), Chronos (Beta), episodic causality/director/composition/scene/portrait (OpenHands), and vault/speech/color (Claude/Manus).
3. Run each branch's isolated checks and confirm its changed paths do not cross another agent's ownership fence.
4. Integrate the creative branches one at a time in this order: Beta, OpenHands, Alpha, Claude/Manus. Resolve only cross-zone/shared-document conflicts with the owning agent; rerun all impacted tests after each integration.
5. Integrate this stabilization branch after its outstanding IBPC service wiring and Studio API contracts are closed and gates pass. Re-run full architecture, unit, integration, lint, type, and deployment checks on the final candidate SHA.
6. Open/review the final PR, verify required CI on the exact head SHA, then merge to `main` only after explicit maintainer approval. Do not infer mergeability or conflict-free status from this proposal.

## Verification record and outstanding work

Stage 1 and Stage 2 gates passed as recorded above. The extracted IBPC primitives passed 548 tests including the architecture suite after regenerating the operation-truth projection. Documentation/version synchronization passed 389 architecture tests and the configured lint/type checks.

Remaining: full IBPC canonical service wiring and tamper-refusal E2E; the five Studio API contracts and API security tests; final all-suite coverage matrix; verification and integration of the four creative branches; and a verified Phase-2 merge. No statement here certifies those items as complete.
