#!/usr/bin/env python3
"""Adversarial mutation harness for the IBPC security closure (Phases B/C/D).

Evidence rule: a guard that has never been attacked is not evidence.  This
script rewrites the shipped IBPC security code with a specific weakening,
re-runs the security suites, and requires them to turn RED.  Every source file
is restored afterwards (including on failure), and the final run must be GREEN
again.

Usage::

    python scripts/ibpc_security_mutations.py           # run every mutation
    python scripts/ibpc_security_mutations.py --list

Exit code 0 means "N/N mutants killed"; anything else means a guard is fake.
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped files byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IBPC = ROOT / "src" / "nexus_ai_agent" / "nagar" / "ibpc"
TESTS = (
    "tests/unit/test_ibpc_security.py",
    "tests/unit/test_ibpc_approval.py",
    "tests/unit/test_ibpc_authority.py",
    "tests/unit/test_ibpc_proof_gate.py",
)


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    edits: tuple[tuple[str, str], ...]
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "turn_id_regex_disabled",
        IBPC / "session.py",
        (
            (
                'TURN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")',
                'TURN_ID_RE = re.compile(r"^.+$", re.DOTALL)',
            ),
        ),
        "traversal/absolute/backslash/overlong turn ids must be rejected at the boundary",
    ),
    Mutation(
        "oexcl_trunc_overwrite",
        IBPC / "session.py",
        (
            (
                "fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
                "fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)",
            ),
        ),
        "staging must never overwrite an existing file (O_EXCL), even on collision",
    ),
    Mutation(
        "world_readable_staging",
        IBPC / "session.py",
        (
            (
                "fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
                "fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)",
            ),
            (
                "os.fchmod(fd, 0o600)  # force perms regardless of umask",
                "pass  # mutation: perms not forced",
            ),
        ),
        "staged audio must be created minimum-privilege (0600)",
    ),
    Mutation(
        "audio_cleanup_removed",
        IBPC / "session.py",
        (
            (
                "            # success, exception, timeout AND cancellation all land here\n"
                "            self._cleanup_audio(root, staged)",
                "            pass  # mutation: cleanup removed",
            ),
        ),
        "the staged audio file must be deleted on success/exception/timeout/cancel",
    ),
    Mutation(
        "tenant_field_not_compared",
        IBPC / "approval.py",
        (
            (
                "        for name in _IDENTITY_FIELDS:\n"
                "            actual = getattr(binding, name)",
                "        for name in _IDENTITY_FIELDS:\n"
                '            if name == "tenant_id":\n'
                "                continue\n"
                "            actual = getattr(binding, name)",
            ),
        ),
        "a binding from another tenant must never validate",
    ),
    Mutation(
        "sequence_field_not_compared",
        IBPC / "approval.py",
        (
            (
                "        for name in _IDENTITY_FIELDS:\n"
                "            actual = getattr(binding, name)",
                "        for name in _IDENTITY_FIELDS:\n"
                '            if name == "sequence_number":\n'
                "                continue\n"
                "            actual = getattr(binding, name)",
            ),
        ),
        "a binding from another turn sequence must never validate",
    ),
    Mutation(
        "single_use_disabled",
        IBPC / "approval.py",
        (
            (
                "            if binding.state is ApprovalState.CONSUMED:\n"
                '                raise ApprovalError("replay", "approval already consumed — execution is single-use")',
                "            if False:\n                pass",
            ),
        ),
        "a consumed approval must never be consumed again (replay refusal)",
    ),
    Mutation(
        "session_binding_not_checked",
        IBPC / "service.py",
        (
            (
                "        if binding.session_id != session.session_id:\n"
                "            raise ApprovalError(\n"
                '                "identity_mismatch_session_id", "binding belongs to another session"\n'
                "            )",
                "        if False:\n            pass",
            ),
        ),
        "cross-session use of an approval must be refused before any enqueue",
    ),
    Mutation(
        "turn_binding_not_checked",
        IBPC / "service.py",
        (
            (
                "        if binding.turn_id != turn.turn_id:\n"
                '            raise ApprovalError("identity_mismatch_turn_id", "binding belongs to another turn")',
                "        if False:\n            pass",
            ),
        ),
        "cross-turn use of an approval must be refused before any enqueue",
    ),
    Mutation(
        "wrong_principal_allowed",
        IBPC / "service.py",
        (
            (
                "        if binding.principal_id != principal_id or session.principal_id != principal_id:",
                "        if False:",
            ),
        ),
        "a different principal must never approve/consume someone else's binding",
    ),
    Mutation(
        "revision_not_checked",
        IBPC / "service.py",
        (
            (
                "        if binding.source_revision != revision:",
                "        if False:",
            ),
        ),
        "a stale-revision approval must never execute",
    ),
    Mutation(
        "asset_not_rehashed",
        IBPC / "service.py",
        (
            (
                "            actual_asset = sha256_file(ctx.source_path)",
                "            actual_asset = binding.source_asset_digest",
            ),
        ),
        "the real source asset must be re-hashed at execute time (tamper detection)",
    ),
    Mutation(
        "preview_digest_not_checked",
        IBPC / "service.py",
        (
            (
                "        if ctx.preview.preview_digest != binding.preview_digest:",
                "        if False:",
            ),
        ),
        "a tampered preview context must never execute",
    ),
    # -- Phase 1: canonical authority continuity --------------------------------
    Mutation(
        "authority_revocation_ignored_at_execute",
        IBPC / "service.py",
        (
            (
                "        try:\n"
                "            verified = self._resolve_authority(session.principal_id, session.project_id)\n"
                "        except CanonicalAuthorityError as exc:\n"
                '            raise ApprovalError("authority_unverified", f"{exc.reason}: {exc.detail}") from exc\n',
                "        verified = ctx.verified_identity  # mutation: trust the cached identity\n",
            ),
        ),
        "authority must be re-verified at execute time (revocation fails closed)",
    ),
    Mutation(
        "binding_ownership_not_checked",
        IBPC / "authority.py",
        (
            (
                "            self._directory.require_binding(binding_ref, principal)  # type: ignore[attr-defined]",
                "            pass  # mutation: binding ownership not checked",
            ),
        ),
        "a channel binding must belong to the acting principal",
    ),
    Mutation(
        "unconfigured_authority_allows",
        IBPC / "service.py",
        (
            (
                "        if authority is None:\n"
                "            raise CanonicalAuthorityError(\n"
                '                "authority_unconfigured",\n'
                '                "no trusted canonical authority is configured; refusing to execute",\n'
                "            )",
                "        if authority is None and False:  # mutation\n"
                "            raise CanonicalAuthorityError(\n"
                '                "authority_unconfigured",\n'
                '                "no trusted canonical authority is configured; refusing to execute",\n'
                "            )",
            ),
        ),
        "a service without a trusted authority must refuse, never run on a cached grant",
    ),
    # -- Phase 2: proof-completeness gate ---------------------------------------
    Mutation(
        "proof_gate_ignores_verdict",
        IBPC / "proof.py",
        (
            (
                "    if verdict is not CheckVerdict.PASS:\n"
                '        reasons.append(f"verdict_not_pass:{verdict.value}")',
                "    if False:  # mutation\n"
                '        reasons.append(f"verdict_not_pass:{verdict.value}")',
            ),
        ),
        "a non-PASS verdict must never be reported as verified proof",
    ),
    Mutation(
        "proof_gate_ignores_journal",
        IBPC / "proof.py",
        (
            (
                '    elif journal.get("chain_verified") is not True:\n'
                '        reasons.append("journal_chain_unverified")',
                '    elif False:  # mutation\n        reasons.append("journal_chain_unverified")',
            ),
        ),
        "an unverified provenance chain must withhold a proof-bearing capsule",
    ),
    Mutation(
        "proof_gate_ignores_attempt",
        IBPC / "proof.py",
        (
            (
                "    if not attempt_identity or attempt_identity.endswith(UNKNOWN_ATTEMPT_SUFFIX):\n"
                '        reasons.append("attempt_identity_missing")',
                '    if False:  # mutation\n        reasons.append("attempt_identity_missing")',
            ),
        ),
        "a missing/placeholder attempt identity must be a proof gap",
    ),
    Mutation(
        "proof_gate_ignores_passport_digest",
        IBPC / "proof.py",
        (
            (
                '        digest = passport.get("passport_digest")\n'
                '        if not isinstance(digest, str) or not digest.startswith("sha256:"):\n'
                '            reasons.append("passport_digest_missing")',
                "        pass  # mutation: passport digest not checked",
            ),
        ),
        "a passport without a digest must be a proof gap",
    ),
    Mutation(
        "proof_gate_outcome_always_verified",
        IBPC / "proof.py",
        (
            (
                "        return EXECUTED_VERIFIED if self.proof_complete else EXECUTED_UNVERIFIED",
                "        return EXECUTED_VERIFIED  # mutation",
            ),
        ),
        "an incomplete proof must never be reported as verified success",
    ),
)


def run_tests() -> int:
    """Run the security suites and return pytest's exit code (0 == GREEN).

    The raw return code is returned, never collapsed to a bool: the caller must
    be able to tell a *genuine* test failure (exit 1, a real kill) from a
    harness/infra failure (exit 2 interrupted, 3 internal, 4 usage, 5 no tests
    collected).  Only exit 1 proves a mutation was caught by an assertion; an
    infrastructure error must never be mistaken for a kill.
    """
    env = dict(os.environ)
    # Pin the in-tree package ahead of any editable install so the mutated file
    # is the one under test (an installed copy would silently false-green).
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--noconftest",
            "-p",
            "no:cacheprovider",
            *TESTS,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        shell=False,
    )
    return result.returncode


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    print("baseline ... ", end="", flush=True)
    if run_tests() != 0:
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    results: list[tuple[str, str, str, str, str]] = []  # name, file, invariant, result, evidence
    killed = 0
    blocked = 0
    for mutation in MUTATIONS:
        rel = mutation.path.relative_to(ROOT)
        original = mutation.path.read_text(encoding="utf-8")
        before_sha = _sha256(mutation.path)
        mutated = original
        applicable = True
        for old, new in mutation.edits:
            if old not in mutated:
                print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
                applicable = False
                break
            mutated = mutated.replace(old, new, 1)
        if not applicable:
            # A dead anchor is a harness-integrity failure, not a caught guard.
            results.append((mutation.name, str(rel), mutation.why, "ANCHOR_DRIFT", "n/a"))
            blocked += 1
            continue
        mutation.path.write_text(mutated, encoding="utf-8")
        try:
            code = run_tests()
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        # The source must be byte-identical after every mutation, even on crash.
        restored = _sha256(mutation.path) == before_sha
        if not restored:
            results.append(
                (mutation.name, str(rel), mutation.why, "RESTORE_FAILED", "sha mismatch")
            )
            blocked += 1
            continue
        if code == 1:
            killed += 1
            print(f"{mutation.name}: killed")
            results.append((mutation.name, str(rel), mutation.why, "KILLED", "pytest exit 1"))
        elif code == 0:
            print(f"{mutation.name}: SURVIVED — {mutation.why}")
            results.append((mutation.name, str(rel), mutation.why, "SURVIVED", "pytest exit 0"))
        else:
            # Exit 2/3/4/5 is an infrastructure/usage error, NOT a kill.
            print(f"{mutation.name}: HARNESS ERROR (pytest exit {code}) — not counted as a kill")
            results.append(
                (mutation.name, str(rel), mutation.why, "HARNESS_ERROR", f"pytest exit {code}")
            )
            blocked += 1

    print("restored baseline ... ", end="", flush=True)
    if run_tests() != 0:
        print("RED — restore failed")
        return 3
    print("GREEN")

    print()
    print("mutation evidence (name | file | invariant | result | evidence)")
    for name, rel, why, result, evidence in results:
        print(f"- {name} | {rel} | {why} | {result} | {evidence}")
    print()
    print(f"{killed}/{len(MUTATIONS)} killed, {blocked} blocked (anchor drift / harness error)")
    if blocked:
        return 4
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
