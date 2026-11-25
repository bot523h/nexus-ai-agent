"""Strict W5 mutation-oracle, restoration and bytecode-cache regression tests."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "w5_mutation_harness", Path(__file__).resolve().parents[2] / "scripts/w5_mutations.py"
)
assert spec is not None and spec.loader is not None
harness = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = harness
spec.loader.exec_module(harness)


@pytest.mark.parametrize("code", [2, 3, 4, 5, -9])
def test_only_pytest_failure_exit_can_be_a_kill(code: int) -> None:
    assert harness.classify(code, "FAILED test::case", "FAILED test::case") == "INVALID"


@pytest.mark.parametrize("marker", harness.BAD_OUTPUT)
def test_infrastructure_failures_are_never_killed(marker: str) -> None:
    assert harness.classify(1, f"FAILED test::case {marker}", "FAILED test::case") == "INVALID"


def test_oracle_requires_expected_failed_node() -> None:
    assert harness.classify(0, "", "FAILED test::case") == "SURVIVED"
    assert harness.classify(1, "FAILED another::case", "FAILED test::case") == "INVALID"
    assert (
        harness.classify(1, "FAILED test::case - AssertionError", "FAILED test::case") == "KILLED"
    )


def test_cache_purge_is_module_specific(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("value = 1\n")
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    cache.write_bytes(b"stale")
    optimized = cache.parent / "module.cpython-311.opt-1.pyc"
    optimized.write_bytes(b"stale")
    neighbor = cache.parent / "other.cpython-311.pyc"
    neighbor.write_bytes(b"keep")
    harness._purge_bytecode_cache(source)
    assert not cache.exists() and not optimized.exists()
    assert neighbor.read_bytes() == b"keep"


def test_mutant_always_restores_bytes_and_purges_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sample.py"
    original = b"value = 123\n"
    source.write_bytes(original)
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    item = harness.Mutant("test", source, "123", "456", "test.py::case", "FAILED test.py::case")

    def fake_pytest(_node: str) -> subprocess.CompletedProcess[str]:
        assert source.read_bytes() == b"value = 456\n"
        cache.write_bytes(b"stale-mutant")
        return subprocess.CompletedProcess([], 1, "FAILED test.py::case - AssertionError", "")

    monkeypatch.setattr(harness, "_pytest", fake_pytest)
    assert harness.run_mutant(item)[0] == "KILLED"
    assert source.read_bytes() == original
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(original).digest()
    assert not cache.exists()


def test_timeout_or_exception_restores_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 123\n")
    item = harness.Mutant("test", source, "123", "456", "test.py::case", "FAILED test.py::case")

    def timed_out(_node: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=[sys.executable], timeout=1)

    monkeypatch.setattr(harness, "_pytest", timed_out)
    assert harness.run_mutant(item)[0] == "INVALID"
    assert source.read_text() == "value = 123\n"


def test_anchors_and_parse_errors_are_invalid_without_writes(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 123\n")
    item = harness.Mutant("test", source, "missing", "x", "x", "x")
    assert harness.run_mutant(item)[0] == "INVALID"
    item = harness.Mutant("test", source, "123", "(", "x", "x")
    assert harness.run_mutant(item)[0] == "INVALID"
    assert source.read_text() == "value = 123\n"


def test_campaign_definitions_are_unique_and_cover_nine_modules() -> None:
    assert len(harness.MUTANTS) == 15
    assert [m.name for m in harness.MUTANTS] == [f"W5-M{i:02d}" for i in range(1, 16)]
    assert len({m.path for m in harness.MUTANTS}) == 9
    for item in harness.MUTANTS:
        assert item.path.read_text().count(item.old) == item.count
