"""Isolated, source-mutant regression checks for M3 lineage invariants."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np  # Keep native modules resident during coverage-mutant collection.

_NUMPY_EXTENSION_KEEPALIVE = np
REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "nexus_ai_agent"

_MUTANTS = (
    (
        "M1_retraction_token_validation",
        Path("creative/project_state/store.py"),
        "if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:",
        "if False:",
        "tests/integration/test_durable_project_revision_pinning.py::test_retract_requires_a_real_fencing_token",
    ),
    (
        "M2_old_version_cleanup",
        Path("creative/project_state/store.py"),
        "if remaining_links == 0:",
        "if False:",
        "tests/integration/test_durable_project_revision_pinning.py::test_verified_asset_registration_is_transactional_idempotent_and_fenced",
    ),
    (
        "M3_link_cleanup",
        Path("creative/project_state/store.py"),
        "await session.execute(delete(links).where(link_table.c.link_id == link_id))",
        "pass",
        "tests/integration/test_durable_project_revision_pinning.py::test_verified_asset_registration_is_transactional_idempotent_and_fenced",
    ),
    (
        "M4_asset_cleanup",
        Path("creative/project_state/store.py"),
        "if remaining_versions == 0:",
        "if False:",
        "tests/integration/test_durable_project_revision_pinning.py::test_verified_asset_registration_is_transactional_idempotent_and_fenced",
    ),
    (
        "M5_fence_bypass",
        Path("creative/project_state/store.py"),
        "if not callable(fence_check) or not fence_check():",
        "if False:",
        "tests/integration/test_durable_project_revision_pinning.py::test_verified_asset_registration_is_transactional_idempotent_and_fenced",
    ),
    (
        "M6_digest_bypass",
        Path("creative/project_state/store.py"),
        "if digest != normalized or size != size_bytes or size <= 0:",
        "if False:",
        "tests/integration/test_durable_project_revision_pinning.py::test_verified_asset_registration_is_transactional_idempotent_and_fenced",
    ),
    (
        "M7_shared_path",
        Path("creative/render_jobs.py"),
        'f"{staging.job_id}/{staging.attempt_id}/{staged_name}"',
        'f"{staging.job_id}/{staged_name}"',
        "tests/integration/test_creative_chain_e2e.py::test_integration_two_attempts_same_artifact_name",
    ),
    (
        "M8_nonexistent_version",
        Path("creative/project_state/store.py"),
        "if artifact_version_id is not None:",
        "if False:",
        "tests/integration/test_durable_project_revision_pinning.py::test_binding_refuses_nonexistent_asset_version_reference",
    ),
)


def test_m3_source_mutants_are_killed(tmp_path: Path) -> None:
    for name, relative, old, new, target in _MUTANTS:
        isolated = tmp_path / name
        source_root = isolated / "src" / "nexus_ai_agent"
        shutil.copytree(PACKAGE, source_root, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "migrations", isolated / "migrations")
        shutil.copy2(REPO / "alembic.ini", isolated / "alembic.ini")
        target_file = source_root / relative
        source = target_file.read_text(encoding="utf-8")
        if source.count(old) != 1:
            raise AssertionError(f"{name}: mutation anchor count was {source.count(old)}")
        target_file.write_text(source.replace(old, new, 1), encoding="utf-8")
        compile(target_file.read_text(encoding="utf-8"), str(target_file), "exec")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(isolated / "src") + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", target],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 1, (
            f"{name} did not fail on an assertion (pytest exit {result.returncode}):\n"
            f"{output[-3000:]}"
        )
        assert "FAILED" in output, f"{name} failed before assertions:\n{output[-3000:]}"
        assert "ERROR collecting" not in output and "ImportError" not in output, output[-3000:]
