"""Run only with PYTHONPATH set to a pristine main export; synthetic data only."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from nexus_ai_agent.config.settings import Settings, get_settings
from nexus_ai_agent.maintenance.backup import _dump_sqlite
from nexus_ai_agent.maintenance.housekeeping import run_housekeeping
from nexus_ai_agent.storage import db


def main() -> None:
    observations: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        os.chdir(root)
        for key in ("NEXUS_DB_PATH", "DB_PATH", "NEXUS_DATABASE_URL", "DATABASE_URL"):
            os.environ.pop(key, None)
        get_settings.cache_clear()
        command = [
            sys.executable,
            "-m",
            "nexus_ai_agent.cli",
            "maintenance",
            "housekeeping",
            "--dry-run",
        ]
        before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        process = subprocess.run(command, capture_output=True, text=True, check=True)
        after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        observations["public_cli_dry_run"] = {
            "command": command,
            "before": before,
            "after": after,
            "stdout": process.stdout,
            "stderr": process.stderr,
            "exit_code": process.returncode,
        }
        creative = root / "creative"
        creative.mkdir()
        for name in (
            "old.mp4",
            "creative_jobs.sqlite3",
            "creative_jobs.sqlite3-wal",
            "creative_jobs.sqlite3-shm",
            "creative_jobs.sqlite3-journal",
        ):
            path = creative / name
            path.write_bytes(b"synthetic, not a production database")
            os.utime(path, (1, 1))
        summary = run_housekeeping(settings=Settings(creative_temp_dir=str(creative)), dry_run=True)
        observations["dry_run_deletion"] = {
            "summary": summary,
            "remaining": sorted(p.name for p in creative.iterdir()),
        }
        os.environ["HOME"] = directory
        os.environ["NEXUS_DB_PATH"] = "~/custom.sqlite"
        get_settings.cache_clear()

        async def explicit_runtime_path() -> str:
            # Explicit path bypasses the separate default-selection defect on main.
            async with db.get_session(get_settings().db_path) as session:
                assert session.bind is not None
                actual = str(session.bind.url.database)
            await db._engine.dispose()
            db._engine = None
            db._session_factory = None
            return actual

        actual = asyncio.run(explicit_runtime_path())
        try:
            _dump_sqlite(Path(get_settings().db_path), root / "online-backup.sqlite")
            error = None
        except RuntimeError as exc:
            error = str(exc)
        observations["tilde_path"] = {
            "configured": get_settings().db_path,
            "runtime": actual,
            "backup_error": error,
        }
    print(
        json.dumps(
            {
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "python": sys.version,
                "source": db.__file__,
                "results": observations,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
