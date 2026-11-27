#!/usr/bin/env python3
"""Wave 6B — adversarial mutation harness for the five Studio execution modules.

An invariant that has never been attacked is not evidence.  This harness takes
the *shipped* Wave 1-5 modules and rewrites one guard, one sign and one
containment check at a time, then demands that the real Wave 1-5 suites notice.

For each of the 40 mutants (M01-M40) the campaign:

* refuses to start unless the anchor occurs **exactly once** in the source and
  the mutated file still parses (``ast.parse``);
* writes the mutant, purges the bytecode cache for that file, and runs the exact
  test node IDs that must fail;
* credits ``VALID_KILL`` **only** when pytest exits 1, the output carries no
  infrastructure marker (``SyntaxError``, ``ImportError``,
  ``ModuleNotFoundError``, ``INTERNALERROR``, ``no tests ran``), at least one
  expected node ID appears on a ``FAILED`` line, and a real failure effect is
  present;
* restores the original bytes in a ``finally`` block and byte-compares (plus
  SHA-256) against the pre-mutation digest — a mismatch aborts with exit 4;
* requires the whole campaign to start green and finish green again.

Bytecode determinism is the W6.7 lesson, applied here: ``.pyc`` validation uses
the source's size and its mtime in *whole seconds*, and a mutate/restore cycle
finishes inside one second, so a cached ``.pyc`` can shadow the bytes actually
on disk.  Every target's cache is purged before the pre-flight run, after
writing the mutant, and after restoring, and every pytest subprocess runs with
``PYTHONDONTWRITEBYTECODE=1``.

The campaign mutates the working tree in place, so exactly one campaign may run
against a checkout at a time (pytest and CI are serial by construction).

Usage::

    python scripts/run_wave4_studio_e2e_mutations.py            # 40/40 required
    python scripts/run_wave4_studio_e2e_mutations.py --list

Exit code 0 means "40/40 mutants killed".
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: This process writes no bytecode of its own, so a campaign can never seed the
#: cache it is about to reason about.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
STUDIO = ROOT / "src" / "nexus_ai_agent" / "creative" / "studio"
IBPC = ROOT / "src" / "nexus_ai_agent" / "nagar" / "ibpc"
ADAPTERS = ROOT / "src" / "nexus_ai_agent" / "adapters"

BRIDGE = ADAPTERS / "studio_directory_bridge.py"
PINS = STUDIO / "revision_pinning.py"
OUTBOX = IBPC / "execution_outbox.py"
READBACK = STUDIO / "preview_readback.py"
DR = STUDIO / "production_dr.py"

BRIDGE_TESTS = "tests/unit/creative/studio/test_studio_directory_bridge.py"
PINS_TESTS = "tests/unit/creative/studio/test_revision_pinning.py"
OUTBOX_TESTS = "tests/unit/creative/ibpc/test_execution_outbox.py"
READBACK_TESTS = "tests/unit/creative/studio/test_preview_readback.py"
DR_TESTS = "tests/unit/creative/studio/test_production_dr.py"

#: Every suite the campaign must leave green.
SUITES: tuple[str, ...] = (
    BRIDGE_TESTS,
    PINS_TESTS,
    OUTBOX_TESTS,
    READBACK_TESTS,
    DR_TESTS,
)


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str
    expected_node_ids: tuple[str, ...]


def _nodes(suite: str, *classes: str) -> tuple[str, ...]:
    return tuple(f"{suite}::{cls}" for cls in classes)


#: 40 mutants, eight per module.  Each one attacks a *different* invariant:
#: containment, constant-time comparison, the canonicalization scheme, the
#: locking discipline, the state machine, the rational arithmetic, the signature.
MUTATIONS: tuple[Mutation, ...] = (
    # ------------------------------------------------------------------ #
    # M01-M08 — the authority bridge (adapters/studio_directory_bridge.py)
    # ------------------------------------------------------------------ #
    Mutation(
        "M01_bridge_repr_reports_the_wrong_directory_state",
        BRIDGE,
        "f\"directory={'attached' if self._directory is not None else 'none'})\"",
        "f\"directory={'none' if self._directory is not None else 'attached'})\"",
        "the repr is the operator's only inline view of which directories are "
        "wired; a swapped label misreports a live read-through as absent",
        _nodes(
            BRIDGE_TESTS,
            "TestAuthorityVocabulary::test_bridge_repr_names_the_store_and_directory_state",
        ),
    ),
    Mutation(
        "M02_bridge_principal_status_gate_loosened",
        BRIDGE,
        "if principal_status != PrincipalStatus.ACTIVE.value:",
        "if principal_status == PrincipalStatus.REVOKED.value:",
        "ACTIVE must be the only acceptable status: SUSPENDED is reachable (the "
        "suite produces it) and must be refused exactly like REVOKED",
        _nodes(
            BRIDGE_TESTS, "TestFourWayInvariant::test_revoked_and_suspended_principals_are_refused"
        ),
    ),
    Mutation(
        "M03_bridge_role_gate_disabled",
        BRIDGE,
        "if action is not None and not role_permits(role, action):",
        "if False and action is not None and not role_permits(role, action):",
        "the role gate is the whole authorization decision; disabling it must be fatal",
        _nodes(
            BRIDGE_TESTS, "TestFourWayInvariant::test_viewer_and_auditor_cannot_execute_or_approve"
        ),
    ),
    Mutation(
        "M04_bridge_proof_leaves_out_the_role",
        BRIDGE,
        '            "role": self.role.value,',
        '            "role": "",',
        "the proof names the role it was issued for; an empty role silently "
        "degrades every downstream decision",
        _nodes(
            BRIDGE_TESTS, "TestUnifiedExecutionAuthorityProof::test_proof_payload_is_stable_json"
        ),
    ),
    Mutation(
        "M05_bridge_binding_ref_blank_check_dropped",
        BRIDGE,
        "if not binding_ref or not binding_ref.strip():",
        "if not binding_ref:",
        "an all-whitespace binding reference must be refused, not resolved",
        _nodes(BRIDGE_TESTS, "TestFourWayInvariant::test_resolve_principal_requires_a_binding_ref"),
    ),
    Mutation(
        "M06_bridge_binding_ref_loses_tenant_scope",
        BRIDGE,
        'return f"{CANONICAL_BINDING_PREFIX}{tenant_id}:{principal_id}"',
        'return f"{CANONICAL_BINDING_PREFIX}{principal_id}"',
        "the canonical binding ref is tenant-scoped; dropping the tenant would let "
        "one tenant's binding satisfy another's",
        _nodes(
            BRIDGE_TESTS,
            "TestAuthorityVocabulary::test_canonical_binding_ref_is_deterministic_and_tenant_scoped",
        ),
    ),
    Mutation(
        "M07_bridge_principal_tenant_check_inverted",
        BRIDGE,
        "if not hmac.compare_digest(principal_tenant, expected_tenant):",
        "if hmac.compare_digest(principal_tenant, expected_tenant):",
        "the principal's tenant must *equal* the expected tenant; inverting the "
        "comparison admits every foreign tenant",
        _nodes(BRIDGE_TESTS, "TestFourWayInvariant::test_cross_tenant_expected_tenant_is_refused"),
    ),
    Mutation(
        "M08_bridge_proof_reports_the_wrong_schema",
        BRIDGE,
        '_PROOF_SCHEMA = "nexus.studio.execution_authority_proof.v1"',
        '_PROOF_SCHEMA = "nexus.studio.execution_authority_proof.v0"',
        "the proof schema is what a verifier dispatches on; a wrong version makes "
        "every proof unparseable to a correct consumer",
        _nodes(
            BRIDGE_TESTS, "TestUnifiedExecutionAuthorityProof::test_proof_payload_is_stable_json"
        ),
    ),
    # ------------------------------------------------------------------ #
    # M09-M16 — revision pinning (creative/studio/revision_pinning.py)
    # ------------------------------------------------------------------ #
    Mutation(
        "M09_pins_foreign_keys_off",
        PINS,
        'self._conn.execute("PRAGMA foreign_keys=ON")',
        'self._conn.execute("PRAGMA foreign_keys=OFF")',
        "foreign keys must be enforced on every connection, or a pin can "
        "reference a project row that does not exist",
        _nodes(PINS_TESTS, "TestPinRevision::test_wal_pragmas_and_foreign_keys"),
    ),
    Mutation(
        "M10_pins_journal_mode_rollback",
        PINS,
        'self._conn.execute("PRAGMA journal_mode=WAL")',
        'self._conn.execute("PRAGMA journal_mode=DELETE")',
        "WAL is what lets a reader snapshot the head while a writer is mid-pin; "
        "rollback mode changes durability and concurrency semantics",
        _nodes(PINS_TESTS, "TestPinRevision::test_wal_pragmas_and_foreign_keys"),
    ),
    Mutation(
        "M11_pins_drop_symlink_refusal",
        PINS,
        "if stat_module.S_ISLNK(stat_result.st_mode):\n"
        "            raise AssetPinViolationError(\n"
        '                "symlink_rejected", f"{candidate} is a symlink; pins are for real files"',
        "if False and stat_module.S_ISLNK(stat_result.st_mode):\n"
        "            raise AssetPinViolationError(\n"
        '                "symlink_rejected", f"{candidate} is a symlink; pins are for real files"',
        "a symlinked asset escapes the authorized root; refusing it is the jail",
        _nodes(PINS_TESTS, "TestPinAssetFromDisk::test_rejects_a_symlinked_file"),
    ),
    Mutation(
        "M12_pins_drop_root_containment",
        PINS,
        "if not resolved.is_relative_to(root):\n"
        "            raise AssetPinViolationError(\n"
        '                "path_escape", f"{resolved} is outside the authorized root {root}"',
        "if False and not resolved.is_relative_to(root):\n"
        "            raise AssetPinViolationError(\n"
        '                "path_escape", f"{resolved} is outside the authorized root {root}"',
        "the path jail is the only thing preventing a pin outside the root",
        _nodes(PINS_TESTS, "TestPinAssetFromDisk::test_rejects_a_path_outside_the_authorized_root"),
    ),
    Mutation(
        "M13_pins_parent_head_check_inverted",
        PINS,
        "if not hmac.compare_digest(str(head_revision_id), str(envelope.parent_revision_id)):",
        "if hmac.compare_digest(str(head_revision_id), str(envelope.parent_revision_id)):",
        "optimistic concurrency asserts parent == head; inverting it inverts the rule",
        _nodes(
            PINS_TESTS,
            "TestPinRevision::test_linear_chain_one_two_three_marks_predecessors_superseded",
        ),
    ),
    Mutation(
        "M14_pins_worker_plan_digest_unchecked",
        PINS,
        "if not hmac.compare_digest(envelope.plan_jcs_sha256, expected_plan_jcs_sha256):",
        "if False and not hmac.compare_digest(envelope.plan_jcs_sha256, expected_plan_jcs_sha256):",
        "a worker that stops verifying the plan digest would execute a different "
        "plan than was approved",
        _nodes(PINS_TESTS, "TestVerifyWorkerExecutionPins::test_plan_digest_mismatch_is_drift"),
    ),
    Mutation(
        "M15_pins_head_query_drops_the_tenant",
        PINS,
        '"SELECT head_revision_id, head_revision_number, tenant_id "',
        '"SELECT head_revision_id, head_revision_number, head_revision_id "',
        "the head row's tenant column is what the cross-tenant guard reads; if the "
        "query returns the revision id there instead, the guard compares a revision "
        "id against a tenant and every later pin on the project is refused",
        _nodes(
            PINS_TESTS,
            "TestPinRevision::test_linear_chain_one_two_three_marks_predecessors_superseded",
        ),
    ),
    Mutation(
        "M16_pins_retry_digest_ignores_the_asset_bytes",
        PINS,
        "if hmac.compare_digest(existing.revision_jcs_sha256, retry_digest):",
        "if hmac.compare_digest(existing.revision_id, envelope.revision_id):",
        "an idempotent retry must prove it is the *same* payload; comparing ids "
        "alone lets a changed asset set reuse a revision id",
        _nodes(
            PINS_TESTS, "TestPinRevision::test_conflicting_payload_under_the_same_id_is_rejected"
        ),
    ),
    # ------------------------------------------------------------------ #
    # M17-M24 — execution outbox (nagar/ibpc/execution_outbox.py)
    # ------------------------------------------------------------------ #
    Mutation(
        "M17_outbox_timeout_targets_terminal",
        OUTBOX,
        'target="AWAITING_WORKER",',
        'target="FAILED_TERMINAL",',
        "a poll timeout must park the turn, never fail it",
        _nodes(
            OUTBOX_TESTS,
            "TestCrashRecovery::test_poll_timeout_is_awaiting_worker_not_a_failure",
        ),
    ),
    Mutation(
        "M18_outbox_reconcile_ignores_consumed_rows",
        OUTBOX,
        'target="APPROVAL_CONSUMED",',
        'target="INTENT_STAGED",',
        "consuming the approval must advance the row; writing it back to "
        "INTENT_STAGED re-arms a spent single-use approval",
        _nodes(
            OUTBOX_TESTS, "TestCrashRecovery::test_crash_after_approval_consumed_never_reconsumes"
        ),
    ),
    Mutation(
        "M19_outbox_payload_scope_check_skipped",
        OUTBOX,
        "if not isinstance(actual, str) or not hmac.compare_digest(actual, declared[key]):",
        "if False:",
        "the payload scope guard stops one principal's approval being staged against another scope",
        _nodes(
            OUTBOX_TESTS,
            "TestFailClosed::test_payload_scope_mismatch_is_refused_before_any_write",
        ),
    ),
    Mutation(
        "M20_outbox_idempotency_key_drops_turn_id",
        OUTBOX,
        "(tenant_id, project_id, session_id, turn_id, approval_id, revision_jcs_sha256)",
        "(tenant_id, project_id, session_id, approval_id, "
        "revision_jcs_sha256, revision_jcs_sha256)",
        "the key must be derived from all six fields; two different turns must "
        "never share an intent",
        _nodes(
            OUTBOX_TESTS,
            "TestIdentityAndVocabulary::test_idempotency_key_is_deterministic_and_field_separated",
        ),
    ),
    Mutation(
        "M21_outbox_payload_conflict_not_raised",
        OUTBOX,
        "if not hmac.compare_digest(existing.plan_payload_json, payload_json):",
        "if False and not hmac.compare_digest(existing.plan_payload_json, payload_json):",
        "the same key with a different payload is a conflict, never a silent overwrite",
        _nodes(
            OUTBOX_TESTS,
            "TestFailClosed::test_different_payload_for_a_live_key_conflicts",
        ),
    ),
    Mutation(
        "M22_outbox_verification_skipped",
        OUTBOX,
        "if self._verify is not None:",
        "if False and self._verify is not None:",
        "an unverified completion must never be reported as success",
        _nodes(OUTBOX_TESTS, "TestHappyPath::test_verification_refusal_is_terminal"),
    ),
    Mutation(
        "M23_outbox_job_repointing_allowed",
        OUTBOX,
        'if not hmac.compare_digest(row.job_id or "", job_id):',
        'if False and not hmac.compare_digest(row.job_id or "", job_id):',
        "one intent may record exactly one job id; re-pointing it would orphan the first attempt",
        _nodes(
            OUTBOX_TESTS,
            "TestCrashRecovery::test_replaying_a_different_job_id_for_one_intent_is_refused",
        ),
    ),
    Mutation(
        "M24_outbox_unknown_row_silently_skipped",
        OUTBOX,
        "        if row is None:\n"
        '            raise ExecutionOutboxError("unknown_outbox", '
        'f"{outbox_id!r} does not exist")\n'
        "        return row",
        "        if row is None:\n            return _MISSING_ROW\n        return row",
        "an unknown outbox id must be a typed refusal; anything else is a crash or "
        "a silent no-op on a request the caller believes succeeded",
        _nodes(OUTBOX_TESTS, "TestFailClosed::test_invalid_store_selectors_and_limits_are_refused"),
    ),
    # ------------------------------------------------------------------ #
    # M25-M32 — preview readback (creative/studio/preview_readback.py)
    # ------------------------------------------------------------------ #
    Mutation(
        "M25_readback_containment_includes_end",
        READBACK,
        "return self.start.seconds <= instant.seconds < self.end.seconds",
        "return self.start.seconds <= instant.seconds <= self.end.seconds",
        "the span is half-open; including the end point double-counts the boundary",
        _nodes(READBACK_TESTS, "TestRationalTimeSpan::test_half_open_containment"),
    ),
    Mutation(
        "M26_readback_frames_truncate_instead_of_round",
        READBACK,
        "return (2 * numerator + denominator) // (2 * denominator)",
        "return numerator // denominator",
        "half-up rounding keeps a 12.5-frame span a whole 13 frames; truncation "
        "silently loses a frame",
        _nodes(
            READBACK_TESTS,
            "TestFrameArithmetic::test_frame_count_uses_half_up_rounding_and_never_a_float",
        ),
    ),
    Mutation(
        "M27_readback_digest_leaves_out_the_timebase",
        READBACK,
        '            "timebase": _fraction_text(self.timebase),',
        '            "timebase": "",',
        "the timebase is part of what was shown; dropping it from the digest means "
        "a 25fps and a 30000/1001 readback hash identically",
        _nodes(READBACK_TESTS, "TestWysiwys::test_tampering_with_the_timebase_breaks_the_digest"),
    ),
    Mutation(
        "M28_readback_integrity_swallows_the_comparison",
        READBACK,
        "        except Exception:  # noqa: BLE001 - fail-closed by construction\n"
        "            return False",
        "        except Exception:  # noqa: BLE001 - fail-closed by construction\n"
        "            return True",
        "a readback too corrupted to re-hash must be *unverified*; returning True "
        "on the re-hash failure turns corruption into approval",
        _nodes(
            READBACK_TESTS,
            "TestWysiwys::test_verification_fails_closed_when_the_readback_cannot_be_hashed",
        ),
    ),
    Mutation(
        "M29_readback_accepts_a_float",
        READBACK,
        "raise PreviewReadbackError(\n"
        '        "invalid_time",\n'
        '        f"{field_name} must be an int, Fraction or '
        "'n/d' string, got {type(value).__name__}\",\n"
        "    )",
        "return Fraction(value)",
        "a float must be refused at the boundary; accepting one reintroduces the "
        "rounding this module exists to remove",
        _nodes(READBACK_TESTS, "TestRationalTime::test_floats_are_refused_outright"),
    ),
    Mutation(
        "M30_readback_expected_digest_unchecked",
        READBACK,
        "return hmac.compare_digest(readback.preview_digest, expected_digest)",
        "return True",
        "the approval's recorded digest must be compared, not assumed",
        _nodes(READBACK_TESTS, "TestWysiwys::test_a_wrong_expected_digest_fails"),
    ),
    Mutation(
        "M31_readback_moved_cuts_ignored",
        READBACK,
        "and _operation_signature(base_map[key]) != _operation_signature(head_map[key])",
        "and False",
        "a moved cut must be reported as changed, never silently dropped",
        _nodes(
            READBACK_TESTS,
            "TestCompareRevisions::test_a_moved_cut_is_reported_as_changed_not_add_plus_remove",
        ),
    ),
    Mutation(
        "M32_readback_drift_uses_rounded_microseconds",
        READBACK,
        '"0"',
        '"0.0"',
        "identity is decided on the exact rational drift; a rounded spelling "
        "changes what 'identical' means",
        _nodes(
            READBACK_TESTS,
            "TestCompareRevisions::test_a_revision_compared_with_itself_is_identical",
        ),
    ),
    # ------------------------------------------------------------------ #
    # M33-M40 — disaster recovery (creative/studio/production_dr.py)
    # ------------------------------------------------------------------ #
    Mutation(
        "M33_dr_signature_comparison_always_true",
        DR,
        "return hmac.compare_digest(self.sign(payload), signature_hex.strip().lower())",
        "return True",
        "a signature check that always passes makes the manifest advisory",
        _nodes(
            DR_TESTS, "TestBackupManifest::test_a_manifest_signed_by_another_key_does_not_verify"
        ),
    ),
    Mutation(
        "M34_dr_restore_skips_verification",
        DR,
        "if not report.verified or report.manifest is None:",
        "if False and (not report.verified or report.manifest is None):",
        "restoring an unverified bundle is the exact disaster this guard blocks",
        _nodes(DR_TESTS, "TestRestore::test_a_tampered_bundle_is_never_restored"),
    ),
    Mutation(
        "M35_dr_verify_ignores_key_id",
        DR,
        '"""True only for a signature this key produced over exactly these fields."""\n'
        "        if key.key_id != self.key_id:\n"
        "            return False",
        '"""True only for a signature this key produced over exactly these fields."""\n'
        "        if False and key.key_id != self.key_id:\n"
        "            return False",
        "a manifest must be verified by the key that signed it, not any key",
        _nodes(
            DR_TESTS, "TestBackupManifest::test_a_manifest_signed_by_another_key_does_not_verify"
        ),
    ),
    Mutation(
        "M36_dr_key_secret_leaks_in_repr",
        DR,
        "secret_hex='<redacted>'",
        "secret_hex={self.secret_hex!r}",
        "the redacted repr is what keeps the key out of every log",
        _nodes(DR_TESTS, "TestBackupKeyMaterial::test_the_secret_never_appears_in_repr_or_str"),
    ),
    Mutation(
        "M37_dr_staging_integrity_ignored",
        DR,
        'if staged_integrity != "ok":',
        'if False and staged_integrity != "ok":',
        "the staged copy is verified before it is swapped in; skipping it trusts "
        "a file that was never read",
        _nodes(
            DR_TESTS,
            "TestRestore::test_a_staged_copy_that_fails_integrity_is_discarded",
        ),
    ),
    Mutation(
        "M38_dr_quarantine_skipped",
        DR,
        "                if target.is_file():\n                    quarantined = str(",
        "                if False and target.is_file():\n                    quarantined = str(",
        "the pre-restore copy is the only recoverable copy of the live database; "
        "restoring without quarantining destroys the evidence",
        _nodes(
            DR_TESTS,
            "TestRestore::test_restoring_over_a_live_database_quarantines_it_and_drops_stale_sidecars",
        ),
    ),
    Mutation(
        "M39_dr_cancellation_releases_immediately",
        DR,
        "lease.detach()",
        "lease.release()",
        "releasing on cancellation frees a slot whose thread is still running: "
        "the over-admission bug",
        _nodes(
            DR_TESTS,
            "TestStudioResourceGovernor::test_cancellation_keeps_the_slot_until_the_thread_finishes",
        ),
    ),
    Mutation(
        "M40_dr_short_key_accepted",
        DR,
        "if len(raw) < _KEY_BYTES:",
        "if False and len(raw) < _KEY_BYTES:",
        "a short HMAC key is a weak signature; the length floor is the guard",
        _nodes(DR_TESTS, "TestBackupKeyMaterial::test_a_short_key_is_refused"),
    ),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _purge_bytecode_cache(source_path: Path) -> None:
    """Drop every cached bytecode artefact for ``source_path``.

    A ``.pyc`` is validated against the source's size and its mtime truncated to
    whole seconds, so an equal-byte-length mutation written in the same second as
    the last compile can be shadowed by the cached bytecode (or, after the
    restore, a *mutated* ``.pyc`` can survive next to pristine source).
    """
    try:
        cache_path: Path | None = Path(importlib.util.cache_from_source(str(source_path)))
    except (NotImplementedError, ValueError):  # pragma: no cover - exotic loaders
        cache_path = None
    if cache_path is not None:
        cache_path.unlink(missing_ok=True)
    pycache = source_path.parent / "__pycache__"
    if pycache.is_dir():
        for stale in pycache.glob(f"{source_path.stem}.*.pyc"):
            stale.unlink(missing_ok=True)
    importlib.invalidate_caches()


def _run_pytest(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "--no-header", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


_FAILED_RE = re.compile(r"FAILED\s+(\S+)")


def _is_valid_kill(
    result: subprocess.CompletedProcess[str], expected_node_ids: tuple[str, ...]
) -> tuple[bool, str]:
    """The five-condition VALID_KILL rule (anchor count/parse handled by caller)."""
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 1:
        return False, f"pytest exit code {result.returncode} (not 1)"
    lowered = output.lower()
    for marker in (
        "internalerror",
        "importerror",
        "modulenotfounderror",
        "syntaxerror",
        "indentationerror",
        "collected 0 items",
        "no tests ran",
    ):
        if marker in lowered:
            return False, f"infrastructure failure in output ({marker})"
    failed_nodes = {
        match.group(1)
        for match in (_FAILED_RE.search(line) for line in output.splitlines())
        if match
    }
    if not any(node in failed_nodes for node in expected_node_ids):
        return False, (f"none of the expected node IDs failed (failed: {sorted(failed_nodes)})")
    # A real test failure, not infrastructure noise.  Three shapes count:
    #   * an assertion failure ("AssertionError" / "assert ..."),
    #   * a typed exception the code under test raised (pytest prints it on an
    #     "E " line: ``E   nexus...AssetPinViolationError: symlink_rejected``),
    #   * a "DID NOT RAISE" from ``pytest.raises``, which *is* a failed guard.
    # Matching only literal "AssertionError"/"ValueError" would reject a typed
    # domain error, so the classifier accepts any raised exception class instead.
    has_assertion = "AssertionError" in output or "assert " in output
    has_raised = bool(re.search(r"^E\s+[\w.]*(?:Error|Exception|Failure)\b", output, re.M))
    if not (has_assertion or has_raised or "Failed:" in output):
        return False, "no valid failure effect in output (no assertion, no raised exception)"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}  - {mutation.why}")
        return 0

    # Pre-flight: purge every target's cache, then require the suites green.
    for target in sorted({mutation.path for mutation in MUTATIONS}):
        _purge_bytecode_cache(target)
    for suite in SUITES:
        if _run_pytest([suite]).returncode != 0:
            print(f"FATAL: suite {suite} is not green before mutation testing", file=sys.stderr)
            return 2

    killed = 0
    survivors: list[str] = []
    for mutation in MUTATIONS:
        path = mutation.path
        original_bytes = path.read_bytes()
        original_digest = _sha256_bytes(original_bytes)
        original_text = original_bytes.decode("utf-8")
        anchor_count = original_text.count(mutation.old)
        if anchor_count != 1:
            print(f"  SKIP {mutation.name}: anchor occurs {anchor_count} times")
            survivors.append(f"{mutation.name} (anchor mismatch)")
            continue
        mutated = original_text.replace(mutation.old, mutation.new, 1)
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            print(f"  SKIP {mutation.name}: mutation does not parse ({exc})")
            survivors.append(f"{mutation.name} (parse failure)")
            continue
        restore_failed: str | None = None
        try:
            path.write_bytes(mutated.encode("utf-8"))
            _purge_bytecode_cache(path)
            result = _run_pytest(list(mutation.expected_node_ids))
            ok, reason = _is_valid_kill(result, mutation.expected_node_ids)
            if ok:
                killed += 1
                print(f"  VALID_KILL {mutation.name}")
            else:
                survivors.append(f"{mutation.name} ({reason})")
                print(f"  SURVIVED {mutation.name}: {reason}")
        finally:
            path.write_bytes(original_bytes)
            _purge_bytecode_cache(path)
            if _sha256_bytes(path.read_bytes()) != original_digest:
                restore_failed = mutation.name
        if restore_failed is not None:
            print(f"  FATAL: file not restored for {restore_failed}", file=sys.stderr)
            return 4

    for suite in SUITES:
        if _run_pytest([suite]).returncode != 0:
            print("FATAL: suite still red after restore", file=sys.stderr)
            return 3

    total = len(MUTATIONS)
    print(f"\n{killed}/{total} mutants killed (VALID_KILL)")
    if survivors:
        print(f"survivors: {', '.join(survivors)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
