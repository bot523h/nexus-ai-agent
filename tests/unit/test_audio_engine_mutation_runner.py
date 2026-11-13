"""The mutation gate must itself reject false kills (real isolated pytest runs)."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def runner():
    name = "tested_controlled_audio_mutations"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "run_audio_engine_mutations.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


@pytest.fixture
def example(tmp_path, runner):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "kernel.py").write_text("def scale(x):\n    return x * 2\n")
    (tmp_path / "tests" / "test_signal.py").write_text(
        "from kernel import scale\n\ndef test_scale():\n    assert scale(3) == 6\n"
    )
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ""\n')
    probe = runner.Probe(
        "example",
        "example-gate",
        "src/kernel.py",
        "return x * 2",
        "return x",
        "test_scale",
        "tests/test_signal.py",
    )
    return tmp_path, probe


def _changed(probe, **changes):
    from dataclasses import replace

    return replace(probe, **changes)


def test_real_assertion_kill_requires_a_passing_control_and_never_edits_live_source(
    runner, example
):
    root, probe = example
    before = (root / probe.path).read_bytes()
    result = runner.run_probe(probe, root=root)
    assert result["verdict"] == "KILLED" and result["control_passed"] == 1
    assert result["kill_kind"] == "ASSERTION_OR_EXPECTATION"
    assert result["exceptions"] == ["AssertionError"]
    assert result["failures"][0]["phase"] == "call"
    assert (root / probe.path).read_bytes() == before


def test_async_autouse_fixtures_are_preserved_in_both_isolated_runs(runner, example):
    root, probe = example
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture(autouse=True)\n"
        "async def asynchronous_cleanup():\n    yield\n"
    )
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nasyncio_mode = "auto"\naddopts = ""\n'
    )
    result = runner.run_probe(probe, root=root)
    assert result["verdict"] == "KILLED" and result["control_passed"] == 1
    assert result["kill_kind"] == "ASSERTION_OR_EXPECTATION"


def test_semantically_equivalent_mutant_survives_and_runtime_crashes_are_distinguished(
    runner, example
):
    root, probe = example
    same = runner.run_probe(_changed(probe, new="return 2 * x"), root=root)
    assert same["verdict"] == "SURVIVED" and same["kill_kind"] is None
    crash = runner.run_probe(_changed(probe, new="return missing_name"), root=root)
    assert crash["verdict"] == "KILLED" and crash["kill_kind"] == "RUNTIME_EXCEPTION"
    assert crash["exceptions"] == ["NameError"]


@pytest.mark.parametrize("attack", ["syntax", "missing", "ambiguous", "unreadable"])
def test_invalid_mutants_and_anchors_cannot_masquerade_as_kills(runner, example, attack):
    root, probe = example
    expected = {
        "syntax": "INVALID_MUTANT",
        "missing": "ANCHOR_MISSING",
        "ambiguous": "AMBIGUOUS_ANCHOR",
        "unreadable": "SOURCE_UNREADABLE",
    }
    if attack == "syntax":
        probe = _changed(probe, new="return (")
    if attack == "missing":
        probe = _changed(probe, old="not in the source")
    if attack == "ambiguous":
        (root / probe.path).write_text(
            "def scale(x):\n    return x * 2\ndef other(x):\n    return x * 2\n"
        )
    if attack == "unreadable":
        probe = _changed(probe, path="src/missing.py")
    assert runner.run_probe(probe, root=root)["verdict"] == expected[attack]


@pytest.mark.parametrize(
    "attack", ["collection", "setup", "skipped", "no_tests", "baseline_assertion"]
)
def test_failed_or_absent_controls_never_kill_mutants(runner, example, attack):
    root, probe = example
    bad_tests = {
        "collection": "import absent_dependency\n",
        "setup": (
            "import pytest\n@pytest.fixture\ndef broken():\n"
            "    raise RuntimeError('setup failed')\ndef test_scale(broken):\n    assert False\n"
        ),
        "skipped": (
            "import pytest\n@pytest.mark.skip(reason='not evidence')\n"
            "def test_scale():\n    assert False\n"
        ),
        "baseline_assertion": "def test_scale():\n    assert False\n",
    }
    if attack == "no_tests":
        probe = _changed(probe, test="test_nonexistent")
    else:
        (root / probe.test_file).write_text(bad_tests[attack])
    assert runner.run_probe(probe, root=root)["verdict"] == "BASELINE_FAILED"


def _clean():
    return dict(
        exit_code=0,
        collected=["test_signal.py::test_scale"],
        collection_errors=[],
        reports=[
            dict(
                nodeid="test_signal.py::test_scale",
                phase=phase,
                outcome="passed",
                exception=None,
                xfail=False,
            )
            for phase in ("setup", "call", "teardown")
        ],
    )


@pytest.mark.parametrize(
    "attack",
    [
        "collection",
        "setup",
        "teardown",
        "different_ids",
        "skip",
        "xfail",
        "timeout",
        "empty_calls",
        "false_exit",
        "lazy_import",
        "syntax_in_call",
    ],
)
def test_mutant_infrastructure_failures_are_not_call_phase_kills(runner, attack):
    control = _clean()
    mutant = copy.deepcopy(control)
    mutant["exit_code"] = 1
    if attack == "collection":
        mutant["collection_errors"] = ["could not import"]
    if attack in ("setup", "teardown"):
        mutant["reports"][0 if attack == "setup" else 2]["outcome"] = "failed"
    if attack == "different_ids":
        mutant["collected"] = ["a different test"]
    if attack == "skip":
        mutant["reports"][1]["outcome"] = "skipped"
    if attack == "xfail":
        mutant["reports"][1]["xfail"] = True
    if attack == "timeout":
        mutant["exit_code"] = "timeout"
    if attack == "empty_calls":
        mutant["reports"] = []
        mutant["exit_code"] = 0
    if attack in ("lazy_import", "syntax_in_call"):
        mutant["reports"][1].update(
            outcome="failed",
            exception="ModuleNotFoundError" if attack == "lazy_import" else "SyntaxError",
        )
    # `false_exit`: an rc=1 without a failed test call is not evidence either.
    assert runner._classify(control, mutant) == ("INFRASTRUCTURE_ERROR", [])


def test_subprocess_timeout_is_reported_not_counted_as_a_kill(runner, example, monkeypatch):
    root, probe = example

    def expire(*a, **kw):
        raise runner.subprocess.TimeoutExpired("pytest", 0.001)

    monkeypatch.setattr(runner.subprocess, "run", expire)
    result = runner._pytest(root, root, probe, "timeout", timeout=0.001)
    assert result["exit_code"] == "timeout"


def test_all_110_anchors_are_unique_syntactically_valid_and_selectors_exist(runner):
    import ast

    assert len(runner.PROBES) == 110 and len(runner.STAGE_B_PROBES) == 33
    assert len({probe.name for probe in runner.PROBES}) == 110
    for probe in runner.PROBES:
        source = (ROOT / probe.path).read_text()
        assert source.count(probe.old) == 1, probe.name
        compile(source.replace(probe.old, probe.new), probe.path, "exec")
    for probe in runner.PROBES:
        tree = ast.parse((ROOT / probe.test_file).read_text())
        tests = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert any(probe.test in name for name in tests), (probe.name, probe.test)


def test_empty_filter_and_invalid_worker_configuration_fail_closed(runner):
    for args in (["--filter", "not-a-real-probe"], ["--workers", "0"]):
        with pytest.raises(SystemExit) as raised:
            runner.main(args)
        assert raised.value.code == 2


def test_lazy_import_failure_is_not_a_semantic_mutation_kill(runner, example):
    root, probe = example
    result = runner.run_probe(
        _changed(probe, new="return __import__('absent_mutation_test_dependency')"), root=root
    )
    assert result["verdict"] == "INFRASTRUCTURE_ERROR"
    assert result["kill_kind"] is None


def test_manifest_binds_the_test_source_and_ignores_only_excluded_caches(runner, example):
    root, probe = example
    before = runner._input_manifest(root)
    assert set(before) == {"pyproject.toml", "src/kernel.py", "tests/test_signal.py"}
    cache = root / "src" / "__pycache__"
    cache.mkdir()
    (cache / "kernel.pyc").write_bytes(b"not experiment input")
    assert runner._input_manifest(root) == before
    (root / probe.test_file).write_text("def test_other():\n    assert True\n")
    assert runner._input_manifest(root) != before


@pytest.mark.parametrize("condition", ["killed", "survived", "filtered", "live_edit"])
def test_cli_accepts_only_a_complete_all_killed_unchanged_campaign(
    runner, example, monkeypatch, capsys, condition
):
    import json

    root, probe = example
    monkeypatch.setattr(runner, "REPO", root)
    monkeypatch.setattr(
        runner, "PROBES", tuple(_changed(probe, name=f"probe-{n:03d}") for n in range(110))
    )

    def fake_probe(item):
        if condition == "live_edit":
            (root / probe.path).write_text("def scale(x):\n    return x * 7\n")
        return dict(
            name=item.name,
            verdict="SURVIVED" if condition == "survived" else "KILLED",
            kill_kind=None if condition == "survived" else "ASSERTION_OR_EXPECTATION",
        )

    monkeypatch.setattr(runner, "run_probe", fake_probe)
    target = root / "result.json"
    args = ["--workers", "1", "--json", "--json-out", str(target)]
    if condition == "filtered":
        args += ["-k", "probe-109"]
    assert runner.main(args) == (1 if condition in ("survived", "live_edit") else 0)
    report = json.loads(capsys.readouterr().out)
    assert json.loads(target.read_text()) == report
    assert report["accepted"] is (condition == "killed")
    assert report["full_campaign"] is (condition != "filtered")
    assert report["inputs_unchanged"] is (condition != "live_edit")
    assert len(report["input_manifest_sha256"]) == 64
