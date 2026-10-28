# ruff: noqa: E501
"""Mutation check for the Asset Library ingest-race guarantees (task-266, D-0026).

Applies ONE source edit at a time to the real module, runs the targeted tests, and
requires them to fail ("killed").  The original file is always restored (in a
``finally``), and the script verifies the tree is byte-identical afterwards.

    python scripts/asset_library_mutation_check.py

Exit status is non-zero if any mutant survives or an anchor no longer matches.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/nexus_ai_agent/creative/assets"
TESTS = [
    "tests/integration/test_asset_library_ingest_cas.py",
    "tests/integration/test_asset_library_replay_safety.py",
    "tests/integration/test_asset_library_recovery.py",
    "tests/integration/test_asset_library_lost_race.py",
]


@dataclass(frozen=True)
class Mutant:
    name: str
    file: str
    old: str
    new: str


MUTANTS = [
    Mutant(
        "M1 complete_ingest drops the STAGING guard",
        "repository.py",
        "                orm.AssetIngest.state == IngestState.STAGING.value,  # type: ignore[arg-type]\n            )\n            .values(\n                state=IngestState.READY.value,",
        "            )\n            .values(\n                state=IngestState.READY.value,",
    ),
    Mutant(
        "M2 mark_failed drops the STAGING guard",
        "repository.py",
        "                orm.AssetIngest.state == IngestState.STAGING.value,  # type: ignore[arg-type]\n            )\n            .values(\n                state=IngestState.FAILED.value,\n                error_code=error_code,\n                staging_key=None,",
        "            )\n            .values(\n                state=IngestState.FAILED.value,\n                error_code=error_code,\n                staging_key=None,",
    ),
    Mutant(
        "M3 set_staging_key drops the STAGING guard",
        "repository.py",
        "                orm.AssetIngest.state == IngestState.STAGING.value,  # type: ignore[arg-type]\n            )\n            .values(staging_key=staging_key",
        "            )\n            .values(staging_key=staging_key",
    ),
    Mutant(
        "M4 complete_ingest stops recording the digest",
        "repository.py",
        "                content_sha256=content_sha256,\n                size_bytes=size_bytes,\n                # Recovery finalises",
        "                # Recovery finalises",
    ),
    Mutant(
        "M5 downgrade applies to any state",
        "repository.py",
        "                orm.AssetIngest.state == IngestState.READY.value,  # type: ignore[arg-type]",
        "                orm.AssetIngest.ingest_id == ingest_id,  # type: ignore[arg-type]",
    ),
    Mutant(
        "M6 lost CAS in publish_and_commit is ignored",
        "ingestion.py",
        "        raise IngestAlreadyResolved(ingest_id)\n    return asset_id, version_id, deduplicated",
        "        pass\n    return asset_id, version_id, deduplicated",
    ),
    Mutant(
        "M7 differing replay that lost the race is served",
        "service.py",
        '            if (replay_sha, replay_size) != (final.content_sha256, final.size_bytes):\n                raise IdempotencyConflict("idempotency key was reused with different content")\n',
        "",
    ),
    Mutant(
        "M8 adopted replay file is not reclaimed on a lost race",
        "service.py",
        "                self._blob_store.remove_staging(owned_staging_key)\n",
        "                pass\n",
    ),
    Mutant(
        "M9 recover_all shares one transaction again",
        "service.py",
        "                reports.append(await self._reconcile_committing(session, repo, record))\n            return reports",
        "                reports.append(await reconcile_ingest(repo, self._blob_store, record))\n            await session.commit()\n            return reports",
    ),
]


def _run_tests() -> bool:
    """True when the targeted tests PASS."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *TESTS,
            "-q",
            "--no-header",
            "-x",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def main() -> int:
    originals = {m.file: (ASSETS / m.file).read_text(encoding="utf-8") for m in MUTANTS}
    if not _run_tests():
        print("BASELINE FAILS: fix the tests before measuring mutants")
        return 2
    survived: list[str] = []
    broken: list[str] = []
    try:
        for m in MUTANTS:
            path = ASSETS / m.file
            source = originals[m.file]
            if source.count(m.old) != 1:
                broken.append(f"{m.name}: anchor matched {source.count(m.old)} times")
                print(f"ANCHOR  {m.name}")
                continue
            path.write_text(source.replace(m.old, m.new, 1), encoding="utf-8")
            try:
                killed = not _run_tests()
            finally:
                path.write_text(source, encoding="utf-8")
            print(f"{'KILLED ' if killed else 'SURVIVED'} {m.name}")
            if not killed:
                survived.append(m.name)
    finally:
        for name, text in originals.items():
            (ASSETS / name).write_text(text, encoding="utf-8")
    intact = all((ASSETS / n).read_text(encoding="utf-8") == t for n, t in originals.items())
    print(
        f"\n{len(MUTANTS) - len(survived) - len(broken)}/{len(MUTANTS)} killed; tree intact: {intact}"
    )
    return 0 if not survived and not broken and intact else 1


if __name__ == "__main__":
    raise SystemExit(main())
