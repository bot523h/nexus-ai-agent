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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IBPC = ROOT / "src" / "nexus_ai_agent" / "nagar" / "ibpc"
TESTS = (
    "tests/unit/test_ibpc_security.py",
    "tests/unit/test_ibpc_approval.py",
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
)


def run_tests() -> bool:
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
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    print("baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    killed = 0
    survivors: list[str] = []
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        mutated = original
        applicable = True
        for old, new in mutation.edits:
            if old not in mutated:
                print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
                applicable = False
                break
            mutated = mutated.replace(old, new, 1)
        if not applicable:
            survivors.append(mutation.name)
            continue
        mutation.path.write_text(mutated, encoding="utf-8")
        try:
            green = run_tests()
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        if green:
            print(f"{mutation.name}: SURVIVED — {mutation.why}")
            survivors.append(mutation.name)
        else:
            killed += 1
            print(f"{mutation.name}: killed")

    print("restored baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — restore failed")
        return 3
    print("GREEN")
    print(f"{killed}/{len(MUTATIONS)} killed")
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
