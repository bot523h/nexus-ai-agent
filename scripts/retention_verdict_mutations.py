#!/usr/bin/env python3
"""Mutation probes for the thread-scoped retention verdict.

The defect this harness exists to keep dead
-------------------------------------------
``nexus checkpoints inspect`` derived a thread's destructive verdict
(``pinned`` / ``active`` / ``resumable_within_window`` / ``would_delete``)
from ``metadata[-1]`` -- one row of a ``SELECT`` with no ``ORDER BY``.  The
lifecycle index is a rowid table, so that row is *insertion order*, and two
logically identical threads got opposite answers.  Measured before the fix::

    stale row written SECOND -> SELECT order ['cp_live', 'cp_old']
        pinned=False  active=False  would_delete=True     <-- a live thread
    stale row written FIRST  -> SELECT order ['cp_old', 'cp_live']
        pinned=True   active=True   would_delete=False

The invariant under test is a quantifier asymmetry: protection is
EXISTENTIAL, destruction is UNIVERSAL, zero rows is UNKNOWN and retained.

Probes
======
======  ===================================================  =====================
probe   mutation                                             killed by
======  ===================================================  =====================
#1      CLI reads the verdict off the last row again          inspect-v1 contract
#2      protection flipped EXISTENTIAL -> UNIVERSAL           quantifier tests
#3      destruction flipped UNIVERSAL -> EXISTENTIAL          quantifier tests
#4      empty-index fail-closed guard removed                 unknown-is-retained
#5      ``active`` re-spelled independently of ``pinned``     alias tests
#6      primitive measures only the first row                 primitive tests
#7      ``RESUMABILITY_WINDOW`` re-spelled as a literal       drift guard
#8      ``RetentionPolicy.max_age`` re-spelled as a literal   drift guard
#9      M1 missing access evidence becomes deletable         fail-closed tests
#10     M2 the active pin is ignored                         quantifier tests
#11     M3 recent access is ignored                          window tests
#12     M6 resumable == not deletable (known guard dropped)  truth-preservation
#13     M8 ``row_reason`` becomes a shadow predicate         biconditional test
#14     M8 ``deletable`` silently re-based on ``created_at`` divergence sweep
#15     reason loses its NO_EVIDENCE fallback                biconditional test
#16     ``protection_reason`` hard-coded to ``none``         inspect-v1 contract
#17     ``deletable`` re-implemented in parallel             biconditional test
======  ===================================================  =====================

Mandated-probe mapping: M1=#9, M2=#10, M3=#11, M4=#6 (a vetoing row is never
measured), M5=#4, M6=#12, M7=#7+#8, M8=#13+#14.  #1/#2/#3/#5/#15/#16 are
additional probes for the thread-scoped verdict and its reason.

Protocol (same shape as ``scripts/gate5_mutation_probes.py``): baseline
GREEN -> apply mutant -> expect RED -> restore bytes -> SHA restored -> GREEN
again.  Nothing is left mutated and the run is idempotent.  A probe counts as
"caught" only when the targeted tests actually flip GREEN -> RED (non-zero
exit), never on warnings.

Usage:  python scripts/retention_verdict_mutations.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "--tb=no"]

POLICY = REPO / "src/nexus_ai_agent/domain/policies/retention.py"
CLI = REPO / "src/nexus_ai_agent/cli.py"
LIFECYCLE = REPO / "src/nexus_ai_agent/storage/checkpoint_lifecycle.py"

V = "tests/unit/test_thread_retention_verdict.py::"
INSP = "tests/unit/test_inspect_contract.py::"
D = "tests/unit/test_retention_policy_divergence.py::"


@dataclass(frozen=True)
class Probe:
    name: str
    target: Path
    anchor: str  # exact text to replace (must be unique in the file)
    mutant: str
    tests: tuple[str, ...]  # must flip GREEN -> RED


_CLI_VERDICT_ANCHOR = """            verdict = thread_retention(
                (
                    RetentionRecord(item.created_at, item.last_accessed_at, item.active_until)
                    for item in metadata
                ),
                now=now,
            )
"""

_PINNED_ANCHOR = "        pinned=any(pinned(record, now=current) for record in measured),\n"

_DELETABLE_ANCHOR = (
    "        deletable=bool(measured)"
    " and all(deletable(record, now=current) for record in measured),\n"
)

_ACTIVE_ANCHOR = """    @property
    def active(self) -> bool:
        \"\"\"Alias of :attr:`pinned` — one concept, one implementation.\"\"\"
        return self.pinned
"""

_MEASURED_ANCHOR = "    measured: Sequence[RetentionRecord] = tuple(records)\n"

PROBES: tuple[Probe, ...] = (
    Probe(
        name="#1 CLI verdict from the last row again",
        target=CLI,
        anchor=_CLI_VERDICT_ANCHOR,
        mutant="""            _last = metadata[-1:]  # MUTATION: last row decides
            verdict = thread_retention(
                (
                    RetentionRecord(item.created_at, item.last_accessed_at, item.active_until)
                    for item in _last
                ),
                now=now,
            )
""",
        tests=(
            INSP + "test_inspect_reports_a_pinned_thread_as_pinned",
            INSP + "test_inspect_verdict_is_invariant_under_lifecycle_row_order",
        ),
    ),
    Probe(
        name="#2 protection EXISTENTIAL -> UNIVERSAL",
        target=POLICY,
        anchor=_PINNED_ANCHOR,
        mutant="        pinned=all(pinned(record, now=current) for record in measured),\n",
        tests=(
            V + "test_protection_is_existential_one_pinned_row_protects_the_thread",
            V + "test_the_verdict_is_a_function_of_the_rows_not_of_their_order",
            INSP + "test_inspect_reports_a_pinned_thread_as_pinned",
        ),
    ),
    Probe(
        name="#3 destruction UNIVERSAL -> EXISTENTIAL",
        target=POLICY,
        anchor=_DELETABLE_ANCHOR,
        mutant=(
            "        deletable=bool(measured)"
            " and any(deletable(record, now=current) for record in measured),\n"
        ),
        tests=(
            V + "test_destruction_is_universal_every_row_must_be_deletable",
            V + "test_unknown_access_state_blocks_deletion_of_the_whole_thread",
            INSP + "test_inspect_would_delete_requires_every_row_to_be_deletable",
        ),
    ),
    Probe(
        name="#4 empty-index fail-closed guard removed",
        target=POLICY,
        anchor=_DELETABLE_ANCHOR,
        mutant=("        deletable=all(deletable(record, now=current) for record in measured),\n"),
        tests=(
            V + "test_zero_rows_is_unknown_and_never_deletable",
            V + "test_rows_count_is_the_number_of_rows_examined",
        ),
    ),
    Probe(
        name="#5 `active` re-spelled independently of `pinned`",
        target=POLICY,
        anchor=_ACTIVE_ANCHOR,
        mutant="""    @property
    def active(self) -> bool:
        return False  # MUTATION: a second, divergent spelling of one predicate
""",
        tests=(
            V + "test_protection_is_existential_one_pinned_row_protects_the_thread",
            INSP + "test_inspect_reports_a_pinned_thread_as_pinned",
        ),
    ),
    Probe(
        name="#6 primitive measures only the first row",
        target=POLICY,
        anchor=_MEASURED_ANCHOR,
        mutant="    measured: Sequence[RetentionRecord] = tuple(records)[:1]\n",
        tests=(
            V + "test_rows_count_is_the_number_of_rows_examined",
            V + "test_destruction_is_universal_every_row_must_be_deletable",
        ),
    ),
    Probe(
        name="#7 RESUMABILITY_WINDOW re-spelled as a literal",
        target=POLICY,
        anchor=(
            "RESUMABILITY_WINDOW: Final[timedelta] = "
            "DEFAULT_RETENTION_DECISION.resumability_window\n"
        ),
        mutant="RESUMABILITY_WINDOW: Final[timedelta] = timedelta(days=7)  # MUTATION\n",
        tests=(
            V + "test_the_resumability_window_has_exactly_one_source",
            "tests/architecture/test_glossary_liveness.py::test_retention_contract_remains_live",
        ),
    ),
    Probe(
        name="#8 RetentionPolicy.max_age re-spelled as a literal",
        target=LIFECYCLE,
        anchor="    max_age: timedelta = RESUMABILITY_WINDOW\n",
        mutant="    max_age: timedelta = timedelta(days=30)  # MUTATION\n",
        tests=(V + "test_the_resumability_window_has_exactly_one_source",),
    ),
    # ── M1: missing access evidence becomes deletable ────────────────────
    Probe(
        name="#9 M1 missing access becomes deletable",
        target=POLICY,
        anchor=(
            "    if record.last_accessed_at is None:\n        return RetentionReason.NO_EVIDENCE\n"
        ),
        mutant=(
            "    if record.last_accessed_at is None:\n"
            "        return RetentionReason.NONE  # MUTATION: no evidence inferred as safe\n"
        ),
        tests=(
            V + "test_unknown_access_state_blocks_deletion_of_the_whole_thread",
            V + "test_row_reason_can_explain_but_never_contradict_deletable",
            D + "test_resumability_policy_is_fail_closed_on_missing_evidence",
            INSP + "test_inspect_reports_absence_of_evidence_as_no_evidence",
        ),
    ),
    # ── M2: the active pin is ignored ────────────────────────────────────
    Probe(
        name="#10 M2 active pin ignored",
        target=POLICY,
        anchor=(
            "    return record.active_until is not None and current < _utc(record.active_until)\n"
        ),
        mutant="    return False  # MUTATION: an explicit pin no longer protects\n",
        tests=(
            V + "test_protection_is_existential_one_pinned_row_protects_the_thread",
            V + "test_reason_distinguishes_pinned_from_recent_from_unknown",
            D + "test_states_where_both_policies_agree",
            INSP + "test_inspect_reports_a_pinned_thread_as_pinned",
        ),
    ),
    # ── M3: recent access is ignored ─────────────────────────────────────
    Probe(
        name="#11 M3 recent access ignored",
        target=POLICY,
        anchor=(
            "    if current - _utc(record.last_accessed_at) < RESUMABILITY_WINDOW:\n"
            "        return RetentionReason.RECENT_ACCESS\n"
            "    return RetentionReason.NONE\n"
        ),
        mutant="    return RetentionReason.NONE  # MUTATION: window not consulted\n",
        tests=(
            V + "test_reason_distinguishes_pinned_from_recent_from_unknown",
            D + "test_states_where_both_policies_agree",
            INSP + "test_inspect_distinguishes_recency_from_an_explicit_pin",
        ),
    ),
    # ── M6: resumable == not deletable, without semantic justification ───
    Probe(
        name="#12 M6 resumable == not deletable (drops the known guard)",
        target=CLI,
        anchor=(
            '                "resumable_within_window": verdict.known and not verdict.deletable,\n'
        ),
        mutant=('                "resumable_within_window": not verdict.deletable,  # MUTATION\n'),
        tests=(INSP + "test_inspect_does_not_claim_resumability_it_has_no_evidence_for",),
    ),
    # ── M8a: the reason becomes a shadow predicate ───────────────────────
    Probe(
        name="#13 M8 row_reason diverges from deletable (shadow predicate)",
        target=POLICY,
        anchor=(
            "    current = _utc(now)\n"
            "    if pinned(record, now=current):\n"
            "        return RetentionReason.PINNED\n"
            "    if record.last_accessed_at is None:\n"
            "        return RetentionReason.NO_EVIDENCE\n"
        ),
        mutant=(
            "    current = _utc(now)\n"
            "    if record.last_accessed_at is None:\n"
            "        return RetentionReason.NO_EVIDENCE\n"
        ),
        tests=(
            V + "test_row_reason_can_explain_but_never_contradict_deletable",
            V + "test_reason_distinguishes_pinned_from_recent_from_unknown",
        ),
    ),
    # ── M8b: the resumability policy silently converges on the other one ──
    Probe(
        name="#14 M8 deletable re-based on created_at (silent policy merge)",
        target=POLICY,
        anchor="    if current - _utc(record.last_accessed_at) < RESUMABILITY_WINDOW:\n",
        mutant=("    if current - _utc(record.created_at) < RESUMABILITY_WINDOW:  # MUTATION\n"),
        tests=(
            D + "test_axis2b_is_unreachable_in_production_and_is_documented_as_such",
            D + "test_states_where_both_policies_agree",
            D + "test_the_divergence_set_is_exactly_the_two_documented_axes",
        ),
    ),
    # ── the reason loses its NO_EVIDENCE fallback ────────────────────────
    Probe(
        name="#15 reason loses its NO_EVIDENCE fallback (biconditional broken)",
        target=POLICY,
        anchor=(
            "    RetentionReason.PINNED,\n"
            "    RetentionReason.RECENT_ACCESS,\n"
            "    RetentionReason.NO_EVIDENCE,\n"
            ")\n"
        ),
        mutant=("    RetentionReason.PINNED,\n    RetentionReason.RECENT_ACCESS,\n)\n"),
        tests=(
            V + "test_reason_is_none_exactly_when_the_thread_is_deletable",
            V + "test_reason_distinguishes_pinned_from_recent_from_unknown",
            INSP + "test_inspect_reports_absence_of_evidence_as_no_evidence",
        ),
    ),
    # ── protection_reason drops out of the CLI contract ──────────────────
    # ── deletable stops delegating and becomes a parallel copy ───────────
    Probe(
        name="#17 deletable re-implemented in parallel (drops the pin check)",
        target=POLICY,
        anchor="    return row_reason(record, now=now) is RetentionReason.NONE\n",
        mutant=(
            "    if record.last_accessed_at is None:\n"
            "        return False\n"
            "    return (\n"
            "        _utc(now) - _utc(record.last_accessed_at) >= RESUMABILITY_WINDOW\n"
            "    )  # MUTATION: parallel copy of the decision, pin check dropped\n"
        ),
        tests=(
            V + "test_row_reason_can_explain_but_never_contradict_deletable",
            V + "test_reason_is_none_exactly_when_the_thread_is_deletable",
            INSP + "test_inspect_reports_a_pinned_thread_as_pinned",
            D + "test_states_where_both_policies_agree",
        ),
    ),
    Probe(
        name="#16 protection_reason hard-coded to `none`",
        target=CLI,
        anchor='                "protection_reason": str(verdict.reason),\n',
        mutant='                "protection_reason": "none",  # MUTATION\n',
        tests=(
            INSP + "test_inspect_reports_why_a_thread_is_retained",
            INSP + "test_inspect_distinguishes_recency_from_an_explicit_pin",
            INSP + "test_inspect_reports_absence_of_evidence_as_no_evidence",
        ),
    ),
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_tests(tests: tuple[str, ...]) -> int:
    return subprocess.run([*PYTEST, *tests], cwd=REPO, capture_output=True, text=True).returncode


def _apply(probe: Probe) -> None:
    text = probe.target.read_text(encoding="utf-8")
    if text.count(probe.anchor) != 1:
        raise SystemExit(
            f"anchor for {probe.name} is not unique in {probe.target}: "
            f"{text.count(probe.anchor)} matches"
        )
    probe.target.write_text(text.replace(probe.anchor, probe.mutant), encoding="utf-8")


def main() -> int:
    failures: list[str] = []
    caught = 0

    # Fail loudly up front: an anchor that no longer matches means the
    # implementation moved, and a silently-skipped probe is worse than none.
    for probe in PROBES:
        occurrences = probe.target.read_text(encoding="utf-8").count(probe.anchor)
        if occurrences != 1:
            print(f"FAIL: {probe.name}: anchor occurs {occurrences}× in {probe.target}")
            failures.append(f"{probe.name}: anchor not unique ({occurrences})")
    if failures:
        print("\n=== summary: harness cannot run, anchors are stale ===")
        return 1

    for probe in PROBES:
        original = probe.target.read_bytes()
        before_sha = hashlib.sha256(original).hexdigest()
        print(f"\n=== {probe.name} ({probe.target.relative_to(REPO)})")

        baseline = _run_tests(probe.tests)
        if baseline != 0:
            failures.append(f"{probe.name}: baseline not GREEN (exit {baseline})")
            print(f"  baseline: NOT GREEN (exit {baseline}) — probe invalid")
            continue
        print("  baseline: GREEN")

        try:
            _apply(probe)
        except SystemExit as exc:
            failures.append(f"{probe.name}: {exc}")
            print(f"  mutant: NOT APPLIED — {exc}")
            continue
        mutant_exit = _run_tests(probe.tests)
        if mutant_exit == 0:
            failures.append(f"{probe.name}: mutant survived (tests stayed GREEN)")
            print("  mutant:  SURVIVED (tests still green) — invariant NOT protected")
        else:
            caught += 1
            print(f"  mutant:  RED (exit {mutant_exit}) — caught")

        probe.target.write_bytes(original)
        if _sha(probe.target) != before_sha:
            failures.append(f"{probe.name}: restore changed the file")
            print(f"  restore: SHA MISMATCH != {before_sha[:12]}…")
            continue
        print(f"  restore: SHA restored {before_sha[:12]}…")

        restored = _run_tests(probe.tests)
        if restored != 0:
            failures.append(f"{probe.name}: not GREEN after restore (exit {restored})")
            print(f"  rerun:   NOT GREEN (exit {restored})")
        else:
            print("  rerun:   GREEN")

    print(f"\n=== summary: {caught}/{len(PROBES)} mutants caught ===")
    for failure in failures:
        print(f"FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
