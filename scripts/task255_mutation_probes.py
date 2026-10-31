#!/usr/bin/env python3
"""Mutation probes for task-255 AttemptStaging publication callbacks.

Applies targeted mutations to the publication callbacks and verifies the
test suite catches each mutant (mutant RED → restored → GREEN).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
TARGET = REPO / "src/nexus_ai_agent/creative/render_jobs.py"
TEST = REPO / "tests/integration/test_creative_chain_e2e.py"
PYTHON = REPO / ".venv/bin/python"

MUTATIONS = [
    {
        "name": "remove_staging_write",
        "desc": "Skip staging write (no artifact staged)",
        "find": "    data = artifact_path.read_bytes()\n    staging.write(staged_name, data)",
        "replace": "    pass  # staging write removed by mutation",
    },
    {
        "name": "remove_backup",
        "desc": "Skip the .prev backup creation",
        "find": "    if published_path.exists():\n        # Atomic same-directory rename: previous bytes move to .prev.\n        published_path.rename(backup_path)",
        "replace": "    pass  # backup removed by mutation",
    },
    {
        "name": "remove_atomic_publish",
        "desc": "Replace atomic publish with direct copy",
        "find": "    resolved = staging.publish(staged_path, target_rel)",
        "replace": "    import shutil as _sh\n    resolved = staging.final_root / target_rel\n    resolved.parent.mkdir(parents=True, exist_ok=True)\n    _sh.copy2(staged_path, resolved)\n    staged_path.unlink(missing_ok=True)",
    },
    {
        "name": "remove_retract_restore",
        "desc": "Skip backup restore in retract",
        "find": "        if backup_path.exists():\n            if published_path.exists():\n                published_path.unlink()\n            backup_path.rename(published_path)",
        "replace": "        pass  # restore removed by mutation",
    },
    {
        "name": "remove_retract_staged_cleanup",
        "desc": "Skip staged temp removal in retract",
        "find": "    if staged_path.exists():\n        staged_path.unlink()",
        "replace": "    pass  # staged cleanup removed by mutation",
    },
]


def apply_mutation(find: str, replace: str) -> bool:
    content = TARGET.read_text()
    if find not in content:
        return False
    TARGET.write_text(content.replace(find, replace))
    return True


def restore() -> None:
    """Restore from git."""
    subprocess.run(
        ["git", "checkout", "--", str(TARGET.relative_to(REPO))],
        cwd=REPO,
        check=True,
        capture_output=True,
    )


def run_tests() -> tuple[bool, str]:
    result = subprocess.run(
        [
            str(PYTHON),
            "-m",
            "pytest",
            str(TEST),
            "-q",
            "-p",
            "no:randomly",
            "-k",
            "creative_render_publication or creative_render_retract or creative_render_finalize",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0, result.stdout + result.stderr


def main() -> int:
    caught = 0
    missed = 0
    for mut in MUTATIONS:
        # Apply mutation
        if not apply_mutation(mut["find"], mut["replace"]):
            print(f"  {mut['name']}: SKIP — pattern not found")
            missed += 1
            continue

        # Run tests — should FAIL (mutant caught)
        ok, output = run_tests()
        if ok:
            print(f"  {mut['name']}: MISSED — tests passed with mutation!")
            missed += 1
        else:
            # Extract failure count
            fails = output.count("FAILED")
            print(f"  {mut['name']}: CAUGHT ({fails} test(s) failed) — {mut['desc']}")
            caught += 1

        # Restore
        restore()

    print(f"\n{'=' * 50}")
    print(f"Mutation probes: {caught} caught, {missed} missed out of {len(MUTATIONS)}")
    return 0 if missed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
