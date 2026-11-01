"""Durable principal/channel/project authorization with real SQLite races."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from nexus_ai_agent.adapters.sql_application_directory import (
    SQLApplicationDirectory,
    channel_binding_id,
)
from nexus_ai_agent.application.principal import (
    BindingConflictError,
    ChannelBinding,
    PrincipalId,
)
from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError
from nexus_ai_agent.storage.models import (
    AppChannelBinding,
    AppPrincipal,
    AppProject,
    AppProjectMembership,
)


@pytest.fixture()
def engine(tmp_path: object) -> Iterator[object]:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    instance = create_engine(
        f"sqlite:///{tmp_path / 'directory.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(instance)
    yield instance
    instance.dispose()


def test_first_contact_and_membership_survive_directory_reopen(engine: object) -> None:
    directory = SQLApplicationDirectory(engine)
    binding = ChannelBinding("cli", "operator-a")
    principal = directory.resolve_or_create(binding)
    assert directory.resolve_or_create(binding) == principal
    access = directory.ensure_project("project-a", principal)
    directory.bind(principal, ChannelBinding("web", "device-a"))

    reopened = SQLApplicationDirectory(engine)
    assert reopened.resolve(binding) == principal
    assert reopened.require_binding(channel_binding_id(binding), principal) == binding
    assert reopened.authorize(access.actor, "project-a") == access
    assert reopened.bindings_of(principal) == frozenset(
        {binding, ChannelBinding("web", "device-a")}
    )


def test_binding_is_never_silently_moved(engine: object) -> None:
    directory = SQLApplicationDirectory(engine)
    binding = ChannelBinding("telegram", "42")
    owner = directory.resolve_or_create(binding)
    other = directory.resolve_or_create(ChannelBinding("cli", "other"))
    with pytest.raises(BindingConflictError):
        directory.bind(other, binding)
    assert directory.resolve(binding) == owner
    with pytest.raises(AuthorizationError):
        directory.require_binding(channel_binding_id(binding), other)


def test_concurrent_first_contact_mints_exactly_one_principal(engine: object) -> None:
    binding = ChannelBinding("cli", "same-operator")

    def contact() -> str:
        return str(SQLApplicationDirectory(engine).resolve_or_create(binding))

    with ThreadPoolExecutor(max_workers=12) as pool:
        seen = list(pool.map(lambda _: contact(), range(24)))
    assert len(set(seen)) == 1
    with Session(engine) as session:  # type: ignore[arg-type]
        assert len(session.exec(select(AppPrincipal)).all()) == 1
        assert len(session.exec(select(AppChannelBinding)).all()) == 1


def test_concurrent_project_first_use_has_one_membership_without_duplicates(
    engine: object,
) -> None:
    directory = SQLApplicationDirectory(engine)
    principal = directory.resolve_or_create(ChannelBinding("cli", "project-owner"))

    def ensure() -> tuple[str, str]:
        access = SQLApplicationDirectory(engine).ensure_project("shared-project", principal)
        return access.actor.actor_id, access.project_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        seen = list(pool.map(lambda _: ensure(), range(16)))
    assert set(seen) == {(str(principal), "shared-project")}
    with Session(engine) as session:  # type: ignore[arg-type]
        assert len(session.exec(select(AppProject)).all()) == 1
        assert len(session.exec(select(AppProjectMembership)).all()) == 1


def test_membership_and_permissions_fail_closed(engine: object) -> None:
    directory = SQLApplicationDirectory(engine)
    principal = directory.resolve_or_create(ChannelBinding("cli", "reader"))
    directory.ensure_project("read-only", principal, permissions=frozenset({"project:read"}))
    with pytest.raises(AuthorizationError, match="permissions"):
        directory.ensure_project("read-only", principal)

    stranger = PrincipalId.new()
    with pytest.raises(AuthorizationError):
        directory.authorize(ActorIdentity(kind="user", actor_id=str(stranger)), "read-only")
    with pytest.raises(AuthorizationError):
        directory.authorize(ActorIdentity(kind="service", actor_id=str(principal)), "read-only")
    with pytest.raises(AuthorizationError):
        directory.authorize(ActorIdentity(kind="user", actor_id="telegram:42"), "read-only")


def test_malformed_stored_permissions_are_not_authority(engine: object) -> None:
    directory = SQLApplicationDirectory(engine)
    principal = directory.resolve_or_create(ChannelBinding("cli", "damaged"))
    access = directory.ensure_project("project-damaged", principal)
    with Session(engine) as session:  # type: ignore[arg-type]
        row = session.get(AppProjectMembership, ("project-damaged", str(principal)))
        assert row is not None
        row.permissions_json = "{not-json"
        session.add(row)
        session.commit()
    with pytest.raises(AuthorizationError, match="malformed"):
        directory.authorize(access.actor, "project-damaged")
