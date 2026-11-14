"""Parity between ``scripts/ci_parity.py`` and the workflow it reproduces.

The runner exists because the CI selection does not fit in a workstation's
``/tmp``, and a runner that silently drifts from CI is worse than no runner: it
would report "green" for a selection CI does not run.  Four properties are
pinned here, all on pure functions plus one real temporary directory:

* the marker expression is **read from** ``.github/workflows/ci.yml`` at run time
  (scanned as text — the fast rail ships no YAML dependency, the same convention
  ``test_ci_lint_parity.py`` follows), and a workflow without a selection is a
  hard failure rather than a guess;
* **collection equivalence**: the CI selection's node ids and the union of the
  chunks' node ids must be the same set — nothing left out, nothing collected
  twice, nothing invented.  The comparison is pure, so all three failure modes
  are proven on fixtures instead of waiting for a real drift;
* **isolation**: every chunk gets its own ``nexus_ci_parity_*`` temporary
  directory through ``TMPDIR``/``TEMP``/``TMP`` and ``--basetemp``, and only that
  directory is removed — a shared path such as ``/tmp/pytest-of-<user>`` is never
  touched;
* the runner is reachable as ``make test-parity`` and declared ``.PHONY``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RUNNER = REPO_ROOT / "scripts" / "ci_parity.py"
MAKEFILE = REPO_ROOT / "Makefile"

#: The command the ``test-parity`` target must run, exactly.
PARITY_COMMAND = "python scripts/ci_parity.py"

_CI_SELECTION = re.compile(r"pytest[^\n]*-m\s+\"(?P<marker>[^\"]+)\"")


def _load_runner() -> ModuleType:
    """Import ``scripts/ci_parity.py`` by path (``scripts`` is not a package).

    The module has to be registered in :data:`sys.modules` *before* it runs: its
    dataclasses resolve their own module through that mapping, and a bare
    ``module_from_spec`` leaves them looking at ``None``.
    """
    cached = sys.modules.get("ci_parity_runner")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("ci_parity_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _recipe(makefile_text: str, target: str) -> str:
    """The tab-indented recipe lines directly under *target*."""
    recipe: list[str] = []
    inside = False
    for line in makefile_text.splitlines():
        if line.strip() == f"{target}:":
            inside = True
            continue
        if inside:
            if line.startswith("\t"):
                recipe.append(line[1:].strip())
            elif line.strip() == "" or line.startswith("#"):
                continue
            else:
                break
    return "\n".join(recipe)


# --------------------------------------------------------------------------- #
# the selection comes from the workflow
# --------------------------------------------------------------------------- #
def test_the_marker_is_read_from_the_workflow_not_copied() -> None:
    """``workflow_marker()`` returns exactly the workflow's expression."""
    runner = _load_runner()
    marker = _CI_SELECTION.search(WORKFLOW.read_text(encoding="utf-8"))
    assert marker is not None, "the workflow no longer declares a pytest -m selection"
    assert runner.workflow_marker() == marker.group("marker")


def test_a_workflow_without_a_selection_fails_closed() -> None:
    """No selection means no run: the runner never guesses what CI executes."""
    runner = _load_runner()
    try:
        runner.ci_marker("jobs:\n  test:\n    steps:\n      - run: pytest -q tests\n")
    except SystemExit as exc:
        assert "no `pytest ... -m" in str(exc)
    else:  # pragma: no cover - the guard is the point
        raise AssertionError("a workflow without a -m selection was accepted")


# --------------------------------------------------------------------------- #
# collection equivalence
# --------------------------------------------------------------------------- #
def test_collection_equivalence_holds_for_identical_sets() -> None:
    runner = _load_runner()
    proof = runner.compare_collections(
        ["a.py::t1", "a.py::t2", "b.py::t3"],
        {"tests/a": ["a.py::t1", "a.py::t2"], "tests/b": ["b.py::t3"]},
    )
    assert proof.ok
    assert proof.union == frozenset({"a.py::t1", "a.py::t2", "b.py::t3"})
    assert proof.as_dict()["equal"] is True


def test_a_test_no_chunk_collects_breaks_equivalence() -> None:
    """The union must not be allowed to be a *subset* of the CI selection."""
    runner = _load_runner()
    proof = runner.compare_collections(
        ["a.py::t1", "a.py::t2", "b.py::t3"],
        {"tests/a": ["a.py::t1"], "tests/b": ["b.py::t3"]},
    )
    assert not proof.ok
    assert proof.missing == ("a.py::t2",)
    assert proof.duplicated == () and proof.unexpected == ()


def test_a_test_collected_by_two_chunks_breaks_equivalence() -> None:
    """Running the same test twice is not parity either."""
    runner = _load_runner()
    proof = runner.compare_collections(
        ["a.py::t1", "b.py::t3"],
        {"tests/a": ["a.py::t1", "b.py::t3"], "tests/b": ["b.py::t3"]},
    )
    assert not proof.ok
    assert proof.duplicated == ("b.py::t3",)


def test_a_test_the_selection_does_not_run_breaks_equivalence() -> None:
    """A chunk that collects something CI skips (a slow test) is a drift too."""
    runner = _load_runner()
    proof = runner.compare_collections(["a.py::t1"], {"tests/a": ["a.py::t1", "a.py::slow_one"]})
    assert not proof.ok
    assert proof.unexpected == ("a.py::slow_one",)


def test_node_ids_are_parsed_from_collect_only_output() -> None:
    """``pytest --collect-only -q`` lines are the node ids; prose is not."""
    runner = _load_runner()
    output = (
        "tests/unit/test_a.py::test_one\n"
        "tests/unit/test_a.py::test_two[param-one]\n"
        "\n"
        "3 tests collected in 0.12s\n"
        "==================== warnings summary ====================\n"
    )
    assert runner.parse_node_ids(output) == frozenset(
        {"tests/unit/test_a.py::test_one", "tests/unit/test_a.py::test_two[param-one]"}
    )


def test_the_verification_kind_is_labelled_honestly() -> None:
    """A chunked local run is never presented as a GitHub Actions run."""
    runner = _load_runner()
    assert runner.VERIFICATION_KIND == "CHUNKED_SUITE_VERIFICATION_WITH_COLLECTION_EQUIVALENCE"
    assert runner.NOT_REPRODUCED, "the runner must state what it does not reproduce"
    assert any("GitHub Actions" in item for item in runner.NOT_REPRODUCED)


# --------------------------------------------------------------------------- #
# chunks
# --------------------------------------------------------------------------- #
def test_every_test_directory_is_covered_by_a_chunk() -> None:
    """No directory that holds tests may be absent from the local run."""
    runner = _load_runner()
    chunks = runner.discover_chunks()
    assert chunks, "the runner discovered no chunks"

    roots: list[Path] = []
    for chunk in chunks:
        path = REPO_ROOT / chunk.path
        assert path.is_dir(), f"chunk {chunk.path!r} is not a directory"
        assert runner._has_tests(path), f"chunk {chunk.path!r} has no test modules"
        roots.append(path)

    uncovered = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests").rglob("test_*.py")
        if not any(root in path.parents for root in roots)
    )
    assert not uncovered, f"test modules outside every chunk: {uncovered}"

    # The heaviest tree must be split, or the run does not fit a small disk.
    assert any(chunk.path.startswith("tests/creative/") for chunk in chunks), (
        "tests/creative is no longer split into per-engine chunks"
    )


def test_a_requested_subtree_selects_its_chunks_and_nothing_else() -> None:
    """``ci_parity.py tests/creative`` runs the engine chunks only."""
    runner = _load_runner()
    chunks = runner.discover_chunks()
    selected = runner.filter_chunks(chunks, ["tests/creative"])
    assert selected and all(chunk.path.startswith("tests/creative/") for chunk in selected)
    assert runner.filter_chunks(chunks, []) == chunks

    try:
        runner.filter_chunks(chunks, ["tests/not-a-tree"])
    except SystemExit as exc:
        assert "not a test chunk" in str(exc)
    else:  # pragma: no cover - the guard is the point
        raise AssertionError("an unknown path was accepted")


# --------------------------------------------------------------------------- #
# isolation and space
# --------------------------------------------------------------------------- #
def test_each_chunk_gets_its_own_temporary_directory_and_only_it_is_removed(
    tmp_path: Path, monkeypatch
) -> None:
    """A shared ``/tmp/pytest-of-<user>`` is never touched, only our own dir."""
    runner = _load_runner()
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    shared = tmp_path / "pytest-of-user"
    (shared / "pytest-0").mkdir(parents=True)
    (shared / "pytest-0" / "artefact.bin").write_bytes(b"keep me")

    inside: list[Path] = []
    with runner.isolated_tempdir() as directory:
        inside.append(directory)
        assert directory.name.startswith(runner.TEMP_PREFIX)
        assert directory.is_dir()
        (directory / "work.tmp").write_text("chunk scratch")
    assert not inside[0].exists(), "the chunk's own directory must be removed"
    assert (shared / "pytest-0" / "artefact.bin").read_bytes() == b"keep me"


def test_the_chunk_environment_points_every_temp_variable_into_the_isolated_dir(
    tmp_path: Path,
) -> None:
    """``TMPDIR``/``TEMP``/``TMP`` are the isolation; ``PYTEST_ADDOPTS`` is dropped."""
    runner = _load_runner()
    env = runner.chunk_env(tmp_path, base={"PYTEST_ADDOPTS": "--bogus", "PATH": "/bin"})
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == str(tmp_path)
    assert "PYTEST_ADDOPTS" not in env, "a stray pytest option could change the selection"
    assert env["PATH"] == "/bin", "the rest of the environment must survive"


def test_a_chunk_that_does_not_fit_is_skipped_not_started() -> None:
    """A partial parity run reports the part it did not cover."""
    runner = _load_runner()
    oversized = runner.Chunk("tests/architecture", budget_bytes=1 << 62)
    assert not runner.fits(oversized, available=runner.free_bytes())
    assert runner.fits(oversized, available=oversized.estimated_bytes)
    assert not runner.fits(oversized, available=oversized.estimated_bytes - 1)
    assert all(chunk.estimated_bytes > 0 for chunk in runner.discover_chunks())
    assert runner.free_bytes() > 0


def test_the_chunk_command_is_the_ci_selection_for_one_directory(tmp_path: Path) -> None:
    """The argv keeps CI's flags and adds only the chunk and its temp root."""
    runner = _load_runner()
    argv = runner.pytest_argv(runner.Chunk("tests/unit"), tmp_root=tmp_path, marker="not slow")
    assert argv[-1] == "tests/unit", "the chunk must be the last argument"
    for flag in ("-q", "-rfEs", "--tb=short"):
        assert flag in argv, f"the runner dropped {flag}"
    # ``-m`` appears twice: the interpreter's ``-m pytest`` and the marker.  The
    # marker is the one after the module name.
    marker_index = argv.index("-m", argv.index("pytest"))
    assert argv[marker_index + 1] == "not slow"
    assert argv[argv.index("--basetemp") + 1] == str(tmp_path)


# --------------------------------------------------------------------------- #
# the rail is wired
# --------------------------------------------------------------------------- #
def test_makefile_exposes_the_parity_target_and_it_is_phony() -> None:
    """The runner is reachable through ``make``, and cannot be shadowed by a file."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert PARITY_COMMAND in _recipe(text, "test-parity"), (
        f"`make test-parity` must run {PARITY_COMMAND!r}"
    )
    phony = next((line for line in text.splitlines() if line.startswith(".PHONY:")), "")
    assert "test-parity" in phony.split(), "`test-parity` must be declared .PHONY"
