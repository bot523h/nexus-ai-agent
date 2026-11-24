"""Wave 11: validate the 30-mutation campaign harness itself."""

from __future__ import annotations

import hashlib
import importlib.util
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


def test_run_mutation_kills_and_restores() -> None:
    mutation = harness.MUTATIONS[0]
    source = ROOT / mutation.source
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    outcome = harness.run_mutation(mutation)
    assert outcome == "killed"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert source.read_text(encoding="utf-8").count(mutation.old) == 1


def test_run_mutation_detects_survivor_and_restores() -> None:
    mutation = harness.Mutation(
        name="neutral",
        wave=1,
        source="src/nexus_ai_agent/storage/sqlite_pragma_calculus.py",
        test="tests/unit/test_w10x_wave1_sqlite_pragma_calculus.py",
        old="import math\n",
        new="import math  # neutral mutation\n",
    )
    source = ROOT / mutation.source
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    outcome = harness.run_mutation(mutation)
    assert outcome == "survived"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert "# neutral mutation" not in source.read_text(encoding="utf-8")
