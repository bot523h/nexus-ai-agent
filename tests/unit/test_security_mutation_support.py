from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from subprocess import CompletedProcess
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SUPPORT_PATH = ROOT / "scripts" / "security_mutation_support.py"


def _load_support_module() -> ModuleType:
    name = "security_mutation_support_test_subject"
    spec = importlib.util.spec_from_file_location(name, SUPPORT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_unrelated_failure_cannot_kill_a_mutant_by_mentioning_expected_nodeid() -> None:
    support = _load_support_module()
    expected = "tests/unit/test_security_mutation_support.py::test_intended_failure"
    actual = "tests/unit/test_security_mutation_support.py::test_unrelated_failure"
    output = "\n".join(
        (
            "E AssertionError: an unrelated diagnostic mentions " + expected,
            "=========================== short test summary info ============================",
            f"FAILED {actual} - AssertionError: unrelated failure",
            "============================== 1 failed in 0.01s ===============================",
        )
    )
    result = CompletedProcess(args=["pytest"], returncode=1, stdout=output, stderr="")

    assert not support._is_killed_by_expected_nodeids(result, (expected,))


def test_expected_nodeid_must_appear_in_pytest_short_failure_summary() -> None:
    support = _load_support_module()
    expected = "tests/unit/test_security_mutation_support.py::test_intended_failure"
    result = CompletedProcess(
        args=["pytest"],
        returncode=1,
        stdout=f"FAILED {expected} - AssertionError: intended mutation observed\n",
        stderr="",
    )

    assert not support._is_killed_by_expected_nodeids(result, (expected,))


def test_exact_failed_nodeid_in_short_summary_kills_mutant() -> None:
    support = _load_support_module()
    expected = "tests/unit/test_security_mutation_support.py::test_intended_failure"
    output = "\n".join(
        (
            "=========================== short test summary info ============================",
            f"FAILED {expected} - AssertionError: intended mutation observed",
        )
    )
    result = CompletedProcess(args=["pytest"], returncode=1, stdout=output, stderr="")

    assert support._is_killed_by_expected_nodeids(result, (expected,))


def test_collection_error_exit_code_is_not_a_killed_mutant() -> None:
    support = _load_support_module()
    expected = "tests/unit/test_security_mutation_support.py::test_intended_failure"
    output = f"ImportError while collecting; traceback mentioned {expected}"
    result = CompletedProcess(args=["pytest"], returncode=2, stdout=output, stderr="")

    assert not support._is_killed_by_expected_nodeids(result, (expected,))
