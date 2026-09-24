"""Test-only trusted composition for the hardened Nagar envelope.

Production binds its own service actor; tests never get an implicit bypass
from the bus. Keep old pack tests focused on their operation's behaviour while
exercising exactly the same authorization and schema gates as real callers.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    CommandProvenance,
    Project,
    TargetRef,
    TypedCommand,
)

TEST_ACTOR = ActorIdentity(kind="service", actor_id="nagar.contract-test")
TEST_PROVENANCE = CommandProvenance(source="service", source_id="nagar.contract-test")


def authorized_bus(project: Project, registry: CapabilityRegistry | None = None) -> CommandBus:
    return CommandBus(
        project,
        registry=registry,
        authorizer=ProjectAccess(
            actor=TEST_ACTOR,
            project_id=project.project_id,
            permissions=frozenset({"project:read", "project:write"}),
        ),
    )


def command_for(bus_or_project: CommandBus | str, **fields: Any) -> TypedCommand:
    project_id = (
        bus_or_project.project.project_id
        if isinstance(bus_or_project, CommandBus)
        else bus_or_project
    )
    return TypedCommand(
        actor=TEST_ACTOR,
        target=TargetRef(project_id=project_id),
        provenance=TEST_PROVENANCE,
        **fields,
    )
