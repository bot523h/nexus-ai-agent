"""SQLite authority preflight for asset-library startup recovery."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path


def check_asset_database(database: Path) -> None:
    """Refuse missing files or absent ingest tables before schema initialization.

    SQLite reopens the path, so callers must trust its parent against replacement.
    """
    fd = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("asset library database must be an existing regular file")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assetingest'"
            ).fetchone()
            if table is None:
                raise ValueError("asset library database is missing its ingest authority")
    finally:
        os.close(fd)
