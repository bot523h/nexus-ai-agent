# NEXUS v3.13 Platform Stabilization Ledger

**Branch:** `arena/ad22cc3c-nexus-ai-agent`
**Baseline:** `c3aa7c85e4b64400cb8d51b0880ef5e6cafd4e1b`
**Latest implementation commit before this empirical rehearsal ledger update:** `4dfb2b8f`

## Delivery commits

| Commit | Delivered and checked |
|---|---|
| `8ed5927c` | PR #335 database recovery/readiness integration; 441 focused plus architecture tests passed at that checkpoint. |
| `2b929b8c` | PR #336 execution staging/fencing integration; 444 focused plus architecture tests passed at that checkpoint. |
| `1dde6906` | PR #334 Connect foundation and adapter relocation; JCS API and PR #331 truth-check target. |
| `82bd7428` | PR #330 Delta/Shadow/Signature primitives and operation-truth projection update. |
| `525869cc` | Version/documentation synchronization. |
| `88bf7141` | Initial truthful stabilization ledger. |
| `3e5922eb` | Connect SQLModel deprecation cleanup and explicit dual-scheme digest verifier. Four Connect persistence tests passed with no SQLModel warning; canonicalization, JCS, and architecture suites passed (443 tests in the combined run). |
| `e565669e` | Canonical IBPC service scope digest minted at approval and compared before dispatch; tamper refusal integration test. IBPC + architecture run passed (573 tests). |
| `4dfb2b8f` | PR #328 same-origin PWA shell, package assets, authenticated browser session, and updated read boundary. PWA/session/inventory + architecture run passed (437 tests). **The five requested backend contracts are not implemented.** |

The above are checkpoint test counts, not a claim that the whole repository suite was run at each commit. The current architecture count has evolved from the original 367-test baseline as tests were added.

## PR disposition

- **#335/#336:** integrated as recorded in the first two commits.
- **#334:** Connect persistence SQL is placed in the storage adapter; the Connect package retains its port/model boundary. Python 3.10 enum compatibility and docs indexing were repaired.
- **#333:** JCS stays additive. `CanonicalizationScheme` and `verify_canonical_digest` support strict selected-scheme verification and explicit migration fallback. Historical ledger/passport call sites remain on the legacy serializer.
- **#331:** `make truth-check` is green on the current tree.
- **#330:** scope-digest verification is now enforced at the canonical `nagar/ibpc/service.py` approval/dispatch boundary. Delta and Shadow capabilities are represented by the existing canonical parser clarification and preview phases, but the newly extracted specialized Delta/Shadow visual primitives are not yet called by the service. Full requested intent-to-passport E2E coverage remains incomplete.
- **#328:** PWA static shell and authenticated cookie session are integrated. `GET /me`, scoped jobs, intent/approval submission, AssetLibrary endpoints, and passport-guarded artifact downloads remain outstanding; do not describe this PR as fully completed.

## Empirical non-destructive creative-branch merge rehearsal

The branches were fetched and rehearsed with `git merge-tree --write-tree`; the synthetic merge commits were created only as unreachable objects to model sequential trees. No creative branch was merged into this working branch. Conflict status is based on a mechanical merge from the stabilization head, carrying forward the clean Beta tree when available. The board and evidence projections are expected coordination hotspots.

| Order | Exact remote head tested | Changed-path overlap | Result |
|---|---|---|---|
| OMEGA `arena/288ef95e-nexus-ai-agent` | `fba5e0e0d3ccc37c09cfeefc50cc141db17885bb` | `.agents/board.json`, `OPERATION_TRUTH.json`, `docs/README.md` | `CONFLICT`: `.agents/board.json`, `OPERATION_TRUTH.json`; docs index auto-merged. Recompute truth and reconcile index/board after integration. |
| CHRONOS `arena/dc164076-nexus-ai-agent` | `906cb9ec21476bfc730718abeaae97c72271dd9e` | `docs/README.md` | `CLEAN_AUTO_MERGE`; documentation index requires semantic review. |
| ATLAS/AETHEL `arena/atlas-episodic-causality-lab` | `dab3b2f9ebe9a65215e651a755f9f027c1409b5c` | `.agents/board.json`, `.openhands/memory/2026-11-12.md`, `.openhands/memory/MEMORY.md` | `CONFLICT`: board plus add/add `2026-11-12.md`; memory needs owner-led reconciliation. |
| HYPERION `arena/hyperion-vault-speech-color` | `c285c60de1b16a306b38167e721bfd563246b3c0` | `.agents/board.json` | `CONFLICT`: board only; reconcile coordination metadata, not creative implementation files. |

These results prove that the branches are **not** conflict-free as-is. The stated non-invasion rule is maintained: this branch does not change files under `src/nexus_ai_agent/creative/` or `src/nexus_ai_agent/audio_engine/`. This merge rehearsal does not alter that rule and is not an integration/CI test of the creative implementations.

## Current verification and remaining closure

Latest targeted checks after Milestone 3: `ruff check`, `ruff format --check`, `mypy src`, and `make truth-check` passed. Studio/PWA plus architecture passed 437 tests. A full repository run with coverage was attempted: **6,893 passed, 50 skipped, 11 failed**. The failures include migration-head expectation/chain issues and CI/environment-sensitive tests; see the exact test output on the run for the failure names. Coverage across the selected platform packages was **93% overall** (3,443 statements, 248 missed); individual new modules were below 95% (for example canonicalization 82%, SQL Connect adapter 85%, Connect crypto 85%, IBPC service 89%). The full-suite gate and requested per-module 95% gate therefore **failed**. The full run reported 1,325 warnings across the entire repository; the Connect persistence focused run now has zero SQLModel deprecation warnings. External deployment/PostgreSQL integration was not certified.

Remaining production closure: use the specialized Delta/Shadow algorithms in canonical service flow if they are requirements beyond existing parsing/preview; complete their end-to-end and scope-transplant adversarial tests; implement and security-test all five Studio backend contracts; run the complete suite and coverage matrix; then re-run the merge rehearsal after the documented board/projection/index conflicts are addressed. No `VERIFIED_COMPLETE` claim is made for those gaps.
