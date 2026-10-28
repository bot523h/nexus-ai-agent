#!/usr/bin/env python3
"""Replay the A-06 API-gate security mutations as a fail-closed CI campaign.

The historical temporary driver was not committed.  The A–N identifiers and
intent below are therefore auditable reifications of the merged PR #226 table
and the live source/test anchors, not a claim that the deleted SourceEdit bodies
were recovered byte-for-byte.  Every edit is exact-one, behavior-changing,
targeted at one named test oracle, and restored/verified by the shared runner.

Run locally with::

    python scripts/api_gate_mutations.py --report ci-artifacts/api-gate-mutations.json
"""

from __future__ import annotations

import argparse
from pathlib import Path

from security_mutation_support import Mutation, SourceEdit, run_reported_mutation_suite

GUARD = "tests/integration/test_api_gate_inventory.py"
DASHBOARD = "src/nexus_ai_agent/api/dashboard.py"
API_APP = "src/nexus_ai_agent/api/app.py"
STUDIO = "src/nexus_ai_agent/api/studio.py"

EXPECTED_IDS = tuple("ABCDEFGHIJKLMN")
BASELINE_TESTS = ("tests/integration/test_api_gate_inventory.py",)


def _probe(
    mutation_id: str,
    name: str,
    path: str,
    old: str,
    new: str,
    test: str,
    oracle: str,
    rationale: str,
) -> Mutation:
    return Mutation(
        name=name,
        edits=(SourceEdit(path, old, new),),
        tests=(f"{GUARD}::{test}",),
        expected_failures=(oracle,),
        rationale=rationale,
        mutation_id=mutation_id,
    )


MUTATIONS = (
    _probe(
        "A",
        "dashboard_router_bearer_dependency_removed",
        "src/nexus_ai_agent/api/dashboard.py",
        "    dependencies=[Depends(require_dashboard_token)],",
        "    dependencies=[],",
        "test_every_gated_operation_fails_closed_without_security_configuration",
        "operations that are not fail-closed",
        "The dashboard router must retain its bearer-token dependency.",
    ),
    _probe(
        "B",
        "unconfigured_webhook_secret_check_removed",
        API_APP,
        "    if not secret or provided is None or not secrets.compare_digest(provided, secret):",
        "    if False:  # mutation: accept unconfigured webhook deliveries",
        "test_every_gated_operation_fails_closed_without_security_configuration",
        "operations that are not fail-closed",
        "An absent Telegram webhook secret must reject delivery, not accept it.",
    ),
    _probe(
        "C",
        "invalid_hmac_signature_check_removed",
        API_APP,
        "    if not hmac_mod.compare_digest(signature.strip().lower(), expected):\n"
        '        raise HTTPException(status_code=401, detail="invalid signature")',
        "    if False:  # mutation: accept invalid HMAC signatures\n"
        '        raise HTTPException(status_code=401, detail="invalid signature")',
        "test_the_legacy_creative_surface_opens_only_with_a_valid_signature",
        "invalid signature accepted with status",
        "A configured HMAC surface must reject an incorrect signature.",
    ),
    _probe(
        "D",
        "studio_project_allowlist_check_removed",
        STUDIO,
        "    if project_id not in _configured_projects():",
        "    if False:  # mutation: skip the Studio project allowlist",
        "test_the_studio_surface_enforces_project_scope_after_authentication",
        "project scope was bypassed",
        "A valid bearer token must not bypass the independent Studio project scope.",
    ),
    _probe(
        "E",
        "studio_bearer_authentication_removed",
        STUDIO,
        "    require_dashboard_token(authorization)",
        "    # mutation: skip Studio bearer authentication",
        "test_the_studio_surface_enforces_project_scope_after_authentication",
        "Studio route accepted missing bearer",
        "The Studio route must authenticate before applying project authorization.",
    ),
    _probe(
        "F",
        "hmac_freshness_window_removed",
        API_APP,
        "    if age > _HMAC_MAX_AGE_SECONDS:",
        "    if False:  # mutation: accept stale HMAC timestamps",
        "test_the_legacy_creative_surface_opens_only_with_a_valid_signature",
        "stale signature accepted with status",
        "A cryptographically correct signature outside the freshness window is invalid.",
    ),
    _probe(
        "G",
        "dashboard_accepts_any_nonempty_bearer_token",
        DASHBOARD,
        "    if not hmac.compare_digest(provided, expected):",
        "    if not provided:  # mutation: accept any non-empty token",
        "test_the_dashboard_surface_opens_with_the_configured_bearer_token",
        "accepted a wrong token",
        "The dashboard must compare the presented bearer token to its configured token.",
    ),
    _probe(
        "H",
        "mounted_subapplication_recursion_removed",
        GUARD,
        '        for attribute in ("routes", "app"):',
        "        for attribute in ():  # mutation: ignore mounted applications",
        "test_a_mounted_sub_application_cannot_hide_from_the_inventory",
        "mounted operation missing from inventory.mounted",
        "A mounted FastAPI sub-application must be included in route enumeration.",
    ),
    _probe(
        "I",
        "opaque_container_classification_check_removed",
        GUARD,
        "    assert not unclassified_containers, (",
        "    assert not [], (",
        "test_unexempted_opaque_container_requires_an_explicit_owner",
        "opaque-route classification accepted an unexempted container",
        "A non-enumerable mount must be reported or explicitly exempted with a reason.",
    ),
    _probe(
        "J",
        "mounted_operation_security_owner_check_removed",
        GUARD,
        "    assert not unowned_mounted, (",
        "    assert not [], (",
        "test_mounted_operations_are_required_to_have_a_named_security_owner",
        "unowned mounted operation passed the security-owner contract",
        "Every mounted operation must have a named authentication/authorization owner.",
    ),
    _probe(
        "K",
        "empty_path_root_mount_dropped",
        GUARD,
        '        elif getattr(route, "path", None) is not None:',
        '        elif getattr(route, "path", None):  # mutation: drop empty root mounts',
        "test_root_mount_empty_path_is_recorded_as_a_container",
        "root mount missing from inventory.containers",
        "Starlette stores a root Mount with an empty path; it must not be dropped.",
    ),
    _probe(
        "L",
        "container_opacity_inferred_from_path_prefix",
        GUARD,
        "        enumerated.update(registered.containers)\n",
        "        enumerated.update(\n"
        "            key for key in containers if registered.path.startswith(key[1])\n"
        "        )\n",
        "test_parent_operation_prefix_does_not_enumerate_a_static_mount",
        "a parent operation sharing the prefix incorrectly made the mount look enumerated",
        "A sibling path prefix does not prove child-container enumeration.",
    ),
    _probe(
        "M",
        "container_chain_identity_reduced_to_path_only",
        GUARD,
        "        child_chain = chain + ((id(route), effective),) if is_container else chain",
        "        child_chain = chain + ((effective,),) if is_container else chain",
        "test_nested_root_mount_chain_keeps_route_object_identity",
        "container chain lost the enclosing route identity",
        "Nested containers with the same effective path must retain distinct route identities.",
    ),
    _probe(
        "N",
        "container_table_identity_reduced_to_path_only",
        GUARD,
        "            containers[(id(route), path)] = (type(route).__name__, _container_key(path))",
        "            containers[path] = (type(route).__name__, _container_key(path))",
        "test_nested_root_mount_table_keeps_route_object_identity",
        "container table keys are not object-identity keyed",
        "The reported container table must not collapse different mounts sharing one path.",
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("ci-artifacts/api-gate-mutations.json"),
        help="path for the deterministic, commit-bound JSON report",
    )
    args = parser.parse_args(argv)
    return run_reported_mutation_suite(
        "api-gate-mutations",
        BASELINE_TESTS,
        MUTATIONS,
        EXPECTED_IDS,
        args.report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
