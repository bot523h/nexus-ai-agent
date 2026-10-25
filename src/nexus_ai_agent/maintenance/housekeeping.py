"""Fail-safe housekeeping for temporary files and backup retention.

The command has an explicit simulation contract: ``dry_run=True`` performs
read-only discovery only. It must not unlink, rename, write, truncate, or
call a remote delete operation. Local deletion uses the same descriptor-based
filesystem boundary as the file tools so a symlink or parent-directory swap
cannot redirect cleanup outside the configured temporary root.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.providers.r2 import R2Provider
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

from .backup import BLOB_BACKUP_PREFIX, STAMP_FORMAT

log = get_logger(__name__)


def _clean_temp_dir(
    temp_dir: Path,
    cutoff: dt.datetime,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Discover stale regular files and optionally unlink them safely.

    Symlinks are never followed or deleted. In simulation mode this function
    only reports candidates; even the local filesystem mutation is skipped.
    """
    if not temp_dir.exists() or temp_dir.is_symlink():
        return []

    workspace = WorkspaceFilesystem(temp_dir)
    candidates: list[tuple[Path, str]] = []
    for candidate in workspace.iter_files():
        try:
            if candidate.is_symlink():
                continue
            mtime = dt.datetime.fromtimestamp(candidate.stat().st_mtime, dt.timezone.utc)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        relative_path = candidate.relative_to(workspace.root)
        relative_name = "/".join(relative_path.parts)
        candidates.append((candidate, relative_name))

    selected: list[str] = []
    for candidate, relative in sorted(candidates, key=lambda item: str(item[0])):
        if dry_run:
            selected.append(str(candidate))
            continue
        try:
            workspace.unlink(relative)
        except (OSError, ValueError):
            # A concurrent deletion is harmless. A boundary refusal is not a
            # success, so retain visibility by not claiming this path removed.
            continue
        selected.append(str(candidate))
    return selected


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
    """Clean stale local files and prune old R2 database backups.

    ``*_planned`` contains the read-only selection. ``*_removed``/
    ``*_deleted`` contains only mutations that actually completed; therefore a
    dry run cannot be misreported as successful deletion.
    """
    if temp_max_age_hours < 0:
        raise ValueError("temp_max_age_hours must be non-negative")
    if backup_retention_days < 0:
        raise ValueError("backup_retention_days must be non-negative")

    now = dt.datetime.now(dt.timezone.utc)
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "temp_files_planned": [],
        "temp_files_removed": [],
        "backups_planned": [],
        "backups_deleted": [],
        "r2_skipped_reason": None,
    }

    temp_files = _clean_temp_dir(
        Path(settings.creative_temp_dir),
        now - dt.timedelta(hours=temp_max_age_hours),
        dry_run=dry_run,
    )
    summary["temp_files_planned"] = temp_files
    if not dry_run:
        summary["temp_files_removed"] = temp_files

    provider = R2Provider.from_settings(settings)
    if not provider.is_configured():
        summary["r2_skipped_reason"] = "R2 not configured — local cleanup only"
        log.info("maintenance_housekeeping_no_r2", dry_run=dry_run)
        return summary

    cutoff = now - dt.timedelta(days=backup_retention_days)
    expired: list[str] = []
    for key in asyncio.run(provider.list_files(prefix=f"{BLOB_BACKUP_PREFIX}/")):
        stamp = _parse_backup_stamp(key)
        if stamp is not None and stamp < cutoff:
            expired.append(key)

    summary["backups_planned"] = expired
    if not expired:
        log.info("maintenance_housekeeping_noop", backups_expired=0, dry_run=dry_run)
        return summary

    if dry_run:
        summary["r2_skipped_reason"] = "dry-run: nothing deleted"
        return summary

    asyncio.run(provider.delete_objects(keys=expired))
    summary["backups_deleted"] = expired
    log.info("maintenance_housekeeping_ok", backups_deleted=len(expired))
    return summary
