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

## Changed / tested / evidence

Production repair commit: `244a01ce8b2d751e2f35699c82a4c758321691f8`.
Only `storage/db.py` changes production behavior. No existing test was deleted,
weakened or rewritten. The new regression is outside foreign `tests/unit/` leases.

| Gate | Exact source | Result |
|---|---|---|
| New regression before fix | main `2351f099e2f789b80de05c6b382554b5cdcec0f3` | **5 failed, 3 passed** |
| Same regression after fix | `244a01ce8b2d751e2f35699c82a4c758321691f8` | **8 passed** |
| DB / URLs / migrations / backup / board subsystem | same repair SHA | **84 passed** |
| Reintroduce hardcoded session path, independent archive | same repair SHA + explicit mutant | **4 failed, 4 passed**; mutant killed |
| Reintroduce hardcoded schema path, independent archive | same repair SHA + explicit mutant | **1 failed, 7 passed**; mutant killed |
| Restore both changes | independent archive, original source restored | **8 passed** |
| Targeted ruff check + format | same repair SHA | PASS |
| Board overlap check | same repair SHA | PASS; independent live-branch prefix check also performed |
| Full local suite / mypy / CI-equivalent install | this mission | **BLOCKED — deferred to gates owner**, not executed and not called green |
| Exact final branch-head CI | final report commit | Pending at report publication; authoritative SHA/job/result/date snapshot must be posted to the PR after push |

Commands, environment, timestamps, SHA, exit codes and stdout are in
`OWNER_HARDENING_EVIDENCE/{red-main,targeted-runs,mutation-runs}.json` and their
named logs. Local Python is 3.11.2, with a minimal declared-dependency diagnostic
environment, **not** the complete CI `pip install -e '.[dev]'` environment. The
first board-test import failed because numpy was absent; installing declared
numpy/Pillow resolved that environment failure. An initial board-check invocation
used space-separated paths; the CLI requires commas. It was rerun correctly and
passed before push. Neither failed attempt is represented as a product failure.

The two killed mutants prove only the DB default-path guards. They are **not**
proof of the requested async/blocking architecture guard, full graph behavior,
file sandbox containment, or any other mission invariant.

## Database consumer/resolver inventory

Inventory evidence: `db-inventory.txt` and the broader `db-path-search.txt` cover
static source and migrations. This is a path/call-site inventory, not a claim that
every backend or dynamic call path has been executed.

| Consumer / resolver | Effective source | Contract / evidence / remaining issue |
|---|---|---|
| Settings | NEXUS_DB_PATH → DB_PATH → data/app.sqlite; PostgreSQL URL aliases separately | Existing canonical configuration; directory creation is a side effect |
| storage/db.py get_session | explicit SQLite override; else PostgreSQL URL; else Settings.db_path after this fix | Runtime SQLite write + actual bound URL tested; existing PostgreSQL unit routing tests retained |
| storage/db.py create_all_tables | explicit SQLite path or Settings.db_path after this fix | SQLite-only primitive; not a PostgreSQL schema initializer |
| storage/db.py _get_engine, decide_sqlite_bootstrap, resolve_migration_url | supplied path / settings, expanduser | Migration URL matches runtime for absolute, relative, alias, default cases |
| storage/migrations.py + migrations/env.py | resolve_migration_url and settings path | Existing upgrade/downgrade/schema tests pass; no new migration |
| maintenance/backup.py | PostgreSQL resolver, else Path(settings.db_path) | Real SQLite online-copy reads runtime sentinel; **tilde normalization remains inconsistent** |
| bot/app.py and bot/feature_handlers.py | inject settings.db_path into ConversationStore and ReferralEngine | Production callers are explicit; direct constructor defaults still hardcode data/app.sqlite (not repaired) |
| features/conversation_store.py and referral.py | explicit/default SQLite constructor path | Direct no-argument construction still requires resolver reconciliation; not certified as a documented separate DB |
| features/ads, analytics, channel_manager, engagement, games, gamification, moderation, owner_control, personality, viral_engine | synchronous SQLite engine using settings.db_path | Configured SQLite path used; these do not automatically become PostgreSQL consumers when DATABASE_URL is set |
| features/force_join, anonymous_chat | synchronous SQLite helper using settings.db_path | Same backend/normalization caveat; no feature or moderation-policy change |
| features/tools.py ReminderSystem | explicit path or settings.db_path | SQLite-only persistence; unchanged |
| core/async_db.py AsyncDB | constructor-supplied path | Generic injected SQLite utility; no implicit resolver |
| worker.job_queue_db_path + bot/app + CLI + adapters/in_process_job_queue.py | settings.db_path + .jobs.sqlite3 | Existing **separate durable queue**; worker helper is canonical for composition roots; no queue claim taken |
| api/app.get_creative_registry + creative/job_registry.py | creative_temp_dir / creative_jobs.sqlite3 | Existing **separate creative job DB inside cleanup root**; dangerous placement, no protective cleanup policy yet |
| storage/langgraph_checkpoint.py | database_url for PG or settings.checkpoint_path for SQLite | Separate checkpoint store; SQLite adapter and lifecycle store take explicit paths |
| storage/checkpoint_reconciler.lifecycle_db_path | checkpoint path + .lifecycle, with :memory: special case | Separate lifecycle index; not a disposable artifact |
| memory/long_term.py | settings.vector_path or explicit :memory: evaluation store | Separate vector-memory SQLite store |
| features/rag.py | settings.chroma_db_path | Separate Chroma persistence directory; not the application DB |
| PostgreSQL adoption / checkpoint backends | explicit normalized database_url / connection pool | Existing separate backend schema paths; real service proof belongs to exact-SHA CI, not local mocks |

The global “all consumers, one consistent normalized resolver unless explicitly
separate” requirement is **NOT CLOSED** by the two-default repair. In particular,
`~/custom.sqlite` expands in runtime but backup tries the literal tilde path and
fails. Relative paths retain their existing cwd-relative semantics. No production
schema/data was migrated or modified.

## Fresh remaining P0 reproductions

On exact main, `reproduce_remaining.py` uses only a temporary cwd/HOME and
synthetic files. Run with `PYTHONPATH` pointing at a pristine main export and
`PYTHONDONTWRITEBYTECODE=1`; the command and observations are recorded in
`remaining-public-cli.txt` and `remaining-p0.json`.

1. The **real public CLI** `python -m nexus_ai_agent.cli maintenance housekeeping
   --dry-run` exits 0 and prints “nothing was changed”, yet creates `data`,
   `data/cache`, and `models` in an initially empty working directory.
2. `run_housekeeping(dry_run=True)` deletes stale synthetic `old.mp4`,
   `creative_jobs.sqlite3`, and its `-wal`, `-shm`, `-journal` names.
3. With a temporary HOME and `NEXUS_DB_PATH=~/custom.sqlite`, runtime's explicit
   configured SQLite path expands correctly, but `_dump_sqlite` receives the
   literal settings path and raises “SQLite database not found”.

The detailed finding ledger is `OWNER_HARDENING_EVIDENCE/findings.csv`. Filename
filters alone are not accepted as the future DB-protection policy; canonical
paths, sidecars, symlink handling, eligible-only deletion, and race tests remain
required. No outside-lane implementation was copied from PR #90.

## Task-122 reconciliation (fresh, read-only)

The earlier packet remains absent, but the **new** report is now accessible at
PR #89 head `2f0df4f11eada4307d6170cab9cffff027a5b1a3`:
`FORENSIC_REPORT_task122.md`, all sections A–O read. Its source URL and SHA256 are
in `task122-review.json`. GitHub independently shows successful push and PR checks
on this exact current head (24 successful rollup entries = two 12-job runs), not
just on its implementation ancestor `60188f2`.

This reclassifies “exact head CI unavailable” for that **PR**, not for main and
not for this hardening branch. The owner report claims cross-chat/user flood
regressions, 8/8 killed mutations, an async offload AST guard, 12 real command
wirings, and typed failure checks. Those behavioral assertions were not rerun
here and are not silently promoted to accepted closure. Anonymous peer safety is
still explicitly a product decision there. No automatic merge, handler edit,
policy guess or acceptance of fake→real behavior occurred in this lane.

## Coverage, CI, risk and handoff

`file-coverage.csv` inventories tracked files at repair SHA `244a01ce…`, marks
manual review, targeted test execution, DB-search-only coverage and NOT_REVIEWED
honestly, and records content hashes. It is not a whole-repository audit claim.

The final PR must retain separate **Problem / Root cause / Fix / Security / Data /
Execution truth / Tests / exact evidence** sections and disclose inherited release
scope. Use draft status while full gate ownership/closure is unresolved. Published
check results must name the exact final SHA, job, result and date; parent-green,
this report, or a board claim cannot substitute. A post-push PR comment can carry
that immutable SHA snapshot without creating a self-referential report commit.

**Risk:** housekeeping data loss and CLI dry-run mutation remain reproducible on
main; path normalization is not globally unified; shell/files/executor/LLM/
registry/queue/HMAC/packs/architecture/fake-embedding claims still require their
own live reproduction and closure. No release, tag, merge or production-ready
claim is authorized or made. The original checkout must match its opening byte
hashes and Git status at handoff.

**Verdict: NEEDS_FIX** for the overall mission. The SQLite default-selection subset
is **FIXED_TESTED** locally, not a blanket certification.

**Next blocker:** coordinate with the PR #90 housekeeping owner and CLI owner to
close the complete zero-filesystem-mutation dry-run contract, including settings
initialization, without overlapping their leases.

### Reproducer containment correction

A final safety review found `creative_temp_dir` defaults to the shared sandbox
path `/tmp/nexus_creative`, not the temporary cwd. The first CLI probe reported
zero removals, but it still consulted that default root; its evidence is retained
and is **not** described as fully contained. The corrected standalone reproducer
clears inherited credentials/path settings and explicitly selects a temporary
`CREATIVE_TEMP_DIR`. The new regression fixture also directs every directory
created by settings initialization into `tmp_path`. No product assertion was
weakened and no production behavior changed in this correction. Replay evidence
is `remaining-public-cli-contained.txt` and `contained-runs.json`; the original
checkout's bytes and status remain unchanged.
