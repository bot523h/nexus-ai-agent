# Documentation Map

Single index for everything under `docs/`. If a document is not listed here it either lives in
`docs/history/` (archived, superseded) or it is a defect — `tests/unit/test_docs_integrity.py`
fails on an unindexed file, a broken relative link, an unbalanced Mermaid fence, or leftover
placeholder text.

**Front door:** [`architecture.md`](architecture.md). **How this map works:**
[ADR 0001](architecture/adr/0001-docs-as-code-layout.md) (layout) ·
[ADR 0002](architecture/adr/0002-docs-as-code-enforcement.md) (what is enforced, and what waits).

---

## Architecture (living views — updated in place)

| Document | What it is |
|---|---|
| [architecture.md](architecture.md) | Front door: the system in one picture + the reading order. |
| [architecture/OVERVIEW.md](architecture/OVERVIEW.md) | C4 context & container views, stakeholders, the five quality attributes, frozen constraints, deployment topologies. |
| [architecture/OVERVIEW.fa.md](architecture/OVERVIEW.fa.md) | خلاصهٔ فارسی و نقشهٔ مسیر مطالعه (navigation summary; English wins on conflict). |
| [architecture/MODULE_MAP.md](architecture/MODULE_MAP.md) | Layer diagram, package inventory, boundary laws → the tests that enforce them, extension recipes. |
| [architecture/RUNTIME_FLOWS.md](architecture/RUNTIME_FLOWS.md) | Message, slideshow, render-lane, checkpoint, migration, and webhook flows — each with its failure contract. |
| [architecture/CREATIVE_STUDIO.md](architecture/CREATIVE_STUDIO.md) | Nagar: capability model, permission ladder, packs, activation gap, TDD coverage ledger, render lane. |
| [architecture/CREATIVE_DIRECTION.md](architecture/CREATIVE_DIRECTION.md) | Nagar product direction: the intent-first backbone loop, the first vertical slice (single-project intent loop), deferred capabilities and their triggers. |
| [architecture/COMMAND_CAPABILITY_CONTRACT.md](architecture/COMMAND_CAPABILITY_CONTRACT.md) | Nagar Gate 2: canonical command envelope, authorization/capability/policy/reference pipeline, idempotency, revision, and evidence limits. |
| [architecture/PROVENANCE_LEDGER.md](architecture/PROVENANCE_LEDGER.md) | The causal ledger (Project Graph journal): tamper-evident recording of every durable job transition, and the Artifact Passport — the read-only, fail-closed projection that reconciles chain vs. authority and re-measures artifacts. |
| [architecture/JOB_LIFECYCLE.md](architecture/JOB_LIFECYCLE.md) | Canonical job lifecycle: chain, state machine with owners+invariants, side-effect boundary, artifact verification contract, retry/failure semantics, Result chain. |
| [architecture/DATA_AND_STORAGE.md](architecture/DATA_AND_STORAGE.md) | Every store, ownership, Alembic chain, retention, portability, and what never enters a store. |
| [architecture/SECURITY.md](architecture/SECURITY.md) | Trust boundaries, STRIDE threat → control → evidence table, P0 audit follow-through, review checklist. |
| [architecture/OBSERVABILITY.md](architecture/OBSERVABILITY.md) | Structured events, metric policy, health semantics, inspection commands, the short alert list. |
| [architecture/TESTING.md](architecture/TESTING.md) | Test taxonomy, the four gates, determinism rules, baseline numbers, honest gaps. |
| [architecture/PORTS.md](architecture/PORTS.md) | The six hexagonal ports, their invariants and their signature tests. |
| [architecture/REFERENCES.md](architecture/REFERENCES.md) | The external standards followed (C4, arc42, MADR 4.0, fitness functions, docs-as-code, STRIDE) and what was deliberately not adopted. |
| [architecture/DATA_LIFECYCLE.md](architecture/DATA_LIFECYCLE.md) | Data lifecycle contract: stages, retention, checkpoint schemas, the forbidden operation journal. |
| [architecture/RETENTION_DECISION.md](architecture/RETENTION_DECISION.md) | Retention policy decision record (30-day resumability window, circuit breaker). |
| [architecture/LLM_PROVIDERS.md](architecture/LLM_PROVIDERS.md) | Provider chain, cooldowns, anti-retry-storm rule, strict-privacy flag. |
| [architecture/adr/README.md](architecture/adr/README.md) | Doc-layer decision index + rules. |
| [architecture/adr/template.md](architecture/adr/template.md) | MADR 4.0-lite template (Confirmation section mandatory). |
| [architecture/adr/0001-docs-as-code-layout.md](architecture/adr/0001-docs-as-code-layout.md) | Documentation layout: living views + one index + language policy. |
| [architecture/adr/0002-docs-as-code-enforcement.md](architecture/adr/0002-docs-as-code-enforcement.md) | Enforce documentation in Python now; Node toolchain on a recorded trigger. |
| [architecture/adr/0003-mermaid-flowchart-for-c4-views.md](architecture/adr/0003-mermaid-flowchart-for-c4-views.md) | Express C4 views with Mermaid `flowchart`. |
| [architecture/adr/0004-board-schema-2.md](architecture/adr/0004-board-schema-2.md) | Coordination board schema 2: prerequisites, acceptance criteria, history split. |
| [architecture/adr/0005-canonical-command-capability-contract.md](architecture/adr/0005-canonical-command-capability-contract.md) | Canonical command and capability contract: versioning, location, and reconciliation. |
| [architecture/adr/0006-capability-pack-trust-root.md](architecture/adr/0006-capability-pack-trust-root.md) | Capability packs get an explicit trust root (Ed25519, offline, fail-closed). |
| [architecture/adr/0007-multisource-referee-exit-codes.md](architecture/adr/0007-multisource-referee-exit-codes.md) | The overlap referee reports unverifiable data as exit 2, not as a pass. |
| [architecture/adr/0008-canonical-identity-arbitration.md](architecture/adr/0008-canonical-identity-arbitration.md) | One authority per identity, one durable lineage; dispositions for competing open PRs. |
| [architecture/adr/0011-restricted-shell-flag-grammar.md](architecture/adr/0011-restricted-shell-flag-grammar.md) | The restricted shell validates a declared argument grammar, not a forbidden-option list. |
| [architecture/adr/0012-coordination-ref-board-authority.md](architecture/adr/0012-coordination-ref-board-authority.md) | The claim board lives on the coordination ref `refs/nexus/board` (compare-and-swap writes); `.agents/board.json` is a frozen archive guarded in CI (audit A-04). |
| [architecture/TRUST_CONTROL_PLANE.md](architecture/TRUST_CONTROL_PLANE.md) | Trust/truth domains A–E: interfaces, authorities, non-bypass guards, honest status. |
| [architecture/TRUTH_DOCTOR.md](architecture/TRUTH_DOCTOR.md) | The repo-truth authority (law R16): one pure-stdlib scanner for version lock-step, docs index/link integrity, boundary-law→test resolution, documentation claim witnesses, and truthy-default verdict reads — with its finding codes and honest limits. |

## Overnight (dated engineering record — one-night session)

| Document | What it is |
|---|---|
| [overnight/RECON.md](overnight/RECON.md) | Live read-only reconnaissance of `main` @ `e5b326b` with file:line evidence (what exists, what is absent). |
| [overnight/MODEL_OPTIONAL.md](overnight/MODEL_OPTIONAL.md) | The additive model-optional cognition boundary: contract, invariants, Model Kill Test, honest limits. |
| [overnight/RESEARCH.md](overnight/RESEARCH.md) | Dated deep-research record: each source → finding → decision → Nagar impact. |
| [overnight/COGNITION_CONVERGENCE.md](overnight/COGNITION_CONVERGENCE.md) | One path from model cognition to execution: gateway, registry-derived authority, fail-closed selection, mechanical anti-bypass gates, honest limits. |
| [overnight/FREE_TEXT_SLICE.md](overnight/FREE_TEXT_SLICE.md) | The Gate-C vertical slice: free text → typed proposal → registry → CommandBus → verified `timeline.trim` artifact, with the adversarial matrix and honest limits. |

## Authoritative records

| Document | What it is |
|---|---|
| [DECISION_LOG.md](DECISION_LOG.md) | **The** architecture decision log. When any summary disagrees with it, this file wins. |
| [../ROADMAP_STATUS.md](../ROADMAP_STATUS.md) | Living roadmap status with per-wave commit anchors. |
| [../REQUIREMENTS_LEDGER.md](../REQUIREMENTS_LEDGER.md) | PR1/PR2/PR3 checkpoint-lifecycle requirements ledger. |
| [../AGENTS.md](../AGENTS.md) | Multi-agent coordination contract + current board summary. |
| [NAGAR_70_OPERATIONS_TDD.md](NAGAR_70_OPERATIONS_TDD.md) | Phase 6 (Nagar creative studio) technical design baseline — 71 operation ids. |
| [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) | End-user guide: bring any NEXUS database to the Alembic head. |

## Protocols

| Document | What it is |
|---|---|
| [MULTI_AGENT_PROTOCOL.md](MULTI_AGENT_PROTOCOL.md) | Multi-agent coordination protocol (English). |
| [MULTI_AGENT_PROTOCOL.fa.md](MULTI_AGENT_PROTOCOL.fa.md) | همان پروتکل به فارسی. |

## Operations

| Document | What it is |
|---|---|
| [ops/DEPLOY_RUNBOOK.md](ops/DEPLOY_RUNBOOK.md) | Koyeb deploy runbook: preflight → deploy → smoke → rollback → incidents. |
| [ops/deployment-koyeb.md](ops/deployment-koyeb.md) | Koyeb scale-to-zero deployment guide. |
| [ops/NEON_LIFECYCLE_RUNBOOK.md](ops/NEON_LIFECYCLE_RUNBOOK.md) | Neon/PostgreSQL lifecycle runbook. |
| [ops/r2-storage.md](ops/r2-storage.md) | Cloudflare R2 blob tier: setup + maintenance secrets. |
| [ops/COLOR_LANE.md](ops/COLOR_LANE.md) | Color/exposure lane runbook: EV→filter mapping, hard contracts, FFmpeg-free validation, troubleshooting. |
| [ops/PACK_RUNTIME.md](ops/PACK_RUNTIME.md) | Unified pack runtime (wave 5): composition, activation gate, op-gap ledger, composition checklist for new packs. |
| [ops/RUNBOOK_HARDENING.md](ops/RUNBOOK_HARDENING.md) | Runbook hardening pass (wave-4 step 8): boundary conditions, failure modes, operator knobs. |

## Audits (dated, immutable records)

| Document | What it is |
|---|---|
| [audits/AUDIT_REPORT_2026-09-21.md](audits/AUDIT_REPORT_2026-09-21.md) | Full architecture audit (P0 findings + command honesty matrix). |
| [audits/HANDOFF_ANALYSIS_2026-09-21.md](audits/HANDOFF_ANALYSIS_2026-09-21.md) | Board GC + engineered handoff analysis (agent D session). |
| [audits/REPO_HYGIENE_REPORT_2026-09-21.md](audits/REPO_HYGIENE_REPORT_2026-09-21.md) | Repository hygiene pass: docs reorganisation, release alignment, branch janitorial work. |
| [audits/ARCHITECTURE_DOCS_2026-09-21.md](audits/ARCHITECTURE_DOCS_2026-09-21.md) | Architecture-documentation pass (task-131): what was written, verified, and enforced. |
| [audits/WAVE5_ACTIVATION_2026-09-21.md](audits/WAVE5_ACTIVATION_2026-09-21.md) | Wave-5 activation audit: unified runtime, six packs active, coverage harness, honest 10-task forward network. |
| [audits/FORENSIC_PR40_CI_2026-09-21.md](audits/FORENSIC_PR40_CI_2026-09-21.md) | Forensic CI analysis of the red PR#40 wave-4 batch (root cause + A/B proof). |
| [audits/SESSION_PLAN_2026-09-21_01a0c506.md](audits/SESSION_PLAN_2026-09-21_01a0c506.md) | Session plan record (agent 01a0c506): wave-4 hardening batch planning. |
| [audits/PR32_TRIAGE_2026-09-21.md](audits/PR32_TRIAGE_2026-09-21.md) | Forensic per-file triage of PR#32 vs merged PR#34 (15 SUPERSEDED / 4 DROP / 3 PORT / 4 ADAPT) — basis of the supersession. |
| [audits/P0_STABILIZATION_2026-09-24.md](audits/P0_STABILIZATION_2026-09-24.md) | Owner-directed P0 day (task-165/166/167): legacy `/creative/*` lane disposition, studio surface wiring, verifiable backups — repro/evidence-first report. |
| [audits/VERIFICATION_GAP_REPORT_2026-09-24.md](audits/VERIFICATION_GAP_REPORT_2026-09-24.md) | Task-180 verification closure (Agent D): GAP-A/B/C closed with executable proof (slideshow/story/pdf artifact verifiers), GAP-D recorded as formal gap, attacks A–H + 3 reverted mutations. |
| [audits/CROSS_PR_TRUTH_2026-09-24.md](audits/CROSS_PR_TRUTH_2026-09-24.md) | Cross-PR truth gate: #67/#68/#70/#71 layer matrix with the branch ≠ PR ≠ merged ≠ CI-on-main distinction, measured with git/gh. |
| [audits/VERIFICATION_TRUTH_MATRIX.json](audits/VERIFICATION_TRUTH_MATRIX.json) | Machine-readable OPERATION→COMMAND→CAPABILITY→JOB→RUNTIME→ARTIFACT→VERIFIER→COMPLETED matrix (PASS/MISSING/BLOCKED/NOT_APPLICABLE, evidence-named per cell). |
| [audits/GATE5_CLOSURE_2026-09-24.md](audits/GATE5_CLOSURE_2026-09-24.md) | Task-181 Gate 5 closure/reconciliation (Agent E): claim-audit truth matrix (prior Gate-5-local claims vs GitHub), PR#71/#74 reconciliation, four reproduced defects (typed failure → COMPLETED, one-state failures, publication leak, trace job_id=null), fixes with regression + 6/6 mutation probes. |
| [audits/GATE5_TRUTH_MATRIX.json](audits/GATE5_TRUTH_MATRIX.json) | Machine-readable Gate 5 claim→evidence matrix + acceptance-gate answers (CLOSED only with Git + CI evidence). |
| [audits/DEAD_ENGINES_2026-09-22.md](audits/DEAD_ENGINES_2026-09-22.md) | Dead-engine verification (zero importers, measured gates), five-option wiring analysis, negative controls, and the recorded residuals. |
| [audits/CONTINUUM_CLOSURE_2026-09-27.md](audits/CONTINUUM_CLOSURE_2026-09-27.md) | Continuum evidence-foundation closure (task-184 / task-154): 95%-per-pack coverage gate, fail-closed snapshot, replayable mutation campaign, exact-SHA CI — with the recorded lost-lineage recovery. |
| [audits/MASTER_ARCHITECTURAL_ASSESSMENT_2026-09-27.md](audits/MASTER_ARCHITECTURAL_ASSESSMENT_2026-09-27.md) | Exact-SHA Phase Zero assessment: actual runtime, trust boundaries, Continuum/GitHub truth, five-angle research decisions, gaps, and risk register. |
| [audits/EXECUTION_PLAN_2026-09-27.md](audits/EXECUTION_PLAN_2026-09-27.md) | Phased implementation plan with dependencies, verification/CI strategy, operational gates, and Definition of Done. |
| [audits/FORENSIC_MAINLINE_RECOVERY_2026-10-06.md](audits/FORENSIC_MAINLINE_RECOVERY_2026-10-06.md) | Exact-SHA mainline truth audit: release-metadata defects (README version, lockstep guard, Continuum snapshot) with the delivered fixes and honest production limitations. |
| [audits/2026-09-28-master-forensic-evolution.md](audits/2026-09-28-master-forensic-evolution.md) | Storage/R2 forensic evolution report: live truth, governance, four unified-cloud fixes, two R2 fail-closed fixes, mutation evidence, and residual handoffs. |
| [audits/NEXUS_STABILIZATION_HANDOFF_2026-10-09.md](audits/NEXUS_STABILIZATION_HANDOFF_2026-10-09.md) | Dated Phase-1 stabilization handoff: live PR inventory, exact-SHA remediation evidence, Board fence correction, CI blockers, and backup/restore limits. |
| [audits/COORDINATION_2026-09-21.md](audits/COORDINATION_2026-09-21.md) | Coordination record of session `01a0c54f` (2026-09-21): claims, collisions and hand-offs of the wave-4 batch (moved from the repository root in the 2026-10-15 closeout). |
| [audits/WAVE5_COORDINATION_01a0c5da.md](audits/WAVE5_COORDINATION_01a0c5da.md) | Coordination record of session `01a0c5da` (2026-09-21, wave 5): claim, collision record, re-apply log — companion of `WAVE5_ACTIVATION_2026-09-21.md` (moved from the root). |
| [audits/STORAGE_RESILIENCE_CHECKUP_2026-09-21.md](audits/STORAGE_RESILIENCE_CHECKUP_2026-09-21.md) | Ten-step storage-resilience check-up and hand-over (2026-09-21, Persian; session `01a0c58a`, base `b422512`) (moved from the root). |
| [audits/LIVE_ENGINEERING_BASELINE_2026-09-26.md](audits/LIVE_ENGINEERING_BASELINE_2026-09-26.md) | Reproducible snapshot of the 2026-09-26 engineering baseline: exact commands, versions and gate outputs (moved from the root). |
| [audits/OWNER_WIDE_FORENSIC_AUDIT_2026-09-26.md](audits/OWNER_WIDE_FORENSIC_AUDIT_2026-09-26.md) | Owner-wide forensic audit (2026-09-26): scope and truth boundary, verified/unverified claims per surface (moved from the root). |
| [audits/MASTER_ENGINEERING_TRUTH_REPORT_2026-09-27.md](audits/MASTER_ENGINEERING_TRUTH_REPORT_2026-09-27.md) | Master engineering truth report (2026-09-27): exact-SHA state of the repository, release/CI/runtime truth and residual risks (moved from the root). |
| [audits/2026-10-15-closeout/INDEX.md](audits/2026-10-15-closeout/INDEX.md) | **Closeout evidence package (PR #218, owner brief #3):** index of the generated inventory (`module_inventory.csv/json`, `remediation_map.json`, Mission-2 status CSV), the ledgers below and the raw gate/suite outputs under `evidence/`. |
| [audits/2026-10-16-completion/INDEX.md](audits/2026-10-16-completion/INDEX.md) | Completion mission (2026-10-16) evidence package index: live truth, independent PR #218 audit, module disposition, defect P1-1, census, blockers, final report. |
| [audits/2026-10-16-completion/01_LIVE_TRUTH.md](audits/2026-10-16-completion/01_LIVE_TRUTH.md) | Live truth on 2026-10-16: remote refs, per-job CI conclusions, merge state, collaborator list, board lease, and the working-tree recovery proof. |
| [audits/2026-10-16-completion/02_PR218_INDEPENDENT_AUDIT.md](audits/2026-10-16-completion/02_PR218_INDEPENDENT_AUDIT.md) | Independent file-by-file audit of PR #218: secrets, test weakening, migrations, workflows, CAS wiring, board-ref CAS, generated-file agreement. |
| [audits/2026-10-16-completion/03_MODULE_DISPOSITION.md](audits/2026-10-16-completion/03_MODULE_DISPOSITION.md) | 284/284 production modules dispositioned: method, limitations, the bucket rules, the 45 hand-read diffs, remaining registered risks. |
| [audits/2026-10-16-completion/04_DEFECTS_AND_FIXES.md](audits/2026-10-16-completion/04_DEFECTS_AND_FIXES.md) | Defects P1-1 (HuggingFace key parsing) and P1-2 (census tooling: compare direction and compare base) with root cause, fix, mutation proof; the 97 new tests; the three disclosed test-setup changes; what was deliberately not fixed here. |
| [audits/2026-10-16-completion/05_PR_CENSUS_AND_INTEGRATION_LEDGER.md](audits/2026-10-16-completion/05_PR_CENSUS_AND_INTEGRATION_LEDGER.md) | Regenerated census of 63 open PRs (2026-10-16T11:04:15Z, corrected main-relative/base-relative fields), the reviewer roster, per-PR dispositions and the ordered integration ledger. |
| [audits/2026-10-16-completion/06_OPERATIONS_AND_BLOCKERS.md](audits/2026-10-16-completion/06_OPERATIONS_AND_BLOCKERS.md) | Live backup failure evidence, dependency-audit workflow status, supply-chain/container decisions with acceptance criteria, what ran and what did not. |
| [audits/2026-10-16-completion/07_FINAL_REPORT.md](audits/2026-10-16-completion/07_FINAL_REPORT.md) | Final report: SHAs, gates, statuses, the exact merge refusal, and the first next action for the owner. |
| [audits/2026-10-16-completion/module_disposition.json](audits/2026-10-16-completion/module_disposition.json) | Machine-readable disposition for all 284 production modules (JSON). |
| [audits/2026-10-16-completion/module_disposition.csv](audits/2026-10-16-completion/module_disposition.csv) | Machine-readable disposition for all 284 production modules (CSV). |
| [audits/2026-10-16-completion/review_records.json](audits/2026-10-16-completion/review_records.json) | Per-path review verdicts for the 56 modules read by hand in this audit. |
| [audits/2026-10-16-completion/pr_census.csv](audits/2026-10-16-completion/pr_census.csv) | Live PR census 2026-10-16T08:22Z (CSV). |
| [audits/2026-10-16-completion/pr_reconciliation.md](audits/2026-10-16-completion/pr_reconciliation.md) | Per-PR reconciliation (generated): category, head, files/commits relative to `main`, dependency, owner and next action for all 63 open PRs. |
| [audits/2026-10-15-overnight-stabilization/INDEX.md](audits/2026-10-15-overnight-stabilization/INDEX.md) | Overnight Autonomous Stabilization (brief #4) evidence package index: phases 0–9, 12, 15; reproduction commands. |
| [audits/2026-10-15-overnight-stabilization/01_LIVE_TRUTH_AND_PR218.md](audits/2026-10-15-overnight-stabilization/01_LIVE_TRUTH_AND_PR218.md) | Live state, exact-head CI runs with per-job conclusions, honest merge blocker, lease renewal through the coordination ref. |
| [audits/2026-10-15-overnight-stabilization/02_PR_CENSUS_AND_LEDGER.md](audits/2026-10-15-overnight-stabilization/02_PR_CENSUS_AND_LEDGER.md) | Census of all 63 open PRs (states, board collisions, overlap, merge order) with the machine-readable ledger. |
| [audits/2026-10-15-overnight-stabilization/03_A04_BOARD_STRUCTURAL_REMEDY.md](audits/2026-10-15-overnight-stabilization/03_A04_BOARD_STRUCTURAL_REMEDY.md) | A-04 remedy: coordination ref design, adversarial tests, live bootstrap, 7/7 merge-tree acceptance proof. |
| [audits/2026-10-15-overnight-stabilization/04_ASSET_LIBRARY_AND_CREATIVE_GRAPH.md](audits/2026-10-15-overnight-stabilization/04_ASSET_LIBRARY_AND_CREATIVE_GRAPH.md) | What exists for assets/graph/passport, the gap analysis, the CAS Asset Library delivered, what remains open. |
| [audits/2026-10-15-overnight-stabilization/05_COMMANDBUS_AUDIT.md](audits/2026-10-15-overnight-stabilization/05_COMMANDBUS_AUDIT.md) | Intent≠Authority audit of the CommandBus against the brief's checklist; no orchestration library, with proof. |
| [audits/2026-10-15-overnight-stabilization/06_OBSERVABILITY_AND_PROVIDER_CONTRACTS.md](audits/2026-10-15-overnight-stabilization/06_OBSERVABILITY_AND_PROVIDER_CONTRACTS.md) | Correlation-id chain as built, PII posture, OpenTelemetry decision, provider contract audit limits. |
| [audits/2026-10-15-overnight-stabilization/07_OPERATIONS_BACKUPS_SUPPLY_CHAIN.md](audits/2026-10-15-overnight-stabilization/07_OPERATIONS_BACKUPS_SUPPLY_CHAIN.md) | Backups (blocked on secrets), new scheduled dependency audit, lockfile/image/chromadb dispositions, smoke test. |
| [audits/2026-10-15-overnight-stabilization/08_MODULE_INVENTORY_AND_REACHABILITY.md](audits/2026-10-15-overnight-stabilization/08_MODULE_INVENTORY_AND_REACHABILITY.md) | Recomputed inventory at the overnight head and dispositions for DEFERRED / CHANGED_UNREVIEWED / untested modules. |
| [audits/2026-10-15-overnight-stabilization/09_RESEARCH_DECISION_BACKLOG.md](audits/2026-10-15-overnight-stabilization/09_RESEARCH_DECISION_BACKLOG.md) | Ten research-informed decisions with NOW/NEXT/LATER horizons and triggers; nothing adopted from research alone. |
| [audits/2026-10-15-overnight-stabilization/10_FINAL_REPORT_AND_SKEPTICAL_AUDIT.md](audits/2026-10-15-overnight-stabilization/10_FINAL_REPORT_AND_SKEPTICAL_AUDIT.md) | Final report (SHAs, runs, reconciled counts, P0/P1, blockers, first next action) and negative-control self-audit. |
| [audits/2026-10-15-closeout/EXECUTIVE_SUMMARY.md](audits/2026-10-15-closeout/EXECUTIVE_SUMMARY.md) | One-page verdict (`READY_WITH_EXPLICIT_EXTERNAL_BLOCKER`): per-finding status A-01…A-19/GOV with proof, numbers at the code head, the owner's ordered actions, and what is *not* claimed. |
| [audits/2026-10-15-closeout/MODULE_AUDIT.md](audits/2026-10-15-closeout/MODULE_AUDIT.md) | How the 283 production modules were classified (`REMEDIATED_WITH_TESTS` / `VERIFIED` / `VERIFIED_WITH_LIMITATIONS` / `CHANGED_UNREVIEWED` / `DEFERRED`), the counts, and the limits of the mechanical rule. |
| [audits/2026-10-15-closeout/TECHNICAL_DEBT_LEDGER.md](audits/2026-10-15-closeout/TECHNICAL_DEBT_LEDGER.md) | Measured remaining debt with the guard for each item: unselected ruff rules, complexity/legacy baselines, untested modules, dependency advisories, and the structural audit items not yet built (A-04 board, lockfile, multi-stage image). |
| [audits/2026-10-15-closeout/PR_CONVERGENCE.md](audits/2026-10-15-closeout/PR_CONVERGENCE.md) | Disposition of the 62 other open PRs (`pr_convergence_ledger.csv`), the serial integration order after #218, and the rules under which a PR may be closed. |
| [audits/2026-10-15-closeout/CI_AND_RELEASE_EVIDENCE.md](audits/2026-10-15-closeout/CI_AND_RELEASE_EVIDENCE.md) | Commit ledger of #218 with the regression test per commit, CI runs per head, local gate outputs, release-lineage facts (no tag asserted), governance lease. |
| [audits/2026-10-15-closeout/SECURITY_AND_CONTAINER.md](audits/2026-10-15-closeout/SECURITY_AND_CONTAINER.md) | Security controls with their proving tests, `pip-audit` exposure analysis, and the container verification (unit contract + CI `container` job) with its stated limitation. |
| [audits/2026-10-15-closeout/EXTERNAL_BLOCKERS.md](audits/2026-10-15-closeout/EXTERNAL_BLOCKERS.md) | Everything that cannot be closed from inside the repository — required review, backup secrets, CodeRabbit file cap, live-deployment and live-provider checks, release tag — each with the exact owner action. |

## History (archived — read-only, never a source of truth)

| Document | What it is |
|---|---|
| [history/architecture-v2.0.0.md](history/architecture-v2.0.0.md) | The v2.0.0 architecture diagram, archived with a banner when the suite replaced it. |
| [history/PHASE_D_ARCHITECTURE.md](history/PHASE_D_ARCHITECTURE.md) | Phase D (schema management) engineering record; superseded by DECISION_LOG. |
| [history/v2-roadmap-strategy.md](history/v2-roadmap-strategy.md) | Pre-v2 growth strategy (June 2025); delivered items landed, rest superseded by the seven-phase roadmap. |
| [history/todo-v1.2.0.md](history/todo-v1.2.0.md) | Session checklist, v1.2.0 era. |
| [history/todo-v1.3.0.md](history/todo-v1.3.0.md) | Session checklist, v1.3.0 era. |
| [history/todo-v2.0.0.md](history/todo-v2.0.0.md) | Session checklist, v2.0.0 era. |
| [history/todo-v2.1.0.md](history/todo-v2.1.0.md) | Session checklist, v2.1.0 era. |
| [history/todo-step3-pending.md](history/todo-step3-pending.md) | Unfinished items from the v1.x step-3 plan. |

---

## Rules of this map

1. **One index.** A new document is added to the right table above in the same PR that adds the file.
2. **Living vs dated.** `architecture/` is updated in place; `audits/` and `history/` are dated and immutable.
3. **Every structural claim has an enforcer.** If a page states a boundary or invariant, it names the test that guards it ([`MODULE_MAP.md`](architecture/MODULE_MAP.md) §3 is the register).
4. **Numbers are reproducible.** Documents that quote counts (files, operations, tests) include the command that regenerates them ([`TESTING.md`](architecture/TESTING.md) §6).
5. **Language.** English is canonical for architecture, CI, and PR text; Persian summaries exist for the owner and defer to the English text. The board and AGENTS.md remain bilingual by design.
6. **Enforcement.** `pytest -q tests/unit/test_docs_integrity.py` — run it before pushing a documentation change.
