"""Core schema fingerprint — Alembic head vs local manifest (O1).

Scope-qualified safety contract (S6):

* LangGraph schema mismatch (golden fingerprint) disables cleanup.
* Core schema mismatch is *warning only* — it is reported in the manifest
  and in the log, but it never disables the lifecycle or the reconciler.

The database head is read from ``alembic_version`` with a plain SELECT —
no migration is ever run (R7).  The manifest head is the head of the local
``migrations/versions`` chain, walked read-only.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

CORE_ALGORITHM = "core-v1"

MATCH = "match"
WARNING_DB_HEAD_MISSING = "warning:db_head_missing"
WARNING_MANIFEST_UNAVAILABLE = "warning:manifest_unavailable"


def core_schema_fingerprint(connection: sqlite3.Connection) -> str | None:
    """Read the core migration head from ``alembic_version`` (no migration run).

    Returns ``None`` when the table or row is absent (e.g. a fresh
    checkpoint-only database) or when the connection is broken.  A
    connection error is NOT drift: it yields ``None`` like an absent head,
    and callers surface it as a warning, never as a mismatch.
    """
    try:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone()
            is None
        ):
            return None
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


def core_manifest_head(migrations_dir: Path | None = None) -> str | None:
    """Head of the local Alembic chain, walked read-only (no migration run).

    Returns ``None`` when the chain cannot be determined or has more than
    one head (both fail safe: no manifest => no comparison).
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo = migrations_dir or Path(__file__).resolve().parents[3] / "migrations"
    try:
        config = Config()
        config.set_main_option("script_location", str(repo))
        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception:
        return None
    return heads[0] if len(heads) == 1 else None


def compare_core_schema(db_head: str | None, manifest_head: str | None) -> str:
    """Compare the two heads; every mismatch is a *warning*, never a disable."""
    if manifest_head is None:
        return WARNING_MANIFEST_UNAVAILABLE
    if db_head is None:
        return WARNING_DB_HEAD_MISSING
    if db_head != manifest_head:
        return f"warning:db_head_mismatch:db={db_head},manifest={manifest_head}"
    return MATCH


def evaluate_core_head(db_head: str | None, manifest_head: str | None) -> str:
    """Compare + log (manifest/log only — never disables anything)."""
    status = compare_core_schema(db_head, manifest_head)
    if status != MATCH:
        log.warning(
            "core schema mismatch (warning only; nothing disabled)",
            extra={"event": "core_schema_mismatch", "status": status, "db_head": db_head},
        )
    return status
