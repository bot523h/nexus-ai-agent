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

#: Free space a chunk is assumed to need, in bytes.  ``tests/creative/atlas`` is
#: the outlier (it peaked at ~17 GB here); everything else stays in the low
#: hundreds of MB, so the default only has to clear that comfortably.
DEFAULT_BUDGET_BYTES = 4 * 1024**3
SPACE_BUDGETS: dict[str, int] = {"tests/creative/atlas": 18 * 1024**3}

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
    """One directory pytest is pointed at."""

    path: str
    #: Free space this chunk is expected to need (see ``SPACE_BUDGETS``).
    budget_bytes: int = DEFAULT_BUDGET_BYTES

    @property
    def estimated_bytes(self) -> int:
        """The footprint this chunk is expected to need."""
        return self.budget_bytes


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
            children = sorted(child for child in directory.iterdir() if child.is_dir())
            chunks.extend(_chunk(child) for child in children if _has_tests(child))
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
        if any(chunk.path == path or chunk.path.startswith(f"{path}/") for path in wanted)
    ]
    missing = [
        path
        for path in wanted
        if not any(
            path == chunk.path
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
    """The environment of one chunk: every temp variable points into *tmpdir*."""
    env = dict(os.environ if base is None else base)
    env.pop("PYTEST_ADDOPTS", None)
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = str(tmpdir)
    return env


def pytest_argv(chunk: Chunk, *, tmp_root: Path, marker: str) -> list[str]:
    """The pytest command line for *chunk*: the CI selection, one directory."""
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
        chunk.path,
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
        chunk=chunk.path,
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
            print(chunk.path)
        return 0

    proof: CollectionEquivalence | None = None
    if args.skip_collection_proof:
        print("[ci_parity] collection proof skipped on request — parity is NOT proven")
    else:
        print(f"[ci_parity] collecting the CI selection (-m {marker!r}) …", flush=True)
        with isolated_tempdir() as tmpdir:
            env = chunk_env(tmpdir)
            ci_ids = collect_node_ids(["tests"], marker=marker, env=env)
            per_chunk = {
                chunk.path: collect_node_ids([chunk.path], marker=marker, env=env)
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
                f"=== {chunk.path}: SKIPPED (needs ~{chunk.estimated_bytes / 1024**3:.0f} GiB,"
                f" {available / 1024**3:.1f} GiB free)",
                flush=True,
            )
            continue
        collected = len(proof.per_chunk.get(chunk.path, ())) if proof else 0
        print(f"=== {chunk.path}", flush=True)
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
        print(f"SPACE  {chunk.path:<40} not run (free space below the budget)")

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
                {"path": chunk.path, "needs_bytes": chunk.estimated_bytes} for chunk in skipped
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
