"""Generate a SHA-based, reproducible evidence bundle for mission P0.

Every claim in the bundle is bound to the exact revision and to the sha256 of
the artifacts it was measured against, so a reviewer can re-run the same
commands on the same commit and compare hashes. Nothing here is fabricated:
each entry records a command that was actually executed and its real output.

Usage:  python3 scripts/p0_evidence.py [--out docs/evidence/p0]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("NEXUS_DATABASE_URL", "postgresql://postgres:pg@127.0.0.1:5433/nexus")


def run(
    cmd: list[str], *, env: dict[str, str] | None = None, unset: tuple[str, ...] = ()
) -> dict[str, object]:
    """Run a command and capture the real, reproducible result.

    ``unset`` removes variables entirely (an empty string is not the same as an
    absent variable for the settings loader, which rejects a blank URL).
    """
    base = {**os.environ, **(env or {})}
    for key in unset:
        base.pop(key, None)
    proc = subprocess.run(
        cmd,
        cwd=REPO,
        capture_output=True,
        text=True,
        env=base,
        timeout=1800,
    )
    return {
        "command": " ".join(cmd),
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout.strip().splitlines()[-6:],
        "stderr_tail": proc.stderr.strip().splitlines()[-6:],
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_rev() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/evidence/p0")
    args = ap.parse_args()
    out_dir = REPO / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    rev = git_rev()
    bundle: dict[str, object] = {
        "schema": "nexus-p0-evidence/v1",
        "mission": "NEXUS P0 database recovery / durability",
        "source_revision": rev,
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "postgres_url": PG_URL.replace(":pg@", ":***@"),
        },
        "checks": {},
    }

    # 1. Migrations base->head on the live cluster (real, not SQLite).
    bundle["checks"]["migrations"] = run(  # type: ignore[index]
        [sys.executable, "-m", "nexus_ai_agent.cli", "migrate"],
        env={"NEXUS_DATABASE_URL": PG_URL, "NEXUS_REQUIRE_POSTGRES": "1"},
    )

    # 2. Persistence/durability integration tests on the live cluster.
    bundle["checks"]["persistence_tests"] = run(  # type: ignore[index]
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "tests/integration/test_postgres_persistence.py",
        ],
        env={"NEXUS_DATABASE_URL": PG_URL, "NEXUS_PG_TEST_DESTRUCTIVE": "1"},
    )

    # 3. End-to-end DR proof: dump -> verify -> restore -> row preserved.
    bundle["checks"]["dr_proof"] = run(  # type: ignore[index]
        [sys.executable, "scripts/dr_proof.py"],
        env={"NEXUS_DATABASE_URL": PG_URL, "NEXUS_REQUIRE_POSTGRES": "1"},
    )

    # 4. The production DB policy fails closed.
    bundle["checks"]["backend_policy"] = run(  # type: ignore[index]
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "tests/unit/test_backend_policy.py",
        ],
        env={"NEXUS_DATABASE_URL": "", "NEXUS_REQUIRE_POSTGRES": ""},
        unset=("NEXUS_DATABASE_URL", "NEXUS_REQUIRE_POSTGRES"),
    )

    # 5. The truth gate still matches the sources.
    bundle["checks"]["truth_gate"] = run([sys.executable, "-m", "nexus_ai_agent.nagar", "--check"])  # type: ignore[index]

    # The fix sites, bound to their exact content hash at this revision.
    bundle["fixed_sites"] = {
        rel: sha256_file(REPO / rel)
        for rel in (
            "src/nexus_ai_agent/maintenance/backup.py",
            "src/nexus_ai_agent/maintenance/restore.py",
            "src/nexus_ai_agent/storage/db.py",
        )
    }

    all_ok = all(c["exit_code"] == 0 for c in bundle["checks"].values())  # type: ignore[union-attr]
    bundle["all_checks_passed"] = all_ok

    dest = out_dir / "evidence.json"
    dest.write_text(json.dumps(bundle, indent=2) + "\n")
    print(f"wrote {dest.relative_to(REPO)} (all_checks_passed={all_ok})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
