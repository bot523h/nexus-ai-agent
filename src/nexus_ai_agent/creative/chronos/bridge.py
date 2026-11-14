"""Chronos dispatch registry for the NEXUS staged execution spine."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from nexus_ai_agent.creative.studio.models import ActorIdentity

from .chronos_port import (
    ChronosCommand,
    ChronosExecutionReceipt,
    ChronosHandler,
    ChronosPort,
    execute_chronos_command,
)
from .nexus_spine_adapter import (
    CHRONOS_STAGED_SPINE_MASTER,
    ArtifactPassportSink,
    ChronosAssetVersionStore,
    ChronosCommandBus,
    ChronosLineageRepositoryProtocol,
    ChronosSpineAttempt,
    ChronosSpineExecutionReceipt,
    FencingTokenReader,
    PreVerifyHook,
    compute_chronos_recipe_identity,
    execute_chronos_staged_attempt,
    register_chronos_staged_spine_operation,
)

CHRONOS_SPINE_OPERATIONS: Final[frozenset[str]] = frozenset({CHRONOS_STAGED_SPINE_MASTER})


def dispatch_chronos_spine_operation(
    operation_name: str,
    workspace_root: str | Path,
    attempt: ChronosSpineAttempt,
    *,
    active_fencing_token: FencingTokenReader,
    before_verify: PreVerifyHook | None = None,
    command_bus: ChronosCommandBus | None = None,
    command_actor: ActorIdentity | None = None,
    asset_version_store: ChronosAssetVersionStore | None = None,
    passport_sink: ArtifactPassportSink | None = None,
    lineage_repository: ChronosLineageRepositoryProtocol | None = None,
) -> ChronosSpineExecutionReceipt:
    """Dispatch only the registered staged operation and fail closed on denial."""
    if operation_name not in CHRONOS_SPINE_OPERATIONS:
        raise ValueError(f"unsupported Chronos spine operation: {operation_name}")
    return execute_chronos_staged_attempt(
        workspace_root,
        attempt,
        active_fencing_token=active_fencing_token,
        before_verify=before_verify,
        command_bus=command_bus,
        command_actor=command_actor,
        asset_version_store=asset_version_store,
        passport_sink=passport_sink,
        lineage_repository=lineage_repository,
    )


def build_chronos_staged_spine_handler(
    workspace_root: str | Path,
    attempt: ChronosSpineAttempt,
    *,
    active_fencing_token: FencingTokenReader,
    before_verify: PreVerifyHook | None = None,
    command_bus: ChronosCommandBus | None = None,
    command_actor: ActorIdentity | None = None,
    asset_version_store: ChronosAssetVersionStore | None = None,
    passport_sink: ArtifactPassportSink | None = None,
    lineage_repository: ChronosLineageRepositoryProtocol | None = None,
) -> ChronosHandler:
    """Bind one immutable attempt to the dependency-injected :class:`ChronosPort`.

    The small ChronosCommand envelope carries references and recipe identity,
    never raw media bytes. Its handler checks the entire immutable request
    fingerprint before entering the real stage/verify/publish execution path.
    """
    identity = compute_chronos_recipe_identity(attempt)
    expected_inputs = (attempt.project_id, attempt.parent_revision_id, attempt.attempt_id)
    expected_parameters = {
        "fencing_token": str(attempt.fencing_token),
        "operation_name": attempt.operation_name,
        "recipe_digest": identity.recipe_digest,
    }

    def handle(command: ChronosCommand) -> ChronosExecutionReceipt:
        if (
            command.operation != CHRONOS_STAGED_SPINE_MASTER
            or command.inputs != expected_inputs
            or dict(command.parameters) != expected_parameters
            or not command.deterministic
            or command.external_api_allowed
        ):
            raise ValueError("ChronosPort staged-spine command does not match its bound attempt")
        receipt = dispatch_chronos_spine_operation(
            CHRONOS_STAGED_SPINE_MASTER,
            workspace_root,
            attempt,
            active_fencing_token=active_fencing_token,
            before_verify=before_verify,
            command_bus=command_bus,
            command_actor=command_actor,
            asset_version_store=asset_version_store,
            passport_sink=passport_sink,
            lineage_repository=lineage_repository,
        )
        if receipt.recipe_identity.recipe_digest != identity.recipe_digest:
            raise ValueError("executed Chronos recipe differs from the port's bound identity")
        return ChronosExecutionReceipt(
            operation=CHRONOS_STAGED_SPINE_MASTER,
            output_path=receipt.asset_version.published_path,
            sha256=receipt.asset_version.output_content_sha256,
            quality_claim="REAL-DISK_CHRONOS_SPINE_VERIFIED",
            metadata={
                "verification_tier": receipt.verification.verification_tier,
                "merkle_root": receipt.verification.merkle_root,
                "asset_version_id": receipt.asset_version.asset_version_id,
                "passport_digest": receipt.artifact_passport.passport_digest,
            },
        )

    return handle


def staged_spine_command(attempt: ChronosSpineAttempt) -> ChronosCommand:
    """Build the compact immutable port command for a previously bound attempt."""
    identity = compute_chronos_recipe_identity(attempt)
    return ChronosCommand(
        operation=CHRONOS_STAGED_SPINE_MASTER,
        inputs=(attempt.project_id, attempt.parent_revision_id, attempt.attempt_id),
        parameters={
            "fencing_token": str(attempt.fencing_token),
            "operation_name": attempt.operation_name,
            "recipe_digest": identity.recipe_digest,
        },
        deterministic=True,
        external_api_allowed=False,
    )


__all__ = [
    "CHRONOS_SPINE_OPERATIONS",
    "CHRONOS_STAGED_SPINE_MASTER",
    "ChronosCommand",
    "ChronosExecutionReceipt",
    "ChronosHandler",
    "ChronosPort",
    "build_chronos_staged_spine_handler",
    "dispatch_chronos_spine_operation",
    "execute_chronos_command",
    "register_chronos_staged_spine_operation",
    "staged_spine_command",
]
