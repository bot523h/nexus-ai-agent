"""Cross-pack contract: perception advice must be accepted by ``audio.normalize_loudness``."""

from __future__ import annotations

import hashlib

from nexus_ai_agent.creative.packs.audio.models import NormalizeLoudnessInput
from nexus_ai_agent.creative.packs.perception.models import (
    OPERATION_ASSESS_LOUDNESS,
    LoudnessEvidence,
)
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Timeline,
    TypedCommand,
    new_project,
)

SHA = "sha256:" + hashlib.sha256(b"x").hexdigest()


def test_normalize_arguments_are_a_valid_audio_normalize_loudness_input() -> None:
    registry = build_runtime_registry()
    project = new_project("p", "P", Timeline(timeline_id="t", duration_us=1_000_000)).model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="song", media_kind="audio", content_sha256=SHA, duration_us=1_000_000
                )
            ]
        }
    )
    bus = CommandBus(project, registry=registry)
    evidence = LoudnessEvidence.seal(
        engine="ffmpeg 6.1.1",
        asset_sha256=SHA,
        duration_us=1_000_000,
        integrated_lufs=-23.0,
        loudness_range_lu=3.0,
        true_peak_dbfs=-6.0,
    )
    out = bus.dispatch(
        TypedCommand(
            command_id="assess",
            operation=OPERATION_ASSESS_LOUDNESS,
            input={"asset_id": "song", "evidence": evidence.model_dump(mode="json")},
        )
    ).output
    arguments = out["normalize_arguments"]
    parsed = NormalizeLoudnessInput.model_validate({**arguments, "confirmed": True})
    assert parsed.audio_asset_id == "song"
    assert parsed.target_lufs == arguments["target_lufs"]
