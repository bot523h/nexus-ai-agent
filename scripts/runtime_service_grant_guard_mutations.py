#!/usr/bin/env python3
# ruff: noqa: E501  (the anchors/mutants are verbatim source lines, kept intact)
"""Service-grant guard mutation probes (G1-G11).

A targeted mutation harness in the same shape as the established
``scripts/execution_core_mutations.py``: for each fail-closed property the
runtime service-grant architecture guard claims, one mutant weakens the exact
guard line, the targeted guard tests must flip GREEN -> RED, the source bytes
are restored, and the tests must be GREEN again.  The run is idempotent and
leaves the tree byte-identical.

The guard itself lives in ``tests/architecture/test_runtime_service_grants.py``
(it is an architecture guard, so its harness mutates that file rather than a
``src/`` module).  Refresh the anchors whenever the guard is reworded.

====  =====================================================  ==============================
G1    an empty ``_asset_refs(..., ())`` tuple is accepted       empty/unresolved shapes
G2    an empty ``input_refs`` tuple is accepted                 empty/unresolved + aliases
G3    the resolver imports bindings from another function       order-independent scope
G4    ``input_refs`` cycle detection is dropped                 cyclic input_refs binding
G5    ``asset_ids`` cycle detection is dropped                  cyclic asset_ids binding
G6    an unresolved ``input_refs`` binding resolves anyway      unresolved binding
G7    a non-asset ``ref_type`` is accepted                      non-asset ref_type
G8    the service ``actor_id`` is no longer pinned              mismatched actor_id
G9    the expected permission set is widened                    direct-bus + render factory
G10   an unscoped bus authorizer binding is accepted            unscoped authorizer
G11   a foreign project expression is accepted                  foreign-project expression
====  =====================================================  ==============================

G5 note: cycle detection is the *only* thing that terminates that loop, so its
removal does not fail fast — the guard suite spins forever and the probe is
killed by the per-mutant timeout.  That is the property being pinned: without
cycle detection the resolver cannot terminate, so the suite can never be GREEN.

Usage:  .venv/bin/python scripts/runtime_service_grant_guard_mutations.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "--tb=no"]

GUARD = REPO / "tests/architecture/test_runtime_service_grants.py"
GUARD_T = "tests/architecture/test_runtime_service_grants.py::"

#: A non-terminating mutant must not hang the harness forever.
TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Probe:
    name: str
    anchor: str
    mutant: str
    tests: tuple[str, ...]


PROBES: tuple[Probe, ...] = (
    Probe(
        name="G1 empty _asset_refs asset tuple is accepted",
        anchor='assert asset_ids.elts, "_asset_refs must receive at least one asset id"',
        mutant="assert True  # MUTATION: a claim-less asset tuple is accepted",
        tests=(f"{GUARD_T}test_input_ref_guard_rejects_empty_or_unresolved_shapes",),
    ),
    Probe(
        name="G2 empty input_refs tuple is accepted",
        anchor='assert node.elts, "input_refs must not be an empty tuple"',
        mutant="assert True  # MUTATION: an empty input_refs tuple is accepted",
        tests=(
            f"{GUARD_T}test_input_ref_guard_rejects_empty_or_unresolved_shapes",
            f"{GUARD_T}test_input_ref_guard_does_not_import_aliases_from_another_function",
        ),
    ),
    Probe(
        name="G3 resolver imports bindings from another function",
        anchor="and _function_scope(tree, candidate) is scope",
        mutant="and True  # MUTATION: every binding in the file becomes in-scope",
        tests=(f"{GUARD_T}test_input_ref_guard_ignores_aliases_declared_after_the_inspected_call",),
    ),
    Probe(
        name="G4 input_refs cycle detection is dropped",
        anchor='assert node.id not in seen, f"cyclic input_refs binding: {node.id}"',
        mutant="assert True  # MUTATION: no input_refs cycle detection",
        tests=(f"{GUARD_T}test_input_ref_guard_rejects_cyclic_alias_bindings",),
    ),
    Probe(
        name="G5 asset_ids cycle detection is dropped",
        anchor=(
            'assert asset_ids.id not in asset_seen, f"cyclic asset_ids binding: {asset_ids.id}"'
        ),
        mutant="assert True  # MUTATION: no asset_ids cycle detection (loop cannot terminate)",
        tests=(f"{GUARD_T}test_input_ref_guard_rejects_cyclic_asset_ids_bindings",),
    ),
    Probe(
        name="G6 unresolved input_refs binding resolves anyway",
        anchor='assert node.id in bindings, f"unresolved input_refs binding: {node.id}"',
        mutant="assert True  # MUTATION: an unresolved binding is looked up anyway",
        tests=(f"{GUARD_T}test_input_ref_guard_rejects_unresolved_alias_bindings",),
    ),
    Probe(
        name="G7 non-asset ref_type is accepted",
        anchor='assert isinstance(ref_type, ast.Constant) and ref_type.value == "asset"',
        mutant="assert isinstance(ref_type, ast.Constant)  # MUTATION: any ref_type passes",
        tests=(f"{GUARD_T}test_input_ref_guard_rejects_non_asset_ref_types",),
    ),
    Probe(
        name="G8 service actor_id is no longer pinned",
        anchor=(
            'assert isinstance(fields["actor_id"], ast.Constant) '
            'and fields["actor_id"].value == actor_id'
        ),
        mutant='assert isinstance(fields["actor_id"], ast.Constant)  # MUTATION: any actor_id',
        tests=(f"{GUARD_T}test_service_identity_guard_rejects_a_mismatched_actor_id",),
    ),
    Probe(
        name="G9 expected permission set is widened",
        anchor='EXPECTED_PERMISSIONS = {"project:read", "project:write"}',
        mutant='EXPECTED_PERMISSIONS = {"project:read", "project:write", "project:admin"}  # MUTATION',
        tests=(
            f"{GUARD_T}test_direct_slideshow_buses_require_non_null_scoped_service_grants",
            f"{GUARD_T}test_render_job_factory_and_command_use_the_same_scoped_service_grant",
        ),
    ),
    Probe(
        name="G10 unscoped bus authorizer binding is accepted",
        anchor="assert _name(authorizer) == access_name",
        mutant="assert True  # MUTATION: any access object authorizes the bus",
        tests=(f"{GUARD_T}test_direct_bus_guard_rejects_an_unscoped_authorizer_binding",),
    ),
    Probe(
        name="G11 foreign project expression is accepted",
        anchor='assert project_id.attr == "project_id" and ast.unparse(project_id) in project_names',
        mutant='assert project_id.attr == "project_id"  # MUTATION: ownership not proven',
        tests=(f"{GUARD_T}test_input_ref_guard_rejects_a_foreign_project_expression",),
    ),
)


def _run_tests(tests: tuple[str, ...]) -> int | None:
    """Return the pytest exit code, or ``None`` when the mutant never terminates."""
    try:
        result = subprocess.run(
            [*PYTEST, *tests],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    return result.returncode


def _apply(probe: Probe) -> None:
    text = GUARD.read_text(encoding="utf-8")
    if text.count(probe.anchor) != 1:
        raise SystemExit(
            f"anchor for {probe.name} is not unique in {GUARD.relative_to(REPO)}: "
            f"{text.count(probe.anchor)} matches"
        )
    GUARD.write_text(text.replace(probe.anchor, probe.mutant), encoding="utf-8")


def main() -> int:
    failures: list[str] = []
    caught = 0
    for probe in PROBES:
        original = GUARD.read_bytes()
        before_sha = hashlib.sha256(original).hexdigest()
        print(f"\n=== {probe.name}")
        try:
            baseline = _run_tests(probe.tests)
            if baseline is None:
                failures.append(f"{probe.name}: baseline did not terminate")
                print("  baseline: NOT GREEN (timeout) — probe invalid")
                continue
            if baseline != 0:
                failures.append(f"{probe.name}: baseline not GREEN (exit {baseline})")
                print(f"  baseline: NOT GREEN (exit {baseline}) — probe invalid")
                continue
            print("  baseline: GREEN")

            _apply(probe)
            mutated = _run_tests(probe.tests)
        finally:
            GUARD.write_bytes(original)

        after_sha = hashlib.sha256(GUARD.read_bytes()).hexdigest()
        if after_sha != before_sha:
            failures.append(f"{probe.name}: source not restored byte-identically")
            print("  restore: FAILED (bytes differ)")
            continue

        if mutated == 0:
            failures.append(f"{probe.name}: mutation NOT caught (tests stayed GREEN)")
            print("  mutant: NOT CAUGHT — tests stayed GREEN")
            continue

        restored = _run_tests(probe.tests)
        if restored != 0:
            failures.append(f"{probe.name}: restored tree not GREEN (exit {restored})")
            print(f"  restored: NOT GREEN (exit {restored})")
            continue

        caught += 1
        verdict = (
            "CAUGHT (non-terminating mutant)" if mutated is None else f"CAUGHT (exit {mutated})"
        )
        print(f"  mutant: {verdict}; restored: GREEN; sha {before_sha[:12]}")

    print(f"\n=== summary: {caught}/{len(PROBES)} mutations caught")
    if failures:
        for failure in failures:
            print(f"  FAIL: {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
