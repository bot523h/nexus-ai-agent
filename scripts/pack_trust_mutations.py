#!/usr/bin/env python3
"""Adversarial mutation harness for the pack trust plane.

Evidence rule: a guard that has never been attacked is not evidence.  This
script rewrites the shipped trust code with a specific weakening, re-runs the
trust suite, and requires the suite to turn RED.  Every source file is restored
afterwards (including on failure), and the final run must be GREEN again.

Usage::

    python scripts/pack_trust_mutations.py           # run every mutation
    python scripts/pack_trust_mutations.py --list

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
PACKS = ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"
TESTS = (
    "tests/unit/test_pack_trust_root.py",
    "tests/architecture/test_pack_trust_boundary.py",
)


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "unknown_publisher_becomes_trusted",
        PACKS / "trust.py",
        "TRUSTED_STATES: frozenset[TrustState] = frozenset({TrustState.VERIFIED})",
        "TRUSTED_STATES: frozenset[TrustState] = frozenset(\n    {TrustState.VERIFIED, TrustState.UNKNOWN_PUBLISHER}\n)",
        "an unknown publisher must never be trusted",
    ),
    Mutation(
        "revoked_key_still_verifies",
        PACKS / "trust.py",
        "    active = [key for key in keys if key.active]",
        "    active = list(keys)",
        "a revoked key must not verify a pack",
    ),
    Mutation(
        "signature_no_longer_covers_capabilities",
        PACKS / "trust.py",
        '    payload = manifest.model_dump(mode="json")\n    security = dict(payload.get("security", {}))',
        '    payload = manifest.model_dump(mode="json")\n    payload.pop("capabilities", None)\n    payload.pop("permissions", None)\n    security = dict(payload.get("security", {}))',
        "dropping a field from the canonical bytes lets a signed pack be widened",
    ),
    Mutation(
        "canonical_bytes_lose_their_scheme_tag",
        PACKS / "trust.py",
        '    return b"nexus.capability-pack.signature.v1\\n" + body.encode("ascii")',
        '    return body.encode("ascii")',
        "cross-scheme replay protection must stay in the signed bytes",
    ),
    Mutation(
        "missing_trust_root_passes",
        PACKS / "trust.py",
        "    if trust_root is None:\n        return TrustDecision(\n            TrustState.NO_TRUST_ROOT,",
        "    if trust_root is None:\n        return TrustDecision(\n            TrustState.VERIFIED,",
        "no authority must never mean 'trusted'",
    ),
    Mutation(
        "malformed_signature_accepted",
        PACKS / "trust.py",
        "    if len(hex_part) != SIGNATURE_BYTES * 2:\n        return None",
        "    if len(hex_part) > SIGNATURE_BYTES * 2:\n        return None",
        "a structurally wrong signature must not reach the verifier",
    ),
    Mutation(
        "malleable_signature_accepted",
        PACKS / "ed25519.py",
        '    if s >= _L:\n        raise Ed25519Error("signature S is not canonical (S >= L): malleable signature rejected")',
        "    if s >= _L:\n        s %= _L",
        "S >= L must be rejected (RFC 8032 canonical S)",
    ),
    Mutation(
        "small_order_key_accepted",
        PACKS / "ed25519.py",
        '    if _has_small_order(a_point):\n        raise Ed25519Error("public key has small order and is rejected")',
        "    if False:\n        pass",
        "a small-order public key verifies attacker-chosen signatures",
    ),
    Mutation(
        "verification_result_ignored",
        PACKS / "ed25519.py",
        '    if not _equal(_scalar_mult(_B, s), _point_add(r_point, _scalar_mult(a_point, k))):\n        raise Ed25519Error("signature does not verify")',
        "    return",
        "the verification equation must actually be checked",
    ),
    Mutation(
        "activation_gate_removed",
        PACKS / "registry.py",
        '        if pack.anchor == "external" and pack.report.signature_state not in (\n            TRUSTED_SIGNATURE_STATES\n        ):',
        "        if False:",
        "an untrusted external pack must never become executable",
    ),
    Mutation(
        "placeholder_reported_as_signed",
        PACKS / "manifest.py",
        "        return self.signature.startswith(PLACEHOLDER_SIGNATURE_PREFIXES)",
        "        return False",
        "a placeholder must not be reported as a real signature state",
    ),
    Mutation(
        "broken_trust_root_silently_ignored",
        PACKS / "verify.py",
        "        except TrustRootError as exc:  # misconfiguration must be visible\n            root, root_error = None, str(exc)",
        "        except TrustRootError:\n            root, root_error = None, None",
        "a misconfigured trust root must be reported, not swallowed",
    ),
)


def run_tests() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", *TESTS],
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
        if mutation.old not in original:
            print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
            survivors.append(mutation.name)
            continue
        mutation.path.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
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
