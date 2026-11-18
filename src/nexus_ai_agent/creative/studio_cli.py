"""Local studio workspace operations: ``init`` and ``doctor`` (no CLI framework here).

The Typer wrapper lives in :mod:`nexus_ai_agent.creative.studio_typer` so this
module stays importable without the CLI dependency. ``serve`` is a thin wrapper
over uvicorn and is registered there too.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: Directory layout created by ``studio init``, relative to the workspace root.
WORKSPACE_DIRS = ("projects", "assets", "artifacts", "queue")
_DIR_MODE = 0o700


class StudioCliError(RuntimeError):
    """A refusal while preparing the workspace (e.g. a symlink where a dir should be)."""


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.healthy else 1


def studio_init(workspace: Path) -> list[Path]:
    """Create the workspace layout with owner-only directories. Idempotent.

    Refuses if a required path already exists as a symlink or a non-directory,
    so ``init`` never follows or repurposes something it did not create.
    """
    root = Path(workspace)
    created: list[Path] = []
    root.mkdir(parents=True, exist_ok=True)
    if stat.S_ISLNK(os.lstat(root).st_mode):
        raise StudioCliError("workspace root is a symlink")
    for name in WORKSPACE_DIRS:
        target = root / name
        if target.is_symlink():
            raise StudioCliError(f"{name} is a symlink; refusing to use it")
        if target.exists() and not target.is_dir():
            raise StudioCliError(f"{name} exists and is not a directory")
        if not target.exists():
            target.mkdir(mode=_DIR_MODE)
            created.append(target)
        os.chmod(target, _DIR_MODE)
    return created


def _binary_ok(binary: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "-version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}"
    first = completed.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    return completed.returncode == 0, first[0] if first else f"rc={completed.returncode}"


def _atomic_roundtrip(root: Path) -> tuple[bool, str]:
    """Write, fsync, replace and read back a probe file inside the workspace."""
    payload = b"nexus-studio-doctor"
    try:
        fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".doctor.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        final = root / ".doctor.probe"
        os.replace(tmp_name, final)
        ok = final.read_bytes() == payload
        final.unlink(missing_ok=True)
        return ok, "atomic write + replace + read-back"
    except OSError as exc:
        return False, type(exc).__name__


def _sqlite_roundtrip(root: Path) -> tuple[bool, str]:
    """Create, write and read a scratch SQLite database in the queue directory."""
    db = root / "queue" / ".doctor.sqlite3"
    try:
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS doctor_probe (v TEXT NOT NULL)")
            conn.execute("DELETE FROM doctor_probe")
            conn.execute("INSERT INTO doctor_probe (v) VALUES ('ok')")
            conn.commit()
            row = conn.execute("SELECT v FROM doctor_probe").fetchone()
        finally:
            conn.close()
        return row == ("ok",), "sqlite write + read"
    except sqlite3.Error as exc:
        return False, type(exc).__name__
    finally:
        db.unlink(missing_ok=True)


def _compiler_ok() -> tuple[bool, str]:
    try:
        from nexus_ai_agent.creative.storyboard_compiler import (
            compile_storyboard,
            verify_manifest_integrity,
        )

        manifest = compile_storyboard("INT. ROOM - DAY\nA: Hello.\n")
        ok = verify_manifest_integrity(manifest)
        return bool(ok), f"compiled {manifest.total_shots} shot(s), integrity={ok}"
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return False, type(exc).__name__


def studio_doctor(
    workspace: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> DoctorReport:
    """Probe the real environment. Exit code 0 only when every check passes."""
    root = Path(workspace)
    checks: list[DoctorCheck] = []
    for name, binary in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        ok, detail = _binary_ok(binary)
        checks.append(DoctorCheck(name=f"{name}_runs", ok=ok, detail=detail))
    layout_ok = root.is_dir() and all(
        (root / d).is_dir() and not (root / d).is_symlink() for d in WORKSPACE_DIRS
    )
    checks.append(
        DoctorCheck(
            name="workspace_layout",
            ok=layout_ok,
            detail="run `studio init` first" if not layout_ok else "all directories present",
        )
    )
    if layout_ok:
        ok, detail = _atomic_roundtrip(root)
        checks.append(DoctorCheck(name="atomic_write", ok=ok, detail=detail))
        ok, detail = _sqlite_roundtrip(root)
        checks.append(DoctorCheck(name="queue_sqlite", ok=ok, detail=detail))
    else:
        checks.append(DoctorCheck(name="atomic_write", ok=False, detail="skipped: no layout"))
        checks.append(DoctorCheck(name="queue_sqlite", ok=False, detail="skipped: no layout"))
    ok, detail = _compiler_ok()
    checks.append(DoctorCheck(name="storyboard_compiler", ok=ok, detail=detail))
    return DoctorReport(checks=tuple(checks))


__all__ = [
    "WORKSPACE_DIRS",
    "DoctorCheck",
    "DoctorReport",
    "StudioCliError",
    "studio_doctor",
    "studio_init",
]
