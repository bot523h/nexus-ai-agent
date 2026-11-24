"""Concrete durable ingress composition for the state-only operation subset.

External-effect registrations are refused until an operation-specific destination
adapter and verifier are composed. A boolean durable_effects declaration is not
proof. This factory deliberately does not turn arbitrary handlers into trusted
filesystem writers. Credentials are provisioned server-side, separate from the
IBPC key. Deploy behind TLS and protect credential configuration and disk roots.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
from nexus_ai_agent.adapters.ibpc_plan_approval import IBPCPlanApprovalVerifier
from nexus_ai_agent.application.principal import PrincipalId
from nexus_ai_agent.creative.studio.authorization import ProjectAccess, ProjectAuthorizer
from nexus_ai_agent.creative.studio.bus import compose_durable_command_bus
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry, build_wave1_registry
from nexus_ai_agent.creative.studio.durable_dedupe import (
    ApprovalBinding,
    ApprovalVerifier,
    PlanApprovalRequirement,
)
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AuthorizationError,
    CommandResult,
    Project,
    TypedCommand,
)
from nexus_ai_agent.creative.studio.runtime import DurableStudioExecutionRuntime
from nexus_ai_agent.nagar.ibpc.approval_mac import ApprovalEnvelope, StaticKeyRing, issue_envelope


class ProvisionedPrincipalAuthority:
    """Separate bearer authentication and IBPC signing authorities.

    issue is called only after the facade checks project:approve. A valid MAC
    alone is not proof of a human decision; invocation of the authenticated
    approval endpoint is the explicit decision recorded by the application.
    """

    def __init__(self, credentials: Mapping[str, PrincipalId], secret: bytes) -> None:
        if not credentials or any(len(token) < 32 for token in credentials):
            raise ValueError("provision distinct bearer credentials with at least 32 characters")
        self._credentials = tuple(
            (hashlib.sha256(token.encode()).digest(), principal)
            for token, principal in credentials.items()
        )
        self._keys = StaticKeyRing({"w4": secret}, active_key_id="w4")

    def authenticate(self, credential: str) -> ActorIdentity:
        if not isinstance(credential, str) or len(credential) > 4096:
            raise AuthorizationError("invalid credential")
        digest = hashlib.sha256(credential.encode()).digest()
        for expected, principal in self._credentials:
            if hmac.compare_digest(expected, digest):
                return ActorIdentity(kind="user", actor_id=str(principal))
        raise AuthorizationError("invalid credential")

    def issue(self, requirements: tuple[PlanApprovalRequirement, ...]) -> str:
        issued = datetime.now(timezone.utc)
        expires = issued + timedelta(seconds=60)
        grants = []
        for requirement in requirements:
            binding = ApprovalBinding(
                **asdict(requirement),
                approval_id=secrets.token_hex(16),
                nonce=secrets.token_hex(32),
                issued_at=issued.timestamp(),
                expires_at=expires.timestamp(),
            )
            envelope = issue_envelope(
                self._keys,
                approval_id=binding.approval_id,
                payload=asdict(binding),
                issued_at=issued.isoformat(),
                expires_at=expires.isoformat(),
            )
            grants.append({"binding": asdict(binding), "envelope": asdict(envelope)})
        return json.dumps(grants, sort_keys=True, separators=(",", ":"))

    def verify_token(self, token: str) -> tuple[tuple[ApprovalBinding, ...], ApprovalVerifier]:
        try:
            if not isinstance(token, str) or len(token.encode()) > 1_048_576:
                raise ValueError("token exceeds bounds")
            grants = json.loads(token)
            if not isinstance(grants, list) or not 1 <= len(grants) <= 256:
                raise ValueError("invalid grant count")
            bindings = tuple(ApprovalBinding(**grant["binding"]) for grant in grants)
            envelopes = {
                grant["binding"]["approval_id"]: ApprovalEnvelope(**grant["envelope"])
                for grant in grants
            }
            verifier = IBPCPlanApprovalVerifier(self._keys, envelopes)
            if len(envelopes) != len(bindings) or not all(
                verifier.verify(binding) for binding in bindings
            ):
                raise ValueError("invalid approval")
            return bindings, verifier
        except (TypeError, ValueError, KeyError) as exc:
            raise AuthorizationError("invalid approval token") from exc


class ProvisionedProjectAuthorizer:
    def __init__(self, grants: Mapping[str, ProjectAccess]) -> None:
        self._grants = dict(grants)

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        access = self._grants.get(actor.actor_id)
        if access is None:
            raise AuthorizationError("principal has no project access")
        return access.authorize(actor, project_id)


def build_production_durable_command_bus(
    db_path: Path,
    destination_root: Path,
    *,
    approval_secret: bytes | str,
    project: Project,
    credentials: Mapping[str, PrincipalId],
    authorizer: ProjectAuthorizer,
    verifiers: Mapping[str, Callable[[Project, TypedCommand, CommandResult, str], bool]],
    registry: CapabilityRegistry | None = None,
    operation_truth_path: Path | None = None,
) -> DurableStudioExecutionRuntime:
    """Build mandatory SQLite-backed authenticated ingress, with no legacy path.

    Project and authentication configuration are mandatory: db_path and a MAC
    key alone cannot identify a human or authorize project access.
    """
    if not destination_root.is_dir():
        raise ValueError("destination root must be provisioned")
    secret = approval_secret.encode() if isinstance(approval_secret, str) else approval_secret
    registry = registry if registry is not None else build_wave1_registry()
    for operation in registry.list_operations():
        if registry.get_spec(operation).external_effects:
            raise ValueError("external operations require an operation-specific fenced composition")
    truth_path = (
        operation_truth_path or Path(__file__).resolve().parents[3] / "OPERATION_TRUTH.json"
    )
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    if truth.get("schema") != "nagar.operation_truth.v1":
        raise ValueError("unsupported operation truth schema")
    rows = truth["operations"]
    contracts = {row["operation_id"]: row["evidence"] for row in rows}
    if len(contracts) != len(rows):
        raise ValueError("duplicate operation truth identity")
    registry.verify_declared_contracts(contracts)
    authority = ProvisionedPrincipalAuthority(credentials, secret)
    store = DurablePlanDedupeStore(db_path)
    bus = compose_durable_command_bus(
        project,
        registry=registry,
        authorizer=authorizer,
        store=store,
        verifiers=verifiers,
    )
    return DurableStudioExecutionRuntime(bus, store, authority, authorizer)
