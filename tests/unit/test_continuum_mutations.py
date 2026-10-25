"""Contract of the replayable mutation campaign (``continuum.mutations``).

The real campaign runs in CI (job ``continuum-evidence``).  These tests pin
what makes its verdict trustworthy on a toy repository: only pytest exit 1 is
a kill, survivors and invalid runs fail the campaign, every file is restored
byte for byte, and a dirty target or red baseline refuses to run at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.continuum import mutations
from nexus_ai_agent.continuum.mutations import CATALOG, FAMILIES, Mutation, run_campaign

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_catalogue_replays_exactly_against_this_checkout() -> None:
    assert mutations.catalog_issues(REPO_ROOT) == []


def test_every_contract_family_is_attacked() -> None:
    assert {mutation.family for mutation in CATALOG} == set(FAMILIES)


def test_version_scoped_mutations_cover_every_supported_interpreter() -> None:
    for version in ((3, 10), (3, 11), (3, 12)):
        denominator = [m for m in CATALOG if m.family == "denominator" and m.applies_to(version)]
        assert len(denominator) >= 4, version


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def toy(tmp_path: Path) -> Path:
    root = tmp_path / "toy"
    (root / "src" / "toy").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / "src" / "toy" / "__init__.py").write_text(
        "def clamp(value):\n    return max(0, min(value, 10))\n", encoding="utf-8"
    )
    (root / "tests" / "test_toy.py").write_text(
        "from toy import clamp\n\ndef test_clamp():\n    assert clamp(15) == 10\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "toy")
    return root


def _mutation(identifier: str, original: str, mutated: str, **kw: object) -> Mutation:
    return Mutation(
        identifier,
        "numerator",
        "src/toy/__init__.py",
        original,
        mutated,
        ("tests/test_toy.py::test_clamp",),
        "toy",
        **kw,  # type: ignore[arg-type]
    )


KILLED = _mutation("K", "min(value, 10)", "min(value, 11)")
SURVIVOR = _mutation("S", "max(0, ", "max(-1, ")
INVALID = _mutation("I", "def clamp(value):", "def clamp(value)")
LATER = _mutation("L", "max(0, ", "max(1, ", min_python=(9, 0))


def test_only_a_failing_test_run_counts_as_a_kill(toy: Path) -> None:
    before = (toy / "src" / "toy" / "__init__.py").read_bytes()
    report = run_campaign(toy, (KILLED, SURVIVOR, INVALID, LATER))
    statuses = {r["id"]: r["status"] for r in report["mutations"]}  # type: ignore[union-attr,index]
    assert statuses == {"K": "killed", "S": "survived", "I": "invalid", "L": "not-applicable"}
    assert report["passed"] is False
    assert "mutations not killed and restored: S, I" in report["problems"][0]  # type: ignore[index]
    assert all(r["restored"] is True for r in report["mutations"])  # type: ignore[union-attr,index]
    assert (toy / "src" / "toy" / "__init__.py").read_bytes() == before


def test_a_fully_killed_campaign_passes_with_replayable_records(toy: Path) -> None:
    report = run_campaign(toy, (KILLED,))
    assert report["passed"] is True
    (record,) = report["mutations"]  # type: ignore[misc]
    assert record["observed_exit"] == 1
    assert record["observed_failed"] == ["tests/test_toy.py::test_clamp"]
    assert record["sha256_before"] == record["sha256_after"]
    assert record["command"].startswith("python -m pytest")
    assert record["original"] == "min(value, 10)" and record["mutated"] == "min(value, 11)"


def test_dirty_targets_ambiguous_originals_and_red_baselines_refuse_to_run(toy: Path) -> None:
    ambiguous = _mutation("A", "value", "other")
    assert "occurs 2 times" in run_campaign(toy, (ambiguous,))["problems"][0]  # type: ignore[index]

    target = toy / "src" / "toy" / "__init__.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "# leftover mutant\n", encoding="utf-8")
    dirty = run_campaign(toy, (KILLED,))
    assert dirty["passed"] is False and dirty["mutations"] == []
    assert "target file is not clean" in dirty["problems"][0]  # type: ignore[index]
    target.write_text(original, encoding="utf-8")

    (toy / "tests" / "test_toy.py").write_text(
        "def test_clamp():\n    assert False\n", encoding="utf-8"
    )
    _git(toy, "commit", "-qam", "red")
    red = run_campaign(toy, (KILLED,))
    assert red["passed"] is False
    assert red["problems"] == ["baseline is not green (pytest exit 1)"]


def test_unknown_ids_are_refused(toy: Path) -> None:
    report = run_campaign(toy, (KILLED,), only=["K", "ZZ"])
    assert report["passed"] is False
    assert "unknown mutation ids: ZZ" in report["problems"]  # type: ignore[operator]


def test_the_file_is_restored_even_if_the_run_crashes(
    toy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = toy / "src" / "toy" / "__init__.py"
    before = target.read_bytes()
    calls = {"n": 0}
    real = mutations._run_tests

    def crash_second(root: Path, tests: tuple[str, ...]) -> tuple[int, list[str]]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(root, tests)

    monkeypatch.setattr(mutations, "_run_tests", crash_second)
    with pytest.raises(KeyboardInterrupt):
        run_campaign(toy, (KILLED,))
    assert target.read_bytes() == before
