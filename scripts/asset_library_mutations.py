#!/usr/bin/env python3
"""Adversarial mutation harness for the Nagar Asset Library V1 (task-266).

Evidence rule: an invariant that has never been attacked is not evidence.  Each
mutation below rewrites one shipped security/correctness guard, re-runs the
asset-library suites, and requires them to turn RED.  Every source file is
restored afterwards -- including on failure -- and the final run must be GREEN
again.

The guards attacked:

* containment (absolute-path rejection);
* NUL-byte rejection;
* the blob-key shape check (a digest must be 64 lowercase hex characters);
* content-addressed dedup verification (reused bytes must match the key);
* open()-time re-verification (a served blob must hash back to its key);
* the bounded-read size limit.

Usage::

    python scripts/asset_library_mutations.py          # run every mutation
    python scripts/asset_library_mutations.py --list

Exit code 0 means "N/N mutants killed".
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped files byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FS_POLICY = ROOT / "src" / "nexus_ai_agent" / "tools" / "filesystem_policy.py"
BLOB_STORE = ROOT / "src" / "nexus_ai_agent" / "creative" / "assets" / "blob_store.py"
MODELS = ROOT / "src" / "nexus_ai_agent" / "storage" / "models.py"
DB = ROOT / "src" / "nexus_ai_agent" / "storage" / "db.py"
SERVICE = ROOT / "src" / "nexus_ai_agent" / "creative" / "assets" / "service.py"
TESTS = (
    "tests/unit/test_asset_library_blob_store.py",
    "tests/integration/test_asset_library_filesystem_security.py",
    "tests/integration/test_asset_library_persistence.py",
    "tests/unit/test_asset_library_service.py",
    "tests/unit/test_storage_wal_init_race.py",
)


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "absolute_path_check_removed",
        FS_POLICY,
        '        raise FilesystemBoundaryError("absolute paths are not allowed")',
        "        pass",
        "an absolute path must never be accepted; removing the anchor check lets "
        "a caller name a path like /etc/passwd directly",
    ),
    Mutation(
        "nul_byte_check_removed",
        FS_POLICY,
        '        raise FilesystemBoundaryError("path contains a NUL byte")',
        "        pass",
        "a NUL byte can truncate a path at the C layer; it must fail closed",
    ),
    Mutation(
        "blob_key_shape_check_relaxed",
        BLOB_STORE,
        '        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):',
        "        if False:",
        "the content key must be exactly 64 lowercase hex characters; a relaxed "
        "check lets '/etc/passwd' or '..' become a storage path",
    ),
    Mutation(
        "dedup_verification_bypassed",
        BLOB_STORE,
        "            if existing != (sha256, size):",
        "            if False:",
        "reusing an existing blob without verifying its bytes against the key "
        "would serve content that does not match its address",
    ),
    Mutation(
        "open_verification_bypassed",
        BLOB_STORE,
        "        if hashlib.sha256(data).hexdigest() != expected:",
        "        if False:",
        "open() must re-hash the served bytes against the key; skipping it serves tampered content",
    ),
    Mutation(
        "bounded_read_limit_removed",
        FS_POLICY,
        "                        if size > max_bytes:",
        "                        if False:",
        "the bounded streaming write must reject an oversize stream; removing the "
        "check lets an unbounded stream exhaust disk",
    ),
    Mutation(
        "unique_constraint_removed",
        MODELS,
        '        UniqueConstraint("project_id", "idempotency_key", name="uq_assetingest_project_idem"),',
        "        # mutated: scoped idempotency UNIQUE constraint removed",
        "scoped idempotency must be backed by a database UNIQUE constraint; without "
        "it concurrent same-key ingests create two independent assets",
    ),
    Mutation(
        "replay_digest_check_bypassed",
        SERVICE,
        "            if observed != (existing.content_sha256, existing.size_bytes):",
        "            if False:",
        "a reused idempotency key must be compared against the measured content "
        "digest; skipping it silently returns the earlier asset for different bytes",
    ),
    Mutation(
        "schema_changed_no_longer_retryable",
        DB,
        '    return "already exists" in message or "schema has changed" in message',
        '    return "already exists" in message',
        "a concurrent first-use DDL stampede can surface SQLITE_SCHEMA "
        "(database schema has changed); if the retry predicate no longer "
        "recognises it the loser propagates instead of reloading the schema",
    ),
    Mutation(
        "first_use_init_lock_removed",
        DB,
        """    lock = _init_locks.setdefault(normalized_path, asyncio.Lock())
    async with lock:
        if normalized_path in _initialized_paths:
            return
        await _set_wal_mode(engine)
        await create_all_metadata(engine, SQLModel.metadata)
        _initialized_paths.add(normalized_path)""",
        """    await _set_wal_mode(engine)
    await create_all_metadata(engine, SQLModel.metadata)
    _initialized_paths.add(normalized_path)""",
        "the per-path init lock serialises first-use DDL; without it every "
        "concurrent caller runs create_all and their DDL invalidates each "
        "other's schema snapshot",
    ),
)

TEST_TIMEOUT_SECONDS = 600


def run_tests() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", *TESTS],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"(test run exceeded {TEST_TIMEOUT_SECONDS}s) ", end="", flush=True)
        return False
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    print("baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    killed = 0
    survivors: list[str] = []
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
            survivors.append(mutation.name)
            continue
        try:
            mutation.path.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8"
            )
            green = run_tests()
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        if green:
            print(f"{mutation.name}: SURVIVED — {mutation.why}")
            survivors.append(mutation.name)
        else:
            killed += 1
            print(f"{mutation.name}: killed")

    print("restored baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — restore failed")
        return 3
    print("GREEN")
    print(f"{killed}/{len(MUTATIONS)} killed")
    if survivors:
        print("survivors: " + ", ".join(survivors))
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
