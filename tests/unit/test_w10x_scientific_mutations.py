"""Wave 11: validate the 30-mutation campaign harness itself."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "w10x_scientific_mutations.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("w10x_scientific_mutations", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


harness = _load()


def test_campaign_has_thirty_mutants_three_per_wave() -> None:
    mutations = harness.MUTATIONS
    assert len(mutations) == 30
    waves = Counter(mutation.wave for mutation in mutations)
    assert waves == {wave: 3 for wave in range(1, 11)}
    assert len({mutation.name for mutation in mutations}) == 30


def test_preflight_is_structurally_valid() -> None:
    mutations = harness.preflight()
    assert len(mutations) == 30
    for mutation in mutations:
        assert mutation.old != mutation.new
        source = ROOT / mutation.source
        assert source.is_file()
        assert (ROOT / mutation.test).is_file()
        assert source.read_text(encoding="utf-8").count(mutation.old) == 1


def test_preflight_rejects_missing_snippet(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = harness.Mutation(
        name="bad",
        wave=1,
        source="src/nexus_ai_agent/storage/sqlite_pragma_calculus.py",
        test="tests/unit/test_w10x_wave1_sqlite_pragma_calculus.py",
        old="this snippet does not exist",
        new="nope",
    )
    monkeypatch.setattr(harness, "MUTATIONS", (bad,))
    with pytest.raises(SystemExit):
        harness.preflight()


def test_bytecode_cache_purge(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    cache_dir = tmp_path / "__pycache__"
    cache_dir.mkdir()
    exact = cache_dir / "sample.cpython-311.pyc"
    alias = cache_dir / "sample.other.pyc"
    exact.write_bytes(b"x")
    alias.write_bytes(b"y")
    harness._purge_bytecode_cache(source)
    assert not exact.exists()
    assert not alias.exists()


def _sandbox(tmp_path: Path) -> Path:
    shutil.copytree(
        ROOT / "src",
        tmp_path / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return tmp_path


def _copy_test(sandbox: Path, relative: str) -> None:
    destination = sandbox / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / relative, destination)


def test_run_mutation_kills_and_restores_in_isolated_sandbox(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    mutation = harness.MUTATIONS[0]
    source = sandbox / mutation.source
    _copy_test(sandbox, mutation.test)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    outcome = harness.run_mutation(mutation, repo_root=sandbox)
    assert outcome == "killed"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert source.read_text(encoding="utf-8").count(mutation.old) == 1


def test_run_mutation_detects_survivor_and_restores(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    mutation = harness.Mutation(
        name="neutral",
        wave=1,
        source="src/nexus_ai_agent/storage/sqlite_pragma_calculus.py",
        test="tests/unit/test_w10x_wave1_sqlite_pragma_calculus.py",
        old="import math\n",
        new="import math  # neutral mutation\n",
    )
    source = sandbox / mutation.source
    _copy_test(sandbox, mutation.test)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    outcome = harness.run_mutation(mutation, repo_root=sandbox)
    assert outcome == "survived"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert "# neutral mutation" not in source.read_text(encoding="utf-8")


def test_classifier_requires_real_test_failure() -> None:
    killed = subprocess.CompletedProcess(["pytest"], 1, "1 failed, 7 passed\n", "")
    assert harness._classify_mutant_outcome(killed) == "killed"
    survived = subprocess.CompletedProcess(["pytest"], 0, "8 passed\n", "")
    assert harness._classify_mutant_outcome(survived) == "survived"
    syntax = subprocess.CompletedProcess(["pytest"], 2, "", "SyntaxError: bad mutant")
    assert harness._classify_mutant_outcome(syntax) == "invalid"
    collection = subprocess.CompletedProcess(["pytest"], 2, "", "ERROR collecting tests")
    assert harness._classify_mutant_outcome(collection) == "invalid"
    import_error = subprocess.CompletedProcess(["pytest"], 1, "1 failed", "ImportError")
    assert harness._classify_mutant_outcome(import_error) == "invalid"


def test_run_mutation_marks_collection_failure_invalid(tmp_path: Path) -> None:
    sandbox = tmp_path
    (sandbox / "src" / "pkg").mkdir(parents=True)
    (sandbox / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (sandbox / "src" / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    test_path = sandbox / "tests" / "unit" / "test_mod.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import missing_dependency_that_cannot_exist\n\ndef test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    mutation = harness.Mutation(
        "invalid_collection",
        1,
        "src/pkg/mod.py",
        "tests/unit/test_mod.py",
        "VALUE = 1",
        "VALUE = 2",
    )
    assert harness.run_mutation(mutation, repo_root=sandbox) == "invalid"
