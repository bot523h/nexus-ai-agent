# Forensic Mainline Recovery — 2026-10-06

**Status:** `HARDENED_BUT_NOT_COMPLETE`

**Scope:** Live-truth audit of `bot523h/nexus-ai-agent`, current `main`, open pull requests, governance board, architecture boundaries, release metadata, and reproducible local evidence. No PR was merged, closed, deleted, rebased, or force-pushed by this audit.

## 1. Live truth

| Item | Exact evidence |
|---|---|
| Repository | `bot523h/nexus-ai-agent` |
| Current main at audit start | `8de0edd7d6bd182be2f53ccba8df45cc9fbd09a8` |
| Audit branch | `arena/p0-4-version-truth-successor` |
| Open PR count | `78` via GitHub REST `/pulls?state=open&per_page=100` |
| Board | schema 2; many historical/expired entries remain; the board is not a current PR disposition ledger |
| Gates owner | the board records the former `ci-gates-steward` as `completed_released`; no current live gates owner was established by this audit |

## 2. Release-metadata defects measured on current main

| Defect | Measured evidence |
|---|---|
| README stale version | `README.md` line 5 `> **Version: v3.12.0**` and line 75 `## Nagar image generation and slideshow (v3.12.0)` while `VERSION` and `pyproject.toml [project].version` are both `3.13.0` |
| Lockstep guard blind to README | `scripts/check_version_lockstep.py` compared only `VERSION`, `pyproject.toml`, and the newest released `CHANGELOG` heading — the README label could drift silently |
| Continuum snapshot stale | `python -m nexus_ai_agent.cli continuum verify` reports `test count mismatch: expected 2924, found 3250` and `environment fingerprint mismatch` |

## 3. Changes delivered by this audit

### Checkpoint 1 — README truth

Corrected both current user-facing `v3.12.0` labels to `v3.13.0`. The historical v3.13.0 sections already present are untouched; only the two stale labels change.

### Checkpoint 2 — executable release guard

`scripts/check_version_lockstep.py` and `tests/unit/test_version_lockstep.py` now enforce the README canonical `**Version:**` label in addition to `VERSION`, `pyproject.toml`, and the newest released `CHANGELOG` heading. An adversarial stale-README fixture proves the guard fails when the label drifts.

### Checkpoint 3 — Continuum refresh

`.nexus/continuum.json` refreshed from a clean checkout with the measured test count and this machine's environment fingerprint. `python -m nexus_ai_agent.cli continuum verify` passes after the snapshot commit.

### Checkpoint 4 — forensic report

This report, indexed in `docs/README.md`.

## 4. Test proof

| Command | Result |
|---|---|
| `python -m pytest -q tests/unit/test_version_lockstep.py tests/unit/test_docs_integrity.py` | passed |
| `python scripts/check_version_lockstep.py` | passed; all release declarations `3.13.0` |
| `python -m nexus_ai_agent.cli continuum verify` | passed after the snapshot commit |
| `python -m ruff check` / `ruff format --check` | passed |

## 5. Current main gaps / real backlog

### NOW

1. Establish a current human-owned gates owner and reconcile the board's historical claims with the 78 live PRs.
2. Produce a machine-generated all-PR disposition matrix from exact base/head SHAs, changed files, checks, reviews, and board ownership.
3. Review #160 for repository-side correctness while keeping production backup status `BLOCKED_EXTERNAL` until restore evidence exists.
4. Resolve the identity-arbitration question (ADR 0008 successor) against the existing canonical identity model before any adoption.

### NEXT

1. Refresh living architecture documents whose `Verified against` SHA is older than current main; do not rewrite dated audit records.
2. Add a reproducible PR-to-board overlap report to the current governance workflow.

### LATER

1. Any broad unified cognitive fabric abstraction.
2. Real production backup/restore evidence requiring credentials and external infrastructure.
3. Branch deletion, PR close/merge, history rewrite, or force-push decisions.

## 6. Production limitations

Repository-side tests, local artifact checks, and CI metadata do not prove production. Missing evidence includes production credentials, external object-store identity/retention, isolated restore, deployment smoke against the exact deployed SHA, and measured RPO/RTO. Status is therefore not `VERIFIED`.

## 7. Final verdict

`HARDENED_BUT_NOT_COMPLETE`

The mainline was not mass-merged or mass-closed. Safe, evidence-backed release-metadata defects were corrected and each completed unit was committed, pushed, remote-verified, and left clean. Remaining architectural and production decisions are explicitly classified rather than fabricated as complete.
