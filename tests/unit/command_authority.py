"""Shared explicit command identity and project grant for pack unit tests."""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity

TEST_SERVICE_ACTOR = ActorIdentity(kind="service", actor_id="nexus.unit-test")
TEST_PROJECT_PERMISSIONS = frozenset({"project:read", "project:write"})


def make_test_authorizer(
    project: Any, *, permissions: frozenset[str] = TEST_PROJECT_PERMISSIONS
) -> ProjectAccess:
    """Create a fixed project grant; never derive it from command claims."""
    project_id = project if isinstance(project, str) else project.project_id
    return ProjectAccess(
        actor=TEST_SERVICE_ACTOR,
        project_id=project_id,
        permissions=permissions,
    )
