# NEXUS engineering skills (repo-local)

Version-controlled, evidence-backed skills that future OpenHands sessions
discover and reuse. Each skill lives in its own directory with a `SKILL.md`
(YAML frontmatter: `name`, `description`) and optional `references/`.

This directory is the supported repo-local skill location for OpenHands
(`.agents/skills/`). Skills are additive knowledge; they never override the
coordination rules in `AGENTS.md` / `.agents/board.json`.

## Index

| Skill | Activation | Status | Latest evidence |
|---|---|---|---|
| `python-first-use-db-init-race` | intermittent `database schema has changed` (SQLITE_SCHEMA) / `database is locked` (SQLITE_BUSY) on a fresh DB; or new tables triggering flakes | `VERIFIED` | commit `4a8ac85`: 3 regression tests, 2 mutation probes killed, 15/15 repeated runs green |

## Lifecycle

`CANDIDATE → RESEARCHED → IMPLEMENTED → TESTED → VERIFIED → MAINTAINED`

A skill is promoted to `VERIFIED` only when it has reproducible evidence
(commands + observed results + a mutation or negative test that turns RED when
the guard is removed). A skill that fails validation is corrected or explicitly
marked `UNVERIFIED` — never silently retained.

## Adding a skill

1. Research the authoritative source; record it under **Provenance** with a date.
2. Create `.agents/skills/<name>/SKILL.md` with the 11 required sections:
   purpose, activation, required context, procedure, safety boundaries,
   validation, success criteria, failure handling, known limitations,
   provenance, maintenance.
3. Validate it against a real problem; keep the exact evidence.
4. Add a row to the table above with its status and latest evidence.
