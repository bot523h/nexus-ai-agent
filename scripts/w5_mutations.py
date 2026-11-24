#!/usr/bin/env python3
"""Run deterministic W5 security mutations with a strict pytest kill oracle.

Mutations run serially against this checkout under a cooperative process lock.
Each original file is restored and byte-verified even on interrupts. Run on a
quiescent checkout: non-cooperating editors are not fenced by this lock.
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import importlib
import importlib.util
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src/nexus_ai_agent"
HARVEST = "tests/unit/test_w5_harvested_capabilities.py"
DRILL = "tests/unit/test_w5_encrypted_backup_drill.py"
BATCH_A = "tests/unit/test_w5_batch_a_boundaries.py"
BATCH_B = "tests/unit/test_w5_batch_b_boundaries.py"
FS = "tests/unit/test_w5_filesystem_boundaries.py"
RESTORE = "tests/unit/test_w5_restore_boundaries.py"
BAD_OUTPUT = (
    "SyntaxError",
    "IndentationError",
    "ImportError",
    "ModuleNotFoundError",
    "collected 0 items",
    "no tests ran",
    "INTERNALERROR",
)


@dataclass(frozen=True)
class Mutant:
    name: str
    path: Path
    old: str
    new: str
    test: str
    expected: str
    count: int = 1


def mutant(
    name: str, path: str, old: str, new: str, file: str, test: str, count: int = 1
) -> Mutant:
    node = f"{file}::{test}"
    return Mutant(name, SRC / path, old, new, node, f"FAILED {node}", count)


MUTANTS = (
    mutant(
        "W5-M01",
        "creative/assets/admission.py",
        '(require_exclusive and self.mode != "exclusive")',
        "False",
        HARVEST,
        "test_admission_lease_reuse_requires_active_same_root_and_mode",
    ),
    mutant(
        "W5-M02",
        "creative/assets/admission.py",
        "(current.st_dev, current.st_ino) != self.inode",
        "False",
        HARVEST,
        "test_admission_lease_detects_replaced_lock_inode",
    ),
    mutant(
        "W5-M03",
        "creative/assets/blob_store.py",
        "admission_lease.validate_for(self._fs.root, require_exclusive=True)",
        "admission_lease.validate_for(self._fs.root, require_exclusive=False)",
        HARVEST,
        "test_cleanup_requires_live_exclusive_lease_and_explicit_active_set",
    ),
    mutant(
        "W5-M04",
        "creative/assets/ingestion.py",
        "if (measured_sha, measured_size) != (content_sha256, size_bytes):",
        "if (measured_sha, measured_size) == (content_sha256, size_bytes):",
        BATCH_B,
        "test_publish_rejects_invalid_publication_before_metadata",
    ),
    mutant(
        "W5-M05",
        "creative/assets/service.py",
        "async with self._blob_store.admission():\n            return await self._ingest_admitted(",
        "if True:\n            return await self._ingest_admitted(",
        "tests/integration/test_asset_library_recovery.py",
        "test_ingest_transaction_fences_other_process_until_sigkill",
    ),
    mutant(
        "W5-M06",
        "storage/asset_startup.py",
        "if not stat.S_ISREG(os.fstat(fd).st_mode):",
        "if False:",
        "tests/unit/test_w5_admission_boundaries.py",
        "test_startup_rejects_directory_as_database",
    ),
    mutant(
        "W5-M07",
        "tools/filesystem_policy.py",
        "os.unlink(dest_name, dir_fd=dest_dir_fd)",
        "pass",
        HARVEST,
        "test_corrupted_candidate_rolls_back_only_its_own_inode",
    ),
    mutant(
        "W5-M08",
        "tools/filesystem_policy.py",
        "finally:\n                os.close(fd)\n\n    def iter_bytes",
        "finally:\n                pass\n\n    def iter_bytes",
        FS,
        "test_file_fstat_failure_is_typed_and_descriptor_is_released",
    ),
    mutant(
        "W5-M09",
        "storage/encrypted_backup_drill.py",
        "_canonical(aad)",
        '_canonical({k: v for k, v in aad.items() if k != "created_at_utc"})',
        DRILL,
        "test_forged_creation_timestamp_is_rejected_and_does_not_displace_valid",
        2,
    ),
    mutant(
        "W5-M10",
        "storage/encrypted_backup_drill.py",
        "except (ValueError, TypeError, OSError, InvalidTag, KeyError):\n        pass",
        (
            'except InvalidTag:\n        return b"", None\n'
            "    except (ValueError, TypeError, OSError, KeyError):\n        pass"
        ),
        DRILL,
        "test_authentication_rejects_every_mutated_aad_field",
    ),
    mutant(
        "W5-M11",
        "storage/encrypted_backup_drill.py",
        "require_trusted_key_source: bool = True,\n    retain_generations:",
        "require_trusted_key_source: bool = False,\n    retain_generations:",
        DRILL,
        "test_raw_key_requires_explicit_legacy_opt_in",
    ),
    mutant(
        "W5-M12",
        "storage/encrypted_backup_drill.py",
        (
            "except (OSError, ValueError):\n                    corrupt.append(name)"
            "\n            valid.sort"
        ),
        "except (OSError, ValueError):\n                    pass\n            valid.sort",
        DRILL,
        "test_retention_corrupt_newer_cannot_displace_valid_old",
    ),
    mutant(
        "W5-M13",
        "storage/encrypted_backup_drill.py",
        "fcntl.flock(reader_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
        "pass",
        BATCH_A,
        "test_retention_preserves_generation_if_reader_arrives_after_validation",
    ),
    mutant(
        "W5-M14",
        "maintenance/backup.py",
        "safe_url, password = split_dsn_password(database_url)",
        "safe_url, password = database_url, None",
        "tests/unit/test_maintenance_backup.py",
        "test_pg_dump_credentials_never_reach_argv",
    ),
    mutant(
        "W5-M15",
        "maintenance/restore.py",
        "_fetch_r2_artifact(provider, key, report, temp_dir=staged)",
        (
            "_fetch_r2_artifact(provider, key, report, "
            + 'temp_dir=Path(tempfile.mkdtemp(prefix="mutation_fetch_")))'
        ),
        RESTORE,
        "test_remote_restore_success_cleans_download_scratch",
    ),
)


def _purge_bytecode_cache(source_path: Path) -> None:
    """Remove this source's CPython cache variants without touching other modules."""
    cache = Path(importlib.util.cache_from_source(str(source_path)))
    cache.unlink(missing_ok=True)
    directory = source_path.parent / "__pycache__"
    if directory.is_dir():
        for path in directory.glob(f"{source_path.stem}.*.pyc"):
            path.unlink(missing_ok=True)
    importlib.invalidate_caches()


def classify(returncode: int, output: str, expected: str) -> str:
    if any(marker in output for marker in BAD_OUTPUT):
        return "INVALID"
    if returncode == 0:
        return "SURVIVED"
    if returncode != 1:
        return "INVALID"
    return "KILLED" if expected in output else "INVALID"


def _pytest(node: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", node],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def run_mutant(item: Mutant) -> tuple[str, str]:
    original = item.path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    source = original.decode("utf-8")
    if source.count(item.old) != item.count:
        return "INVALID", f"anchor count {source.count(item.old)} != {item.count}"
    changed = source.replace(item.old, item.new)
    try:
        ast.parse(changed, filename=str(item.path))
    except SyntaxError as exc:
        return "INVALID", f"mutant does not parse: {exc}"
    try:
        item.path.write_bytes(changed.encode("utf-8"))
        _purge_bytecode_cache(item.path)
        try:
            proc = _pytest(item.test)
        except subprocess.TimeoutExpired:
            return "INVALID", "test timed out"
        output = proc.stdout + "\n" + proc.stderr
        verdict = classify(proc.returncode, output, item.expected)
        return verdict, f"exit={proc.returncode}" if verdict == "KILLED" else output[-3000:]
    finally:
        item.path.write_bytes(original)
        _purge_bytecode_cache(item.path)
        restored = item.path.read_bytes()
        if restored != original or hashlib.sha256(restored).hexdigest() != digest:
            raise RuntimeError(f"source restoration failed: {item.path}")


def main() -> int:
    lock_path = Path("/tmp/nexus-w5-mutation-campaign.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            print("FATAL: unsafe mutation campaign lock", file=sys.stderr)
            return 2
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("FATAL: another W5 campaign is running", file=sys.stderr)
            return 2
        for path in {item.path for item in MUTANTS}:
            _purge_bytecode_cache(path)
        for node in dict.fromkeys(item.test for item in MUTANTS):
            try:
                baseline = _pytest(node)
            except subprocess.TimeoutExpired:
                print(f"FATAL baseline timed out: {node}", file=sys.stderr)
                return 2
            if baseline.returncode != 0:
                print(
                    f"FATAL baseline failed: {node}\n{baseline.stdout}\n{baseline.stderr}",
                    file=sys.stderr,
                )
                return 2
        counts = {"KILLED": 0, "SURVIVED": 0, "INVALID": 0}
        for item in MUTANTS:
            verdict, detail = run_mutant(item)
            counts[verdict] += 1
            print(f"{item.name} {verdict} {detail}", flush=True)
        for node in dict.fromkeys(item.test for item in MUTANTS):
            try:
                restored_baseline = _pytest(node)
            except subprocess.TimeoutExpired:
                print(f"FATAL restored baseline timed out: {node}", file=sys.stderr)
                return 2
            if restored_baseline.returncode != 0:
                print(
                    f"FATAL restored baseline failed: {node}\n{restored_baseline.stdout}",
                    file=sys.stderr,
                )
                return 2
        print(
            f"SUMMARY total={len(MUTANTS)} killed={counts['KILLED']} "
            f"survived={counts['SURVIVED']} invalid={counts['INVALID']} not_run=0"
        )
        return 0 if counts["KILLED"] == len(MUTANTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
