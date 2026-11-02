"""Phase 1 — canonical authority continuity for the IBPC slice.

The service must never treat a session's ``principal_id``/``project_id`` as a
grant and must never mint a binding id: it resolves the acting identity through
an injected :class:`CanonicalAuthorityPort` (here, a real
``SQLApplicationDirectory`` behind ``DirectoryAuthority``) and fails closed with
a typed error when the identity cannot be trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from nexus_ai_agent.adapters.sql_application_directory import (
    SQLApplicationDirectory,
    channel_binding_id,
)
from nexus_ai_agent.application.principal import ChannelBinding
from nexus_ai_agent.nagar.ibpc.authority import (
    CanonicalAuthorityError,
    DirectoryAuthority,
    PrincipalFacts,
)

PROJECT = "ibpc-authority-project"


@pytest.fixture()
def directory(tmp_path: Path) -> SQLApplicationDirectory:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'directory.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    return SQLApplicationDirectory(engine)


def test_resolves_verified_principal_project_and_binding(
    directory: SQLApplicationDirectory,
) -> None:
    binding = ChannelBinding("cli", "owner")
    principal = directory.resolve_or_create(binding)
    directory.ensure_project(PROJECT, principal)
    facts = DirectoryAuthority(directory).resolve_principal(
        principal_id=str(principal),
        project_id=PROJECT,
        binding_ref=channel_binding_id(binding),
    )
    assert isinstance(facts, PrincipalFacts)
    assert facts.principal_id == str(principal)
    assert facts.project_id == PROJECT
    assert facts.channel_binding_id == channel_binding_id(binding)


def test_unknown_principal_is_a_typed_refusal(directory: SQLApplicationDirectory) -> None:
    binding = ChannelBinding("cli", "owner")
    owner = directory.resolve_or_create(binding)
    directory.ensure_project(PROJECT, owner)
    with pytest.raises(CanonicalAuthorityError) as exc:
        DirectoryAuthority(directory).resolve_principal(
            principal_id="not-a-principal",
            project_id=PROJECT,
            binding_ref=channel_binding_id(binding),
        )
    assert exc.value.reason == "malformed_principal"


def test_non_member_is_refused(directory: SQLApplicationDirectory) -> None:
    owner = directory.resolve_or_create(ChannelBinding("cli", "owner"))
    outsider = directory.resolve_or_create(ChannelBinding("cli", "outsider"))
    directory.ensure_project(PROJECT, owner)
    with pytest.raises(CanonicalAuthorityError) as exc:
        DirectoryAuthority(directory).resolve_principal(
            principal_id=str(outsider),
            project_id=PROJECT,
            binding_ref=channel_binding_id(ChannelBinding("cli", "outsider")),
        )
    assert exc.value.reason == "authorization_denied"


def test_binding_of_another_principal_is_refused(directory: SQLApplicationDirectory) -> None:
    owner = directory.resolve_or_create(ChannelBinding("cli", "owner"))
    other = directory.resolve_or_create(ChannelBinding("cli", "other"))
    directory.ensure_project(PROJECT, owner)
    # owner is a member, but presents *other*'s binding reference
    with pytest.raises(CanonicalAuthorityError) as exc:
        DirectoryAuthority(directory).resolve_principal(
            principal_id=str(owner),
            project_id=PROJECT,
            binding_ref=channel_binding_id(ChannelBinding("cli", "other")),
        )
    assert exc.value.reason == "authorization_denied"
    assert other is not None


def test_empty_binding_is_refused(directory: SQLApplicationDirectory) -> None:
    owner = directory.resolve_or_create(ChannelBinding("cli", "owner"))
    directory.ensure_project(PROJECT, owner)
    with pytest.raises(CanonicalAuthorityError) as exc:
        DirectoryAuthority(directory).resolve_principal(
            principal_id=str(owner), project_id=PROJECT, binding_ref=""
        )
    assert exc.value.reason == "missing_binding"


def test_authority_port_is_runtime_checkable(directory: SQLApplicationDirectory) -> None:
    from nexus_ai_agent.nagar.ibpc.authority import CanonicalAuthorityPort

    assert isinstance(DirectoryAuthority(directory), CanonicalAuthorityPort)
