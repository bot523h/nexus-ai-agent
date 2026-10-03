"""Phase 3 (§10.1): One Authority Per Responsibility structural boundary gate.

Declares the canonical module/class/protocol for all 20 responsibilities in
Section 5.2 and enforces via AST scanning that no competing authority classes
appear outside their canonical module.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "nexus_ai_agent"

# Section 5.2: All 20 responsibilities -> (canonical_module, canonical_symbol)
CANONICAL_AUTHORITY_REGISTRY: dict[str, tuple[str, str]] = {
    "1_principal_actor_identity": (
        "nexus_ai_agent.creative.studio.models",
        "ActorIdentity",
    ),
    "2_project_access_policy_authorization": (
        "nexus_ai_agent.creative.studio.authorization",
        "ProjectAuthorizer",
    ),
    "3_capability_registry_operation_metadata": (
        "nexus_ai_agent.creative.studio.capabilities",
        "CapabilityRegistry",
    ),
    "4_command_envelope_validation": (
        "nexus_ai_agent.creative.studio.models",
        "TypedCommand",
    ),
    "5_command_bus_execution": (
        "nexus_ai_agent.creative.studio.bus",
        "CommandBus",
    ),
    "6_creative_intent_creative_ir_plan_compilation": (
        "nexus_ai_agent.creative.intelligence.ir",
        "CreativeWork",
    ),
    "7_timeline_temporal_truth_timebase_rational_math": (
        "nexus_ai_agent.creative.studio.models",
        "TimeBase",
    ),
    "8_reference_resolution": (
        "nexus_ai_agent.creative.studio.references",
        "ReferenceResolver",
    ),
    "9_project_state_revision_transaction_commit": (
        "nexus_ai_agent.creative.studio.models",
        "PlanTransaction",
    ),
    "10_undo_redo_semantics": (
        "nexus_ai_agent.creative.studio.models",
        "UndoConflictError",
    ),
    "11_job_queue_worker_leases_fencing_tokens": (
        "nexus_ai_agent.creative.studio.persistence",
        "JobAttemptLease",
    ),
    "12_idempotency_replay_suppression": (
        "nexus_ai_agent.creative.studio.persistence",
        "DurableStudioStore",
    ),
    "13_checkpoint_lifecycle_persistence": (
        "nexus_ai_agent.storage.checkpoint_lifecycle_store",
        "SQLiteCheckpointLifecycleStore",
    ),
    "14_storage_key_validation_local_cache_containment": (
        "nexus_ai_agent.storage.providers.local_cache",
        "LocalCacheProvider",
    ),
    "15_media_execution_render_worker_boundary": (
        "nexus_ai_agent.creative.rendering.executor",
        "LaneArtifact",
    ),
    "16_artifact_passport_independent_media_verification": (
        "nexus_ai_agent.creative.studio.passport",
        "ArtifactPassport",
    ),
    "17_execution_receipts_lineage_provenance_graph": (
        "nexus_ai_agent.creative.spine.models",
        "CreativeGraph",
    ),
    "18_ssrf_remote_url_safety": (
        "nexus_ai_agent.core.ssrf_guard",
        "SafeAsyncTransport",
    ),
    "19_board_coordination_referee": (
        "nexus_ai_agent.continuum.gate",
        "GATE_SCHEMA",
    ),
    "20_continuum_ledger_verification_gate": (
        "nexus_ai_agent.continuum.security_mutation_support",
        "classify_mutation_outcome",
    ),
}

# Classes that must have exactly ONE definition site across all of src/nexus_ai_agent/
UNIQUE_AUTHORITY_CLASSES: dict[str, str] = {
    "ActorIdentity": "src/nexus_ai_agent/creative/studio/models.py",
    "ProjectAuthorizer": "src/nexus_ai_agent/creative/studio/authorization.py",
    "CapabilityRegistry": "src/nexus_ai_agent/creative/studio/capabilities.py",
    "TypedCommand": "src/nexus_ai_agent/creative/studio/models.py",
    "CommandBus": "src/nexus_ai_agent/creative/studio/bus.py",
    "PlanTransaction": "src/nexus_ai_agent/creative/studio/models.py",
    "CreativeWork": "src/nexus_ai_agent/creative/intelligence/ir.py",
    "CreativeExecutionSpine": "src/nexus_ai_agent/creative/spine/execution.py",
    "CreativeGraph": "src/nexus_ai_agent/creative/spine/models.py",
    "LocalCacheProvider": "src/nexus_ai_agent/storage/providers/local_cache.py",
    "SafeAsyncTransport": "src/nexus_ai_agent/core/ssrf_guard.py",
}


def test_all_20_canonical_responsibilities_are_declared_and_importable() -> None:
    assert len(CANONICAL_AUTHORITY_REGISTRY) == 20
    for responsibility, (module_name, symbol_name) in CANONICAL_AUTHORITY_REGISTRY.items():
        module = importlib.import_module(module_name)
        assert hasattr(module, symbol_name), (
            f"Responsibility {responsibility} missing {symbol_name} in {module_name}"
        )


def test_no_competing_class_definitions_exist_for_canonical_authorities() -> None:
    definitions: dict[str, list[str]] = {name: [] for name in UNIQUE_AUTHORITY_CLASSES}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in definitions:
                definitions[node.name].append(rel)

    for class_name, expected_path in UNIQUE_AUTHORITY_CLASSES.items():
        actual = definitions[class_name]
        assert actual == [expected_path], (
            f"Expected {class_name} to be defined only in {expected_path}, found {actual}"
        )
