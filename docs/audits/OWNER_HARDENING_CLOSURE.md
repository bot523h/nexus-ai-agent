# OWNER HARDENING CLOSURE — scoped SQLite default-path repair

## Pre-change decision record (2026-09-26 UTC)

- **Problem:** no-argument `get_session()` and `create_all_tables()` select
  `data/app.sqlite` instead of the configured `NEXUS_DB_PATH`.
- **Evidence:** live main is `2351f099e2f789b80de05c6b382554b5cdcec0f3`;
  `storage/db.py` contains both hardcoded defaults. Settings, migration resolution
  and backup source use `Settings.db_path`. A fresh executable red regression is
  required before editing production code; historical audit observations alone
  are not closure evidence.
- **Root cause:** duplicated fallback defaults bypass the settings resolver.
- **Decision/design:** use the existing settings source in both SQLite default
  branches. Preserve explicit paths, tilde expansion, PostgreSQL priority in
  `get_session`, and the SQLite-only nature of `create_all_tables`. No new DB,
  migration, schema, dependency, backend abstraction, or cache redesign.
- **Rejected:** changing all feature constructors or globally removing directory
  creation from settings; both enlarge the behavioral scope. Copying PR #90 or
  silently editing its claimed housekeeping lane is also rejected.
- **Scope/owner/files:** branch `arena/01a0da13-nexus-ai-agent`; three additive
  file-scoped subclaims in existing board zones cover `storage/db.py`, the new
  `tests/integration/test_owner_db_path_contract.py`, this report and its evidence
  directory. Existing claims, zones and timestamps remain untouched. No takeover.
- **Dependencies:** baseline settings/SQLModel/SQLAlchemy/aiosqlite. Backup remains
  read-only under task-167 ownership. No production data or external cloud used.
- **Tests:** configured/relative/legacy-alias/unset paths; no-argument schema
  creation; explicit override; runtime write → migration URL → SQLite online
  backup read; existing SQLite and PostgreSQL resolver regressions.
- **Acceptance:** new tests demonstrably red on exact main and green after repair;
  targeted subsystem green; final-SHA CI measured, not inherited. Full local gates
  remain deferred to gates owner. This subset cannot certify the whole mission.

## Live ownership and scope boundary

The latest remote snapshot includes PR #90, head
`07106c29f8ff0506ffb43b3b41a9c5de66b03a81`, owning housekeeping dry-run, and PR #89,
head `2f0df4f11eada4307d6170cab9cffff027a5b1a3`, continuing task-122. Neither is
merged into the verified main baseline. Do not infer closure from either board
claim. CLI ownership remains task-181; `get_settings()` creates directories before
CLI housekeeping executes, so local unlink protection alone cannot prove the
CLI's zero-filesystem-change contract. A protected-path policy is still required
for non-dry housekeeping. Full local gates have multiple live claimed owners;
this mission does not select one, modify their leases, or claim gates ownership.

The previous forensic/audit packet is not present in this resumed workspace;
Notion access is unavailable. Historical counts and reports are not fresh proof.
The original `/home/user/nexus-ai-agent` checkout remains read-only, including its
four modified and two untracked files. Work occurs in an independent clone.

## Inherited branch scope (not part of this repair)

Starting head `690483c2e1fdd46b67b785cc30689ccb7515b2d0` already has four commits
above main: `9e8c2df`, `f5453da`, `7809b9e`, `690483c`. They change six paths:
`.agents/board.json`, `.dockerignore`, `README.md`, `README_RELEASE_TRUTH.md`,
`tests/unit/test_container_context.py`, `tests/unit/test_version_lockstep.py`.
Preserved without rewriting, reverting or force-pushing. No new README change.
Any PR on this fixed session branch must disclose that inherited scope and isolate
this repair by commit range; the aggregate diff is not hardening-only.

## Search-10 / decision inputs

1. **GitHub:** current main/branch and open-PR API snapshots.
2. **Code:** db.py defaults, settings aliases, housekeeping, backup, worker.
3. **Import graph:** get_session consumers and DB constructor/call-site inventory.
4. **Architecture:** existing settings + SQLite primitive vs backend selector.
5. **Tests:** DB/bootstrap/migration/backup contracts, new independent regression.
6. **CI:** ci.yml pins install and lint/pytest/parity/PostgreSQL jobs; no gate edits.
7. **Runtime:** synthetic temporary SQLite files only; red then green execution.
8. **Security/data:** prevent split runtime/backup source; preserve explicit backend
   and path overrides; do not use production databases.
9. **Board:** all live branch-owned claims checked for file-prefix overlap; raw
   selected claims archived. Board commands that garbage-collect are not used.
10. **Alternatives:** reuse settings versus second resolver/global config rewrite;
    prefer the existing source with narrow behavior change.

Independent evidence types for this decision: CODE, executable TEST/RUNTIME, and
live GIT/BOARD. CI for the patched SHA is a separate, still-required evidence type.
