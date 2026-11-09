"""Unit tests for the composition IR, the licence ledger and the media contract.

These are the *contract* tests of Gate 3: identity, versioning, the typed
media contract, the versioned parameter schema and the resource model.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.composition.contracts import (
    CATEGORIES,
    CompositionPlan,
    ExpectedOutput,
    ExternalInput,
    LicenseLedger,
    LicenseRecord,
    PlanNode,
    PortBinding,
    ResourceEstimate,
    ResourceRequirements,
)
from nexus_ai_agent.creative.studio.models import MediaContract, MediaPort


def _node(node_id: str = "n1", **kwargs) -> PlanNode:
    payload = {
        "node_id": node_id,
        "operation": "media.resize_image",
        "capability_id": "media.resize_image",
        "capability_version": "1.0.0",
        "operation_schema_version": 1,
        "params": {},
        "bindings": (),
    }
    payload.update(kwargs)
    return PlanNode(**payload)


def _plan(nodes, **kwargs) -> CompositionPlan:
    payload = {
        "nodes": tuple(nodes),
        "expected_outputs": (ExpectedOutput(name="out", node_id="n1", output_port="resized"),),
    }
    payload.update(kwargs)
    return CompositionPlan(**payload)


# ---------------------------------------------------------------------------
# identity and versioning
# ---------------------------------------------------------------------------


def test_a_plan_carries_a_stable_fingerprint() -> None:
    plan = _plan([_node()])
    assert plan.fingerprint().startswith("sha256:")
    assert plan.fingerprint() == _plan([_node()]).fingerprint()


def test_the_fingerprint_changes_when_the_plan_changes() -> None:
    first = _plan([_node()])
    second = _plan([_node(params={"width": 10})])
    assert first.fingerprint() != second.fingerprint()


def test_the_canonical_json_is_sorted_and_stable() -> None:
    plan = _plan([_node()])
    rendered = plan.canonical_json()
    assert rendered == plan.canonical_json()
    # sorted keys: "expected_outputs" before "nodes" before "plan_version"
    assert rendered.index('"expected_outputs"') < rendered.index('"nodes"')
    assert rendered.index('"nodes"') < rendered.index('"plan_version"')
    # and inside a node the keys are sorted too
    assert rendered.index('"capability_id"') < rendered.index('"operation"')


def test_a_plan_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _plan([_node()], surprise=True)  # type: ignore[call-arg]


def test_node_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        _plan([_node("a"), _node("a")])


def test_external_input_names_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        _plan(
            [_node()],
            external_inputs=(
                ExternalInput(name="src", media_kind="image"),
                ExternalInput(name="src", media_kind="image"),
            ),
        )


def test_a_node_may_bind_each_input_port_only_once() -> None:
    binding = PortBinding(input_port="source", source="external", external="src")
    with pytest.raises(ValidationError):
        _node(bindings=(binding, binding))


def test_a_binding_names_exactly_one_source() -> None:
    with pytest.raises(ValidationError):
        PortBinding(input_port="source", source="external", node_id="other")
    with pytest.raises(ValidationError):
        PortBinding(input_port="source", source="node_output", external="src")


def test_the_plan_version_is_pinned() -> None:
    assert _plan([_node()]).plan_version == 1
    with pytest.raises(ValidationError):
        _plan([_node()], plan_version=0)


# ---------------------------------------------------------------------------
# the typed media contract
# ---------------------------------------------------------------------------


def test_the_media_contract_declares_every_dimension_the_mission_names() -> None:
    contract = MediaContract(
        kind="video",
        container="mp4",
        codec="h264",
        pixel_format="yuv420p",
        color_space="bt709",
        frame_rate=25.0,
        time_base="1/12800",
        sample_rate=48_000,
        channel_layout="stereo",
        bit_depth=8,
        alpha="none",
        width=1920,
        height=1080,
        duration_us_min=1_000_000,
        duration_us_max=10_000_000,
    )
    assert contract.kind == "video"
    assert contract.width == 1920
    assert contract.duration_us_max == 10_000_000


def test_the_media_contract_refuses_an_incoherent_shape() -> None:
    with pytest.raises(ValidationError):
        MediaContract(kind="video", frame_rate=-1.0)
    with pytest.raises(ValidationError):
        MediaContract(kind="video", duration_us_min=10, duration_us_max=1)


def test_a_contract_accepts_a_narrower_producer() -> None:
    required = MediaContract(kind="image")
    produced = MediaContract(kind="image", container="png", codec="png")
    assert required.accepts(produced) is True


def test_a_contract_refuses_a_different_kind() -> None:
    required = MediaContract(kind="image")
    assert required.accepts(MediaContract(kind="video")) is False


def test_a_contract_refuses_a_colour_space_mismatch() -> None:
    required = MediaContract(kind="image", color_space="gray")
    assert required.accepts(MediaContract(kind="image", color_space="rgb")) is False
    assert required.accepts(MediaContract(kind="image", color_space="gray")) is True


def test_a_contract_is_frozen() -> None:
    contract = MediaContract(kind="image")
    with pytest.raises(ValidationError):
        contract.kind = "video"  # type: ignore[misc]


def test_a_media_port_carries_a_name_and_a_contract() -> None:
    port = MediaPort(name="source", contract=MediaContract(kind="image"), required=True)
    assert port.name == "source"
    assert port.contract.kind == "image"


# ---------------------------------------------------------------------------
# the licence ledger
# ---------------------------------------------------------------------------


def test_the_ledger_kinds_the_twelve_categories_apart() -> None:
    assert len(CATEGORIES) == 12
    assert "runtime_software" in CATEGORIES
    assert "input_asset" in CATEGORIES
    assert "distribution_restrictions" in CATEGORIES
    assert "network_use" in CATEGORIES


def test_an_unknown_category_stays_unknown() -> None:
    ledger = LicenseLedger(records=())
    assert ledger.unknown_categories == CATEGORIES
    assert ledger.effective_allow_commercial is False


def test_a_permitted_but_unverified_licence_is_not_commercially_approved() -> None:
    ledger = LicenseLedger(
        records=(
            LicenseRecord(
                category="runtime_software",
                spdx_id="GPL-2.0-or-later",
                allow_commercial=True,
                commercial_use_verified=False,
            ),
        )
    )
    assert ledger.effective_allow_commercial is False
    assert ledger.unknown_categories == tuple(
        category for category in CATEGORIES if category != "runtime_software"
    )


def _complete_permissive_ledger() -> LicenseLedger:
    return LicenseLedger(
        records=tuple(
            LicenseRecord(
                category=category,
                spdx_id="MIT",
                allow_commercial=True,
                commercial_use_verified=True,
            )
            for category in CATEGORIES
        )
    )


def test_a_complete_verified_permissive_ledger_is_commercially_approved() -> None:
    """Approval needs **every** category, not just the ones someone recorded."""
    ledger = _complete_permissive_ledger()
    assert ledger.unknown_categories == ()
    assert ledger.effective_allow_commercial is True


def test_one_verified_record_does_not_approve_the_ledger() -> None:
    """An omitted category is ignorance, not permission."""
    ledger = LicenseLedger(
        records=(
            LicenseRecord(
                category="runtime_software",
                spdx_id="GPL-2.0-or-later",
                allow_commercial=True,
                commercial_use_verified=True,
            ),
        )
    )
    assert ledger.effective_allow_commercial is False
    assert len(ledger.unknown_categories) == 11


def test_the_ledger_refuses_an_unknown_category() -> None:
    with pytest.raises(ValidationError):
        LicenseRecord(category="not_a_category", spdx_id="MIT")  # type: ignore[arg-type]


def test_the_tool_licence_never_implies_the_output_rights() -> None:
    """A GPL tool with a verified licence still says nothing about the media."""
    ledger = LicenseLedger(
        records=(
            LicenseRecord(
                category="runtime_software",
                spdx_id="GPL-2.0-or-later",
                copyleft=True,
                allow_commercial=True,
                commercial_use_verified=True,
            ),
        )
    )
    assert "input_asset" in ledger.unknown_categories
    # The tool's own record allows commercial use ...
    assert ledger.of("runtime_software")[0].effective_allow_commercial is True
    # ... but the ledger as a whole does not approve anything while eleven
    # categories are unknown -- an in-permissive tool licence never becomes an
    # output-media right.
    assert len(ledger.unknown_categories) == 11
    assert ledger.effective_allow_commercial is False


# ---------------------------------------------------------------------------
# the resource model
# ---------------------------------------------------------------------------


def test_resource_requirements_refuse_nonsense() -> None:
    with pytest.raises(ValidationError):
        ResourceRequirements(ram_mb=0)
    with pytest.raises(ValidationError):
        ResourceRequirements(cpu_threads=0)
    with pytest.raises(ValidationError):
        ResourceRequirements(timeout_seconds=0)


def test_the_estimate_keeps_peak_and_total_apart() -> None:
    estimate = ResourceEstimate(
        peak_ram_mb=512,
        total_ram_mb=1024,
        peak_concurrent_ram_mb=512,
        total_disk_mb=256,
        scratch_mb=128,
        cpu_threads=2,
        gpu_memory_mb=0,
        network_egress=False,
        max_timeout_seconds=300,
        node_count=3,
    )
    assert estimate.peak_ram_mb != estimate.total_ram_mb
    assert estimate.peak_concurrent_ram_mb == estimate.peak_ram_mb


def test_the_estimate_refuses_a_zero_node_count() -> None:
    with pytest.raises(ValidationError):
        ResourceEstimate(
            peak_ram_mb=1,
            total_ram_mb=1,
            peak_concurrent_ram_mb=1,
            total_disk_mb=1,
            scratch_mb=1,
            cpu_threads=1,
            gpu_memory_mb=0,
            network_egress=False,
            max_timeout_seconds=1,
            node_count=0,
        )


def test_a_contract_refuses_a_producer_whose_duration_is_unknown() -> None:
    """An unknown bound is not a fit.

    The required contract names a minimum duration; a producer that cannot say
    how long its clip is must be refused, because "we did not measure" cannot
    satisfy "at least one second".
    """
    required = MediaContract(kind="video", duration_us_min=1_000_000)
    assert required.accepts(MediaContract(kind="video", duration_us_min=2_000_000)) is True
    assert required.accepts(MediaContract(kind="video")) is False

    upper = MediaContract(kind="video", duration_us_max=1_000_000)
    assert upper.accepts(MediaContract(kind="video", duration_us_max=900_000)) is True
    assert upper.accepts(MediaContract(kind="video")) is False


def test_an_external_input_contract_follows_its_declared_kind() -> None:
    """A video input never quietly defaults to an *image* contract."""
    derived = ExternalInput(name="clip", media_kind="video")
    assert derived.contract is not None
    assert derived.contract.kind == "video"

    with pytest.raises(ValidationError):
        ExternalInput(name="clip", media_kind="video", contract=MediaContract(kind="image"))
