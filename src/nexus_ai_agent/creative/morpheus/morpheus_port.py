"""Wave 14: Morpheus Studio Port & CommandBus Integration Layer.

Exposes Morpheus Omnia autonomous operations ("morpheus.staged_spine_master",
"morpheus.character_capsule_verify", "morpheus.multiphysics_relight_render")
with strict allowlist enforcement, CommandBus authorization hooks, idempotency replay,
and typed responses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from nexus_ai_agent.creative.morpheus.coupled_multiphysics_ipc_contact import (
    step_coupled_morpheus_multiphysics,
)
from nexus_ai_agent.creative.morpheus.episodic_character_capsule_vault import (
    CharacterIdentityCapsule,
    verify_character_capsule_integrity,
)
from nexus_ai_agent.creative.morpheus.morpheus_real_media_spine import (
    MorpheusArtifactPassportRecord,
    MorpheusAssetVersionRecord,
    MorpheusDurableLineageStore,
    MorpheusLineageRepositoryProtocol,
    MorpheusStagedSpineAdapter,
    MorpheusStageRequest,
)

MORPHEUS_PORT_OPERATIONS: Final[tuple[str, ...]] = (
    "morpheus.staged_spine_master",
    "morpheus.character_capsule_verify",
    "morpheus.multiphysics_relight_render",
)

PortStatus = Literal["SUCCESS", "REFUSED"]


@dataclass(frozen=True)
class MorpheusPortResponse:
    """Typed response envelope returned by MorpheusPort operations."""

    operation: str
    status: PortStatus
    data: dict[str, Any] | None = None
    error_message: str | None = None
    idempotent_replayed: bool = False


class MorpheusPort:
    """Production Studio Port exposing Morpheus capabilities with CommandBus authorization."""

    def __init__(
        self,
        workspace_root: Path,
        active_fencing_token_getter: Callable[[], str],
        command_authorizer: Callable[[str, str], bool] | None = None,
        lineage_repository: MorpheusLineageRepositoryProtocol | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.active_fencing_token_getter = active_fencing_token_getter
        self.command_authorizer = command_authorizer
        self.lineage_repo = (
            MorpheusDurableLineageStore(self.workspace_root)
            if lineage_repository is None
            else lineage_repository
        )
        self.adapter = MorpheusStagedSpineAdapter(
            workspace_root=self.workspace_root,
            active_fencing_token_getter=self.active_fencing_token_getter,
        )
        self._idempotency_cache: dict[str, MorpheusPortResponse] = {}

    def _authorize(self, operation: str, actor_id: str) -> None:
        if operation not in MORPHEUS_PORT_OPERATIONS:
            raise ValueError(f"Operation '{operation}' not in allowlist {MORPHEUS_PORT_OPERATIONS}")
        if self.command_authorizer is not None:
            authorized = False
            try:
                authorized = self.command_authorizer(operation, actor_id)
            except Exception as e:
                raise PermissionError(
                    f"CommandBus authorization error for {actor_id} on {operation}: {e}"
                ) from e
            if not authorized:
                raise PermissionError(
                    f"CommandBus denied actor '{actor_id}' on operation '{operation}'"
                )

    def execute_operation(
        self,
        operation: str,
        payload: dict[str, Any],
        actor_id: str = "nexus_system",
        idempotency_key: str | None = None,
    ) -> MorpheusPortResponse:
        """Route and execute a Morpheus operation with allowlist check and authorization."""
        self._authorize(operation, actor_id)

        # Idempotency replay check
        if idempotency_key:
            cache_key = f"{operation}:{idempotency_key}"
            if cache_key in self._idempotency_cache:
                cached = self._idempotency_cache[cache_key]
                return MorpheusPortResponse(
                    operation=cached.operation,
                    status=cached.status,
                    data=cached.data,
                    error_message=cached.error_message,
                    idempotent_replayed=True,
                )

        try:
            if operation == "morpheus.staged_spine_master":
                res_data = self._handle_staged_spine_master(payload)
            elif operation == "morpheus.character_capsule_verify":
                res_data = self._handle_character_capsule_verify(payload)
            elif operation == "morpheus.multiphysics_relight_render":
                res_data = self._handle_multiphysics_relight_render(payload)
            else:
                raise ValueError(f"Unhandled operation '{operation}'")

            resp = MorpheusPortResponse(operation=operation, status="SUCCESS", data=res_data)
        except Exception as err:
            resp = MorpheusPortResponse(
                operation=operation, status="REFUSED", error_message=str(err)
            )

        if idempotency_key:
            cache_key = f"{operation}:{idempotency_key}"
            self._idempotency_cache[cache_key] = resp

        return resp

    def _handle_staged_spine_master(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = payload["request"]
        if not isinstance(req, MorpheusStageRequest):
            raise TypeError("payload['request'] must be an instance of MorpheusStageRequest")
        version_rec: MorpheusAssetVersionRecord = self.adapter.stage_and_publish(req)
        passport: MorpheusArtifactPassportRecord = self.lineage_repo.record_asset_version(
            version_rec
        )
        return {
            "asset_id": version_rec.asset_id,
            "attempt_id": version_rec.attempt_id,
            "fencing_token": version_rec.fencing_token,
            "published_dir": version_rec.published_dir,
            "published_mp4_path": version_rec.published_mp4_path,
            "manifest_sha256": version_rec.manifest_sha256,
            "merkle_root_hex": version_rec.digests.merkle_root_hex,
            "passport_id": passport.passport_id,
        }

    def _handle_character_capsule_verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        capsule = payload["capsule"]
        if not isinstance(capsule, CharacterIdentityCapsule):
            raise TypeError("payload['capsule'] must be an instance of CharacterIdentityCapsule")
        is_valid = verify_character_capsule_integrity(capsule)
        return {
            "character_id": capsule.character_id,
            "is_valid": is_valid,
            "merkle_root_hex": capsule.merkle_root_hex,
            "capsule_sha256": capsule.capsule_sha256,
        }

    def _handle_multiphysics_relight_render(self, payload: dict[str, Any]) -> dict[str, Any]:
        skeletal_chain = payload["skeletal_chain"]
        joint_rotations = payload["joint_rotations"]
        skinning_weights = payload["skinning_weights"]
        fem_state = payload["fem_state"]
        hair_strands = payload["hair_strands"]
        cloth_vertices = payload["cloth_vertices"]
        dt_s = float(payload.get("dt_s", 0.016667))

        (
            bone_out,
            fem_out,
            hair_out,
            cloth_out,
            receipt,
        ) = step_coupled_morpheus_multiphysics(
            skeletal_chain=skeletal_chain,
            joint_rotations=joint_rotations,
            skinning_weights=skinning_weights,
            fem_state=fem_state,
            hair_strands=hair_strands,
            cloth_vertices=cloth_vertices,
            dt_s=dt_s,
        )
        return {
            "receipt": {
                "min_bone_flesh_distance_mm": receipt.min_bone_flesh_distance_mm,
                "min_flesh_hair_distance_mm": receipt.min_flesh_hair_distance_mm,
                "min_flesh_cloth_distance_mm": receipt.min_flesh_cloth_distance_mm,
                "fem_volume_det_range": receipt.fem_volume_det_range,
                "total_frictional_dissipation_joules": receipt.total_frictional_dissipation_joules,
                "zero_interpenetration_verified": receipt.zero_interpenetration_verified,
            },
            "bone_count": len(bone_out),
            "fem_vertex_count": len(fem_out.vertices),
            "hair_strand_count": len(hair_out),
            "cloth_vertex_count": len(cloth_out),
        }
