"""task-283 — the mutation harness must not become an injection surface.

Security review (``dangerous-subprocess-use-audit``) flagged
``scripts/queue_ownership_mutations.py`` for ``subprocess.run``.  Analysis, not
suppression, is the resolution:

* the call passes an **argv list**, never a string, with ``shell=False``;
* the only variable arguments are pytest *test node ids*;
* those ids are frozen constants in :data:`PROBES` **and** are re-validated
  against a strict pattern before every spawn.

These tests pin both halves: the data path is inert, and anything that is not a
plain ``tests/<path>.py[::name]`` node id is refused before it can reach pytest
(testing the rejection behaviour, so a future generated probe cannot widen it).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _harness() -> Any:
    path = REPO_ROOT / "scripts" / "queue_ownership_mutations.py"
    spec = importlib.util.spec_from_file_location("queue_ownership_mutations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_probe_test_id_is_a_plain_node_id() -> None:
    """The frozen probe table is inert data — no flags, paths or metacharacters."""
    harness = _harness()
    assert harness.PROBES, "the harness must define probes"
    for probe in harness.PROBES:
        assert probe.tests, f"{probe.name} selects no tests"
        harness._validate_node_ids(probe.tests)
        for node in probe.tests:
            assert (REPO_ROOT / node.split("::")[0]).is_file(), f"{node} is not a test file"


@pytest.mark.parametrize(
    "node",
    [
        "-k something",  # would become a pytest option
        "--pdb",
        "tests/x.py; rm -rf /",  # shell metacharacters
        "tests/../../etc/passwd",
        "/etc/passwd",
        "tests/../tests/integration/test_x.py",
        "",
        "tests/integration/test_x.py::a::b",
        "tests/integration/test_x.py -x",
    ],
)
def test_validate_node_ids_refuses_anything_but_a_plain_node_id(node: str) -> None:
    harness = _harness()
    with pytest.raises(ValueError):
        harness._validate_node_ids((node,))


def test_validate_node_ids_accepts_the_shapes_the_probes_use() -> None:
    harness = _harness()
    assert harness._validate_node_ids(
        ("tests/integration/test_queue_ownership_lifecycle.py::test_x",)
    )
    assert harness._validate_node_ids(("tests/unit/test_y.py",))


def test_run_uses_an_argv_list_and_never_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn boundary: list argv, ``shell=False``, no string command."""
    harness = _harness()
    captured: dict[str, object] = {}

    def fake_run(argv: Any, **kwargs: Any) -> SimpleNamespace:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    node = "tests/integration/test_queue_ownership_lifecycle.py::test_x"

    assert harness._run((node,)) == 0
    argv = captured["argv"]
    kwargs = captured["kwargs"]
    assert isinstance(argv, list), "argv must be a list, never a shell string"
    assert argv[-1] == node
    assert all(isinstance(part, str) for part in argv)
    assert kwargs["shell"] is False
    assert kwargs["check"] is False


def test_run_refuses_an_unvalidated_node_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness()
    spawned: list[Any] = []
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: spawned.append(a))

    with pytest.raises(ValueError):
        harness._run(("--pdb",))

    assert spawned == [], "an invalid node id must never reach subprocess.run"


WORKFLOW = REPO_ROOT / ".github/workflows/queue-ownership-mutations.yml"


def test_the_harness_is_wired_into_ci() -> None:
    """Review finding 5: the campaign must be CI-enforced, not locally claimed.

    Parsed textually (no YAML dependency in the test environment): the workflow
    must exist, be runtime-bounded, scoped to the queue/publication surface, run
    the harness with a report, assert the report rather than only exit codes, run
    in an isolated worktree, and upload its evidence even when it fails.
    """
    assert WORKFLOW.is_file(), "the mutation campaign is not wired into CI"
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "timeout-minutes:" in text, "the campaign must be runtime-bounded"
    minutes = int(text.split("timeout-minutes:", 1)[1].splitlines()[0].strip())
    assert 0 < minutes <= 20, f"unexpected campaign budget: {minutes} minutes"

    assert "scripts/queue_ownership_mutations.py" in text
    assert "--report" in text, "the CI step must record machine-readable evidence"
    assert '"survived": 0' in text, "the gate must assert zero surviving mutants"
    assert 'report.get("accepted") is True' in text, (
        "the gate must assert the report verdict, not only pytest exit codes"
    )
    assert "git worktree add" in text, (
        "the campaign must run in an isolated worktree so a crash cannot mutate the checkout"
    )
    assert "upload-artifact" in text, "evidence must be uploaded even when the job fails"
    assert "if: always()" in text
    # Scoped: the trigger paths must name the queue surface, not the whole repo.
    assert "src/nexus_ai_agent/adapters/in_process_job_queue.py" in text
    assert "scripts/queue_ownership_mutations.py" in text


def test_the_workflow_is_not_a_second_copy_of_ci() -> None:
    """The campaign lives in its own file; `ci.yml` is owned by another claim."""
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "queue_ownership_mutations.py" not in ci, (
        "duplicating the campaign inside ci.yml would give two sources of truth"
    )
