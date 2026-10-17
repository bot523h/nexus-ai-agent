# Closeout evidence package — 2026-10-15 (owner brief #3, "zero-debt mainline convergence")

Session `arena/26f7d958-nexus-ai-agent`, board lease `task-270-full-audit-remediation`,
pull request **#218** against `main` `6b398565`. Everything in this folder is either a
machine-generated artefact (with the generating command recorded) or a dated record that
names the commit, test, CI run or PR comment it rests on. Nothing here is a plan.

Status vocabulary (project-wide, unchanged): `VERIFIED` · `VERIFIED_WITH_LIMITATIONS` ·
`HARDENED_BUT_NOT_COMPLETE` · `BLOCKED` · `DEFERRED` — plus, for the inventory only,
`REMEDIATED_WITH_TESTS` and `CHANGED_UNREVIEWED` (defined in `MODULE_AUDIT.md`).

| File | What it is | Provenance |
|---|---|---|
| [EXECUTIVE_SUMMARY.md](EXECUTIVE_SUMMARY.md) | One-page verdict: what is proven, what is blocked, what the owner must do. | hand-written from the artefacts below |
| [MODULE_AUDIT.md](MODULE_AUDIT.md) | How every production module was classified and the resulting counts; limitations of the classification. | derived from `module_inventory.json` |
| `module_inventory.csv` / `module_inventory.json` | 594-file inventory (283 production modules, 277 tests, 24 scripts, 6 migrations, 2 workflows, 2 container files): LOC, AST facts, test references, change flag, prior and closeout status. | `python scripts/module_inventory.py --base origin/main --prior mission2_module_status_6b398565.csv --derive-remediation remediation_map.json --out module_inventory` |
| `remediation_map.json` | Mechanical module → regression-test pairing per PR commit (the only source of `REMEDIATED_WITH_TESTS`). | same command (`--derive-remediation`) |
| `mission2_module_status_6b398565.csv` | The Mission-2 per-module verdict on `main` `6b398565` (244 rows), copied byte-for-byte from PR #198 comment 6188882100 (sha256 `cb52083b0df70962ebdb9456cb2201ddd70f7635b999ce07b1ad9f8311889c13`). | published 2026-10-14 |
| [TECHNICAL_DEBT_LEDGER.md](TECHNICAL_DEBT_LEDGER.md) | Remaining, measured debt with the guard that keeps each item from growing. | `ruff --statistics`, ratchet tests, suite skips |
| [PR_CONVERGENCE.md](PR_CONVERGENCE.md) + `pr_convergence_ledger.csv` | Disposition of all 62 other open PRs (56 `REBASE_REQUIRED`, 2 `MERGE_ELIGIBLE`, 2 `BLOCKED_BY_GOVERNANCE`, 1 `BLOCKED_BY_FAILURE`, 1 `DEFERRED`) and the integration order. | PR #218 comment 6197535030 (live GitHub read 2026-10-15T16:50Z) |
| [CI_AND_RELEASE_EVIDENCE.md](CI_AND_RELEASE_EVIDENCE.md) | Commit ledger of #218, CI runs per head, local gate outputs, release-metadata checks. | `gh run view`, `evidence/*.txt` |
| [SECURITY_AND_CONTAINER.md](SECURITY_AND_CONTAINER.md) | Security review (dependency audit, hashing, SSRF/fs boundaries, fail-closed surfaces) and the container verification (unit contract + CI `container` job). | tests + CI job + `pip-audit` |
| [EXTERNAL_BLOCKERS.md](EXTERNAL_BLOCKERS.md) | Everything that cannot be closed from inside the repository: required review, backup secrets, CodeRabbit file cap, live-deployment checks — each with the exact owner action. | live `gh` reads |
| `evidence/00…10_*.txt` | Raw gate/suite/mutation outputs captured in the sandbox at the named heads (undurable sandbox files copied into Git). | commands recorded in each file header |

Regenerate the inventory on any head (byte-stable, no network):

```bash
python scripts/module_inventory.py --base origin/main \
  --prior docs/audits/2026-10-15-closeout/mission2_module_status_6b398565.csv \
  --derive-remediation docs/audits/2026-10-15-closeout/remediation_map.json \
  --out docs/audits/2026-10-15-closeout/module_inventory
```
