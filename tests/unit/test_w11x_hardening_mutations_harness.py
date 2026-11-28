"""Wave W11X: validate the hardening mutation campaign harness itself.

A mutation campaign that cannot fail is decoration. These tests pin the three
properties that make its ``killed`` verdict mean something:

* every mutant names a real source and a real focused test, and its ``old``
  snippet occurs exactly once (a drifted snippet would silently become a no-op);
* the runner restores the source byte-for-byte, including when the mutant makes
  the module unimportable;
* only a *test failure* counts as a kill — a collection error, a syntax error or
  a zero-test run is ``invalid``, never ``killed``.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "w11x_hardening_mutations.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("w11x_hardening_mutations", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


harness = _load()


def test_the_campaign_covers_every_guarded_subsystem() -> None:
    mutations = harness.MUTATIONS
    assert len(mutations) == 19
    sources = Counter(mutation.source for mutation in mutations)
    assert set(sources) == {
        "src/nexus_ai_agent/maintenance/backup.py",
        "src/nexus_ai_agent/maintenance/ci_selftest.py",
        "src/nexus_ai_agent/maintenance/housekeeping.py",
        "src/nexus_ai_agent/api/studio_surface.py",
        ".agents/evidence/pr-convergence-2026-11-27/tools/close_prs.py",
        ".agents/evidence/pr-convergence-2026-11-27/tools/board_reconcile.py",
        ".agents/evidence/pr-convergence-2026-11-27/tools/analyze.py",
    }
    assert len({mutation.name for mutation in mutations}) == 19


def test_preflight_is_structurally_valid() -> None:
    mutations = harness.preflight()
    assert len(mutations) == 19
    for mutation in mutations:
        assert mutation.old != mutation.new
        assert (ROOT / mutation.source).is_file()
        assert (ROOT / mutation.test).is_file()
        text = (ROOT / mutation.source).read_text(encoding="utf-8")
        assert text.count(mutation.old) == 1, mutation.name


def test_preflight_rejects_a_drifted_snippet(monkeypatch: pytest.MonkeyPatch) -> None:
    original = harness.MUTATIONS
    monkeypatch.setattr(
        harness,
        "MUTATIONS",
        (
            harness.Mutation(
                "drifted",
                "src/nexus_ai_agent/maintenance/housekeeping.py",
                "tests/unit/test_housekeeping_contract.py",
                "this snippet no longer exists",
                "and neither does this",
            ),
        ),
    )
    with pytest.raises(harness.MutationCampaignError, match="matches 0 times"):
        harness.preflight()
    monkeypatch.setattr(harness, "MUTATIONS", original)


def test_preflight_rejects_a_missing_test(monkeypatch: pytest.MonkeyPatch) -> None:
    original = harness.MUTATIONS
    monkeypatch.setattr(
        harness,
        "MUTATIONS",
        (
            harness.Mutation(
                "orphan",
                "src/nexus_ai_agent/maintenance/housekeeping.py",
                "tests/unit/test_does_not_exist.py",
                "BLOB_BACKUP_PREFIX = ",
                "BLOB_BACKUP_PREFIX_BROKEN = ",
            ),
        ),
    )
    with pytest.raises(harness.MutationCampaignError, match="missing test"):
        harness.preflight()
    monkeypatch.setattr(harness, "MUTATIONS", original)


def test_a_source_that_stops_importing_is_invalid_not_killed() -> None:
    """A syntax error kills the *run*, not the mutant: ``invalid`` is not a pass."""

    class _Completed:
        returncode = 1
        stdout = "ERROR collecting tests/unit/test_x.py\n1 error"
        stderr = ""

    assert harness._classify_mutant_outcome(_Completed()) == "invalid"


def test_only_a_real_test_failure_counts_as_a_kill() -> None:
    class _Completed:
        returncode = 1
        stdout = "2 failed, 3 passed in 0.4s"
        stderr = ""

    assert harness._classify_mutant_outcome(_Completed()) == "killed"


def test_a_green_run_is_a_survivor() -> None:
    class _Completed:
        returncode = 0
        stdout = "5 passed"
        stderr = ""

    assert harness._classify_mutant_outcome(_Completed()) == "survived"


def test_a_run_that_collected_nothing_is_invalid() -> None:
    class _Completed:
        returncode = 1
        stdout = "no tests ran in 0.01s"
        stderr = ""

    assert harness._classify_mutant_outcome(_Completed()) == "invalid"


def test_the_mutant_runs_with_the_repository_src_on_the_path(tmp_path: Path) -> None:
    env = harness._test_env(ROOT)
    assert env["PYTHONPATH"].split(":")[0] == str(ROOT / "src")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_the_runner_restores_the_source_even_when_the_mutant_breaks_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore lives in ``finally``: a hostile mutant cannot leave debris."""
    target = ROOT / "src/nexus_ai_agent/maintenance/housekeeping.py"
    before = target.read_bytes()

    class _Completed:
        returncode = 1
        stdout = "ImportError while importing module\n1 error"
        stderr = ""

    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: _Completed())
    mutation = harness.Mutation(
        "restore_probe",
        "src/nexus_ai_agent/maintenance/housekeeping.py",
        "tests/unit/test_housekeeping_contract.py",
        "BLOB_BACKUP_PREFIX = ",
        "BLOB_BACKUP_PREFIX =  (",
    )
    assert harness.run_mutation(mutation) == "invalid"
    assert target.read_bytes() == before, "the source must be restored byte-for-byte"
