#!/usr/bin/env python3
"""Run three independent local W5 disk benchmarks; no remote backup is implied."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from nexus_ai_agent.creative.assets.admission import asset_admission_lock
from nexus_ai_agent.storage.encrypted_backup_drill import (
    BackupKeyMaterial,
    run_encrypted_sqlite_backup_restore_drill,
)
from nexus_ai_agent.tools.filesystem_policy import FilesystemIntegrityError, WorkspaceFilesystem


def _elapsed_ms(start: int) -> float:
    return (time.perf_counter_ns() - start) / 1_000_000


def _one_run(root: Path) -> dict[str, float]:
    root.mkdir(mode=0o700)
    admission = root / "admission"
    admission.mkdir(mode=0o700)
    started = time.perf_counter_ns()
    for _ in range(50):
        with asset_admission_lock(admission, exclusive=False):
            pass
    lock_ms = _elapsed_ms(started) / 50

    blobs = root / "blobs"
    blobs.mkdir(mode=0o700)
    fs = WorkspaceFilesystem(blobs)
    payload = bytes(range(256)) * 256  # 64 KiB; measured from a real file
    digest = hashlib.sha256(payload).hexdigest()
    fs.write_bytes("stage", payload)
    started = time.perf_counter_ns()
    if not fs.link_within_if_absent("stage", "published", sha256=digest, size=len(payload)):
        raise RuntimeError("publication unexpectedly deduplicated")
    publish_ms = _elapsed_ms(started)
    if (blobs / "published").read_bytes() != payload:
        raise RuntimeError("publication verification failed")

    # Real disk operations with one synthetic fsync fault after the hard link:
    # time includes rollback, and the target must not survive the failure.
    real_fsync = fs._fsync_dir_fd
    calls = 0

    def fail_once(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected benchmark fsync failure")
        real_fsync(fd)

    started = time.perf_counter_ns()
    with patch.object(fs, "_fsync_dir_fd", side_effect=fail_once):
        try:
            fs.link_within_if_absent("stage", "rollback", sha256=digest, size=len(payload))
        except FilesystemIntegrityError:
            pass
        else:
            raise RuntimeError("rollback fault did not fail")
    rollback_ms = _elapsed_ms(started)
    if (blobs / "rollback").exists() or calls < 2:
        raise RuntimeError("rollback did not remove the published inode")

    db = root / "source.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        conn.executemany("INSERT INTO records (payload) VALUES (?)", [("data" * 64,)] * 100)
    work = root / "backups"
    work.mkdir(mode=0o700)
    key = BackupKeyMaterial.generate()
    previous = run_encrypted_sqlite_backup_restore_drill(db, work, key)
    started = time.perf_counter_ns()
    receipt = run_encrypted_sqlite_backup_restore_drill(db, work, key, retain_generations=1)
    backup_ms = _elapsed_ms(started)
    if receipt.retention_receipt is None or previous.archive_path.parent.exists():
        raise RuntimeError("retention did not prune the previous authenticated generation")
    if not receipt.restored_path.exists():
        raise RuntimeError("restored SQLite image is absent")
    return {
        "admission_shared_lock_per_acquire_ms": lock_ms,
        "atomic_publish_64k_ms": publish_ms,
        "atomic_rollback_64k_injected_fsync_ms": rollback_ms,
        "aesgcm_sqlite_backup_restore_retention_ms": backup_ms,
    }


def _summarize(samples: list[float]) -> dict[str, object]:
    ordered = sorted(samples)
    return {
        "samples_ms": samples,
        "min_ms": min(samples),
        "median_ms": statistics.median(samples),
        "mean_ms": statistics.mean(samples),
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max_ms": max(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-head", required=True)
    args = parser.parse_args()
    if len(args.source_head) != 40 or any(c not in "0123456789abcdef" for c in args.source_head):
        parser.error("--source-head must be a full lowercase git SHA")
    with tempfile.TemporaryDirectory(prefix="nexus_w5_benchmark_") as scratch:
        base = Path(scratch)
        measurements = [_one_run(base / f"run-{i}") for i in range(3)]
    result = {
        "source_head": args.source_head,
        "runs": 3,
        "unit": "milliseconds",
        "p95_method": "nearest-rank; with N=3 p95 is the maximum",
        "truth": "REAL-DISK; rollback uses MOCK fault injection; no remote backup",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "metrics": {
            name: _summarize([run[name] for run in measurements]) for name in measurements[0]
        },
    }
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
