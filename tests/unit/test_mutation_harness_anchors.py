"""Anchor-drift and isolation guards for the governance mutation harnesses.

GOV-1 / GOV-2 (2026-10-15) found two silent ways a mutation campaign can
"pass" without attacking anything:

* **anchor drift** — a probe whose ``old`` snippet no longer occurs in the
  target is reported as *NOT APPLIED* only after a multi-minute campaign, and
  a snippet that occurs *twice* would mutate the wrong site;
* **shadowed imports** — a campaign run in an isolated worktree imports the
  editable-installed production package, so every probe "survives" while the
  mutated bytes are never executed.

These tests turn both into an instant unit-test failure.  They load the
harness modules without running them (``runpy`` with a non-``__main__`` name)
and never touch the mutation targets.
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
REFEREE = REPO / "scripts" / "board_referee_mutations.py"
GATE5 = REPO / "scripts" / "gate5_mutation_probes.py"
EXECUTION_CORE = REPO / "scripts" / "execution_core_mutations.py"
RETENTION = REPO / "scripts" / "retention_verdict_mutations.py"
DURABLE = REPO / "scripts" / "durable_project_state_mutations.py"
CI = REPO / ".github" / "workflows" / "ci.yml"


def _load(script: Path) -> dict[str, Any]:
    argv = sys.argv
    sys.argv = [str(script)]
    try:
        return runpy.run_path(str(script), run_name="mutation_harness_under_test")
    finally:
        sys.argv = argv


@pytest.fixture(scope="module")
def referee() -> dict[str, Any]:
    return _load(REFEREE)


@pytest.fixture(scope="module")
def gate5() -> dict[str, Any]:
    return _load(GATE5)


@pytest.fixture(scope="module")
def execution_core() -> dict[str, Any]:
    return _load(EXECUTION_CORE)


@pytest.fixture(scope="module")
def retention() -> dict[str, Any]:
    return _load(RETENTION)


@pytest.fixture(scope="module")
def durable() -> dict[str, Any]:
    return _load(DURABLE)


def test_referee_mutations_anchor_exactly_once_in_agent_board(referee: dict[str, Any]) -> None:
    source = Path(referee["SCRIPT"]).read_text(encoding="utf-8")
    assert Path(referee["SCRIPT"]) == REPO / "scripts" / "agent_board.py"
    drift = {m.name: source.count(m.old) for m in referee["MUTATIONS"] if source.count(m.old) != 1}
    assert drift == {}, f"referee anchors must occur exactly once: {drift}"
    # 42 after the PR #316 prerequisite/typed-PR hardening, the P0 verified-merge
    # and released-status guards, and the live-main-cache binding (was 33 → 38 → 42).
    assert len(referee["MUTATIONS"]) == 42


def test_durable_project_state_anchors_occur_exactly_once(durable: dict[str, Any]) -> None:
    # A non-unique anchor silently mutates only the FIRST occurrence (str.replace
    # count=1), so a duplicated guard would leave a second live copy and the
    # campaign would "kill" the mutant while the real hole stayed open.
    drift: dict[str, int] = {}
    for mutation in durable["MUTATIONS"]:
        count = mutation.path.read_text(encoding="utf-8").count(mutation.old)
        if count != 1:
            drift[mutation.name] = count
    assert drift == {}, f"durable anchors must occur exactly once: {drift}"
    # 34 after the F5/R4 per-job ownership probes (job-scoped retraction +
    # job-named resolve) joined the F14 non-integer-token guards and the F1
    # stale-row fallback refusal (was 29 → 32).
    assert len(durable["MUTATIONS"]) == 34


def test_gate5_probe_anchors_occur_exactly_once_in_their_targets(gate5: dict[str, Any]) -> None:
    drift: dict[str, list[int]] = {}
    for probe in gate5["PROBES"]:
        text = probe.target.read_text(encoding="utf-8")
        counts = [text.count(anchor) for anchor, _mutant in probe.edits()]
        if any(count != 1 for count in counts):
            drift[probe.name] = counts
    assert drift == {}, f"gate5 anchors must occur exactly once: {drift}"
    assert len(gate5["PROBES"]) == 17


def test_gate5_mutants_differ_from_their_anchors_and_keep_binding_counts(
    gate5: dict[str, Any],
) -> None:
    for probe in gate5["PROBES"]:
        for anchor, mutant in probe.edits():
            assert anchor != mutant, probe.name
            # A mutant that changes the number of ``?`` placeholders turns the
            # statement into a sqlite binding error — red for the wrong reason.
            assert anchor.count("?") == mutant.count("?"), probe.name


def test_gate5_pins_pythonpath_to_the_tree_under_mutation(
    gate5: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class _Done:
        returncode = 0

    def fake_run(cmd: list[str], **kwargs: Any) -> _Done:
        seen["cmd"] = cmd
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gate5["_run_tests"](("tests/unit/test_nothing.py::test_x",)) == 0
    env = seen["env"]
    assert env["PYTHONPATH"] == str(Path(gate5["REPO"]) / "src")
    assert Path(seen["cwd"]) == Path(gate5["REPO"])
    assert seen["check"] is False
    assert "-p" in seen["cmd"] and "no:cacheprovider" in seen["cmd"]


def test_ci_runs_both_campaigns_in_a_detached_worktree() -> None:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = workflow["jobs"]["governance-mutations"]
    assert job["timeout-minutes"] <= 25
    runs = [step.get("run", "") for step in job["steps"]]
    assert any("git worktree add --detach" in run for run in runs)
    for script in ("scripts/board_referee_mutations.py", "scripts/gate5_mutation_probes.py"):
        step = next(s for s in job["steps"] if script in s.get("run", ""))
        assert "mutation-worktree" in step["working-directory"], script
    assert any("git diff --exit-code" in run for run in runs)
    assert any("git worktree remove --force" in run for run in runs)


def test_execution_core_mutations_anchor_exactly_once(
    execution_core: dict[str, Any],
) -> None:
    drift: dict[str, int] = {}
    for probe in execution_core["PROBES"]:
        for target, anchor in ((probe.target, probe.anchor), (probe.target2, probe.anchor2)):
            if target is None or anchor is None:
                continue
            count = Path(target).read_text(encoding="utf-8").count(anchor)
            if count != 1:
                drift[f"{probe.name}:{Path(target).name}"] = count
    assert drift == {}, f"execution-core anchors must occur exactly once: {drift}"
    # 27 after the task-262 quarantine-retention probes (operator-bound switch +
    # journal completeness) joined the M1–M25 execution-core closure (was 25).
    assert len(execution_core["PROBES"]) == 27


def test_execution_core_pins_pythonpath_to_the_tree_under_mutation(
    execution_core: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # GOV-1 shadowed-import guard: without this pin an isolated worktree imports
    # the editable-installed package and every probe falsely survives.
    seen: dict[str, Any] = {}

    class _Done:
        returncode = 0

    def fake_run(cmd: list[str], **kwargs: Any) -> _Done:
        seen["cmd"] = cmd
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert execution_core["_run_tests"](("tests/unit/test_nothing.py::test_x",)) == 0
    assert seen["env"]["PYTHONPATH"] == str(Path(execution_core["REPO"]) / "src")
    assert Path(seen["cwd"]) == Path(execution_core["REPO"])
    assert "no:cacheprovider" in seen["cmd"]


def test_ci_runs_execution_core_campaign_in_a_detached_worktree() -> None:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = workflow["jobs"]["execution-core-mutations"]
    assert job["timeout-minutes"] <= 30
    runs = [step.get("run", "") for step in job["steps"]]
    assert any("git worktree add --detach" in run for run in runs)
    step = next(s for s in job["steps"] if "execution_core_mutations.py" in s.get("run", ""))
    assert "mutation-worktree" in step["working-directory"]
    assert any("git diff --exit-code" in run for run in runs)
    assert any("git worktree remove --force" in run for run in runs)


def test_retention_mutations_anchor_exactly_once(retention: dict[str, Any]) -> None:
    drift: dict[str, int] = {}
    for probe in retention["PROBES"]:
        count = Path(probe.target).read_text(encoding="utf-8").count(probe.anchor)
        if count != 1:
            drift[f"{probe.name}:{Path(probe.target).name}"] = count
    assert drift == {}, f"retention anchors must occur exactly once: {drift}"
    assert len(retention["PROBES"]) == 17


def test_retention_pins_pythonpath_and_bounds_the_run(
    retention: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # GOV-1 shadowed-import guard + a bounded run: without the PYTHONPATH pin an
    # isolated worktree imports the editable-installed package and every probe
    # falsely survives; without the timeout a hung mutant stalls the campaign.
    seen: dict[str, Any] = {}

    class _Done:
        returncode = 0

    def fake_run(cmd: list[str], **kwargs: Any) -> _Done:
        seen["cmd"] = cmd
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert retention["_run_tests"](("tests/unit/test_nothing.py::test_x",)) == 0
    assert seen["env"]["PYTHONPATH"] == str(Path(retention["REPO"]) / "src")
    assert Path(seen["cwd"]) == Path(retention["REPO"])
    assert "no:cacheprovider" in seen["cmd"]
    assert seen["timeout"] == retention["TEST_TIMEOUT_S"]


def test_durable_pins_pythonpath_to_the_tree_under_mutation(
    durable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # GOV-1 shadowed-import guard (2026-11-08): the durable campaign ran WITHOUT
    # the PYTHONPATH pin the other four harnesses have, so in an isolated
    # worktree it could import the editable-installed package and every probe
    # would falsely "survive" while the mutated bytes were never executed.
    seen: dict[str, Any] = {}

    class _Done:
        returncode = 0

    def fake_run(cmd: list[str], **kwargs: Any) -> _Done:
        seen["cmd"] = cmd
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert durable["run_tests"]() is True
    assert seen["env"]["PYTHONPATH"] == str(Path(durable["ROOT"]) / "src")
    assert Path(seen["cwd"]) == Path(durable["ROOT"])
    assert seen["timeout"] == durable["TEST_TIMEOUT_SECONDS"]


def test_durable_preflight_refuses_when_the_suites_do_not_collect(
    durable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-collecting target suite (a missing optional dependency, a stale
    # symbol) makes the baseline RED after minutes; the preflight fails fast and
    # closed instead of starting a doomed campaign.
    class _Red:
        returncode = 2
        stdout = "ERROR: collection failed"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Red())
    assert durable["preflight"]() == 2


def test_durable_preflight_treats_a_timeout_as_a_refusal(
    durable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert durable["preflight"]() == 2


def test_durable_preflight_passes_on_a_collecting_suite(
    durable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Green:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Green())
    assert durable["preflight"]() == 0
