# NEXUS-OMNI — Morning Handoff (2026-11-03)

> Mission `NEXUS-OMNI-FORENSIC-RECONSTRUCTION-2026-11-03`. Branch
> `arena/omni-audit-reports`. Base `main` @ `5ce9f7d3`. This is the executive
> summary; the four domain reports carry the evidence.

## Executive summary

Two real defects were found and fixed, both with reproducible evidence and
tests; six audit reports were produced; the 43 open PRs were classified. **No
test was weakened, skipped, or deleted. No PR was force-pushed or mass-merged.
No secret was printed. `main` was not touched directly.**

The security and runtime architecture is genuinely strong — the SSRF guard,
the shell sandbox, the HMAC/bearer/secret gates, the Intent≠Authority gateway,
and the fail-closed privacy policy are all implemented and test-backed, not
merely documented. The real risks are at **integration seams** (a legacy path
that bypasses the privacy policy) and **CI hygiene** (a disk budget that silently
dropped a mutation job, and a scheduled job that had been red for a day).

## Status of `main`

| Item | Value |
|---|---|
| `main` SHA | `5ce9f7d3f558ab903ece5f19018b28a5e2ff9a04` (PR #303 merge) |
| CI on `main` | `CI` workflow green; `queue-ownership-mutations` **red** (fixed by #304) |
| `maintenance` workflow | red every schedule (fixed by #305, code side) |
| Open PRs | 43 + 3 new (#304, #305, #307) |

## Fixes delivered (this campaign)

### PR #304 — CI disk exhaustion (P1) — `arena/omni-ci-disk-budget-20261103`
- **Symptom:** `queue-ownership-mutations` failed at `2026-11-02T21:50` on
  `5ce9f7d3` with `OSError: [Errno 28] No space left on device` — *before the
  campaign ran*. The same SHA passed at `17:59`.
- **Root cause:** `pip install -e ".[dev]"` pulls `sentence-transformers` →
  `torch` (539 MB) + CUDA wheels (`nvidia_cudnn` 519 MB, `nvidia_cufft` 218 MB,
  …) — well over 1.5 GB — for a campaign that needs only the queue stack + pytest.
- **Fix:** install only the campaign's dependency set (the existing
  `trust-mutations` minimal-install pattern).
- **Evidence:** minimal install OK; `56 passed` (16.9 s); full 10-mutant campaign
  `EXIT=0`, 10/10 killed; reproducible from a detached worktree at `5ce9f7d3`;
  worktree restored clean.

### PR #305 — R2 bucket fail-closed validation (P2) — `arena/omni-r2-bucket-validation`
- **Symptom:** the scheduled `housekeeping` job (run `41446266076`, job
  `125280000337`) fails every run with `ParamValidationError: Invalid bucket
  name` — and, because `housekeeping` is the only thing that prunes backups,
  **backup retention was not actually running**.
- **Root cause:** `R2Provider.is_configured()` accepts any non-empty
  `R2_BUCKET`, so a malformed value fails deep inside boto3 with an opaque error.
- **Fix:** validate against the documented R2 rule (3–63 chars, lowercase
  alphanumeric/hyphen, alphanumeric ends) and refuse at the boundary with a
  `ProviderUnavailable` naming `R2_BUCKET`.
- **Evidence:** `46 passed` (`test_r2_provider` + `test_housekeeping_contract` +
  `test_maintenance_backup`); ruff clean; new tests pin the refusal before the
  client is ever called.
- **Owner action still required:** correct the malformed `R2_BUCKET` repository
  secret. The code fix only makes the failure explicit and actionable.

### PR #307 — LangGraph checkpoint CVE pins (P1) — `arena/omni-dep-audit`
- **Symptom:** `pip-audit` flagged both LangGraph checkpoint pins under
  **GHSA-6hmw-9432-67qq** (`langgraph-checkpoint-sqlite` 3.1.1,
  `langgraph-checkpoint-postgres` 3.1.2).
- **Root cause:** the advisory affects the LangGraph `*Store` (namespace memory)
  classes; the pinned versions predate the patched releases.
- **Reachability:** **not directly reachable** — NEXUS imports only
  `SqliteSaver` / `AsyncPostgresSaver` (`langgraph.checkpoint.*`) and never
  `langgraph.store.*`. The bump is defence-in-depth on the state path.
- **Fix:** raise to `langgraph-checkpoint-sqlite==3.1.2` /
  `langgraph-checkpoint-postgres==3.1.3`; regenerate the committed sqlite schema
  golden through the sanctioned `nexus checkpoints golden update` CLI; remove the
  now-redundant empty-`checkpoint_id` workaround in `AsyncCompatibleSqliteSaver`;
  sync `docs/architecture/DATA_LIFECYCLE.md`.
- **Evidence:** full suite `python -m pytest -q -m "not slow"` → **6169 passed,
  39 skipped, 0 failed** (identical to the `main` baseline → no regression);
  checkpoint + docs + architecture suites green; `ruff` + `mypy` clean.

### Deferral recorded (not a second fix)
The `queue-ownership-mutations` disk fix was **already delivered by #304**. An
independent attempt on `arena/omni-dep-audit` produced the *identical* minimal
install set; that duplicate edit was discarded and the overlap recorded in the
board `deferred_log` (`.github/workflows/queue-ownership-mutations.yml` is **not**
touched by #307). No duplicate PR was opened.

## Audit reports produced

| File | Contents |
|---|---|
| `NEXUS_OMNI_MASTER_FORENSIC_AUDIT_2026-11-03.md` | archaeology, inventory, architecture, findings ledger, the 16 answers |
| `NEXUS_OMNI_SECURITY_PRIVACY_AUDIT_2026-11-03.md` | STRIDE/OWASP, bandit review, SSRF, shell sandbox, crypto, AI security, privacy modes, data-flow map, retention matrix |
| `NEXUS_OMNI_CI_CD_SUPPLY_CHAIN_AUDIT_2026-11-03.md` | run root-cause, workflow security, required vs advisory checks, disk budget |
| `NEXUS_OMNI_PR_CONVERGENCE_LEDGER_2026-11-03.md` | all 43 PRs classified A–G, integration matrix, merge order |
| `NEXUS_OMNI_RUNTIME_DATA_RECOVERY_AUDIT_2026-11-03.md` | queue/fencing/passport, outbox, DB/migrations, asset library, backup/restore |

## Open risks (owned, not hidden)

| ID | Sev | Risk | Owner action |
|---|---|---|---|
| SEC-4 | P2 | legacy Telegram chat path bypasses the IBPC `PrivacyPolicy` | decide whether to route it through the policy or document the boundary in a living ADR |
| RT-2 | P2 | restore is proven only for the R2 path, and only when secrets exist | decide whether to add an always-on restore proof |
| CI-2 | P3 | heavy optional extras pulled by default `.[dev]` installs | split `[speech]/[local-llm]/[translate]/[pdf]` legs (extras-matrix covers 3) |
| RT-3 | P3 | no startup guard for the one-queue-owning-process assumption | already in board `next_work` |
| PR-1 | P2 | 10 `D`-class PRs make `cli.py`/`bot/handlers.py`/board conflict for everyone | extract the large PRs into reviewable slices |
| PR-2 | P3 | 3 duplicate/superseded pairs (#113/#115, #135/#140, #295/#218) | dedupe |

## What is NOT proven

- **Production deploy / restore**: no production access → recorded `NOT_PROVEN`.
- **Full-suite green under the heavy closure**: the torch/llama-cpp build is
  environment-blocked in this sandbox (the install itself exceeds the disk/time
  budget here — the very problem #304 fixes). The queue-ownership suite and all
  board/docs/architecture tests were run and are green.
- **A P0 security finding**: none was proven. Absence of evidence is stated as
  such, not as "secure".

## Now / Next / Later

**Now (blocks safe development):**
- Merge #304 (CI), #305 (R2) and #307 (dep CVE) after required checks.
- Owner corrects the `R2_BUCKET` secret.
- Decide SEC-4 (legacy privacy boundary).

**Next (remaining, but does not block current motion):**
- Extract the large `D`-class PRs (#33, #69, #205, #247) into slices.
- Dedupe #113/#115 and #135/#140.
- Split heavy extras out of default CI installs.

**Later (not yet time to build):**
- Always-on restore proof; single-owner startup guard; bounded `_quarantine`
  retention (already tracked in board `next_work`).

## Verification commands (reproducible)

```bash
# R2 fix
pytest tests/unit/test_r2_provider.py tests/unit/test_housekeeping_contract.py \
       tests/unit/test_maintenance_backup.py
# board + docs + architecture gates
pytest tests/unit/test_agent_board.py tests/unit/test_docs_integrity.py
python scripts/check_board_integrity.py
ruff check . && ruff format --check .
# CI fix (campaign, minimal install)
python scripts/queue_ownership_mutations.py     # EXIT=0, 10/10 mutants killed
```
