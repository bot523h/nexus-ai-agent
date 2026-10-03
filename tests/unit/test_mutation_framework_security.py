"""Regression tests for the global mutation framework security contract (Phase 1 §8.3).

Proves:
- OLD LOGIC (weak substring match on non-zero returncode) permits false-kills on
  collection/import errors, unrelated test failures when the target test name
  appears in PASSED/warnings/tracebacks, and internal errors.
- NEW LOGIC (`classify_mutation_outcome`) rejects all false-kills and requires
  exact `FAILED <pytest-nodeid>` matching with `returncode == 1`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.continuum.security_mutation_support import (
    MutationStatus,
    classify_mutation_outcome,
    extract_failed_nodeids,
    legacy_weak_substring_killed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_NODE = (
    "tests/unit/test_security_hardening.py::test_valid_hmac_cannot_reactivate_retired_video_edit"
)
EXPECTED_SHORT_TOKEN = "test_valid_hmac_cannot_reactivate_retired_video_edit"


def test_old_weak_logic_false_kills_on_collection_error_while_new_logic_rejects() -> None:
    collection_error_output = (
        "============================= test session starts ==============================\n"
        "collecting ... collected 0 items / 1 error\n"
        "==================================== ERRORS ====================================\n"
        "________ ERROR collecting tests/unit/test_security_hardening.py ________\n"
        f"ImportError while importing test module ({EXPECTED_NODE}):\n"
        "SyntaxError: invalid syntax\n"
        "=========================== short test summary info ============================\n"
        "ERROR tests/unit/test_security_hardening.py\n"
    )
    # OLD LOGIC: false-kill possible!
    assert legacy_weak_substring_killed(2, collection_error_output, (EXPECTED_SHORT_TOKEN,)) is True

    # NEW LOGIC: false-kill rejected!
    outcome = classify_mutation_outcome(
        returncode=2,
        output=collection_error_output,
        expected_failures=(EXPECTED_NODE,),
    )
    assert outcome.killed is False
    assert outcome.status is MutationStatus.COLLECTION_ERROR


def test_old_weak_logic_false_kills_on_unrelated_failure_when_expected_passed() -> None:
    unrelated_failure_output = (
        f"PASSED {EXPECTED_NODE}\n"
        "FAILED tests/unit/test_security_hardening.py::test_unrelated_regression - AssertionError\n"
        "1 failed, 1 passed in 0.12s\n"
    )
    # OLD LOGIC: false-kill possible because returncode=1 and token is in output!
    assert (
        legacy_weak_substring_killed(1, unrelated_failure_output, (EXPECTED_SHORT_TOKEN,)) is True
    )

    # NEW LOGIC: false-kill rejected!
    outcome = classify_mutation_outcome(
        returncode=1,
        output=unrelated_failure_output,
        expected_failures=(EXPECTED_NODE,),
    )
    assert outcome.killed is False
    assert outcome.status is MutationStatus.UNEXPECTED_FAILURE
    assert outcome.failed_nodeids == (
        "tests/unit/test_security_hardening.py::test_unrelated_regression",
    )


def test_old_weak_logic_false_kills_on_internal_error_while_new_logic_rejects() -> None:
    internal_error_output = (
        f"INTERNALERROR> while running {EXPECTED_NODE}\n"
        "INTERNALERROR> RuntimeError: runner crashed\n"
    )
    assert legacy_weak_substring_killed(3, internal_error_output, (EXPECTED_SHORT_TOKEN,)) is True

    outcome = classify_mutation_outcome(
        returncode=3,
        output=internal_error_output,
        expected_failures=(EXPECTED_NODE,),
    )
    assert outcome.killed is False
    assert outcome.status is MutationStatus.INFRASTRUCTURE_ERROR


def test_timeout_and_pre_existing_failure_are_explicitly_classified_and_never_killed() -> None:
    timeout_outcome = classify_mutation_outcome(
        returncode=None,
        output=f"FAILED {EXPECTED_NODE}\n",
        expected_failures=(EXPECTED_NODE,),
        timed_out=True,
    )
    assert timeout_outcome.killed is False
    assert timeout_outcome.status is MutationStatus.TIMEOUT

    pre_existing_outcome = classify_mutation_outcome(
        returncode=1,
        output=f"FAILED {EXPECTED_NODE}\n",
        expected_failures=(EXPECTED_NODE,),
        baseline_failed=True,
    )
    assert pre_existing_outcome.killed is False
    assert pre_existing_outcome.status is MutationStatus.PRE_EXISTING_FAILURE


def test_survived_mutant_is_classified_as_survived() -> None:
    output = f"PASSED {EXPECTED_NODE}\n1 passed in 0.05s\n"
    outcome = classify_mutation_outcome(
        returncode=0,
        output=output,
        expected_failures=(EXPECTED_NODE,),
    )
    assert outcome.killed is False
    assert outcome.status is MutationStatus.SURVIVED


def test_exact_failed_nodeid_with_exit_code_1_is_classified_as_killed() -> None:
    output = (
        "============================= test session starts ==============================\n"
        "=========================== short test summary info ============================\n"
        f"FAILED {EXPECTED_NODE} - AssertionError: expected 410, got 200\n"
        "1 failed in 0.08s\n"
    )
    assert extract_failed_nodeids(output) == (EXPECTED_NODE,)
    outcome = classify_mutation_outcome(
        returncode=1,
        output=output,
        expected_failures=(EXPECTED_NODE,),
    )
    assert outcome.killed is True
    assert outcome.status is MutationStatus.KILLED
    assert outcome.failed_nodeids == (EXPECTED_NODE,)


def test_bare_substring_without_nodeid_separator_is_rejected() -> None:
    with pytest.raises(ValueError, match="full pytest node IDs"):
        classify_mutation_outcome(
            returncode=1,
            output=f"FAILED {EXPECTED_NODE}\n",
            expected_failures=(EXPECTED_SHORT_TOKEN,),
        )


def test_no_security_mutation_script_uses_substring_kill_detection() -> None:
    scripts_dir = REPO_ROOT / "scripts"
    for path in sorted(scripts_dir.glob("security_mutation*.py")):
        text = path.read_text(encoding="utf-8")
        assert "all(token in output" not in text, (
            f"{path.name} still contains weak substring kill detection"
        )
