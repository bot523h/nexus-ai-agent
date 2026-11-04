#!/usr/bin/env python3
"""Adversarial mutation harness for the Backblaze B2 maintenance blob tier.

Evidence rule: an invariant that has never been attacked is not evidence. Each
mutation below rewrites exactly one shipped guard in the B2 provider (or the
blob-tier selection factory), re-runs the focused suites, and requires them to
turn RED. Every file is restored byte-for-byte afterwards -- including on
failure -- and the final run must be GREEN again.

The guards attacked:

* the endpoint must be ``https`` (a downgrade would leak the SigV4 request);
* the endpoint must not embed credentials;
* the endpoint host must be a B2 host (credential-exfiltration / SSRF boundary);
* the required fields must be present (fail closed before any network call);
* the endpoint host and region must agree;
* the bucket name must be a valid B2 name;
* every remote key must stay inside the ``backups/db/`` namespace;
* list pagination must fail closed on a truncated page without a token;
* the boto3 client must relax the default checksum headers (B2 compatibility);
* the tier factory must prefer a complete B2 tier;
* a partial B2 configuration must be refused, never silently ignored;
* a partial B2 may only fall back to a *complete* R2 tier;
* a partial remote delete must raise, never report fake success.

Usage::

    python scripts/b2_blob_tier_mutations.py          # run every mutation
    python scripts/b2_blob_tier_mutations.py --list

Exit code 0 means "N/N mutants killed".
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
B2 = ROOT / "src" / "nexus_ai_agent" / "storage" / "providers" / "b2.py"
BACKUP = ROOT / "src" / "nexus_ai_agent" / "maintenance" / "backup.py"
TESTS = (
    "tests/unit/test_b2_provider.py",
    "tests/unit/test_housekeeping_contract.py",
    "tests/unit/test_maintenance_backup.py",
    "tests/unit/test_maintenance_workflow_contract.py",
)
TEST_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        name="endpoint_requires_https",
        path=B2,
        old='    if parts.scheme != "https":',
        new="    if False:",
        why="a plain-HTTP endpoint must be refused (the signed request would leak)",
    ),
    Mutation(
        name="endpoint_rejects_embedded_credentials",
        path=B2,
        old="    if parts.username or parts.password:",
        new="    if False:",
        why="an endpoint with embedded credentials must be refused",
    ),
    Mutation(
        name="endpoint_host_allowlist",
        path=B2,
        old="    if not host.endswith(B2_HOST_SUFFIX):",
        new="    if False:",
        why="a non-B2 endpoint host must be refused (credential exfiltration / SSRF)",
    ),
    Mutation(
        name="required_fields_present",
        path=B2,
        old="        if not (self._key_id and self._application_key and self._bucket):",
        new="        if False:",
        why="a missing credential/bucket must fail closed before any network call",
    ),
    Mutation(
        name="endpoint_region_consistency",
        path=B2,
        old="        if encoded is not None and encoded != self._region.strip().lower():",
        new="        if False:",
        why="an endpoint that disagrees with the configured region must be refused",
    ),
    Mutation(
        name="bucket_name_validation",
        path=B2,
        old="        if not _B2_BUCKET_RE.match(bucket):",
        new="        if False:",
        why="a syntactically invalid bucket name must be refused at the boundary",
    ),
    Mutation(
        name="key_prefix_confinement",
        path=B2,
        old='        if not remote_key.startswith(BACKUP_PREFIX) or ".." in remote_key.split("/"):',
        new="        if False:",
        why="a key outside backups/db/ must never be read, written or deleted",
    ),
    Mutation(
        name="list_pagination_fail_closed",
        path=B2,
        old='                if not response.get("IsTruncated"):',
        new="                if True:",
        why="a truncated page without a continuation token must fail closed",
    ),
    Mutation(
        name="checksum_relaxation",
        path=B2,
        old='                    request_checksum_calculation="when_required",',
        new='                    request_checksum_calculation="when_supported",',
        why="boto3 must relax the default checksum headers or B2 rejects PutObject",
    ),
    Mutation(
        name="delete_partial_error",
        path=B2,
        old="                if errors:",
        new="                if False:",
        why="a partial remote delete must raise, never report fake success",
    ),
    Mutation(
        name="tier_selection_prefers_b2",
        path=BACKUP,
        old="    if b2.is_configured():",
        new="    if False:",
        why="a complete B2 tier must be selected over the R2 fallback",
    ),
    Mutation(
        name="partial_b2_is_not_silent",
        path=BACKUP,
        old="    if b2_present:",
        new="    if False:",
        why="a partial B2 configuration must be refused, not silently ignored",
    ),
    Mutation(
        name="partial_b2_falls_back_only_to_a_complete_r2",
        path=BACKUP,
        old="        if not r2.is_configured():",
        new="        if False:",
        why="a partial B2 must only be excused when a complete R2 tier exists",
    ),
)


def run_tests() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *TESTS],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"(test run exceeded {TEST_TIMEOUT_SECONDS}s) ", end="", flush=True)
        return False
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
        try:
            mutation.path.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8"
            )
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
    if survivors:
        print("survivors: " + ", ".join(survivors))
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
