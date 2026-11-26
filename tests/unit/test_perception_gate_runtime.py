"""The admission gate as the *production* runtime reaches it.

``test_perception_gate.py`` proves the policy. This file proves the wiring: the
production composition root binds the gate, the pack operations fail closed
through it, and no production caller can quietly build an ungated registry.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.production_runtime import build_production_registry
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Clip,
    MediaRef,
    Project,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    new_project,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SEC = 1_000_000
CONTENT = "sha256:" + "a" * 64


def _project() -> Project:
    clip = Clip(
        clip_id="clip_a",
        media_ref=MediaRef(
            asset_id="asset_a", content_sha256=CONTENT, media_kind="audio", duration_us=20 * SEC
        ),
        source_range=TimeRangeUS(start_us=0, end_us=20 * SEC),
        timeline_range=TimeRangeUS(start_us=0, end_us=20 * SEC),
    )
    timeline = Timeline(
        timeline_id="tl",
        duration_us=60 * SEC,
        tracks=[Track(track_id="a1", name="A1", kind="audio", clips=[clip])],
    )
    project: Project = new_project("gate_proj", "Gate", timeline)
    return project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="asset_a",
                    media_kind="audio",
                    content_sha256=CONTENT,
                    duration_us=20 * SEC,
                )
            ]
        }
    )


def _silence_evidence(spans: list[tuple[float, float]]) -> Any:
    from nexus_ai_agent.creative.packs.perception.models import (
        SilenceEvidence,
        SilenceSpan,
    )

    return SilenceEvidence.seal(
        engine="ffmpeg 6.1.1",
        asset_sha256=CONTENT,
        duration_us=20 * SEC,
        noise_floor_db=-40.0,
        min_silence_us=500_000,
        spans=tuple(
            SilenceSpan(start_us=int(start * SEC), end_us=int(end * SEC)) for start, end in spans
        ),
    )


def _scene_evidence(cuts: list[float]) -> Any:
    from nexus_ai_agent.creative.packs.perception.models import SceneCutEvidence

    return SceneCutEvidence.seal(
        engine="ffmpeg 6.1.1",
        asset_sha256=CONTENT,
        duration_us=20 * SEC,
        threshold=0.3,
        cuts_us=tuple(int(cut * SEC) for cut in cuts),
    )


def _loudness_evidence(integrated: float | None) -> Any:
    from nexus_ai_agent.creative.packs.perception.models import LoudnessEvidence

    return LoudnessEvidence.seal(
        engine="ffmpeg 6.1.1",
        asset_sha256=CONTENT,
        duration_us=20 * SEC,
        integrated_lufs=integrated,
        loudness_range_lu=4.0,
        true_peak_dbfs=-1.0,
    )


def _loudness_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "asset_id": "asset_a",
        "evidence": _loudness_evidence(-14.0).model_dump(mode="json"),
        "target_lufs": -14.0,
        "tolerance_lu": 1.0,
    }
    payload.update(overrides)
    return payload


def _silence_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "track_id": "a1",
        "clip_id": "clip_a",
        "evidence": _silence_evidence([(2.0, 3.5)]).model_dump(mode="json"),
        "edge": "start",
    }
    payload.update(overrides)
    return payload


def _bus(registry: CapabilityRegistry | None = None) -> CommandBus:
    return CommandBus(_project(), registry=registry or build_production_registry())


def _dispatch(
    operation: str, payload: dict[str, Any], *, registry: CapabilityRegistry | None = None
) -> Any:
    command = TypedCommand(
        command_id=f"cmd_{abs(hash((operation, json.dumps(payload, sort_keys=True))))}",
        operation=operation,
        input=json.loads(json.dumps(payload)),
    )
    return _bus(registry).dispatch(command)


def test_the_production_registry_binds_the_gate() -> None:
    outcome = _dispatch("analysis.assess_loudness", _loudness_payload())
    assert outcome.output["gate"]["admitted"] is True
    assert outcome.output["gate"]["outcome"] == "pass"


def test_a_bare_registry_says_so_instead_of_claiming_a_decision() -> None:
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    outcome = _dispatch(
        "analysis.assess_loudness", _loudness_payload(), registry=build_runtime_registry()
    )
    assert outcome.output["gate"]["outcome"] == "not_configured"
    assert outcome.output["gate"]["admitted"] is None


def test_a_straddling_measurement_publishes_no_gain_through_the_runtime() -> None:
    payload = _loudness_payload(evidence=_loudness_evidence(-15.0).model_dump(mode="json"))
    outcome = _dispatch("analysis.assess_loudness", payload)
    record = outcome.output
    assert record["verdict"] == "refused"
    assert record["gain_db"] is None
    assert record["normalize_arguments"] is None
    assert record["gate"]["outcome"] == "abstain_insufficient_evidence"


def test_an_out_of_band_measurement_publishes_the_gain() -> None:
    payload = _loudness_payload(evidence=_loudness_evidence(-23.0).model_dump(mode="json"))
    outcome = _dispatch("analysis.assess_loudness", payload)
    assert outcome.output["verdict"] == "too_quiet"
    assert outcome.output["gain_db"] == pytest.approx(9.0)
    assert outcome.output["gate"]["outcome"] == "fail"


def test_a_license_conflict_refuses_the_loudness_operation() -> None:
    outcome = _dispatch(
        "analysis.assess_loudness",
        _loudness_payload(licenses=["NON_COMMERCIAL_ONLY"], commercial_intent=True),
    )
    assert outcome.output["verdict"] == "refused"
    assert outcome.output["gate"]["outcome"] == "license_conflict"


def test_a_license_conflict_publishes_no_markers_through_the_runtime() -> None:
    outcome = _dispatch(
        "analysis.mark_silences",
        _silence_payload(licenses=["NON_COMMERCIAL_ONLY"], commercial_intent=True),
    )
    assert outcome.output["markers_added"] == 0
    assert outcome.output["refused"] is True
    assert outcome.output["gate"]["outcome"] == "license_conflict"


def test_a_compatible_license_publishes_the_markers() -> None:
    outcome = _dispatch(
        "analysis.mark_silences", _silence_payload(licenses=["PERMISSIVE_MIT_APACHE"])
    )
    assert outcome.output["markers_added"] == 1  # edge == "start" publishes one marker
    assert outcome.output["gate"]["outcome"] == "license_ok"


def test_an_undeclared_license_makes_no_claim() -> None:
    outcome = _dispatch("analysis.mark_silences", _silence_payload())
    assert outcome.output["markers_added"] > 0
    assert outcome.output["gate"]["outcome"] == "no_license_declared"


def test_the_pack_refuses_an_unknown_license_name_before_dispatch() -> None:
    from nexus_ai_agent.creative.packs.perception.models import MarkSilencesInput

    with pytest.raises(ValidationError):
        MarkSilencesInput.model_validate(_silence_payload(licenses=["MIT"]))


def test_the_gate_record_is_part_of_every_operation_output() -> None:
    scene = _dispatch(
        "analysis.mark_scene_cuts",
        {
            "track_id": "a1",
            "clip_id": "clip_a",
            "evidence": _scene_evidence([5.0]).model_dump(mode="json"),
        },
    )
    assert scene.output["gate"]["outcome"] == "no_license_declared"
    loudness = _dispatch("analysis.assess_loudness", _loudness_payload())
    assert "gate" in loudness.output


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def test_the_composition_root_passes_the_gate() -> None:
    root = REPO_ROOT / "src/nexus_ai_agent/creative/production_runtime.py"
    calls = _calls(root, "_build_runtime_registry")
    assert calls, "creative/production_runtime.py no longer builds the runtime registry"
    assert all(
        any(keyword.arg == "perception_gate" for keyword in call.keywords) for call in calls
    ), "the production registry is built without the perception gate"


@pytest.mark.parametrize(
    "relative",
    [
        "src/nexus_ai_agent/creative/render_jobs.py",
        "src/nexus_ai_agent/jobs/creative_passport.py",
        "src/nexus_ai_agent/nagar/sources.py",
        "src/nexus_ai_agent/cli.py",
    ],
)
def test_production_callers_do_not_build_an_ungated_registry(relative: str) -> None:
    path = REPO_ROOT / relative
    assert not _calls(path, "build_runtime_registry"), f"{relative} builds an ungated registry"
    assert not _calls(path, "build_pack_registry"), f"{relative} builds an ungated pack registry"


def test_the_pack_substrate_still_cannot_import_the_gate() -> None:
    """The whole port exists because the substrate law forbids that import."""
    tree = ast.parse(
        (REPO_ROOT / "src/nexus_ai_agent/creative/packs/runtime.py").read_text(encoding="utf-8")
    )
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(module.startswith("nexus_ai_agent.creative.verification") for module in imported)
