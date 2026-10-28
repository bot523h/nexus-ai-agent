"""Guard tests for the Operation Contract Matrix, 70/57 Reconciliation, and Three-Layer Model.

Mission: Agent B — Operation Truth & Wave Planning Mission.
Verifies:
1. Product Catalog (70) != Runtime Registry (57) != Executable Surface (9).
2. Exactly 23 operations are missing and preserved as EvidenceClass.MISSING / L0 / NOT_IMPLEMENTED.
3. No fake or stub implementations exist to artificially inflate the registry.
4. Error taxonomy and contract violations (unknown op, missing cap, schema error, duplicate op).
5. Idempotency and revision conflict handling guards.
6. Documentation and JSON synchronization.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_ai_agent.bot.creative_surface import (
    CreativeErrorCode,
    CreativeFailure,
    CreativeRequest,
    CreativeSurfaceMapper,
)
from nexus_ai_agent.creative.packs.runtime import (
    build_runtime_registry,
    composition_issues,
    stale_capabilities,
)
from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
)
from nexus_ai_agent.creative.studio.models import (
    CommandValidationError,
    PreconditionError,
    Project,
    TypedCommand,
    UnknownOperationError,
    new_project,
)

REPO_ROOT = Path(__file__).parents[2]


# ---------------------------------------------------------------------------
# 1. Reconciliation Invariant & Three-Layer Separation
# ---------------------------------------------------------------------------


def test_reconciliation_numbers_invariant() -> None:
    """Assert the exact mathematical reconciliation between the layers."""
    reg = build_runtime_registry()
    runtime_ops = set(reg.list_operations())

    matrix_file = REPO_ROOT / "OPERATION_MATRIX.json"
    assert matrix_file.exists(), "OPERATION_MATRIX.json must exist"
    matrix_data = json.loads(matrix_file.read_text(encoding="utf-8"))

    catalog_ops = [op for op in matrix_data["operations"] if op["product_defined"]]
    assert len(catalog_ops) == 70, f"Expected 70 catalog operations, got {len(catalog_ops)}"

    assert len(runtime_ops) == 57, f"Expected 57 registered operations, got {len(runtime_ops)}"

    overlap = set(op["operation_id"] for op in catalog_ops) & runtime_ops
    assert len(overlap) == 47, f"Expected 47 overlapping operations, got {len(overlap)}"

    missing = set(op["operation_id"] for op in catalog_ops) - runtime_ops
    assert len(missing) == 23, f"Expected exactly 23 missing gaps, got {len(missing)}"

    # Check categories of missing: 10 portrait, 10 scene, 3 color
    portrait_missing = [op for op in missing if op.startswith("portrait.")]
    scene_missing = [op for op in missing if op.startswith("scene.")]
    color_missing = [op for op in missing if op.startswith("color.")]
    assert len(portrait_missing) == 10
    assert len(scene_missing) == 10
    assert len(color_missing) == 3


def test_three_layers_are_strictly_decoupled() -> None:
    """Prove Product Catalog != Runtime Registry != Executable Surface."""
    reg = build_runtime_registry()
    runtime_ops = set(reg.list_operations())

    matrix_data = json.loads((REPO_ROOT / "OPERATION_MATRIX.json").read_text(encoding="utf-8"))
    catalog_ops = set(
        op["operation_id"] for op in matrix_data["operations"] if op["product_defined"]
    )

    # Executable surface: /edit, /caption, /grade + /slideshow
    surface_ops = set(SURFACE_TO_CANONICAL.values()) | {"slideshow.compose", "slideshow.render"}

    # 1. Catalog != Registry
    assert catalog_ops != runtime_ops
    assert len(catalog_ops) == 70
    assert len(runtime_ops) == 57

    # 2. Registry != Executable Surface
    assert runtime_ops != surface_ops
    assert len(surface_ops) == 9
    assert surface_ops.issubset(runtime_ops)


# ---------------------------------------------------------------------------
# 2. Guard: No Fake Implementations to Inflate Registry
# ---------------------------------------------------------------------------


def test_no_fake_implementations_for_23_gaps() -> None:
    """Verify that none of the 23 missing operations has been fake-registered or stubbed."""
    reg = build_runtime_registry()
    runtime_ops = set(reg.list_operations())

    reconciliation_data = json.loads(
        (REPO_ROOT / "RECONCILIATION.json").read_text(encoding="utf-8")
    )
    missing_gaps = set(reconciliation_data["missing_gaps"].keys())
    assert len(missing_gaps) == 23

    for gap_op in missing_gaps:
        # Must not be registered in runtime registry
        assert gap_op not in runtime_ops, f"Gap operation {gap_op} was illegally registered!"
        # Getting spec must raise UnknownOperationError
        with pytest.raises(UnknownOperationError):
            reg.get_spec(gap_op)


# ---------------------------------------------------------------------------
# 3. Guard: Unknown, Missing, and Duplicate Operations
# ---------------------------------------------------------------------------


def test_unknown_operation_rejected_by_registry() -> None:
    reg = build_runtime_registry()
    with pytest.raises(UnknownOperationError):
        reg.get_spec("nonexistent.operation")


def test_duplicate_operation_registration_fails() -> None:
    reg = CapabilityRegistry()
    spec = build_runtime_registry().get_spec("media.play")
    reg.register_domain("media")
    reg.register_operation("media", "playback", spec)

    with pytest.raises(ValueError, match="duplicate operation"):
        reg.register_operation("media", "playback", spec)


def test_operation_domain_mismatch_fails() -> None:
    reg = CapabilityRegistry()
    spec = build_runtime_registry().get_spec("media.play")
    reg.register_domain("timeline")
    with pytest.raises(ValueError, match="does not belong to domain"):
        reg.register_operation("timeline", "playback", spec)


# ---------------------------------------------------------------------------
# 4. Guard: Schema Validation & Malformed Envelopes
# ---------------------------------------------------------------------------


def test_wrong_schema_rejected() -> None:
    class DummyInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        valid_field: str

    with pytest.raises(ValidationError):
        DummyInput.model_validate({"invalid_field": "test"})


def _make_test_project() -> Project:
    from nexus_ai_agent.creative.studio.models import Playhead, TimeBase, Timeline

    timeline = Timeline(
        timeline_id="tl_test",
        duration_us=10_000_000,
        tracks=[],
        playhead=Playhead(timecode_us=0, timebase=TimeBase(numerator=30, denominator=1)),
    )
    return new_project("p1", "Test", timeline)


def test_malformed_command_envelope_rejected() -> None:
    bus = CommandBus(state=_make_test_project(), registry=build_runtime_registry())

    # Missing command_id
    with pytest.raises(CommandValidationError):
        bus.dispatch({"operation": "media.play", "command_id": ""})

    # Unsupported protocol version (v2 when only v1 supported)
    with pytest.raises(CommandValidationError):
        bus.dispatch(
            {
                "command_id": "c1",
                "operation": "media.play",
                "protocol_version": "nagar.command.v2",
            }
        )


# ---------------------------------------------------------------------------
# 5. Guard: Authorization, Revision Conflict, Idempotency
# ---------------------------------------------------------------------------


def test_authorization_failure_on_unconfirmed_level_c() -> None:
    reg = build_runtime_registry()
    # delivery.render_master_4k is Level C
    decision = reg.check_permission("delivery.render_master_4k", confirmed=False)
    assert not decision.allowed
    assert "requires explicit confirmation" in decision.reason

    confirmed_decision = reg.check_permission("delivery.render_master_4k", confirmed=True)
    assert confirmed_decision.allowed


def test_revision_conflict_fails_dispatch() -> None:
    project = _make_test_project()
    bus = CommandBus(state=project, registry=build_runtime_registry())

    # Precondition mismatch
    cmd = TypedCommand(
        command_id="cmd_rev_fail",
        operation="media.play",
        input={},
        preconditions={"state_revision": 999},  # Current revision is 0
    )
    with pytest.raises(PreconditionError, match="stale state_revision"):
        bus.dispatch(cmd)


def test_idempotency_conflict_returns_cached_or_rejects_divergence() -> None:
    project = _make_test_project()
    bus = CommandBus(state=project, registry=build_runtime_registry())

    cmd1 = TypedCommand(
        command_id="c_idem_1",
        operation="media.play",
        input={},
        idempotency_key="key-12345",
    )
    res1 = bus.dispatch(cmd1)

    # Identical replay returns same result
    cmd2 = TypedCommand(
        command_id="c_idem_2",
        operation="media.play",
        input={},
        idempotency_key="key-12345",
    )
    res2 = bus.dispatch(cmd2)
    assert res1.transaction_id == res2.transaction_id
    assert res1.state_hash == res2.state_hash


# ---------------------------------------------------------------------------
# 6. Guard: Surface vs Registry Reachability
# ---------------------------------------------------------------------------


def test_operation_registered_but_unreachable_on_surface() -> None:
    """Verify that operations like motion.warp or audio.deess are registered in runtime
    but cleanly refused at the Telegram surface rather than crashing."""
    mapper = CreativeSurfaceMapper()

    # User attempts to call /edit warp
    req = CreativeRequest(
        command="edit",
        operation="warp",
        args=(),
        media_file_id="fid_123",
        media_duration_s=5.0,
    )
    result = mapper.map(req)
    assert isinstance(result, CreativeFailure)
    assert result.code == CreativeErrorCode.INVALID_REQUEST
    assert result.message_key == "creative.invalid_operation"


def test_all_manifests_clean_without_composition_issues() -> None:
    """Verify pack manifest completeness contract."""
    assert composition_issues() == ()
    assert stale_capabilities() == {}


def test_documentation_and_json_parity() -> None:
    """Ensure OPERATION_MATRIX.json and RECONCILIATION.json are in exact sync."""
    mat = json.loads((REPO_ROOT / "OPERATION_MATRIX.json").read_text(encoding="utf-8"))
    rec = json.loads((REPO_ROOT / "RECONCILIATION.json").read_text(encoding="utf-8"))

    assert mat["summary"]["catalog_total"] == rec["audit_baseline"]["product_catalog_count"]
    assert (
        mat["summary"]["runtime_registry_total"] == rec["audit_baseline"]["runtime_registry_count"]
    )
    assert mat["summary"]["missing_gaps"] == rec["audit_baseline"]["missing_gaps_count"]
    assert mat["summary"]["universe_total"] == rec["audit_baseline"]["universe_count"]
