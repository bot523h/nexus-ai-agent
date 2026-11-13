"""Small executable-source mutation campaign for critical IBPC boundaries.

Each mutant runs pytest against an isolated copied package tree. A mutant is
counted killed only when the selected behavioral assertion fails after the
mutated module successfully imports; collection/import errors do not count.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "nexus_ai_agent"


_MUTANTS = (
    (
        "approval_mac",
        Path("nagar/ibpc/approval_mac.py"),
        "return hmac.compare_digest(actual_mac, envelope.mac)",
        "return True",
        "tests/unit/test_ibpc_approval_mac.py::test_approval_envelope_is_keyed_and_scope_bound",
    ),
    (
        "staged_source_digest",
        Path("nagar/ibpc/service.py"),
        "if staged_digest != ctx.source_asset_digest:",
        "if False:",
        "tests/integration/test_ibpc_e2e.py::test_staged_digest_mismatch_refuses_before_enqueue",
    ),
    (
        "proposal_revision_binding",
        Path("nagar/ibpc/service.py"),
        '                "proposal_revision": ctx.ast.source_revision,\n',
        "",
        "tests/integration/test_ibpc_e2e.py::test_scope_digest_explicitly_binds_proposal_revision",
    ),
)


def _apply_mutation(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise AssertionError(f"mutation anchor should occur exactly once in {path}: {old!r}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize("name,relative,old,new,target", _MUTANTS)
def test_source_mutation_is_killed_by_behavioral_regression(
    tmp_path: Path, name: str, relative: Path, old: str, new: str, target: str
) -> None:
    copied_package = tmp_path / "src" / "nexus_ai_agent"
    shutil.copytree(PACKAGE, copied_package, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "migrations", tmp_path / "migrations")
    shutil.copy2(REPO / "alembic.ini", tmp_path / "alembic.ini")
    mutated_file = copied_package / relative
    _apply_mutation(mutated_file, old, new)
    # A syntax failure is not a killed mutant: require successful compilation.
    compile(mutated_file.read_text(encoding="utf-8"), str(mutated_file), "exec")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path / "src") + os.pathsep + env.get("PYTHONPATH", "")
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
    assert result.returncode != 0, f"surviving mutant {name}: {output[-3000:]}"
    assert "FAILED" in output, (
        f"mutant {name} failed before behavioral assertions: {output[-3000:]}"
    )
    assert "ERROR collecting" not in output and "ImportError" not in output, output[-3000:]


def test_atomic_consumption_mutant_is_killed_by_replay_assertion(tmp_path: Path) -> None:
    copied_package = tmp_path / "src" / "nexus_ai_agent"
    shutil.copytree(PACKAGE, copied_package, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "migrations", tmp_path / "migrations")
    shutil.copy2(REPO / "alembic.ini", tmp_path / "alembic.ini")
    approval = copied_package / "nagar" / "ibpc" / "approval.py"
    source = approval.read_text(encoding="utf-8")
    start = source.index("    def consume(")
    end = source.index("    def revoke(", start)
    region = source[start:end]
    if region.count("if binding.state is ApprovalState.CONSUMED:") != 1:
        raise AssertionError("consumed-state replay guard anchor changed")
    if region.count("if binding.state is not ApprovalState.APPROVED:") != 1:
        raise AssertionError("approved-state admission anchor changed")
    region = region.replace("if binding.state is ApprovalState.CONSUMED:", "if False:", 1)
    region = region.replace(
        "if binding.state is not ApprovalState.APPROVED:",
        "if binding.state not in (ApprovalState.APPROVED, ApprovalState.CONSUMED):",
        1,
    )
    approval.write_text(source[:start] + region + source[end:], encoding="utf-8")
    compile(approval.read_text(encoding="utf-8"), str(approval), "exec")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_ibpc_approval.py::TestApprovalLedger::test_replay_after_consume_is_refused",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"surviving replay mutant: {output[-3000:]}"
    assert "FAILED" in output and "ERROR collecting" not in output, output[-3000:]
