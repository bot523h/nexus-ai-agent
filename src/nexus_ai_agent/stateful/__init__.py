"""Scale-to-zero state tiers (task-163, D-0010 / ADR 0005).

Only *security-relevant* state leaves the scale-to-zero container:

* ``rate_limit`` — the access-guard denial limiter (Redis, fixed window,
  fail-closed at decision time);
* ``presence_pg`` — presence with server-clock TTLs (PostgreSQL);
* ``job_queue_pg`` — the application-owned job queue (PostgreSQL,
  SKIP LOCKED claims + stale-steal).

Selected at the composition roots only (``bot/app.py``, ``cli.py``).
Unset URLs keep the legacy in-memory/SQLite behaviour exactly as before.
"""

from __future__ import annotations

from nexus_ai_agent.stateful.job_queue_pg import JOB_QUEUE_PG_TABLE, PgJobQueue
from nexus_ai_agent.stateful.presence_pg import (
    PRESENCE_TABLE,
    PgPresenceStore,
    build_presence_store,
)
from nexus_ai_agent.stateful.rate_limit import (
    RateLimitBackend,
    RedisRateLimiter,
    RedisRateLimitUnavailable,
    build_rate_limit_backend,
)

__all__ = [
    "JOB_QUEUE_PG_TABLE",
    "PRESENCE_TABLE",
    "PgJobQueue",
    "PgPresenceStore",
    "RedisRateLimiter",
    "RedisRateLimitUnavailable",
    "RateLimitBackend",
    "build_presence_store",
    "build_rate_limit_backend",
]
