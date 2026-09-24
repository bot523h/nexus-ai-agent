---
status: accepted
date: 2026-09-23
deciders: session arena/01a0cf98 (task-163), owner arbitration 2026-09-24
consulted: D-0010 in docs/DECISION_LOG.md, scripts/agent_board.py, Koyeb scale-to-zero semantics
informed: all agents working on this repository
---

# 0005. Board-Git truth gate + scale-to-zero state tiers

## Context and Problem Statement

Two incident classes were visible in the repository's own history.
(a) **Board-Git truth drift:** the coordination board (`.agents/board.json`)
lags reality — on 2026-09-23, `task-145` sat `active` after its branch had
already merged (PR#52), and five open PRs (PR#33, PR#56–59) had no live claim
at all. An invisible claim is the setup for split-brain: two agents rewriting
the same file while the shared mirror lies. `praudit` (task-141) was
diagnostic-only; nothing made drift *red*. (b) **Scale-to-zero amnesia:** a
Koyeb web service is killed after ~30 s of idleness, and the state that kept
security invariants alive lived inside that container — the access-guard rate
limiter (process memory), presence (process memory), the job queue (ephemeral
disk). A spammer who paced one request per container-lifetime window paid
nothing: a denial-of-service vector and a bypass by patience.

The full decision record, rejected alternatives, and the pre-push referee
arbitration live in **D-0010** (`docs/DECISION_LOG.md`); this ADR fixes the
architecture and its confirmation.

## Decision Drivers

- The board is a **cache**; `git ls-remote` + the GitHub PR matrix is the
  truth. Drift must be a *blocking* CI failure, and must be re-verified even
  with no pushes (15-minute schedule sweep).
- Only **security-relevant** state leaves the scale-to-zero container.
  Membership and other re-derivable booleans stay cache (re-derivable from
  the Telegram Bot API on wake).
- A security surface fails **closed** at decision time (counter unreachable
  ⇒ deny) and **soft** at composition time (Redis down at boot ⇒ in-process
  fallback, bot stays up).
- Unset URLs must keep the legacy in-memory/SQLite behaviour **exactly** as
  before (zero regression for the SQLite-only deployment).
- No hidden migration: new PostgreSQL tables join the `adopt-pg` head-state
  set explicitly (the `nexus_checkpoint_lifecycle` precedent).

## Considered Options

1. Reconcile the board on demand only (no schedule) — rejected: drift
   between pushes goes red only when someone pushes.
2. A generic "durable everything" rewrite — rejected: re-derivable state
   persisted is cache, not durable state (D-0010, rejected alternative 2).
3. Sliding-window Redis limiter (`ZRANGEBYSCORE`) — rejected: per-request
   cost + `ZREMRANGEBYSCORE` race; a fixed window is exactly right for a
   per-user message-rate policy.
4. **Two mechanisms for truth, three tiers for state** — chosen (below).

## Decision Outcome

Chosen option: **4**.

*Truth gate.* `scripts/board_reconcile.py` (stdlib + `git`/`gh`, **write-free**
— it never mutates the board) reports `MERGED_STILL_OPEN`, `BRANCH_GONE`,
`UNCLAIMED_OPEN_PR`, `INVISIBLE_FILE` as **blocking** and `LEASE_EXPIRED`,
`SCOPE_TRUNCATED` (100-file cap), `OVERLAP` as warnings. The `board-reconcile`
CI job runs `--check` on every push/PR and on a `*/15` schedule sweep.
`.agents/board.json` is exempt from file checks as the coordination medium.
The referee (`agent_board.py check`) keeps pre-push zone arbitration; the
gate keeps continuous truth verification.

*State tiers.* `src/nexus_ai_agent/stateful/`, selected at the composition
roots only (`bot/app.py`, `cli.py`):

- **Rate limit (Redis).** One atomic Lua step — `INCR` + `PEXPIRE` on a
  fixed-window key `nexus:rl:{user}:{floor(now/period)}` — so a kill cannot
  reset the counter and no key exists without a TTL. Fail-closed at decision
  time, fail-soft at composition time.
- **Presence (PostgreSQL).** TTLs set and checked against the *server* clock
  (`now() + make_interval(...)` / `online_until > now()`): a container waking
  40 minutes later reads presence the way the database says it is.
- **Job queue (PostgreSQL).** `nexus_job_queue_pg` mirrors the SQLite queue
  plus `owner_id`/`locked_at`; claims via `SELECT ... FOR UPDATE SKIP LOCKED`
  (no double-execution), stale-steal after a 900 s processing timeout (a
  killed container's rows return to `pending`).

The migration `a41c9e2b7f63` is PostgreSQL-only (SQLite no-op), so the shared
Alembic chain stays ORM-exact on SQLite and `alembic check` sees zero drift
there. The two tables join the Postgres head-state set in
`storage/adopt_pg.py` (the treatment `nexus_checkpoint_lifecycle` received):
a stamped database must contain them; a legacy database missing them fails
adoption explicitly — no implicit repair.

## Confirmation

- `tests/unit/test_board_reconciler.py` — the reconciler's verdict matrix
  (blocking vs warning, coordination-file exemption, directory fences,
  100-file truncation, overlap, CLI exit codes, gh file-object parsing).
- `tests/unit/test_rate_limiter_distributed.py`,
  `tests/unit/test_stateful_presence.py`,
  `tests/unit/test_stateful_job_queue.py`,
  `tests/unit/test_rate_limit_backend_seam.py` — the tier contracts against
  injected fakes (fixed-window key/TTL, server-clock SQL, SKIP LOCKED
  claim/steal single-winner, fail-closed seam).
- `tests/integration/test_rate_limiter_redis.py` — the kill proof (fork →
  SIGKILL → the counter survives), CI `distributed-state` job (redis:7).
- `tests/integration/test_presence_pg.py`,
  `tests/integration/test_job_queue_pg.py` — cross-instance + 2 s steal on
  real PostgreSQL, CI `migrate-postgres` job (pgvector/pg16).
- `tests/unit/test_migrations.py::test_stateful_migration_is_isolated_and_postgres_only`
  + the chain-head pins (`test_core_fingerprint`, `test_ai_memory_consent`,
  `test_migrate_cli`, `test_migrate_adoption`, `test_migrate_race_condition`)
  — lockstep at the new head `a41c9e2b7f63`.
- Live gate: `python scripts/board_reconcile.py --check` → `0 blocking`
  (4 `OVERLAP` warnings, true statements about PR#33's 52-file fence).
