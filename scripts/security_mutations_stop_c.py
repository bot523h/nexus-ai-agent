#!/usr/bin/env python3
"""Mutation gate for STOP-C command actor and authorizer enforcement.

Run with ``python scripts/security_mutations_stop_c.py``. A green exit proves
that production-root grants, missing-actor/authorizer denials, and permission
checks are load-bearing.
"""

from __future__ import annotations

from security_mutation_support import Mutation, SourceEdit, run_mutation_suite

TESTS = (
    "tests/architecture/test_command_capability_boundary.py::test_every_production_command_bus_root_has_a_trusted_authorizer",
    "tests/architecture/test_command_capability_boundary.py::test_every_production_dispatch_envelope_has_a_service_identity",
    "tests/architecture/test_command_capability_boundary.py::test_reachable_operation_authority_matrix_matches_dispatch_and_registry",
    "tests/unit/test_authority_policy.py",
    "tests/unit/test_command_capability_contract.py::TestAuthorization::test_actor_claim_without_authorizer_is_refused",
    "tests/unit/test_command_capability_contract.py::TestAuthorization::test_authorizer_requires_an_actor_claim",
    "tests/unit/test_command_capability_contract.py::TestAuthorization::test_missing_permissions_are_denied",
)
BUS = "src/nexus_ai_agent/creative/studio/bus.py"
RENDER_ROOT = "src/nexus_ai_agent/creative/render_jobs.py"

MUTATIONS = (
    Mutation(
        "production_root_omits_authorizer",
        (SourceEdit(RENDER_ROOT, "        authorizer=authorizer,\n", ""),),
        (TESTS[0],),
        ("test_every_production_command_bus_root_has_a_trusted_authorizer",),
        "every reachable production bus root must bind a project-scoped trusted grant",
    ),
    Mutation(
        "missing_authorizer_no_longer_fails_closed",
        (SourceEdit(BUS, "        if self._authorizer is None:", "        if False:"),),
        (TESTS[4],),
        ("test_actor_claim_without_authorizer_is_refused",),
        "dispatch without a trusted authorizer must raise AuthorizationError",
    ),
    Mutation(
        "missing_actor_no_longer_fails_closed",
        (SourceEdit(BUS, "        if command.actor is None:", "        if False:"),),
        (TESTS[5],),
        ("test_authorizer_requires_an_actor_claim",),
        "dispatch without an actor claim must be denied before operation handling",
    ),
    Mutation(
        "operation_permission_check_removed",
        (
            SourceEdit(
                BUS,
                "        access.require_permissions(descriptor.required_permissions)\n",
                "        pass  # mutation: omit project permission enforcement\n",
            ),
        ),
        (TESTS[6],),
        ("test_missing_permissions_are_denied",),
        "service identity does not grant permissions absent from its project grant",
    ),
)


if __name__ == "__main__":
    raise SystemExit(run_mutation_suite("STOP-C", TESTS, MUTATIONS))
