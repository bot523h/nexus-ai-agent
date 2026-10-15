# CI and release evidence — PR #218 (`arena/26f7d958-nexus-ai-agent` → `main` `6b398565`)

## 1. Commit ledger (oldest first; every commit is on #218, none rewritten)

| SHA | Concern | Regression evidence |
|---|---|---|
| `2c2ebaf` | board lease `task-270` (sole active agent, gates owner) | `scripts/agent_board.py check` |
| `9d31b39` | R0 — real session factory, wired moderation/viral surfaces, fail-closed dashboard (A-01/A-02b/A-03/A-06) | `test_real_session_factory.py`, `test_surface_*.py`, `test_dashboard_api.py` |
| `a357b0f` | R1 — one sync engine per deployment, declared deps, CI hygiene, updater/referral/hash fixes (A-02/A-05/A-07/A-08/A-09/A-12/A-13) | `test_postgres_single_engine.py`, `test_updater.py`, `test_core_fingerprint.py` |
| `8fd4c44` | R2 — nine orphan modules removed under a reachability law (A-10) | `tests/architecture/test_no_orphan_modules.py` |
| `8690a4c` | GOV-1/GOV-2 — referee 14/14, Gate-5 probes 17/17, both campaigns in CI | `governance-mutations` job |
| `807c197` | A-17 — Telegram ids BIGINT on PostgreSQL; deeper A-01/A-02/A-03 validation | `test_telegram_id_width.py`, `test_a01_message_path.py` |
| `60a044f` | A-06 — OpenAPI-driven fail-closed inventory of every route | `test_api_gate_inventory.py` |
| `0632801` | A-14 ruff ratchet widened; A-18 retired-engine disposal | `test_ci_lint_parity.py`, `test_db_engine_retirement.py` |
| `c894d2d` | closeout — schema authority, time contract, updater contract, container hardening, docs (A-07/A-12/A-15/A-16/A-19) | `test_migrations.py`, `test_time_contract.py`, `test_container_contract.py` |
| `72fb067` | CI — failures kept in the pytest summary, failing ids as check annotations | workflow |
| `50abdb0` | 3.10 parity (`datetime.UTC`) + guard for tests/ and scripts/ | `test_ci_extras_parity.py` |
| `4e75d27` | A-01 negative control | `test_a01_message_path.py` |
| `6fb0a97` | A-11 complexity ratchet (law R22) | `tests/architecture/test_complexity_ratchet.py` |
| `d80568f` | A-11 `_process_job` 62 → 17 under a characterisation contract | `test_process_job_characterization.py` (9), exec-core mutants 17/17 |
| `8c4adf2` | A-13 `mypy --strict src` 57 → 0, enforced | `test_strict_typing_boundaries.py`, `test_ci_lint_parity.py` |
| `c52f795` | A-12 event-loop census 35 → 0, ruff `ASYNC` enforced | `test_async_offload.py`, `test_ci_lint_parity.py` |
| `a0c2027` | A-16 CI `container` job | `test_container_contract.py::test_ci_executes_the_image_contract_end_to_end` |
| `7bfd628` | typed filesystem boundary error (re-scoped remainder of #209) | `test_filesystem_policy_primitives.py` 28/28 |
| (docs) | this evidence package, `scripts/module_inventory.py`, `docs/README.md` index | `test_docs_integrity.py` |

## 2. CI runs (workflow `ci.yml`, `pull_request` on #218; a new push cancels the previous run)

| Head | Run | Result |
|---|---|---|
| `6fb0a97` | 38777822652 | **success** — all jobs (lint, test, python-parity ×3, extras-matrix ×4, continuum-evidence ×3, migrate-postgres, governance/trust/temporal/remote-key mutations, release-lineage, merge-base-guard) |
| `8c4adf2` | 38791415279 | cancelled by the next push after lint, test, python-parity ×3 and governance-mutations had passed (only the three continuum-evidence legs were still running) |
| `a0c2027` | 38793879776 | first run that includes the `container` job: job 116625684891 **success**, all 8 steps, 7 min 07 s (build + probes); overall result recorded in the PR #218 ledger comment |
| `7bfd628` + docs | (run created by the final push) | result recorded in the PR #218 ledger comment and in the merge record |

The merge rule is unchanged: `gh pr merge 218 --merge` only after the run on the **exact**
head is green; the merged SHA's run on `main` is the acceptance evidence for the owner.

## 3. Local gates at the code head (sandbox; python 3.11.2, ruff 0.16.8 pinned to CI)

`evidence/09_gates_at_a0c2027.txt`: `ruff check .` clean · `ruff format --check .` 685 files
formatted · `mypy src` (strict) 0 errors in 283 files · `deptry src` clean.
`evidence/08_full_suite_after_a12.txt`: fast suite **4095 passed, 37 skipped**.
`evidence/10_full_suite_at_7bfd628_with_skips.txt`: the same suite on the working tree of the
docs commit (code head `7bfd628` + this package): **4103 passed, 37 skipped**, every skip reason
listed. PostgreSQL leg (45 tests + Alembic upgrade/downgrade), mutation
campaigns (referee 14/14, Gate-5 17/17, execution-core 17/17) and the extras matrix run in CI
on every push (`evidence/02…05_*.txt` hold the sandbox runs of the two harnesses).

## 4. Release verification

* Version lock-step (`tests/unit/test_version_lockstep.py`, CI `release-lineage` via
  `scripts/release_lineage.py`): `pyproject.toml` `3.13.0` = top CHANGELOG release `[3.13.0] —
  2026-09-21` = installed distribution; `## [Unreleased]` present and non-empty.
* No tag was created or asserted by this session. `scripts/release_lineage.py --sha HEAD`
  (2026-10-15) reports: current version `3.13.0`, **no tag `v3.13.0`**, newest GitHub Release
  `v3.3.0` (2026-05-30) — "releases lag the repository" (status *open*, not red). The remote has
  8 `vX.Y.Z` tags, the newest `v3.5.0`; versions `3.6 … 3.13` exist only as CHANGELOG sections.
  The lineage job documents this gap rather than hiding it; cutting the next tag/release from
  the merged `main` is an owner action listed in `EXTERNAL_BLOCKERS.md`.
* Nothing in #218 changes the public version; all entries are under `[Unreleased]`.

## 5. Governance

Lease `task-270-full-audit-remediation`, generation 3, TTL 2026-10-16T12:36:15Z, branch
`arena/26f7d958-nexus-ai-agent`, gates owner. Released only after the merge
(`scripts/agent_board.py release task-270-full-audit-remediation --branch
arena/26f7d958-nexus-ai-agent --expected-generation 3`).
