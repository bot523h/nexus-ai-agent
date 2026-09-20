"""Housekeeping (Phase 5): stale creative temp files + R2 backup retention.

Idempotent: a second run right after the first finds nothing to do.
Safe without R2: when the blob tier is not configured only the local temp
cleanup runs (unlike backups, housekeeping must stay green).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.providers.r2 import R2Provider

from .backup import BLOB_BACKUP_PREFIX, STAMP_FORMAT

log = get_logger(__name__)


def _clean_temp_dir(temp_dir: Path, cutoff: dt.datetime) -> list[str]:
    """Return (and delete) files under ``temp_dir`` last modified before cutoff."""
    removed: list[str] = []
    if not temp_dir.exists():
        return removed
    for candidate in sorted(temp_dir.rglob("*")):
        if not candidate.is_file():
            continue
        mtime = dt.datetime.fromtimestamp(candidate.stat().st_mtime, dt.timezone.utc)
        if mtime < cutoff:
            candidate.unlink(missing_ok=True)
            removed.append(str(candidate))
    return removed


def _parse_backup_stamp(key: str) -> dt.datetime | None:
    """Extract ``YYYYMMDD-HHMMSS`` from ``backups/db/<stamp>/<file>`` keys.

    Keys that do not match the layout are never pruned (safe default).
    """
    parts = key.split("/")
    if len(parts) != 4 or parts[0] != "backups" or parts[1] != "db":
        return None
    try:
        return dt.datetime.strptime(parts[2], STAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def run_housekeeping(
    *,
    settings: Settings,
    dry_run: bool = False,
    temp_max_age_hours: int = 48,
    backup_retention_days: int = 30,
) -> dict[str, Any]:
    """Clean stale creative temp files and prune old R2 database backups."""
    now = dt.datetime.now(dt.timezone.utc)
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "temp_files_removed": [],
        "backups_deleted": [],
        "r2_skipped_reason": None,
    }

    summary["temp_files_removed"] = _clean_temp_dir(
        Path(settings.creative_temp_dir), now - dt.timedelta(hours=temp_max_age_hours)
    )

    provider = R2Provider.from_settings(settings)
    if not provider.is_configured():
        summary["r2_skipped_reason"] = "R2 not configured — local cleanup only"
        log.info("maintenance_housekeeping_no_r2")
        return summary

    cutoff = now - dt.timedelta(days=backup_retention_days)
    expired: list[str] = []
    for key in asyncio.run(provider.list_files(prefix=f"{BLOB_BACKUP_PREFIX}/")):
        stamp = _parse_backup_stamp(key)
        if stamp is not None and stamp < cutoff:
            expired.append(key)

    if not expired:
        log.info("maintenance_housekeeping_noop", backups_expired=0)
        return summary

    if dry_run:
        summary["backups_deleted"] = expired
        summary["r2_skipped_reason"] = "dry-run: nothing deleted"
        return summary

    asyncio.run(provider.delete_objects(keys=expired))
    summary["backups_deleted"] = expired
    log.info("maintenance_housekeeping_ok", backups_deleted=len(expired))
    return summary
