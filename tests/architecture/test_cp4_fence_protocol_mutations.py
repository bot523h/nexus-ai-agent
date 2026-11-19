"""Real source-mutation campaign for the CP4 lease/fence protocol and the trust anchor.

Not an adversarial-input test: for each entry in :data:`MUTANTS` one genuine source
edit is applied to an isolated copy of the package, the mutated source is parsed with
:func:`ast.parse` (a mutant that does not compile proves nothing), and the targeted
test file is run in a *subprocess*. A mutant counts as ``MUTANT KILLED`` only when
pytest exits with ``1`` (TESTS_FAILED) and reports a real ``FAILED test_...`` node —
never a collection, import or syntax crash.

Two layers are covered, because they are the two places where "looks correct" used
to be enough:

* ``storage/personal_cloud_vault/byos_drivers.py`` — the two-phase GC fence. The
  mutants remove, one at a time, the conditional claim, the ownership checks, the
  post-retirement lease read, the conditional (create-if-absent) rollback and the
  envelope-identity guard. Each removal is a plausible refactor and each one breaks
  a specific published guarantee.
* ``creative/storyboard_compiler/editorial_cut_solver.py`` — the mandatory anchor of
  ``verify_manifest_integrity_strict``. The mutants restore the self-attestation hole
  in its several shapes: no anchor required, the text route ignored, an agreement
  mismatch tolerated, the parser's normalization skipped.

Isolation is by hardlink: the copy is instant and costs no disk, and every mutated
file is unlinked *before* it is written, so a hardlink can never write through to the
real repository. Nothing in this module touches ``sys.modules`` — no C extension
(``numpy``, ``scipy``, ``PIL``, ``sqlalchemy``) is ever reloaded in-process, because
each mutant runs in its own interpreter.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"

DRIVERS = "nexus_ai_agent/storage/personal_cloud_vault/byos_drivers.py"
ANCHOR = "nexus_ai_agent/creative/storyboard_compiler/editorial_cut_solver.py"
FENCE_TESTS = "tests/unit/test_personal_cloud_vault_lease_fence.py"
ANCHOR_TESTS = "tests/creative/storyboard_compiler/test_editorial_cut_solver.py"

#: ``(mutant id, relative source path, original text, mutated text, targeted test file)``.
#: Every ``original`` must occur exactly once in the real tree, which
#: ``test_every_mutation_anchor_is_unique`` enforces.
MUTANTS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "M01 post-retirement lease read removed",
        DRIVERS,
        """                if self._lease_state_token() != before_state or valid in (
                    self.read_active_leased_chunks(now_epoch=effective_now)
                ):
                    self._restore_chunk_conditionally(valid, staged_payload)
                    return False
                return True""",
        "                return True",
        FENCE_TESTS,
    ),
    (
        "M02 rollback loses create-if-absent",
        DRIVERS,
        """            object_key=self._object_key("chunks", f"{chunk_id}.chk"),
            body=staged_payload,
            content_type="application/octet-stream",
            extra_headers={"if-none-match": "*"},""",
        """            object_key=self._object_key("chunks", f"{chunk_id}.chk"),
            body=staged_payload,
            content_type="application/octet-stream",""",
        FENCE_TESTS,
    ),
    (
        "M03 fence claim loses if-none-match",
        DRIVERS,
        """            object_key=fence_key,
            body=body,
            content_type="application/json",
            extra_headers={"if-none-match": "*"},""",
        """            object_key=fence_key,
            body=body,
            content_type="application/json",""",
        FENCE_TESTS,
    ),
    (
        "M04 envelope identity guard disabled",
        DRIVERS,
        "        if embedded is not None and embedded != chunk_id:",
        "        if False:",
        FENCE_TESTS,
    ),
    (
        "M05 retire ignores the owner token",
        DRIVERS,
        "        if not fence.token or not hmac.compare_digest(fence.token, token):",
        "        if False:",
        FENCE_TESTS,
    ),
    (
        "M06 live-fence refusal disabled",
        DRIVERS,
        "        fenced = self._live_gc_fenced_chunks(chunk_ids, now_epoch=now_epoch)\n"
        "        if fenced:",
        "        fenced = self._live_gc_fenced_chunks(chunk_ids, now_epoch=now_epoch)\n"
        "        if False:",
        FENCE_TESTS,
    ),
    (
        "M07 trusted text route ignored",
        ANCHOR,
        """    digest: str | None = None
    if trusted_source_text is not None:""",
        """    digest: str | None = None
    if False:""",
        ANCHOR_TESTS,
    ),
    (
        "M08 text/digest disagreement tolerated",
        ANCHOR,
        """        if expected_source_sha256 is not None and not hmac.compare_digest(
            digest, expected_source_sha256
        ):
            return False""",
        "        pass",
        ANCHOR_TESTS,
    ),
    (
        "M09 anchor requirement dropped",
        ANCHOR,
        "        require_trusted_anchor=True,",
        "        require_trusted_anchor=False,",
        ANCHOR_TESTS,
    ),
    (
        "M10 parser normalization skipped",
        ANCHOR,
        "        normalized = normalize_script_text(text)",
        "        normalized = text",
        ANCHOR_TESTS,
    ),
    (
        "M11 byte input refused",
        ANCHOR,
        "    if isinstance(trusted_source_text, bytes):",
        "    if False:",
        ANCHOR_TESTS,
    ),
)

_PYTEST_TESTS_FAILED = 1
_COLLECTION_CRASH_MARKERS = (
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "ERROR collecting",
)


@dataclass(frozen=True)
class KillReceipt:
    """Structured proof that one mutant died from a failing assertion."""

    mutant_id: str
    exit_code: int
    killed_by: str


def _clone_tree(destination: Path) -> Path:
    """Hardlink-copy ``src`` and ``tests`` into ``destination`` (instant, no disk)."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("src", "tests"):
        shutil.copytree(REPO_ROOT / name, destination / name, copy_function=os.link)
    shutil.copy(REPO_ROOT / "pyproject.toml", destination / "pyproject.toml")
    return destination


def _run_pytest(tree: Path, target: str) -> subprocess.CompletedProcess[str]:
    env: dict[str, Any] = dict(os.environ)
    env["PYTHONPATH"] = str(tree / "src")
    env["COVERAGE_FILE"] = "/tmp/.coverage_cp4_mutation"
    return subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider", "-x"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _failed_node_ids(stdout: str) -> list[str]:
    ids: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            node = stripped[len("FAILED ") :].split(" ")[0].strip()
            if node:
                ids.append(node)
    return ids


def test_campaign_has_the_expected_mutants() -> None:
    assert len(MUTANTS) == 11
    assert len({m[0] for m in MUTANTS}) == 11


def test_every_mutation_anchor_is_unique() -> None:
    """Each ``original`` must appear exactly once, or the campaign is meaningless."""
    for mutant_id, rel, old, _new, _target in MUTANTS:
        text = (SRC / rel).read_text(encoding="utf-8")
        assert text.count(old) == 1, f"{mutant_id}: anchor count {text.count(old)} in {rel}"


def test_every_target_test_file_exists() -> None:
    for _mid, _rel, _old, _new, target in MUTANTS:
        assert (REPO_ROOT / target).is_file(), f"missing target test {target}"


@pytest.mark.parametrize(("mutant_id", "rel", "old", "new", "target"), MUTANTS)
def test_mutant_is_killed(
    mutant_id: str,
    rel: str,
    old: str,
    new: str,
    target: str,
    tmp_path: Path,
) -> None:
    """Inject one real defect and require a real assertion kill for it.

    Step A — the unmutated tree is green for the target file (otherwise a "kill" is
    just a pre-existing failure); Step B — the mutated module still parses; Step C —
    pytest exits exactly ``1`` and names a ``FAILED test_...`` node with no
    collection crash markers.
    """
    baseline_tree = _clone_tree(tmp_path / "baseline")
    baseline = _run_pytest(baseline_tree, target)
    assert baseline.returncode == 0, (
        f"{mutant_id}: baseline for {target} is not green (exit {baseline.returncode})\n"
        f"{baseline.stdout[-2000:]}"
    )

    tree = _clone_tree(tmp_path / "mutant")
    module = tree / "src" / rel
    original = (SRC / rel).read_text(encoding="utf-8")
    assert original.count(old) == 1
    mutated = original.replace(old, new, 1)
    ast.parse(mutated, filename=str(module))
    module.unlink()  # break the hardlink before writing: never touch the real repo
    module.write_text(mutated, encoding="utf-8")

    result = _run_pytest(tree, target)
    combined = result.stdout + result.stderr
    node_ids = _failed_node_ids(result.stdout)
    if result.returncode == 0:
        raise AssertionError(
            f"{mutant_id}: MUTANT SURVIVED ({old!r} -> {new!r})\n{result.stdout[-2000:]}"
        )
    assert result.returncode == _PYTEST_TESTS_FAILED, (
        f"{mutant_id}: not an assertion kill (exit {result.returncode})\n{combined[-2000:]}"
    )
    for marker in _COLLECTION_CRASH_MARKERS:
        assert marker not in combined, f"{mutant_id}: infra crash {marker!r}\n{combined[-2000:]}"
    assert node_ids, f"{mutant_id}: exit 1 without a FAILED node id\n{result.stdout[-2000:]}"
    print(f"MUTANT KILLED {mutant_id} -> {node_ids[0]}")
    assert KillReceipt(mutant_id, result.returncode, node_ids[0]).exit_code == _PYTEST_TESTS_FAILED
