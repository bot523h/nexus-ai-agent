from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from nexus_ai_agent.connect.contracts import (
    ConnectionContract,
    ConnectionState,
    CredentialDescriptor,
    CredentialState,
    ScopeGrant,
    authorize_read,
)
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity

NOW = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)


def connection(*, state=ConnectionState.ACTIVE, credential_state=CredentialState.ACTIVE):
    return ConnectionContract(
        connection_id="conn_test",
        principal=ActorIdentity(kind="agent", actor_id="external-test"),
        grant=ScopeGrant(project_ids=("project_a",), scopes=frozenset({"project.read"})),
        credential=CredentialDescriptor(
            credential_id="cred_test",
            state=credential_state,
            expires_at=NOW + timedelta(hours=1),
        ),
        state=state,
        expires_at=NOW + timedelta(hours=1),
        created_at=NOW,
    )


def access(*, actor=None, project_id="project_a", permissions=("project.read",)):
    return ProjectAccess(
        actor=actor or ActorIdentity(kind="agent", actor_id="external-test"),
        project_id=project_id,
        permissions=frozenset(permissions),
    )


def test_valid_read_is_scoped_to_project_and_scope():
    authorize_read(
        connection(), access=access(), project_id="project_a", scope="project.read", now=NOW
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": ConnectionState.REVOKED},
        {"credential_state": CredentialState.REVOKED},
    ],
)
def test_revoked_connection_or_credential_fails_closed(kwargs):
    with pytest.raises(PermissionError):
        authorize_read(
            connection(**kwargs),
            access=access(),
            project_id="project_a",
            scope="project.read",
            now=NOW,
        )


def test_expired_connection_fails_closed():
    item = connection()
    with pytest.raises(PermissionError):
        authorize_read(
            item,
            access=access(),
            project_id="project_a",
            scope="project.read",
            now=NOW + timedelta(hours=2),
        )


def test_cross_project_and_scope_escalation_fail_closed():
    with pytest.raises(PermissionError):
        authorize_read(
            connection(), access=access(), project_id="project_b", scope="project.read", now=NOW
        )
    with pytest.raises(PermissionError):
        authorize_read(
            connection(), access=access(), project_id="project_a", scope="project.write", now=NOW
        )


def test_forged_actor_cannot_use_another_canonical_grant():
    with pytest.raises(PermissionError):
        authorize_read(
            connection(),
            access=access(actor=ActorIdentity(kind="agent", actor_id="different-agent")),
            project_id="project_a",
            scope="project.read",
            now=NOW,
        )


def test_naive_request_time_fails_closed():
    with pytest.raises(PermissionError):
        authorize_read(
            connection(),
            access=access(),
            project_id="project_a",
            scope="project.read",
            now=datetime(2026, 11, 11, 13, 0),
        )


def test_direct_time_methods_fail_closed_for_naive_now():
    with pytest.raises(PermissionError):
        connection().credential.is_usable(datetime(2026, 11, 11, 13, 0))
    with pytest.raises(PermissionError):
        connection().is_usable(datetime(2026, 11, 11, 13, 0))


def test_raw_secret_is_not_a_contract_field():
    with pytest.raises(ValidationError):
        CredentialDescriptor(
            credential_id="cred_test",
            state=CredentialState.ACTIVE,
            raw_secret="must-not-be-accepted",
        )


@pytest.mark.parametrize("field", ["credential_id", "connection_id"])
def test_identifiers_reject_whitespace(field):
    if field == "credential_id":
        with pytest.raises(ValidationError):
            CredentialDescriptor(credential_id=" ", state=CredentialState.ACTIVE)
    else:
        with pytest.raises(ValidationError):
            ConnectionContract(
                connection_id=" ",
                principal=ActorIdentity(kind="agent", actor_id="external-test"),
                grant=ScopeGrant(project_ids=("project_a",), scopes=frozenset({"project.read"})),
                credential=CredentialDescriptor(
                    credential_id="cred_test", state=CredentialState.ACTIVE
                ),
                state=ConnectionState.ACTIVE,
                created_at=NOW,
            )


def test_scope_and_rotation_identifiers_reject_control_or_blank_values():
    with pytest.raises(ValidationError):
        ScopeGrant(project_ids=("project\n",), scopes=frozenset({"project.read"}))
    with pytest.raises(ValidationError):
        ScopeGrant(project_ids=("project\x7f",), scopes=frozenset({"project.read"}))
    with pytest.raises(ValidationError):
        ScopeGrant(project_ids=("project_a",), scopes=frozenset({" project.read"}))
    with pytest.raises(ValidationError):
        CredentialDescriptor(
            credential_id="cred_test",
            state=CredentialState.ACTIVE,
            rotated_from=" ",
        )
    with pytest.raises(ValidationError):
        CredentialDescriptor(
            credential_id="cred_test",
            state=CredentialState.ACTIVE,
            rotated_from="旧-key",
        )
