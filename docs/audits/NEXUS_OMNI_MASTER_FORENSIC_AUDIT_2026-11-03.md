# NEXUS-OMNI — Master Forensic Audit (2026-11-03)

> **Status:** dated record. Mission `NEXUS-OMNI-FORENSIC-RECONSTRUCTION-2026-11-03`,
> branch `arena/omni-audit-reports`. Base `main` @
> `5ce9f7d3f558ab903ece5f19018b28a5e2ff9a04` (PR #303 merge). This is the index
> of the campaign; the four companion reports go deeper on their domains.

## 0. Companion reports

| Report | Domain |
|---|---|
| `NEXUS_OMNI_SECURITY_PRIVACY_AUDIT_2026-11-03.md` | threat model, security, privacy, AI security |
| `NEXUS_OMNI_CI_CD_SUPPLY_CHAIN_AUDIT_2026-11-03.md` | CI, workflows, dependencies, disk budget |
| `NEXUS_OMNI_PR_CONVERGENCE_LEDGER_2026-11-03.md` | every open PR, classification, disposition |
| `NEXUS_OMNI_RUNTIME_DATA_RECOVERY_AUDIT_2026-11-03.md` | queue, DB, storage, asset, recovery, backup |
| `NEXUS_OMNI_MORNING_HANDOFF_2026-11-03.md` | executive summary, next steps |

## 1. Phase Zero — ground truth

| Item | Value | How verified |
|---|---|---|
| `main` SHA | `5ce9f7d3f558ab903ece5f19018b28a5e2ff9a04` | `git rev-parse HEAD` == `origin/main` |
| Commits | 1173 | `git rev-list --count HEAD` |
| Working tree | clean except untracked `.openhands/` | `git status --porcelain` |
| Open PRs | 43 (8 draft) | `GET /pulls?state=open` |
| Board | schema 2, 177 claims, 81 zones, `next_work` 10 | `.agents/board.json` |
| Workflows | 4 | `ls .github/workflows` |
| Tags | v0.2.0-D … v3.5.0 | `git tag` |

No branch was assumed valid by name; each was checked against the API and the
board.

## 2. Repository archaeology (first commit → today)

The project began on **2026-05-26** (`a4eabdf9 Initial commit`) and was
scaffolded two days later as a Python package
(`04c49482 chore: project skeleton, pyproject.toml, Makefile, gitignore`),
then grew in a deliberate order: settings → SQLite/SQLModel → LLM provider
abstraction (FakeLLM + llama.cpp) → LangGraph orchestration → chat/planner/
executor agents → tools.

The history then shows a repeated **"build → audit → harden → freeze"** cycle,
visible in the audit/history documents:

- `docs/history/` holds the dated todo/roadmap snapshots (v1.2.0 … v2.1.0,
  `PHASE_D_ARCHITECTURE.md`, `v2-roadmap-strategy.md`).
- `docs/audits/` holds dated forensic reports (`2026-09-28-master-forensic-evolution.md`,
  `COORDINATION_2026-09-21.md`, …). A large rename commit (`f3616942`,
  `c25f7fe8`) moved root-level reports into `docs/audits/` and `docs/history/`,
  which is when "dated record" was separated from "living view".
- **Reverts are informative**: `777f5371 revert: remove unused token encryption
  module — no consumer exists yet` (no speculative crypto kept);
  `2bb694f3 revert: remove pgvector migration from the Alembic chain`;
  `fd1ca1ae provenance: revert placeholder corruption + close the real finding`.
  These show a culture of removing unproven code rather than accumulating it.

**Why today's architecture looks the way it does:** the early Telegram-bot shape
was progressively re-framed as an *application* whose Telegram bot is one
channel adapter (see `docs/architecture/PRODUCT_FRAME.md` and AGENTS.md §
"Product frame"). That re-frame produced the `Principal` identity model, the
`CommandBus`/`CapabilityRegistry`/`Policy` execution spine, the provenance
ledger, and the Intent≠Authority boundary — all of which are the strongest parts
of the current tree.

### Historical risks that still shape the code

| Historical pattern | Residual today |
|---|---|
| Legacy creative lane (`/creative/video-edit`) | frozen but present; removal sequenced after PR #58 (ADR 0006) |
| Telegram ids used as identity | `Principal` exists; five legacy `telegram` importers outside `bot/` are frozen and gate new ones |
| Speculative modules | reverted rather than kept (token encryption, pgvector) |

## 3. Inventory (first-party coverage)

Counts (reproducible from the tree):

| Surface | Files | LOC |
|---|---|---|
| `src/` Python | 355 | 94,165 |
| `tests/` Python | 368 | 105,415 |
| `scripts/` Python | 36 | 12,950 |
| Workflows | 4 | — |
| `docs/` Markdown | 106 | — |
| Alembic migrations | 7 | — |
| i18n locales | 15 | — |

Largest source packages (LOC): `creative` 26,398 · `nagar` 9,624 · `features`
8,279 · `bot` 7,533 · `storage` 7,287 · `adapters` 6,211 · `continuum` 3,781 ·
`jobs` 2,701.

Largest individual files: `adapters/in_process_job_queue.py` (3,303),
`cli.py` (1,933), `creative/intelligence/ir.py` (1,877).

Every first-party module is covered by the four companion reports at the
domain level (security, CI, PR, runtime/data). The `__pycache__` entries are
build artifacts, not first-party code.

## 4. Architecture assessment

The intended spine (from the mission and AGENTS.md) is:

```
User Intent → Identity → Capability → Policy → Agent Runtime
→ Cognition/Memory/Tools → Typed Proposal → Creative IR → Compiler
→ Command DAG → CommandBus → Execution → Independent Verification
→ Evidence → Artifact Passport → Lineage → Project Graph
```

**What is genuinely implemented (verified in code, each with a test):**

- **Identity** — `application/principal.py` (`PrincipalId`, `ChannelBinding`).
- **Authority boundary** — `nagar/cognition/gateway.py` enforces *never
  MODEL→EXECUTION*; offered operations come from the registry; a proposal cannot
  self-grant (actor/project/schema-version from the composition root).
- **Trusted authorization** — `creative/studio/authorization.py`,
  `creative/assets/authz.py` (no grant from request JSON).
- **Typed IR & contracts** — `creative/intelligence/ir.py`,
  `COMMAND_CAPABILITY_CONTRACT.md`, execution-core invariants I1–I10 mirrored in
  `docs/architecture/EXECUTION_CORE.md` and gated by
  `tests/architecture/test_execution_contract_boundary.py`.
- **Provenance & evidence** — `provenance/`, `docs/architecture/PROVENANCE_LEDGER.md`.
- **SSRF / shell / filesystem** — `core/ssrf_guard.py`,
  `tools/system_shell.py`, `tools/filesystem_policy.py` (see the security report).

**Boundaries / gaps (honest):**

- The **legacy Telegram chat path** does not route through the IBPC
  `PrivacyPolicy`; the privacy modes are enforced in the newer slice. This is a
  documented boundary, not a silent hole (SEC-4).
- `cli.py` (1,933 LOC) and `bot/handlers.py` remain the highest-conflict files
  (AGENTS.md §6); `cli.py` is claimed in the board's merge order.
- `adapters/in_process_job_queue.py` (3,303 LOC) is the largest single file and
  the subject of the queue-ownership mutation campaign.

## 5. Findings ledger (this campaign)

| ID | Sev | Domain | Finding | Status |
|---|---|---|---|---|
| CI-1 | P1 | CI | `queue-ownership-mutations` disk exhaustion before the campaign runs | **FIXED** PR #304 |
| SEC-1 | P2 | storage | malformed `R2_BUCKET` → opaque boto3 `ParamValidationError` | **FIXED** PR #305 |
| SEC-4 | P2 | privacy | legacy chat path bypasses `PrivacyPolicy` | documented; owner decision |
| RT-1 | P2 | runtime | housekeeping job red every schedule (same root cause as SEC-1) | **FIXED** PR #305 |
| CI-2 | P3 | CI | heavy optional extras pulled by default `.[dev]` installs | deferred |
| SEC-2/3 | P3 | security | bench temp dir (B108), `0.0.0.0` bind (B104) | accepted, documented |
| SEC-5 | P3 | privacy | per-user rate limits are in-process (lost on restart) | documented |

No P0 finding was proven. The strongest security controls (SSRF, shell sandbox,
HMAC gates, Intent≠Authority, fail-closed privacy) are real and tested.

## 6. Answers to the mission's 16 questions

1. **First code / path to today** — §2: scaffold 2026-05-28, then a repeated
   build→audit→harden cycle; the Telegram-first shape was re-framed as an
   application with channel adapters.
2. **What actually runs** — queue ownership lifecycle, publication barrier,
   creative render jobs, provenance journal, SSRF/shell/filesystem guards,
   gateway/CommandBus spine (all have enforcing tests).
3. **What is only registered** — cloud privacy modes (`TRUSTED_CLOUD` /
   `EXPERIMENTAL_CLOUD`) have **no provider wired**; egress is a typed refusal.
   The legacy creative lane is frozen/deprecated.
4. **All open PRs** — classified in the PR convergence ledger (43 open).
5. **Architectural-conflict changes** — `cli.py`, `bot/handlers.py`,
   `.agents/board.json`, `docs/README.md` are the shared high-conflict paths.
6. **Top security/privacy weaknesses** — SEC-4 (legacy privacy bypass) is the
   most consequential; no P0.
7. **Data at risk** — transcript/audio/prompt (protected by digests + LOCAL_VAULT
   default); backup artifacts in R2.
8. **Which queue/runtime actually executes** — the durable job queue
   (`adapters/in_process_job_queue.py` + `creative/render_jobs.py`); the
   queue-ownership campaign proves ownership/fencing.
9. **Which CI is correct / needs repair** — all four workflows are
   security-sound; the disk budget of mutation jobs needs the minimal-install
   pattern (fixed for queue-ownership).
10. **Dependencies healthy/reproducible** — declared deps are pinned where it
    matters (`sqlmodel`, `sqlalchemy`, `httpx`, `uvicorn`, …); the risk is the
    *size* of the optional closure, not correctness.
11. **Do errors fail closed** — yes in the reviewed paths (SSRF, HMAC, privacy,
    shell, authz); SEC-1 was the counter-example (failed late with an opaque
    error) and is fixed.
12. **Does every success have a valid artifact/evidence** — the mutation
    campaigns and provenance journal produce evidence; `queue-ownership-mutations`
    produced **none** while red (now fixed).
13. **Is restore/recovery provable** — see the runtime/data report; the R2
    backup round-trip is verified, but the scheduled housekeeping path was red
    (SEC-1) so retention was not actually running.
14. **What was done** — PR #304 (CI P1), PR #305 (R2 P2), six audit reports,
    board claims, no test weakened.
15. **What is not yet proven** — production deployment/restore (no prod access →
    `NOT_PROVEN`), full-suite pass under the heavy closure (torch build is
    environment-blocked here), the legacy-privacy boundary decision.
16. **Can development continue tomorrow without a wave of hidden bugs?** — Yes,
    with the caveats above recorded and owned. The primitives are strong; the
    remaining work is integration seams and CI hygiene, not foundational repair.
