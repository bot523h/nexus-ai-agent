"""Ratchets proving evaluator non-authority and strict adapter isolation."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from nexus_ai_agent.evaluation import runner as runner_module
from nexus_ai_agent.evaluation.corpus import AGENT_FOUNDATION_SUITE
from nexus_ai_agent.evaluation.domain import (
    EvaluationInput,
    GateVerdict,
    ObservedBehavior,
    ObservedOutcome,
)
from nexus_ai_agent.evaluation.runner import run_suite

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "nexus_ai_agent" / "evaluation"
FORBIDDEN_IMPORT_ROOTS = {
    "subprocess",
    "socket",
    "os",
    "shutil",
    "pathlib",
    "sqlite3",
    "sqlalchemy",
    "requests",
    "httpx",
    "nexus_ai_agent.orchestration",
    "nexus_ai_agent.storage",
    "nexus_ai_agent.creative",
    "nexus_ai_agent.agent",
}
FORBIDDEN_CALL_NAMES = {
    "open",
    "exec",
    "eval",
    "authorize",
    "dispatch",
    "execute",
    "write_text",
    "write_bytes",
    "unlink",
    "connect",
    "create_engine",
}


def _module_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else None
    if isinstance(node, ast.ImportFrom):
        return node.module
    return None


def test_evaluator_modules_do_not_import_runtime_authorities_or_io() -> None:
    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = _module_name(node)
            if module and any(
                module == forbidden or module.startswith(forbidden + ".")
                for forbidden in FORBIDDEN_IMPORT_ROOTS
            ):
                offenders.append(f"{path.name}:{node.lineno}:import:{module}")
    assert offenders == []


def test_evaluator_runtime_modules_have_no_direct_authority_or_filesystem_calls() -> None:
    offenders = []
    for filename in ("domain.py", "oracles.py", "runner.py", "mutation_campaign.py"):
        path = PACKAGE / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if name in FORBIDDEN_CALL_NAMES:
                    offenders.append(f"{filename}:{node.lineno}:call:{name}")
    assert offenders == []


def test_runner_invokes_only_the_explicit_adapter_observe_boundary() -> None:
    path = PACKAGE / "runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    runner = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_suite"
    )
    adapter_calls = [
        node
        for node in ast.walk(runner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "adapter"
    ]
    assert len(adapter_calls) == 1
    assert adapter_calls[0].func.attr == "observe"
    assert all(
        isinstance(argument, ast.Attribute) and argument.attr == "evaluation_input"
        for argument in adapter_calls[0].args
    )


def test_public_package_surface_exposes_no_execution_or_authorization_api() -> None:
    source = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    exported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            ):
                exported = [
                    item.value for item in node.value.elts if isinstance(item, ast.Constant)
                ]
    assert exported
    assert not any(
        any(term in name.casefold() for term in ("execute", "authorize", "dispatch", "persist"))
        for name in exported
    )


def test_bad_adapter_return_fails_closed_instead_of_becoming_a_subject_verdict() -> None:
    class InvalidAdapter:
        adapter_id = "invalid-test-adapter"

        def observe(self, evaluation_input: EvaluationInput) -> dict[str, str]:
            return {"outcome": "COMPLETED"}

    run = run_suite(
        AGENT_FOUNDATION_SUITE,
        subject_sha="e5b326b2eaf691a638d030ad57acf1ce60016ef0",
        adapter=InvalidAdapter(),  # type: ignore[arg-type]
    )
    assert run.gate_verdict is GateVerdict.FAIL
    assert all(result.observed is None for result in run.results)
    assert any(
        finding.code.value == "INTERNAL_CONTRACT_FAILURE"
        for result in run.results
        for finding in result.findings
    )
    assert run.e2e_readiness.status.value == "BLOCKED"


def test_oracle_exception_fails_closed_without_leaking_message(monkeypatch) -> None:
    class TypedAdapter:
        adapter_id = "typed-observation-test-adapter"

        def observe(self, evaluation_input: EvaluationInput) -> ObservedBehavior:
            _ = evaluation_input
            return ObservedBehavior(outcome=ObservedOutcome.NOT_STARTED)

    def broken_oracle(case, observed):
        _ = case, observed
        raise ValueError("timestamp=2099-01-01T00:00:00Z token=must-not-leak")

    monkeypatch.setattr(runner_module, "evaluate_observation", broken_oracle)
    run = runner_module.run_suite(
        AGENT_FOUNDATION_SUITE,
        subject_sha="e5b326b2eaf691a638d030ad57acf1ce60016ef0",
        adapter=TypedAdapter(),  # type: ignore[arg-type]
    )

    assert run.gate_verdict is GateVerdict.FAIL
    assert all(result.observed is None for result in run.results)
    assert all(result.status.value == "FAIL" for result in run.results)
    assert all(
        finding.code.value == "INTERNAL_CONTRACT_FAILURE"
        for result in run.results
        for finding in result.findings
    )
    serialized = run.report().to_json()
    assert "2099-01-01" not in serialized
    assert "must-not-leak" not in serialized
    assert run.e2e_readiness.status.value == "BLOCKED"


def test_cli_emits_machine_json_and_fails_closed_without_adapter() -> None:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + existing if existing else "")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexus_ai_agent.evaluation",
            "gate",
            "--subject-sha",
            "e5b326b2eaf691a638d030ad57acf1ce60016ef0",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "AGENT_ASSURANCE_GATE = FAIL"
    assert result.stdout.startswith("{")
    import json

    report = json.loads(result.stdout)
    assert report["verdict"] == "FAIL"
    assert report["e2e_readiness"]["adapter_id"] == "unregistered"
    assert len(report["oracle_versions"]) == 15
    assert report["blocked_conditions"][0]["code"] == "E2E_ADAPTER_UNAVAILABLE"
