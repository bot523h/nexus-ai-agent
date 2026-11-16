#!/usr/bin/env python3
"""Reproduce CI's ``test`` selection locally, chunk by chunk, provably.

CI runs the whole suite in **one** pytest process::

    pytest -v -rfEs -m "not slow" --tb=short

That selection does not fit on a workstation with a small ``/tmp``.  The mutation
campaigns alone (``tests/creative/atlas`` copies a tree per mutant and keeps a
kill ledger for the batch) write more than 15 GB into pytest's temporary root, so
a single-process run of the *same* selection dies with
``OSError: [Errno 28] No space left on device`` — which pytest reports as
*hundreds* of phantom failures.  A red run that is really a full disk is worse
than no run at all: it looks like a broken branch.

This runner keeps the selection **identical** and changes only the shape of the
run, and it proves that rather than asserting it:

* the marker expression is read from ``.github/workflows/ci.yml`` at run time —
  not copied here, so the runner cannot drift from the workflow;
* **collection equivalence**: the node ids pytest collects under that marker for
  the whole ``tests`` tree are compared with the union of the node ids collected
  for the chunks.  The union must equal the CI selection exactly — no test left
  out, no test counted twice.  The proof runs before any test executes, so a red
  parity run cannot hide behind a chunk boundary;
* every chunk runs in its **own isolated temporary directory**
  (``tempfile.mkdtemp(prefix="nexus_ci_parity_")``) with ``TMPDIR``/``TEMP``/``TMP``
  and ``--basetemp`` pointed into it.  Shared ``/tmp`` paths are never deleted:
  only the directory this chunk created is removed, immediately after it;
* a chunk whose estimated footprint does not fit in the free space is reported
  as **skipped for space** instead of being started and left to poison the
  chunks after it.

The result is labelled honestly: :data:`VERIFICATION_KIND` is
``CHUNKED_SUITE_VERIFICATION_WITH_COLLECTION_EQUIVALENCE``.  It is *not* a
GitHub Actions run, it does not reproduce the runner image, the service
containers, the PostgreSQL legs or the ``pull_request`` triggers; it reproduces
the test selection and proves coverage of that selection.

Usage::

    python scripts/ci_parity.py                  # every chunk, in CI order
    python scripts/ci_parity.py tests/unit       # one tree, still chunked
    python scripts/ci_parity.py --list           # show the chunks, run nothing
    python scripts/ci_parity.py --json-out /tmp/parity.json
    python scripts/ci_parity.py --keep-temp      # leave each chunk's temp dir
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: How the report describes itself.  A chunked local run is not a CI run.
VERIFICATION_KIND = "CHUNKED_SUITE_VERIFICATION_WITH_COLLECTION_EQUIVALENCE"

#: What this runner does *not* reproduce (stated in the JSON, not just the docs).
NOT_REPRODUCED: tuple[str, ...] = (
    "the GitHub Actions runner image and its installed system packages",
    "service containers (PostgreSQL for the migrate/parity legs)",
    "the pull_request trigger, required checks and job timeouts",
    "jobs other than `test` (lint, dependency audit, python-parity, migrate-postgres)",
)

#: Trees whose children are chunks in their own right.  ``tests/creative`` holds
#: one directory per engine, and the mutation campaigns in them are what needs
#: the space; everything else is chunked per top-level directory.
SPLIT_ONE_LEVEL: tuple[str, ...] = ("tests/creative",)

#: Free space a chunk is assumed to need, in bytes.  The ATLAS campaigns are the
#: outliers (one directory-wide run peaked at ~17 GB here); everything else stays
#: in the low hundreds of MB, so the default only has to clear that comfortably.
DEFAULT_BUDGET_BYTES = 4 * 1024**3

#: Trees that are one logical chunk but too heavy to run as one directory.  Each
#: entry lists **disjoint, exhaustive** groups of test modules: disjoint so no test
#: is counted twice, exhaustive so no test is left out.  Exhaustiveness is not a
#: promise made here — ``compare_collections`` proves it before anything runs, so a
#: module added to the tree and forgotten here turns the run red (exit 2) instead of
#: silently dropping out of the local reproduction.
ATLAS = "tests/creative/atlas"
SUB_CHUNKS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    ATLAS: (
        (
            "mutation-120",
            (f"{ATLAS}/test_prometheus_mutations_120.py",),
        ),
        (
            "falsification-160",
            (f"{ATLAS}/test_prometheus_falsification_160.py",),
        ),
        (
            "titan-mutations",
            (
                f"{ATLAS}/test_titan_olympus_mutations.py",
                f"{ATLAS}/test_titan_olympus_mutation_killers.py",
            ),
        ),
        (
            "prometheus-governance",
            (
                f"{ATLAS}/test_atlas_spine_governance.py",
                f"{ATLAS}/test_prometheus_coverage_edges.py",
                f"{ATLAS}/test_prometheus_playground_e2e.py",
                f"{ATLAS}/test_prometheus_studio_playground.py",
                f"{ATLAS}/test_prometheus_verifier_and_governance.py",
                f"{ATLAS}/test_studio_render_adapter.py",
            ),
        ),
        (
            "waves-13-22",
            (
                f"{ATLAS}/test_wave13_portal_metrics.py",
                f"{ATLAS}/test_wave14_atmospheric_scattering.py",
                f"{ATLAS}/test_wave15_eikonal_crowd.py",
                f"{ATLAS}/test_wave16_voronoi_fracture.py",
                f"{ATLAS}/test_wave17_anamorphic_optics.py",
                f"{ATLAS}/test_wave18_pontryagin_pacing.py",
                f"{ATLAS}/test_wave19_feature_film_compiler.py",
                f"{ATLAS}/test_wave20_adversarial.py",
                f"{ATLAS}/test_wave20_staged_spine.py",
                f"{ATLAS}/test_wave21_30_benchmarks.py",
                f"{ATLAS}/test_wave21_sdf_gi.py",
                f"{ATLAS}/test_wave22_spectral_ocean.py",
            ),
        ),
        (
            "waves-23-30-and-solvers",
            (
                f"{ATLAS}/test_adjoint_inverse_cinema_solver.py",
                f"{ATLAS}/test_anamorphic_scattering_renderer.py",
                f"{ATLAS}/test_colour_output_transform.py",
                f"{ATLAS}/test_media_cleanup_logging.py",
                f"{ATLAS}/test_nash_dramatic_staging_solver.py",
                f"{ATLAS}/test_port_hamiltonian_world_solver.py",
                f"{ATLAS}/test_riemannian_camera_choreographer.py",
                f"{ATLAS}/test_wasserstein_schrodinger_bridge.py",
                f"{ATLAS}/test_wave23_pyro_blackbody.py",
                f"{ATLAS}/test_wave24_rigid_contact_pgs.py",
                f"{ATLAS}/test_wave25_botanical_wind.py",
                f"{ATLAS}/test_wave26_film_optic.py",
                f"{ATLAS}/test_wave27_modal_foley.py",
                f"{ATLAS}/test_wave28_showrunner_bible.py",
                f"{ATLAS}/test_wave29_media_sine.py",
                f"{ATLAS}/test_wave30_olympus_master.py",
            ),
        ),
    ),
}

#: Space each sub-chunk is assumed to need.  The two campaign files copy and run
#: the tree once per mutant, which is where the gigabytes come from; the wave and
#: solver groups are ordinary test modules.
#:
#: ``atlas#mutation-120`` is the only one measured rather than estimated.  One of its
#: tests builds the whole kill ledger inside a single pytest process and never frees a
#: tree, so the peak is per *process* and no module split carves it up — with bytecode
#: writing on (the default) it was ~53 MB per mutant and ran the machine out of disk at
#: mutant 115 of 120.  ``chunk_env`` now sets ``PYTHONDONTWRITEBYTECODE=1``, which takes
#: the same campaign to ~19 MB per mutant (measured at 1.35 GiB for 70 trees and
#: 1.75 GiB for 91, i.e. ~2.3 GiB at 120) — and it completes.  The budget is that
#: measurement with room to spare for the repository copy; the runner still refuses the
#: sub-chunk (loudly) on a machine without the room.
SPACE_BUDGETS: dict[str, int] = {
    "tests/creative#root": 1 * 1024**3,
    f"{ATLAS}#mutation-120": 3 * 1024**3,
    f"{ATLAS}#falsification-160": 3 * 1024**3,
    f"{ATLAS}#titan-mutations": 3 * 1024**3,
    f"{ATLAS}#prometheus-governance": 2 * 1024**3,
    f"{ATLAS}#waves-13-22": 2 * 1024**3,
    f"{ATLAS}#waves-23-30-and-solvers": 2 * 1024**3,
}

#: Prefix of the per-chunk temporary directory.  The runner deletes only
#: directories it created with this prefix — never a shared path.
TEMP_PREFIX = "nexus_ci_parity_"

_CI_SELECTION = re.compile(r"pytest[^\n]*-m\s+\"(?P<marker>[^\"]+)\"")
_NODE_ID = re.compile(r"^(?P<path>[\w./-]+\.py)::")


# --------------------------------------------------------------------------- #
# the CI selection (read from the workflow, never copied)
# --------------------------------------------------------------------------- #
def ci_marker(workflow_text: str) -> str:
    """The ``-m`` marker expression of the workflow's pytest invocation."""
    match = _CI_SELECTION.search(workflow_text)
    if match is None:
        raise SystemExit(
            f'no `pytest ... -m "..."` selection found in {WORKFLOW}; '
            "the runner will not guess what CI runs"
        )
    return match.group("marker")


def workflow_marker() -> str:
    """The marker expression of the repository's workflow."""
    return ci_marker(WORKFLOW.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# chunks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Chunk:
    """One pytest invocation: a directory, or a named group of modules inside it."""

    #: The test tree this chunk belongs to.  Filtering and the collection proof use
    #: it; for sub-chunks it is the *tree*, not the module list.
    path: str
    #: Free space this chunk is expected to need (see ``SPACE_BUDGETS``).
    budget_bytes: int = DEFAULT_BUDGET_BYTES
    #: Modules pytest is pointed at.  Empty means "the whole ``path`` tree".
    targets: tuple[str, ...] = ()
    #: Human-readable name for a sub-chunk (``tests/…/atlas#mutation-120``).
    label: str = ""

    @property
    def estimated_bytes(self) -> int:
        """The footprint this chunk is expected to need."""
        return self.budget_bytes

    @property
    def name(self) -> str:
        """How this chunk is named in the report (sub-chunks carry their label)."""
        return self.label or self.path

    @property
    def argv_targets(self) -> tuple[str, ...]:
        """The paths handed to pytest: the module group, or the whole tree."""
        return self.targets or (self.path,)


def _has_tests(directory: Path) -> bool:
    """True when *directory* contains at least one test module."""
    return any(directory.rglob("test_*.py"))


def _relative(directory: Path) -> str:
    return directory.relative_to(REPO_ROOT).as_posix()


def discover_chunks(tests_root: Path = TESTS_ROOT) -> list[Chunk]:
    """Every directory pytest should be pointed at, in CI's alphabetical order.

    A tree listed in :data:`SPLIT_ONE_LEVEL` contributes its children; every other
    tree contributes itself.  Directories without test modules are dropped, so a
    support package (``tests/fixtures``) never becomes a chunk.
    """
    chunks: list[Chunk] = []

    def _sub_chunks(tree: str) -> list[Chunk]:
        """The named module groups of *tree* (see :data:`SUB_CHUNKS`)."""
        return [
            Chunk(
                tree,
                SPACE_BUDGETS.get(f"{tree}#{suffix}", DEFAULT_BUDGET_BYTES),
                targets=modules,
                label=f"{tree}#{suffix}",
            )
            for suffix, modules in SUB_CHUNKS[tree]
        ]

    def _chunk(path: Path) -> Chunk:
        relative = (
            path.relative_to(REPO_ROOT).as_posix()
            if path.is_relative_to(REPO_ROOT)
            else path.as_posix()
        )
        return Chunk(relative, SPACE_BUDGETS.get(relative, DEFAULT_BUDGET_BYTES))

    for directory in sorted(path for path in tests_root.iterdir() if path.is_dir()):
        relative = (
            directory.relative_to(REPO_ROOT).as_posix()
            if directory.is_relative_to(REPO_ROOT)
            else directory.as_posix()
        )
        if relative in SPLIT_ONE_LEVEL:
            top_level_tests = tuple(
                _relative(f)
                for f in sorted(directory.iterdir())
                if f.is_file() and f.name.startswith("test_") and f.name.endswith(".py")
            )
            if top_level_tests:
                label = f"{relative}#root"
                chunks.append(
                    Chunk(
                        relative,
                        SPACE_BUDGETS.get(label, DEFAULT_BUDGET_BYTES),
                        targets=top_level_tests,
                        label=label,
                    )
                )
            children = sorted(child for child in directory.iterdir() if child.is_dir())
            for child in children:
                if not _has_tests(child):
                    continue
                child_relative = _relative(child)
                if child_relative in SUB_CHUNKS:
                    chunks.extend(_sub_chunks(child_relative))
                    continue
                chunks.append(_chunk(child))
            continue
        if _has_tests(directory):
            chunks.append(_chunk(directory))
    return chunks


def filter_chunks(chunks: Iterable[Chunk], requested: Sequence[str]) -> list[Chunk]:
    """The chunks that belong to one of the *requested* paths (or all of them)."""
    every = list(chunks)
    wanted = [Path(path).as_posix().rstrip("/") for path in requested]
    if not wanted:
        return every
    selected = [
        chunk
        for chunk in every
        if any(
            chunk.path == path
            or chunk.path.startswith(f"{path}/")
            or chunk.name == path
            or path in chunk.targets
            for path in wanted
        )
    ]
    missing = [
        path
        for path in wanted
        if not any(
            path == chunk.path
            or path == chunk.name
            or path in chunk.targets
            or path.startswith(f"{chunk.path}/")
            or chunk.path.startswith(f"{path}/")
            for chunk in every
        )
    ]
    if missing:
        raise SystemExit(f"not a test chunk: {', '.join(missing)}")
    return selected


# --------------------------------------------------------------------------- #
# collection equivalence
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CollectionEquivalence:
    """The proof that the chunks collect exactly the CI selection."""

    ci_ids: frozenset[str]
    per_chunk: Mapping[str, frozenset[str]]
    #: Node ids collected by more than one chunk (must be empty).
    duplicated: tuple[str, ...] = ()
    #: Node ids in the CI selection that no chunk collects (must be empty).
    missing: tuple[str, ...] = ()
    #: Node ids a chunk collects that the CI selection does not (must be empty).
    unexpected: tuple[str, ...] = ()

    @property
    def union(self) -> frozenset[str]:
        collected: set[str] = set()
        for ids in self.per_chunk.values():
            collected |= ids
        return frozenset(collected)

    @property
    def ok(self) -> bool:
        return not self.duplicated and not self.missing and not self.unexpected

    def as_dict(self) -> dict[str, object]:
        return {
            "ci_selection_ids": len(self.ci_ids),
            "chunk_union_ids": len(self.union),
            "duplicated": list(self.duplicated),
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "equal": self.ok,
            "per_chunk": {path: len(ids) for path, ids in sorted(self.per_chunk.items())},
        }


def compare_collections(
    ci_ids: Iterable[str], per_chunk: Mapping[str, Iterable[str]]
) -> CollectionEquivalence:
    """Compare the CI selection with the union of the chunks (pure)."""
    selection = frozenset(ci_ids)
    chunk_sets = {path: frozenset(ids) for path, ids in per_chunk.items()}
    seen: dict[str, str] = {}
    duplicated: set[str] = set()
    collected: set[str] = set()
    for path, ids in chunk_sets.items():
        for node in ids:
            if node in seen and seen[node] != path:
                duplicated.add(node)
            seen[node] = path
            collected.add(node)
    return CollectionEquivalence(
        ci_ids=selection,
        per_chunk=chunk_sets,
        duplicated=tuple(sorted(duplicated)),
        missing=tuple(sorted(selection - collected)),
        unexpected=tuple(sorted(collected - selection)),
    )


def parse_node_ids(output: str) -> frozenset[str]:
    """The node ids in ``pytest --collect-only -q`` output."""
    node_ids: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("=", "no tests ran", "ERROR", "warning")):
            continue
        if _NODE_ID.match(stripped):
            node_ids.add(stripped)
    return frozenset(node_ids)


def collect_node_ids(
    paths: Sequence[str], *, marker: str, env: Mapping[str, str]
) -> frozenset[str]:
    """Collect (never run) the node ids of *paths* under *marker*."""
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-m",
        marker,
        "-p",
        "no:cacheprovider",
        *paths,
    ]
    completed = subprocess.run(  # noqa: S603 - argv is built here, never from input
        argv, cwd=REPO_ROOT, env=dict(env), capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise SystemExit(
            "collection failed (pytest --collect-only returned "
            f"{completed.returncode}); the run cannot prove equivalence:\n"
            f"{completed.stdout[-4000:]}\n{completed.stderr[-2000:]}"
        )
    return parse_node_ids(completed.stdout + completed.stderr)


# --------------------------------------------------------------------------- #
# space and isolation
# --------------------------------------------------------------------------- #
def collection_scope(requested: Sequence[str], chunks: Sequence[Chunk]) -> list[str]:
    """The paths the CI selection is collected over for this run.

    A run is scoped to what was asked for: the whole ``tests`` tree by default, a
    requested tree, a requested module — or, when a *chunk name* is requested
    (``tests/creative/atlas#mutation-120``), that chunk's own targets.  Handing
    pytest the chunk name itself would fail ("file or directory not found"), and
    handing it the parent tree would collect far more than was asked for.
    """
    if not requested:
        return ["tests"]
    scope: list[str] = []
    for path in requested:
        if (REPO_ROOT / path).exists():
            scope.append(path)
            continue
        matched = [chunk for chunk in chunks if chunk.name == path]
        scope.extend(target for chunk in matched for target in chunk.argv_targets)
        if not matched:
            scope.append(path)  # let pytest report the bad path
    return list(dict.fromkeys(scope))


def free_bytes(path: Path | None = None) -> int:
    """Free space on the filesystem holding *path* (``/tmp`` by default)."""
    target = path or Path(tempfile.gettempdir())
    return shutil.disk_usage(target).free


def fits(chunk: Chunk, available: int) -> bool:
    """True when *chunk* can be started with *available* bytes free."""
    return available >= chunk.estimated_bytes


@contextmanager
def isolated_tempdir(prefix: str = TEMP_PREFIX) -> Iterator[Path]:
    """A private temporary directory, removed when the chunk finishes.

    Only this directory is ever deleted — never a shared path such as
    ``/tmp/pytest-of-<user>``, which may hold another agent's or another run's
    artifacts.
    """
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def chunk_env(tmpdir: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of one chunk: every temp variable points into *tmpdir*.

    ``PYTHONDONTWRITEBYTECODE=1`` is set for the same reason the temp directory is
    isolated.  The ATLAS campaign copies the whole tree once per mutant and runs
    pytest inside each copy; without this, every copy also grows its own
    ``__pycache__`` (and inherits the repository's), which is pure waste in a
    directory that lives for one chunk.  Measured on this machine: the campaign
    went from "ran out of disk at mutant 115 of 120" to completing inside the same
    budget.  It changes nothing about what runs, only whether bytecode is cached.
    """
    env = dict(os.environ if base is None else base)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = str(tmpdir)
    return env


def pytest_argv(chunk: Chunk, *, tmp_root: Path, marker: str) -> list[str]:
    """The pytest command line for *chunk*: the CI selection, one tree or module group."""
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-rfEs",
        "-m",
        marker,
        "--tb=short",
        "--basetemp",
        str(tmp_root),
        "-p",
        "no:cacheprovider",
        *chunk.argv_targets,
    ]


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Result:
    """The outcome of one chunk."""

    chunk: str
    exit_code: int
    summary: str
    collected: int = 0
    duration_seconds: float = 0.0


def _summary(output: str) -> str:
    """The last line pytest prints that carries the counts."""
    for line in reversed(output.strip().splitlines()):
        stripped = line.strip()
        if " passed" in stripped or " failed" in stripped or " error" in stripped:
            return stripped
    return output.strip().splitlines()[-1] if output.strip() else "(no output)"


def run_chunk(
    chunk: Chunk,
    *,
    marker: str,
    keep_temp: bool = False,
    collected: int = 0,
) -> Result:
    """Run one chunk inside its own temporary directory (never raises on red)."""
    started = time.monotonic()
    tmpdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        argv = pytest_argv(chunk, tmp_root=tmpdir, marker=marker)
        completed = subprocess.run(  # noqa: S603 - argv is built here, never from input
            argv,
            cwd=REPO_ROOT,
            env=chunk_env(tmpdir),
            capture_output=True,
            text=True,
            check=False,
        )
        output = completed.stdout + completed.stderr
        print(output, end="" if output.endswith("\n") else "\n")
        if keep_temp:
            print(f"[ci_parity] kept this chunk's temporary directory: {tmpdir}")
    finally:
        if not keep_temp:
            # Only the directory this chunk created is removed — never a shared
            # path such as /tmp/pytest-of-<user>.
            shutil.rmtree(tmpdir, ignore_errors=True)
    return Result(
        chunk=chunk.name,
        exit_code=completed.returncode,
        summary=_summary(output),
        collected=collected,
        duration_seconds=time.monotonic() - started,
    )


def _revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the chunks, prove collection equivalence, report; returns exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="test trees to run (default: all)")
    parser.add_argument("--list", action="store_true", help="list the chunks and exit")
    parser.add_argument("--keep-temp", action="store_true", help="keep each chunk's temp dir")
    parser.add_argument("--json-out", default=None, help="write a machine-readable record")
    parser.add_argument(
        "--budget-gb", type=float, default=None, help="override the per-chunk space budget"
    )
    parser.add_argument(
        "--skip-collection-proof",
        action="store_true",
        help="run the chunks without proving collection equivalence (reported as such)",
    )
    args = parser.parse_args(argv)

    marker = workflow_marker()
    chunks = filter_chunks(discover_chunks(), args.paths)
    if args.budget_gb is not None:
        budget = int(args.budget_gb * 1024**3)
        chunks = [Chunk(chunk.path, budget) for chunk in chunks]
    if args.list:
        for chunk in chunks:
            print(chunk.name)
        return 0

    proof: CollectionEquivalence | None = None
    if args.skip_collection_proof:
        print("[ci_parity] collection proof skipped on request — parity is NOT proven")
    else:
        print(f"[ci_parity] collecting the CI selection (-m {marker!r}) …", flush=True)
        # The proof is scoped to what was asked for: a run over one tree must prove
        # that *this tree's* chunks are exactly its slice of the CI selection, not
        # that they cover the other trees too.
        scope = collection_scope(list(args.paths), chunks)
        with isolated_tempdir() as tmpdir:
            env = chunk_env(tmpdir)
            ci_ids = collect_node_ids(scope, marker=marker, env=env)
            per_chunk = {
                chunk.name: collect_node_ids(list(chunk.argv_targets), marker=marker, env=env)
                for chunk in chunks
            }
        proof = compare_collections(ci_ids, per_chunk)
        print(
            f"[ci_parity] collection: CI {len(proof.ci_ids)} ids, union {len(proof.union)} ids, "
            f"missing {len(proof.missing)}, duplicated {len(proof.duplicated)}, "
            f"unexpected {len(proof.unexpected)}"
        )
        if not proof.ok:
            print("[ci_parity] COLLECTION EQUIVALENCE FAILED — not running the suites")
            for label in ("missing", "duplicated", "unexpected"):
                for node in getattr(proof, label)[:20]:
                    print(f"  {label}: {node}")
        else:
            print(
                "[ci_parity] collection equivalence proven: the chunks are exactly the CI selection"
            )

    results: list[Result] = []
    skipped: list[Chunk] = []
    for chunk in chunks:
        available = free_bytes()
        if not fits(chunk, available):
            skipped.append(chunk)
            print(
                f"=== {chunk.name}: SKIPPED (needs ~{chunk.estimated_bytes / 1024**3:.0f} GiB,"
                f" {available / 1024**3:.1f} GiB free)",
                flush=True,
            )
            continue
        collected = len(proof.per_chunk.get(chunk.name, ())) if proof else 0
        print(f"=== {chunk.name}", flush=True)
        results.append(
            run_chunk(chunk, marker=marker, keep_temp=args.keep_temp, collected=collected)
        )

    print("\n=== summary")
    for result in results:
        state = "ok" if result.exit_code == 0 else "FAILED"
        print(
            f"{state:<6} {result.chunk:<40} {result.summary}"
            f"  [{result.collected} collected, {result.duration_seconds:.1f}s]"
        )
    for chunk in skipped:
        print(f"SPACE  {chunk.name:<40} not run (free space below the budget)")

    if args.json_out:
        record: dict[str, object] = {
            "schema": "nexus-ci-parity-run/v2",
            "verification": VERIFICATION_KIND,
            "not_a_substitute_for": list(NOT_REPRODUCED),
            "selection_marker": marker,
            "selection_source": str(WORKFLOW.relative_to(REPO_ROOT)),
            "revision": _revision(),
            "collection_equivalence": proof.as_dict() if proof else None,
            "chunks": [
                {
                    "path": result.chunk,
                    "exit_code": result.exit_code,
                    "summary": result.summary,
                    "collected": result.collected,
                    "duration_seconds": round(result.duration_seconds, 2),
                }
                for result in results
            ],
            "skipped_for_space": [
                {"path": chunk.name, "needs_bytes": chunk.estimated_bytes} for chunk in skipped
            ],
        }
        Path(args.json_out).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json_out}")

    failed = [result for result in results if result.exit_code != 0]
    if skipped:
        print(
            f"\n{len(skipped)} chunk(s) were not run: the verdict is partial by construction,"
            " and this report says so instead of implying the whole selection passed."
        )
    if proof is not None and not proof.ok:
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
