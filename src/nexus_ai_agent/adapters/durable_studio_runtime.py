"""Concrete durable ingress with an explicit fenced UTF-8 artifact operation.

Arbitrary external handlers remain refused: a durable_effects boolean is not
proof of destination participation. The artifact adapter owns publication and
independent byte-level postcondition checks. Credentials are provisioned
server-side, separate from the IBPC key. Deploy behind TLS and protect roots.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path as StoragePath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
from nexus_ai_agent.adapters.fenced_filesystem_destination import (
    FencedFilesystemEffectDestination as ArtifactDestination,
)
from nexus_ai_agent.adapters.fenced_filesystem_destination import (
    FilesystemObservationVerifier as ArtifactReader,
)
from nexus_ai_agent.adapters.ibpc_plan_approval import IBPCPlanApprovalVerifier
from nexus_ai_agent.application.principal import PrincipalId
from nexus_ai_agent.creative.studio.authorization import ProjectAccess, ProjectAuthorizer
from nexus_ai_agent.creative.studio.bus import compose_durable_command_bus
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.durable_dedupe import (
    ApprovalBinding,
    ApprovalVerifier,
    PlanApprovalRequirement,
    RecoveryRoute,
)
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AuthorizationError,
    CommandResult,
    PermissionLevel,
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

    def __init__(
        self,
        credentials: Mapping[str, PrincipalId],
        secret: bytes,
        authorizer: ProjectAuthorizer | None = None,
    ) -> None:
        if not credentials or any(len(token) < 32 for token in credentials):
            raise ValueError("provision distinct bearer credentials with at least 32 characters")
        self._credentials = tuple(
            (hashlib.sha256(token.encode()).digest(), principal)
            for token, principal in credentials.items()
        )
        self._authorizer = authorizer
        self._repair_scope: ContextVar[tuple[str, str] | None] = ContextVar(
            "w4_repair_scope", default=None
        )
        self._keys = StaticKeyRing({"w4": secret}, active_key_id="w4")

    @contextmanager
    def repair_session(self, credential: str, project_id: str) -> Iterator[ActorIdentity]:
        actor = self.authenticate(credential)
        if self._authorizer is None:
            raise AuthorizationError("repair authority is not composed")
        access = self._authorizer.authorize(actor, project_id)
        if access.actor != actor or access.project_id != project_id:
            raise AuthorizationError("mismatched repair access")
        access.require_permissions(("project:repair",))
        token = self._repair_scope.set((project_id, actor.actor_id))
        try:
            yield actor
        finally:
            self._repair_scope.reset(token)

    def authorize_repair(
        self, project_id: str, operator_id: str, status: str, attempt: int
    ) -> bool:
        return self._repair_scope.get() == (project_id, operator_id)

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


class ArtifactPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=1_048_576)


def _uncomposed_artifact(project: Project, context: OperationContext) -> OperationOutcome:
    raise RuntimeError("filesystem artifact destination is not composed")


def register_filesystem_artifact_operation(registry: CapabilityRegistry) -> None:
    """Register the explicit UTF-8 artifact contract, not arbitrary file writes."""
    registry.register_operation(
        "artifact",
        "publication",
        OperationSpec(
            "artifact.publish",
            "Publish immutable UTF-8 artifact to fenced storage",
            PermissionLevel.REVERSIBLE,
            ArtifactPublishInput,
            _uncomposed_artifact,
            external_effects=True,
            durable_effects=True,
            precondition_contract="w4.artifact.utf8-fenced.v1",
            postcondition_contract="w4.artifact.digest-present.v1",
        ),
    )


def _artifact_handler(
    destination: ArtifactDestination,
) -> Callable[[Project, OperationContext], OperationOutcome]:
    def publish(project: Project, context: OperationContext) -> OperationOutcome:
        if (
            context.effect_key is None
            or context.fencing_epoch is None
            or context.durable_heartbeat is None
        ):
            raise RuntimeError("artifact publication requires a live durable fence")
        context.durable_heartbeat()
        payload = ArtifactPublishInput.model_validate(context.input_data).content.encode("utf-8")
        if len(payload) > 1_048_576:
            raise ValueError("artifact exceeds byte limit")
        observation = destination.apply(
            effect_key=context.effect_key, fencing_epoch=context.fencing_epoch, payload=payload
        )
        return OperationOutcome(
            project,
            context.history,
            {
                "artifact": asdict(observation),
                "payload_digest": hashlib.sha256(payload).hexdigest(),
            },
        )

    return publish


def _artifact_verifier(
    reader: ArtifactReader,
) -> Callable[[Project, TypedCommand, CommandResult, str], bool]:
    def verify(project: Project, command: TypedCommand, result: CommandResult, key: str) -> bool:
        from nexus_ai_agent.creative.studio.durable_dedupe import ExternalEffectObservation

        try:
            observation = ExternalEffectObservation(**result.output["artifact"])
            content = ArtifactPublishInput.model_validate(command.input).content.encode("utf-8")
            return (
                observation.effect_key == key
                and observation.status == "PRESENT"
                and result.output["payload_digest"] == hashlib.sha256(content).hexdigest()
                and reader.verify_payload(observation, hashlib.sha256(content).hexdigest())
            )
        except (KeyError, TypeError, ValueError):
            return False

    return verify


def _artifact_compensation(root: str, project_id: str) -> Callable[[Any], Any]:
    # Only stable string configuration is captured, so approvals/cold replay bind
    # exactly the same compensation code and destination on every process.
    def compensate(result: Any) -> bool:
        from pathlib import Path as DestinationPath

        from nexus_ai_agent.adapters import fenced_filesystem_destination as destination_module
        from nexus_ai_agent.creative.studio import durable_dedupe as evidence_module

        if result is None:
            raise RuntimeError("uncertain publication requires authenticated recovery")
        observation = evidence_module.ExternalEffectObservation(**result.output["artifact"])
        writer = destination_module.FencedFilesystemEffectDestination(
            DestinationPath(root), project_id=project_id
        )
        reader = destination_module.FilesystemObservationVerifier(
            DestinationPath(root), project_id=project_id
        )
        return reader.verify(writer.compensate(observation))

    return compensate


def build_production_durable_command_bus(
    db_path: StoragePath,
    destination_root: StoragePath,
    *,
    approval_secret: bytes | str,
    project: Project,
    credentials: Mapping[str, PrincipalId],
    authorizer: ProjectAuthorizer,
    verifiers: Mapping[str, Callable[[Project, TypedCommand, CommandResult, str], bool]],
    registry: CapabilityRegistry | None = None,
    operation_truth_path: StoragePath | None = None,
) -> DurableStudioExecutionRuntime:
    """Build mandatory SQLite-backed authenticated ingress, with no legacy path.

    Project and authentication configuration are mandatory: db_path and a MAC
    key alone cannot identify a human or authorize project access.
    """
    destination_root = destination_root.resolve()
    if not destination_root.is_dir():
        raise ValueError("destination root must be provisioned")
    secret = approval_secret.encode() if isinstance(approval_secret, str) else approval_secret
    registry = deepcopy(registry) if registry is not None else build_wave1_registry()
    for operation in registry.list_operations():
        spec = registry.get_spec(operation)
        if spec.external_effects and (
            spec.handler is not _uncomposed_artifact
            or spec.input_model is not ArtifactPublishInput
            or spec.precondition_contract != "w4.artifact.utf8-fenced.v1"
            or spec.postcondition_contract != "w4.artifact.digest-present.v1"
            or not spec.durable_effects
        ):
            raise ValueError("external operations require an operation-specific fenced composition")
    truth_path = (
        operation_truth_path or StoragePath(__file__).resolve().parents[3] / "OPERATION_TRUTH.json"
    )
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    if truth.get("schema") != "nagar.operation_truth.v1":
        raise ValueError("unsupported operation truth schema")
    rows = truth["operations"]
    contracts = {row["operation_id"]: row["evidence"] for row in rows}
    if len(contracts) != len(rows):
        raise ValueError("duplicate operation truth identity")
    registry.verify_declared_contracts(contracts)
    authority = ProvisionedPrincipalAuthority(credentials, secret, authorizer)
    destination = ArtifactDestination(destination_root, project_id=project.project_id)
    reader = ArtifactReader(destination_root, project_id=project.project_id)
    bound_registry = CapabilityRegistry()
    bound_verifiers = dict(verifiers)
    compensations: dict[str, Callable[[Any], Any]] = {}
    for operation in registry.list_operations():
        spec = registry.get_spec(operation)
        description = registry.describe(operation)
        domain, capability = description.capability_id.split(".", 1)
        if spec.external_effects:
            spec = replace(spec, handler=_artifact_handler(destination))
            bound_verifiers[operation] = _artifact_verifier(reader)
            compensations[operation] = _artifact_compensation(
                str(destination_root.resolve()), project.project_id
            )
        bound_registry.register_operation(
            domain,
            capability,
            spec,
            capability_version=description.version,
            available=description.available,
        )
    routes = {
        (project.project_id, f"step-{index}"): RecoveryRoute(
            destination, reader, "w4.filesystem.v1", destination
        )
        for index in range(256)
    }
    store = DurablePlanDedupeStore(
        db_path, repair_authorizer=authority.authorize_repair, recovery_routes=routes
    )
    bus = compose_durable_command_bus(
        project,
        registry=bound_registry,
        authorizer=authorizer,
        store=store,
        verifiers=bound_verifiers,
    )
    return DurableStudioExecutionRuntime(
        bus, store, authority, authorizer, _compensations=compensations
    )
