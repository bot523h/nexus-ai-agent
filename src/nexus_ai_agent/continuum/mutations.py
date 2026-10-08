"""Replayable mutation campaign for the Continuum evidence contract.

Each :class:`Mutation` is a single, exact source edit that re-opens one way
evidence could lie (a denominator that counts synthetic lines, a forged
numerator, a threshold bypass, a stale target set, a laundered process
failure, a trusted stale snapshot, a green downstream exit, a non-canonical
artifact).  The campaign proves the test suite *kills* every one of them:

1. every target file must be committed and unmodified (an interrupted earlier
   campaign can never be mistaken for the baseline);
2. the union of the mutations' tests must pass on the unmutated tree;
3. for each mutation: the ``original`` text must occur exactly once; it is
   replaced by ``mutated``; the listed tests run in a fresh interpreter with a
   private bytecode cache; the file is restored and its SHA-256 must equal the
   pre-mutation digest;
4. a mutation is **killed** only when pytest exits ``1`` (tests failed).  Exit
   ``0`` is a **survivor** (the suite must be strengthened — a mutation is
   never deleted to make the campaign pass); any other exit (collection or
   usage error) is an **invalid** run, never a kill.

Mutations whose behaviour depends on the interpreter's line tables declare the
versions where they are observable (``min_python``/``max_python``); they are
recorded as ``not-applicable`` elsewhere rather than silently dropped.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.continuum import provenance

CAMPAIGN_SCHEMA = "nexus.continuum-mutations/1"
FAMILIES = (
    "denominator",
    "numerator",
    "threshold",
    "targets",
    "process",
    "snapshot",
    "downstream",
    "determinism",
    "pack",
    "ci",
)
_TIMEOUT_SECONDS = 900

_PC = "src/nexus_ai_agent/continuum/pack_coverage.py"
_SN = "src/nexus_ai_agent/continuum/snapshot.py"
_PV = "src/nexus_ai_agent/continuum/provenance.py"
_GT = "src/nexus_ai_agent/continuum/gate.py"
_CLI = "src/nexus_ai_agent/cli.py"
_SCRIPT = "scripts/pack_coverage.py"
_SLIDE_MODELS = "src/nexus_ai_agent/creative/packs/slideshow/models.py"
_SLIDE_OPS = "src/nexus_ai_agent/creative/packs/slideshow/operations.py"
_CI = ".github/workflows/ci.yml"

_CONTRACT = "tests/unit/test_pack_coverage_contract.py"
_HARNESS = "tests/unit/test_pack_coverage_harness.py"
_HARDENING = "tests/unit/test_continuum_hardening.py"
_SNAPSHOT = "tests/unit/test_continuum_snapshot_contract.py"
_PROVENANCE = "tests/unit/test_continuum_provenance.py"
_GATE = "tests/unit/test_continuum_gate.py"
_INVARIANTS = "tests/unit/test_slideshow_invariants.py"
_CI_GUARD = "tests/unit/test_ci_continuum_evidence.py"


@dataclass(frozen=True)
class Mutation:
    id: str
    family: str
    file: str
    original: str
    mutated: str
    tests: tuple[str, ...]
    rationale: str
    min_python: tuple[int, int] | None = None
    max_python: tuple[int, int] | None = None

    def applies_to(self, version: tuple[int, int]) -> bool:
        if self.min_python is not None and version < self.min_python:
            return False
        return not (self.max_python is not None and version > self.max_python)


def _m(
    identifier: str,
    family: str,
    file: str,
    original: str,
    mutated: str,
    tests: Iterable[str],
    rationale: str,
    **versions: tuple[int, int],
) -> Mutation:
    return Mutation(
        identifier, family, file, original, mutated, tuple(tests), rationale, **versions
    )


CATALOG: tuple[Mutation, ...] = (
    # --- denominator -------------------------------------------------------
    _m(
        "D1",
        "denominator",
        _PC,
        "if line is not None and line > 0)",
        "if line is not None)",
        [f"{_HARNESS}::test_executable_lines_counts_statements_not_comments_or_docstrings"],
        "count the synthetic RESUME line 0 (3.11+ only emits it)",
        min_python=(3, 11),
    ),
    _m(
        "D2",
        "denominator",
        _PC,
        "    if not ast.parse(source, filename=str(path)).body:\n        return frozenset()",
        "    if False:\n        return frozenset()",
        [f"{_HARNESS}::test_executable_lines_is_empty_for_blank_and_comment_only_modules"],
        "let a comment-only module own an implicit-return line (3.10 numbers it > 0)",
        max_python=(3, 10),
    ),
    _m(
        "D3",
        "denominator",
        _PC,
        "stack.extend(const for const in code.co_consts if isinstance(const, types.CodeType))",
        "stack.extend(())",
        [f"{_CONTRACT}::test_denominator_is_exactly_the_traceable_statement_lines"],
        "drop nested function bodies from the denominator",
    ),
    _m(
        "D4",
        "denominator",
        _PC,
        "executable_modules = [entry for entry in executable_modules if entry[2]]",
        "executable_modules = list(executable_modules)",
        [f"{_CONTRACT}::test_comment_and_blank_modules_are_dropped_from_the_surface"],
        "keep zero-line modules as fake surface entries",
    ),
    _m(
        "D5",
        "denominator",
        _PC,
        '    if not executable_modules:\n        raise ValueError("selected pack surface has zero',
        '    if False:\n        raise ValueError("selected pack surface has zero',
        [f"{_HARDENING}::test_measure_rejects_invalid_unknown_empty_and_zero_line_surfaces"],
        "measure a zero-line surface instead of refusing it",
    ),
    _m(
        "D6",
        "denominator",
        _PC,
        "        if group not in per_group\n",
        "        if False\n",
        [f"{_CONTRACT}::test_a_pack_without_any_executable_surface_is_named_not_silently_dropped"],
        "silently drop a pack whose every module is comment-only from the verdict",
    ),
    # --- numerator ---------------------------------------------------------
    _m(
        "N1",
        "numerator",
        _PC,
        "covered = executable & ran",
        "covered = ran",
        [f"{_CONTRACT}::test_numerator_counts_only_lines_of_the_executable_surface"],
        "count executed lines outside the executable surface",
    ),
    _m(
        "N2",
        "numerator",
        _PC,
        "            or row[2] <= 0\n",
        "            or row[2] < -1\n",
        [f"{_CONTRACT}::test_forged_or_duplicate_line_events_are_rejected"],
        "accept negative hit counts",
    ),
    _m(
        "N3",
        "numerator",
        _PC,
        "        if row[1] in lines:\n",
        "        if False:\n",
        [f"{_CONTRACT}::test_forged_or_duplicate_line_events_are_rejected"],
        "accept duplicated line events",
    ),
    _m(
        "N4",
        "numerator",
        _PC,
        "or self.executed > self.executable:",
        "or self.executed > self.executable + 1:",
        [f"{_CONTRACT}::test_module_counts_must_be_consistent_integers"],
        "allow executed > executable",
    ),
    _m(
        "N5",
        "numerator",
        _PC,
        "if type(self.executed) is not int or type(self.executable) is not int:",
        "if False:",
        [f"{_CONTRACT}::test_module_counts_must_be_consistent_integers"],
        "accept bool/float counts",
    ),
    # --- threshold ---------------------------------------------------------
    _m(
        "T1",
        "threshold",
        _PC,
        "if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):",
        "if not isinstance(threshold, (int, float)):",
        [f"{_CONTRACT}::test_invalid_thresholds_are_refused_everywhere"],
        "accept True as a 1% threshold",
    ),
    _m(
        "T2",
        "threshold",
        _PC,
        "if not math.isfinite(float(threshold)) or threshold < 0:",
        "if threshold < 0:",
        [f"{_CONTRACT}::test_invalid_thresholds_are_refused_everywhere"],
        "accept NaN/inf thresholds (NaN makes every comparison false = green)",
    ),
    _m(
        "T3",
        "threshold",
        _PC,
        "if not math.isfinite(float(threshold)) or threshold < 0:",
        "if not math.isfinite(float(threshold)):",
        [f"{_CONTRACT}::test_invalid_thresholds_are_refused_everywhere"],
        "accept negative thresholds",
    ),
    _m(
        "T4",
        "threshold",
        _PC,
        "    if report.threshold < ACCEPTANCE_THRESHOLD:\n        failures.append(",
        "    if False:\n        failures.append(",
        [f"{_CONTRACT}::test_a_lowered_threshold_is_diagnostic_and_never_accepted"],
        "accept a run whose bar was lowered below the contract",
    ),
    _m(
        "T5",
        "threshold",
        _PC,
        "        if pack.percent < report.threshold:\n            worst =",
        "        if pack.percent <= report.threshold:\n            worst =",
        [f"{_CONTRACT}::test_threshold_boundary_is_inclusive_and_exact"],
        "move the boundary (95.00% must pass)",
    ),
    _m(
        "T6",
        "threshold",
        _PC,
        "ACCEPTANCE_THRESHOLD = 95.0",
        "ACCEPTANCE_THRESHOLD = 85.0",
        [f"{_CONTRACT}::test_the_contract_is_ninety_five_percent_per_pack"],
        "silently lower the contract",
    ),
    _m(
        "T7",
        "threshold",
        _SCRIPT,
        "        default=DEFAULT_THRESHOLD,",
        "        default=85.0,",
        [f"{_CONTRACT}::test_script_defaults_to_the_contract_threshold"],
        "CLI default below the contract",
    ),
    # --- target integrity --------------------------------------------------
    _m(
        "G1",
        "targets",
        _PC,
        '        "tests/unit/test_slideshow_invariants.py",\n',
        "",
        [f"{_CONTRACT}::test_no_pack_test_module_is_left_out_of_the_evidence"],
        "drop a pack test module from the canonical set",
    ),
    _m(
        "G2",
        "targets",
        _PC,
        "        if identity in identities:\n",
        "        if False:\n",
        [f"{_CONTRACT}::test_missing_and_respelled_duplicate_targets_are_refused"],
        "accept duplicate (respelled) targets",
    ),
    _m(
        "G3",
        "targets",
        _PC,
        "        if not _target_path(target).exists():\n",
        "        if False:\n",
        [f"{_CONTRACT}::test_missing_and_respelled_duplicate_targets_are_refused"],
        "accept missing targets",
    ),
    _m(
        "G4",
        "targets",
        _PC,
        "    if target_set != default_targets:\n",
        "    if False:\n",
        [f"{_CONTRACT}::test_canonical_root_with_partial_targets_or_packs_is_not_accepted"],
        "accept a subset of the canonical targets",
    ),
    _m(
        "G5",
        "targets",
        _PC,
        "    if root != DEFAULT_PACK_ROOT.resolve():\n",
        "    if False:\n",
        [f"{_HARDENING}::test_scoped_measurement_is_repeatable_but_cannot_claim_green"],
        "accept an alternate pack root",
    ),
    _m(
        "G6",
        "targets",
        _PC,
        "        for pack in sorted(mapped - shipped)\n",
        "        for pack in sorted(set[str]())\n",
        [f"{_CONTRACT}::test_mapping_issues_name_unmapped_stale_and_manifestless_packs"],
        "keep a stale mapping for a pack that no longer ships",
    ),
    _m(
        "G7",
        "targets",
        _PC,
        "    if unknown:\n",
        "    if False:\n",
        [f"{_HARDENING}::test_measure_rejects_invalid_unknown_empty_and_zero_line_surfaces"],
        "accept an unknown pack selection",
    ),
    _m(
        "G8",
        "targets",
        _PC,
        "    if selected_packs is not None and selected_packs != available_packs:\n",
        "    if False:\n",
        [f"{_CONTRACT}::test_canonical_root_with_partial_targets_or_packs_is_not_accepted"],
        "accept a partial pack selection",
    ),
    _m(
        "G9",
        "targets",
        _PC,
        "        issues.extend(pack_test_import_issues())\n",
        "",
        [f"{_CONTRACT}::test_an_orphaned_pack_test_makes_the_canonical_measurement_unverified"],
        "stop checking the test tree for orphaned pack tests",
    ),
    # --- process -----------------------------------------------------------
    _m(
        "P1",
        "process",
        _PC,
        "        if completed.returncode != 0:\n",
        "        if False:\n",
        [f"{_CONTRACT}::test_broken_trace_children_raise_instead_of_reporting"],
        "ignore a crashed trace child",
    ),
    _m(
        "P2",
        "process",
        _PC,
        '    if payload["nonce"] != nonce:\n',
        "    if False:\n",
        [f"{_CONTRACT}::test_foreign_or_malformed_trace_artifacts_raise"],
        "accept a trace artifact from another run",
    ),
    _m(
        "P3",
        "process",
        _PC,
        "if not isinstance(payload, dict) or set(payload) != _TRACE_ARTIFACT_KEYS:",
        "if not isinstance(payload, dict):",
        [f"{_CONTRACT}::test_foreign_or_malformed_trace_artifacts_raise"],
        "accept extra keys in the trace artifact",
    ),
    _m(
        "P4",
        "process",
        _PC,
        "    if outcomes.deselected:\n",
        "    if False:\n",
        [f"{_CONTRACT}::test_run_outcomes_that_are_not_a_full_green_run_are_issues"],
        "accept a run that deselected tests",
    ),
    _m(
        "P5",
        "process",
        _PC,
        "    if pytest_exit_code == 0 and (outcomes.failed or outcomes.errors):\n",
        "    if False:\n",
        [f"{_CONTRACT}::test_run_outcomes_that_are_not_a_full_green_run_are_issues"],
        "accept exit 0 that contradicts failed tests",
    ),
    _m(
        "P6",
        "process",
        _PC,
        "            self.pytest_exit_code == 0\n            and bool(self.packs)",
        "            True\n            and bool(self.packs)",
        [
            f"{_CONTRACT}::test_missing_evidence_is_unverified_even_with_perfect_numbers",
            f"{_HARDENING}::test_measure_records_a_real_pytest_failure_as_unverified_evidence",
        ],
        "treat a failed pytest run as verified",
    ),
    _m(
        "P7",
        "process",
        _PC,
        '                    self.values["passed"] += 1',
        '                    self.values["passed"] += 0',
        [f"{_CONTRACT}::test_real_trace_child_reports_what_pytest_actually_did"],
        "the real child under-reports passing tests",
    ),
    _m(
        "P8",
        "process",
        _PC,
        '            self.values["deselected"] += len(items)',
        '            self.values["deselected"] += 0',
        [f"{_CONTRACT}::test_real_trace_child_counts_deselection_by_a_conftest"],
        "the real child hides deselection",
    ),
    _m(
        "P9",
        "process",
        _PC,
        "            and self.outcomes is not None\n",
        "",
        [f"{_CONTRACT}::test_missing_evidence_is_unverified_even_with_perfect_numbers"],
        "verify a report that carries no run outcomes",
    ),
    _m(
        "P10",
        "process",
        _SCRIPT,
        "    return EXIT_NOT_ACCEPTED if failures else EXIT_ACCEPTED",
        "    return EXIT_ACCEPTED",
        [f"{_HARNESS}::test_cli_reports_below_threshold_with_exit_code_one"],
        "script exits 0 on a rejected measurement",
    ),
    _m(
        "P11",
        "process",
        _SCRIPT,
        "        json_out.unlink(missing_ok=True)",
        "        json_out.exists()",
        [f"{_CONTRACT}::test_script_invalid_requests_exit_two_and_remove_stale_artifacts"],
        "a stale artifact survives a failed run",
    ),
    _m(
        "P12",
        "process",
        _SCRIPT,
        "        return EXIT_USAGE",
        "        return EXIT_ACCEPTED",
        [f"{_CONTRACT}::test_script_invalid_requests_exit_two_and_remove_stale_artifacts"],
        "script exits 0 on an invalid request",
    ),
    # --- snapshot ----------------------------------------------------------
    _m(
        "S1",
        "snapshot",
        _SN,
        '        if not provenance.COMMIT_ID_PATTERN.fullmatch(values["step"]):\n',
        "        if False:\n",
        [f"{_SNAPSHOT}::test_schema_violations_are_unreadable"],
        "accept a symbolic step such as HEAD",
    ),
    _m(
        "S2",
        "snapshot",
        _PV,
        'COMMIT_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")',
        'COMMIT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")',
        [f"{_SNAPSHOT}::test_schema_violations_are_unreadable"],
        "accept abbreviated or uppercase commit ids",
    ),
    _m(
        "S3",
        "snapshot",
        _SN,
        "            if provenance.is_shallow_repository(_REPO_ROOT):\n",
        "            if False:\n",
        [f"{_SNAPSHOT}::test_shallow_clone_cannot_prove_ancestry"],
        "report state loss (or green) when history is merely unavailable",
    ),
    _m(
        "S4",
        "snapshot",
        _PV,
        "        if is_shallow_repository(root):\n",
        "        if False:\n",
        [f"{_PROVENANCE}::test_shallow_ancestry_is_unknown_not_false"],
        "treat a shallow negative ancestry answer as definitive",
    ),
    _m(
        "S5",
        "snapshot",
        _SN,
        "                    if not source_matches:\n",
        "                    if False:\n",
        [f"{_SNAPSHOT}::test_later_committed_drift_in_any_evidence_root_is_detected"],
        "ignore later committed source drift",
    ),
    _m(
        "S6",
        "snapshot",
        _PV,
        '    "migrations",\n',
        '    "alembic",\n',
        [
            f"{_PROVENANCE}::test_every_evidence_source_path_exists_in_this_repository",
            f"{_SNAPSHOT}::test_later_committed_drift_in_any_evidence_root_is_detected",
        ],
        "reintroduce the stale 'alembic' root (migrations drift undetected)",
    ),
    _m(
        "S7",
        "snapshot",
        _PV,
        "if _under(path, source_paths) and not _disposable(path):",
        "if False:",
        [f"{_SNAPSHOT}::test_dirty_untracked_or_ignored_importable_files_are_drift"],
        "ignore ignored-but-importable source files",
    ),
    _m(
        "S8",
        "snapshot",
        _PV,
        "        if scope is None or _under(path, scope):\n",
        "        if scope is not None and _under(path, scope):\n",
        [f"{_SNAPSHOT}::test_dirty_untracked_or_ignored_importable_files_are_drift"],
        "verify only part of the checkout",
    ),
    _m(
        "S9",
        "snapshot",
        _PV,
        "    raw = outcome.stdout\n",
        "    raw = outcome.stdout.strip()\n",
        [f"{_PROVENANCE}::test_a_tracked_edit_in_the_first_status_record_keeps_its_path"],
        "reintroduce the porcelain strip bug (first record misparsed)",
    ),
    _m(
        "S10",
        "snapshot",
        _SN,
        '    if snapshot.to_json().encode("utf-8") != raw:\n',
        "    if False:\n",
        [f"{_SNAPSHOT}::test_non_canonical_duplicate_key_and_nan_documents_are_unreadable"],
        "accept non-canonical bytes",
    ),
    _m(
        "S11",
        "snapshot",
        _SN,
        "            object_pairs_hook=_reject_duplicate_keys,\n"
        "            parse_constant=_reject_non_finite,",
        "            parse_constant=_reject_non_finite,",
        [f"{_SNAPSHOT}::test_non_canonical_duplicate_key_and_nan_documents_are_unreadable"],
        "let a duplicate key shadow a value",
    ),
    _m(
        "S12",
        "snapshot",
        _SN,
        "    if missing or extra:\n",
        "    if missing:\n",
        [f"{_SNAPSHOT}::test_schema_violations_are_unreadable"],
        "accept extra keys (forged verdicts)",
    ),
    _m(
        "S13",
        "snapshot",
        _SN,
        "if type(expected_count) is not int or expected_count < 0:",
        "if expected_count < 0:",
        [f"{_SNAPSHOT}::test_schema_violations_are_unreadable"],
        "accept bool/string test counts",
    ),
    _m(
        "S14",
        "snapshot",
        _SN,
        "    if type(deselected) is not int or deselected != 0:\n",
        "    if False:\n",
        [f"{_SNAPSHOT}::test_collection_that_deselects_items_is_not_a_count"],
        "count a collection that deselected tests",
    ),
    _m(
        "S15",
        "snapshot",
        _SN,
        "        if snapshot.test_count_expected != actual_tests:\n",
        "        if False:\n",
        [f"{_SNAPSHOT}::test_test_count_and_environment_drift_are_reported_together"],
        "ignore test-count drift",
    ),
    _m(
        "S16",
        "snapshot",
        _SN,
        "        if asdict(snapshot.env_fingerprint) != asdict(expected_environment):\n",
        "        if False:\n",
        [f"{_SNAPSHOT}::test_test_count_and_environment_drift_are_reported_together"],
        "ignore environment drift",
    ),
    _m(
        "S17",
        "snapshot",
        _SN,
        "    if not _working_tree_clean():\n        raise RuntimeError(\n",
        "    if False:\n        raise RuntimeError(\n",
        [f"{_SNAPSHOT}::test_capture_measures_facts_and_refuses_a_dirty_checkout"],
        "publish from a dirty checkout",
    ),
    _m(
        "S18",
        "snapshot",
        _PV,
        "    except BaseException:\n        temporary_path.unlink(missing_ok=True)\n",
        "    except BaseException:\n        temporary_path.exists()\n",
        [f"{_PROVENANCE}::test_atomic_write_replaces_whole_files_only"],
        "leave a partial publication behind",
    ),
    _m(
        "S19",
        "snapshot",
        _PV,
        "        environment.pop(variable, None)\n",
        "        environment.get(variable)\n",
        [f"{_SNAPSHOT}::test_collection_ignores_the_callers_pytest_steering"],
        "let PYTEST_ADDOPTS steer evidence runs",
    ),
    _m(
        "S20",
        "snapshot",
        _SN,
        "    return not provenance.working_tree_drift(_REPO_ROOT)\n",
        "    return not provenance.working_tree_drift(_REPO_ROOT, scope=_SNAPSHOT_SOURCE_PATHS)\n",
        [f"{_SNAPSHOT}::test_dirty_untracked_or_ignored_importable_files_are_drift"],
        "ignore dirt outside the evidence roots (e.g. a leftover publication temp file)",
    ),
    # --- downstream consumers ----------------------------------------------
    _m(
        "C1",
        "downstream",
        _CLI,
        "            raise typer.Exit(code=1)\n"
        '        typer.echo("✓ continuum snapshot matches checkout")',
        "            raise typer.Exit(code=0)\n"
        '        typer.echo("✓ continuum snapshot matches checkout")',
        [f"{_HARDENING}::test_cli_continuum_verify_exits_nonzero_when_snapshot_is_untrusted"],
        "`continuum verify` exits 0 with findings",
    ),
    _m(
        "C2",
        "downstream",
        _CLI,
        '            typer.echo(f"✗ snapshot unreadable: {exc}", err=True)\n'
        "            raise typer.Exit(code=1) from exc",
        '            typer.echo(f"✗ snapshot unreadable: {exc}", err=True)\n'
        "            raise typer.Exit(code=0) from exc",
        [f"{_SNAPSHOT}::test_cli_show_validates_instead_of_echoing_raw_bytes"],
        "`continuum show` exits 0 on an unreadable snapshot",
    ),
    _m(
        "C3",
        "downstream",
        _PC,
        '        elif not report.verified:\n            status = "UNVERIFIED"',
        '        elif False:\n            status = "UNVERIFIED"',
        [f"{_CONTRACT}::test_missing_evidence_is_unverified_even_with_perfect_numbers"],
        "table labels unverified evidence OK",
    ),
    _m(
        "C4",
        "downstream",
        _PC,
        "        elif diagnostic:\n",
        "        elif False:\n",
        [f"{_HARNESS}::test_format_table_never_labels_a_lowered_threshold_ok"],
        "table labels a lowered-threshold run OK",
    ),
    _m(
        "C5",
        "downstream",
        _PC,
        '            "accepted": not failures,',
        '            "accepted": True,',
        [f"{_CONTRACT}::test_a_lowered_threshold_is_diagnostic_and_never_accepted"],
        "artifact claims acceptance regardless of failures",
    ),
    _m(
        "C6",
        "downstream",
        _PC,
        "for failure in coverage_failures(fresh)\n",
        "for failure in ()\n",
        [f"{_CONTRACT}::test_verify_artifact_rejects_a_reproduced_but_unaccepted_measurement"],
        "trust a reproduced artifact that was never accepted",
    ),
    _m(
        "C7",
        "downstream",
        _PC,
        "    if fresh_bytes != raw:\n",
        "    if False:\n",
        [f"{_CONTRACT}::test_verify_artifact_accepts_only_a_byte_identical_reproduction"],
        "trust a forged artifact",
    ),
    _m(
        "C8",
        "downstream",
        _GT,
        "        and any(scenario.expected_fragment in line for line in observed)\n",
        "",
        [f"{_GATE}::test_a_scenario_passes_only_on_exact_exit_and_diagnosis"],
        "gate accepts a rejection for the wrong reason",
    ),
    _m(
        "C9",
        "downstream",
        _GT,
        "        and observed_exit == scenario.expected_exit\n",
        "        and observed_exit is not None\n",
        [f"{_GATE}::test_a_scenario_passes_only_on_exact_exit_and_diagnosis"],
        "gate accepts any exit code",
    ),
    _m(
        "C10",
        "downstream",
        _GT,
        '    report["passed"] = not problems\n',
        '    report["passed"] = True\n',
        [f"{_GATE}::test_run_gate_fails_when_any_row_fails"],
        "gate reports PASSED with failing rows",
    ),
    # --- determinism -------------------------------------------------------
    _m(
        "R1",
        "determinism",
        _PC,
        "        packs = sorted(self.packs, key=lambda pack: pack.pack)\n        failures",
        "        packs = list(self.packs)\n        failures",
        [f"{_CONTRACT}::test_pack_order_does_not_change_the_artifact"],
        "artifact bytes depend on discovery order",
    ),
    # --- the pack invariants that closed the slideshow gap -----------------
    _m(
        "K1",
        "pack",
        _SLIDE_MODELS,
        "        if tuple(sorted(self.beats_us)) != self.beats_us:\n",
        "        if False:\n",
        [f"{_INVARIANTS}::test_beat_grid_rejects_negative_and_unordered_beats"],
        "accept an unordered beat grid",
    ),
    _m(
        "K2",
        "pack",
        _SLIDE_MODELS,
        "            if previous.slot.end_us != current.slot.start_us:\n",
        "            if previous.slot.end_us < current.slot.start_us:\n",
        [f"{_INVARIANTS}::test_plan_rejects_gaps_overlaps_and_short_endings"],
        "accept overlapping shots",
    ),
    _m(
        "K3",
        "pack",
        _SLIDE_OPS,
        "        audio_span = min(audio_duration, target_us)\n",
        "        audio_span = audio_duration\n",
        [f"{_INVARIANTS}::test_compose_with_audio_lays_a_track_that_never_outlasts_the_video"],
        "audio outlasts the video",
    ),
    _m(
        "K4",
        "pack",
        _SLIDE_MODELS,
        "        if (self.width, self.height) != expected:\n",
        "        if False:\n",
        [f"{_INVARIANTS}::test_upscale_rejects_measured_dimensions_that_miss_the_target"],
        "accept an upscale whose measured output misses the target",
    ),
    # --- the CI job that enforces all of the above --------------------------
    _m(
        "CI1",
        "ci",
        _CI,
        '          test "$(git rev-parse HEAD)" = "$GITHUB_SHA"\n',
        "          true\n",
        [f"{_CI_GUARD}::test_the_job_checks_out_full_history_and_binds_to_the_sha"],
        "evidence produced for a different commit than the one under test",
    ),
    _m(
        "CI2",
        "ci",
        _CI,
        "    name: continuum-evidence (${{ matrix.python-version }})\n",
        "    name: continuum-evidence (${{ matrix.python-version }})\n"
        "    continue-on-error: true\n",
        [f"{_CI_GUARD}::test_the_job_is_blocking_on_every_supported_interpreter"],
        "a red evidence job no longer fails the workflow",
    ),
    _m(
        "CI3",
        "ci",
        _CI,
        "          path: ci-artifacts/\n          if-no-files-found: error\n",
        "          path: ci-artifacts/\n          if-no-files-found: ignore\n",
        [f"{_CI_GUARD}::test_artifacts_are_named_for_the_commit_and_interpreter"],
        "a missing evidence artifact is uploaded as success",
    ),
    _m(
        "CI4",
        "ci",
        _CI,
        "          cmp ci-artifacts/pack-coverage.json ci-artifacts/pack-coverage.rerun.json\n",
        "          true\n",
        [f"{_CI_GUARD}::test_the_job_runs_every_verdict_producer_without_diagnostic_flags"],
        "coverage reproducibility is no longer compared",
    ),
    _m(
        "CI5",
        "ci",
        _CI,
        '        python-version: ["3.10", "3.11", "3.12", "3.14"]\n    steps:\n'
        "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2\n"
        "        with:\n          # full history: snapshot",
        '        python-version: ["3.11", "3.12", "3.14"]\n    steps:\n'
        "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2\n"
        "        with:\n          # full history: snapshot",
        [f"{_CI_GUARD}::test_the_job_is_blocking_on_every_supported_interpreter"],
        "the historically failing 3.10 leg is dropped from the evidence matrix",
    ),
    _m(
        "CI6",
        "ci",
        _CI,
        "          python scripts/continuum_mutations.py"
        " --out ci-artifacts/continuum-mutations.json\n",
        "          python scripts/continuum_mutations.py"
        " --out ci-artifacts/continuum-mutations.json || true\n",
        [f"{_CI_GUARD}::test_the_job_runs_every_verdict_producer_without_diagnostic_flags"],
        "a failed mutation campaign is laundered into a green step",
    ),
    _m(
        "CI7",
        "ci",
        _CI,
        "          name: continuum-evidence-${{ github.sha }}-py${{ matrix.python-version }}\n",
        "          name: continuum-evidence-py${{ matrix.python-version }}\n",
        [f"{_CI_GUARD}::test_artifacts_are_named_for_the_commit_and_interpreter"],
        "the uploaded evidence is no longer bound to the commit",
    ),
)


# ---------------------------------------------------------------------------
# campaign
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def catalog_issues(root: Path, catalog: Sequence[Mutation] = CATALOG) -> list[str]:
    """Why the catalogue cannot be replayed exactly as written."""

    issues: list[str] = []
    ids = [mutation.id for mutation in catalog]
    duplicates = sorted({identifier for identifier in ids if ids.count(identifier) > 1})
    if duplicates:
        issues.append(f"duplicate mutation ids: {', '.join(duplicates)}")
    for mutation in catalog:
        if mutation.family not in FAMILIES:
            issues.append(f"{mutation.id}: unknown family {mutation.family!r}")
        if mutation.original == mutation.mutated or not mutation.original:
            issues.append(f"{mutation.id}: mutation is a no-op")
        if not mutation.tests:
            issues.append(f"{mutation.id}: no killing tests declared")
        path = root / mutation.file
        if not path.is_file():
            issues.append(f"{mutation.id}: target file {mutation.file} does not exist")
            continue
        occurrences = path.read_text(encoding="utf-8").count(mutation.original)
        if occurrences != 1:
            issues.append(
                f"{mutation.id}: original text occurs {occurrences} times in {mutation.file}"
            )
        for test in mutation.tests:
            if not (root / test.partition("::")[0]).is_file():
                issues.append(f"{mutation.id}: test file for {test} does not exist")
    return issues


def pytest_command(tests: Sequence[str]) -> list[str]:
    return [
        "-m",
        "pytest",
        "-q",
        "-x",
        "-rf",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_asyncio.plugin",
        "-c",
        "pyproject.toml",
        *tests,
    ]


def _run_tests(root: Path, tests: Sequence[str]) -> tuple[int, list[str]]:
    with tempfile.TemporaryDirectory(prefix="nexus-mutation-pycache-") as cache:
        environment = provenance.isolated_python_environment(root)
        # A private bytecode cache per run: a restored file with the same size
        # and mtime second as its mutant can never reuse the mutant's .pyc.
        environment["PYTHONPYCACHEPREFIX"] = cache
        completed = subprocess.run(
            [sys.executable, *pytest_command(tests)],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    failed = sorted(
        {
            line[len("FAILED ") :].split(" - ", 1)[0].strip()
            for line in completed.stdout.splitlines()
            if line.startswith("FAILED ")
        }
    )
    return completed.returncode, failed


def _committed_and_clean(root: Path, files: Iterable[str]) -> list[str]:
    paths = sorted(set(files))
    status = provenance.working_tree_drift(root, scope=paths)
    return [f"target file is not clean: {entry}" for entry in status]


def run_campaign(
    root: Path = provenance.REPO_ROOT,
    catalog: Sequence[Mutation] = CATALOG,
    *,
    only: Sequence[str] | None = None,
    version: tuple[int, int] | None = None,
) -> dict[str, object]:
    """Run the campaign and return the canonical, machine-readable record."""

    python_version = version or (sys.version_info.major, sys.version_info.minor)
    selected = [m for m in catalog if only is None or m.id in set(only)]
    report: dict[str, object] = {
        "schema": CAMPAIGN_SCHEMA,
        "interpreter": provenance.interpreter_identity(),
        "commit": None,
        "baseline": None,
        "mutations": [],
        "summary": {},
        "problems": [],
        "passed": False,
    }
    problems: list[str] = []
    if only is not None:
        unknown = sorted(set(only) - {m.id for m in catalog})
        if unknown:
            problems.append(f"unknown mutation ids: {', '.join(unknown)}")
    problems.extend(catalog_issues(root, selected))
    try:
        report["commit"] = provenance.current_commit(root)
        problems.extend(_committed_and_clean(root, [m.file for m in selected]))
    except RuntimeError as exc:
        problems.append(f"cannot bind the campaign to a commit: {exc}")
    applicable = [m for m in selected if m.applies_to(python_version)]
    if not applicable:
        problems.append("no applicable mutations selected")
    if problems:
        report["problems"] = problems
        return report

    union = sorted({test for mutation in applicable for test in mutation.tests})
    baseline_exit, baseline_failed = _run_tests(root, union)
    report["baseline"] = {"tests": union, "exit": baseline_exit, "failed": baseline_failed}
    if baseline_exit != 0:
        report["problems"] = [f"baseline is not green (pytest exit {baseline_exit})"]
        return report

    records: list[dict[str, object]] = []
    for mutation in selected:
        records.append(_run_one(root, mutation, python_version))
    report["mutations"] = records
    counts: dict[str, int] = {}
    for record in records:
        status = str(record["status"])
        counts[status] = counts.get(status, 0) + 1
    report["summary"] = dict(sorted(counts.items()))
    failing = [
        str(record["id"])
        for record in records
        if record["status"] not in {"killed", "not-applicable"} or record["restored"] is not True
    ]
    if failing:
        problems.append(f"mutations not killed and restored: {', '.join(failing)}")
    report["problems"] = problems
    report["passed"] = not problems
    return report


def _run_one(root: Path, mutation: Mutation, version: tuple[int, int]) -> dict[str, object]:
    record: dict[str, object] = {
        "id": mutation.id,
        "family": mutation.family,
        "file": mutation.file,
        "original": mutation.original,
        "mutated": mutation.mutated,
        "rationale": mutation.rationale,
        "tests": list(mutation.tests),
        "command": "python " + " ".join(pytest_command(mutation.tests)),
        "expected": "pytest exit 1 (killed)",
        "python_scope": {
            "min": list(mutation.min_python) if mutation.min_python else None,
            "max": list(mutation.max_python) if mutation.max_python else None,
        },
    }
    path = root / mutation.file
    before = path.read_bytes()
    record["sha256_before"] = _sha256(before)
    if not mutation.applies_to(version):
        record.update(
            status="not-applicable",
            observed_exit=None,
            observed_failed=[],
            restored=True,
            sha256_after=record["sha256_before"],
        )
        return record
    text = before.decode("utf-8")
    mutated = text.replace(mutation.original, mutation.mutated, 1)
    try:
        path.write_bytes(mutated.encode("utf-8"))
        observed_exit, failed = _run_tests(root, mutation.tests)
    finally:
        path.write_bytes(before)
    after = path.read_bytes()
    record["sha256_after"] = _sha256(after)
    record["restored"] = after == before
    record["observed_exit"] = observed_exit
    record["observed_failed"] = failed
    if observed_exit == 1:
        record["status"] = "killed"
    elif observed_exit == 0:
        record["status"] = "survived"
    else:
        record["status"] = "invalid"
    return record


def canonical_report(report: Mapping[str, object]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


__all__ = [
    "CAMPAIGN_SCHEMA",
    "CATALOG",
    "FAMILIES",
    "Mutation",
    "canonical_report",
    "catalog_issues",
    "pytest_command",
    "run_campaign",
]
