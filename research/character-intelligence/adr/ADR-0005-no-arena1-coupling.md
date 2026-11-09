# ADR-0005 — No coupling to Arena 1; isolation by construction

- Status: **Accepted** (verified mechanically)
- Date: 2026-11-09

## Context

This mission runs beside an active Arena 1 line of work (composition runtime, packs,
rendering, job queue, IBPC). Sharing code would be convenient and would create two
failure modes at once: a research prototype that can break production, and a
production branch that cannot move without the prototype.

## Decision

1. The prototype lives entirely in four new paths
   (`research/character-intelligence/`, `experiments/character_capsule/`,
   `tests/character_capsule/`, `scripts/character_capsule/`).
2. It imports **nothing** from `nexus_ai_agent`, including canonical-JSON helpers
   (restated in 40 lines instead).
3. It creates no queue, no CommandBus, no registry, no second asset authority.
4. Arena 1 integration is a **proposal** (`INTEGRATION_CONTRACT_PROPOSAL.md`,
   status DEFERRED), never an edit.
5. `pyproject.toml`, CI and `docs/architecture/adr/` are untouched.

## Consequences

* `git status` over every protected path returns zero rows — checked and reported in
  `ARENA1_COMPATIBILITY_REPORT.md`.
* The experiment package is not installed; tests bootstrap `sys.path` explicitly, so
  the coupling cannot creep in unnoticed.
* Cost: duplicated canonical hashing (deliberate), and the CLI needs the repo root on
  `sys.path`. Both are documented rather than hidden.
* A future integration that wants one canonical implementation can delete
  `experiments/character_capsule/canonical.py`; that is a one-file change.

## Alternatives rejected

| alternative | why rejected |
|---|---|
| import Arena 1 utilities "just for hashing" | couples a prototype to installed code; violates the isolation the mission requires |
| put the prototype in `src/` | would enter the installed package and its guard tests |
| new shared package for both sides | that is a new platform decision, not this mission's mandate |

## Evidence

`ARENA1_COMPATIBILITY_REPORT.md` (zero protected-path changes, 259 guard tests green).
