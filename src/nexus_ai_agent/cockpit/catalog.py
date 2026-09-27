"""Read-only projection of the canonical registry; never an execution gateway.

Registration, pack activation, cryptographic verification and successful
execution are different facts. Every response preserves those distinctions.
Input validation is deliberately not a TypedCommand or authorization verdict:
there is no project, actor, reference resolution, file probe or artifact here.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from nexus_ai_agent.creative.packs.runtime import (
    PackRuntime,
    build_pack_runtime,
    composition_issues,
    installed_version,
)

SCHEMA_VERSION = "nexus.cockpit.v1"
MAX_BODY_BYTES = 32_768
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 2_048
MAX_ERRORS = 20
NOT_CHECKED = (
    "actor_authorization",
    "pack_activation",
    "reference_resolution",
    "asset_existence",
    "runtime_dependencies",
    "execution",
    "artifact_verification",
)


class CatalogUnavailable(ValueError):
    """Missing/inconsistent installed pack data must not become an empty green UI."""


class UnknownOperation(ValueError):
    """The lab only accepts an id present in the canonical registry."""


def _schema_names(schema: dict[str, Any]) -> set[str]:
    """Known field names for safe error locations (never echo arbitrary user keys)."""
    names: set[str] = set()
    stack: list[Any] = [schema]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            properties = item.get("properties", {})
            if isinstance(properties, dict):
                names.update(properties)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return names


class Catalog:
    """A process-local catalog snapshot, built without activating any pack.

    Only packaged builtin metadata is projected. Never serialize a Settings
    instance, RegisteredPack.source, arbitrary manifest paths, or user inputs.
    Construct a fresh Catalog (normally by restarting the cockpit) after an
    installed registry upgrade. There is intentionally no hot plugin loading.
    """

    def __init__(self, runtime: PackRuntime | None = None) -> None:
        if runtime is None:
            if composition_issues():
                raise CatalogUnavailable("Installed pack composition is incomplete.")
            runtime = build_pack_runtime(activate=False)
        self._registry = runtime.registry
        statuses = runtime.status()
        expected = {entry.package_id for entry in runtime.composition}
        if {row.package_id for row in statuses} != expected:
            raise CatalogUnavailable("Installed pack manifests are incomplete.")

        packs: list[dict[str, Any]] = []
        for row in statuses:
            pack = runtime.packs.get(row.package_id)
            packs.append(
                {
                    "id": row.package_id,
                    "directory": row.directory,
                    "version": row.version,
                    "display_name": pack.manifest.display_name,
                    "operations": list(row.capabilities),
                    "pending_operations": list(row.pending),
                    "registered_count": len(row.capabilities) - len(row.pending),
                    "active_in_catalog": row.active,
                    "signature_state": row.signature_state,
                    "signature_verified": pack.report.trusted,
                    "anchor": pack.anchor,
                    "external_binaries": list(row.external_binaries),
                    "network_policy": pack.manifest.network_policy.model_dump(mode="json"),
                }
            )

        operations: list[dict[str, Any]] = []
        self._field_names: dict[str, set[str]] = {}
        for operation_id in runtime.operation_ids():
            spec = self._registry.get_spec(operation_id)
            description = self._registry.describe(operation_id)
            operations.append(
                {
                    "id": operation_id,
                    "description": spec.description,
                    "domain": operation_id.split(".", 1)[0],
                    "capability_id": description.capability_id,
                    "capability_version": description.version,
                    "schema_version": description.operation_schema_version,
                    "input_schema": description.operation_schema,
                    "permission_level": spec.permission_level.value,
                    "required_permissions": list(description.required_permissions),
                    "required_packs": list(description.required_packs),
                    "pack_id": description.pack_provider,
                    "execution_modes": list(description.execution_modes),
                    "deterministic": spec.deterministic,
                    "available_in_registry": description.available,
                    "reference_fields": list(spec.reference_fields),
                }
            )
            self._field_names[operation_id] = _schema_names(description.operation_schema)

        levels = Counter(op["permission_level"] for op in operations)
        document = {
            "schema_version": SCHEMA_VERSION,
            "version": installed_version(),
            "scope": "installed_catalog",
            "bot_connected": False,
            "execution_enabled": False,
            "summary": {
                "operations": len(operations),
                "packs": len(packs),
                "domains": len({op["domain"] for op in operations}),
                "pending_operations": sum(len(pack["pending_operations"]) for pack in packs),
                "confirmation_required": levels["C"],
                "permission_levels": {level: levels[level] for level in ("A", "B", "C", "D")},
            },
            "packs": packs,
            "operations": operations,
            "validation": {
                "scope": "input_schema_only",
                "not_checked": list(NOT_CHECKED),
                "max_body_bytes": MAX_BODY_BYTES,
                "max_json_depth": MAX_JSON_DEPTH,
                "max_json_nodes": MAX_JSON_NODES,
            },
        }
        # Hash only stable metadata; refreshing must not invent a new catalog.
        stable = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        self.fingerprint = hashlib.sha256(stable.encode()).hexdigest()
        document["fingerprint"] = self.fingerprint
        self._document_json = json.dumps(document, ensure_ascii=True)

    def snapshot(self) -> dict[str, Any]:
        """Return a detached view so response consumers cannot mutate the catalog."""
        document: dict[str, Any] = json.loads(self._document_json)
        document["observed_at"] = datetime.now(timezone.utc).isoformat()
        return document

    def validate_input(self, operation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation_id not in self._registry:
            raise UnknownOperation("Operation is not registered.")
        spec = self._registry.get_spec(operation_id)
        result: dict[str, Any] = {
            "operation": operation_id,
            "scope": "input_schema_only",
            "catalog_fingerprint": self.fingerprint,
            "execution_authorized": False,
            "executed": False,
            "not_checked": list(NOT_CHECKED),
            "errors": [],
        }
        try:
            # Same Pydantic input model/coercion semantics as the existing bus,
            # but deliberately no bus call, handler, project or side effect.
            spec.input_model.model_validate(payload)
        except ValidationError as exc:
            names = self._field_names[operation_id]
            errors = exc.errors(include_url=False, include_context=False, include_input=False)
            result["valid"] = False
            result["error_count"] = len(errors)
            result["errors"] = [
                {
                    "path": [
                        part if isinstance(part, int) or part in names else "?"
                        for part in error["loc"]
                    ],
                    "code": error["type"],
                    # Model-level validators can interpolate payloads into
                    # their messages. Do not return those messages or contexts.
                    "message": "Input does not satisfy the registered schema.",
                }
                for error in errors[:MAX_ERRORS]
            ]
        else:
            result["valid"] = True
            result["error_count"] = 0
            result["defaulted_fields"] = sorted(set(spec.input_model.model_fields) - set(payload))
        return result
