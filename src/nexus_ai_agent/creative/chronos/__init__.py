"""Deterministic, offline-first media physics kernels and concrete handler factory."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .contracts import Continuity, KernelError, RationalTimestamp


def build_default_chronos_handlers() -> dict[str, Callable[..., Any]]:
    """Build local adapters for the Studio port's six canonical Wave-1 operations.

    The Studio module stays dependency-free: composition direction is Chronos adapter
    to pure Studio port. Each adapter accepts the immutable command protocol.
    """
    import hashlib
    from pathlib import Path

    from nexus_ai_agent.creative.studio.chronos_port import ChronosExecutionReceipt

    from . import acoustics_lipsync as acoustic
    from . import color_science as color
    from . import geometry, timeline
    from .verifier import verify_persisted_relighting

    def receipt(
        command: Any, output: Path, digest: str, claim: str, metadata: dict[str, str]
    ) -> Any:
        return ChronosExecutionReceipt(command.operation, str(output), digest, claim, metadata)

    def wav(command: Any) -> Any:
        paths = [Path(item) for item in command.inputs]
        output = Path(command.parameters["output"])
        result = timeline.assemble_pcm_wav_stream_copy(
            paths, output, Path(command.parameters["staging_dir"])
        )
        return receipt(
            command,
            output,
            result.sha256,
            result.quality_claim,
            {"verification_mode": result.verification_mode},
        )

    def ffmpeg(command: Any) -> Any:
        output = Path(command.parameters["output"])
        result = timeline.concat_copy([Path(item) for item in command.inputs], output)
        return receipt(
            command,
            output,
            result.sha256,
            result.quality_claim,
            {"duration": str(result.duration_seconds)},
        )

    def scale(command: Any) -> Any:
        p = command.parameters
        import numpy as np

        k = np.array(
            [[float(p["fx"]), 0, float(p["cx"])], [0, float(p["fy"]), float(p["cy"])], [0, 0, 1]]
        )
        result = geometry.backproject_character_height_m(
            (float(p["top_u"]), float(p["top_v"])),
            (float(p["bottom_u"]), float(p["bottom_v"])),
            float(p["depth_m"]),
            k,
        )
        output = Path(p["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(str(result), encoding="utf-8")
        return receipt(
            command,
            output,
            hashlib.sha256(output.read_bytes()).hexdigest(),
            "WORLD_SPACE_CHARACTER_SCALE",
            {"height_m": str(result.world_height_m)},
        )

    def relight(command: Any) -> Any:
        paths = [Path(item) for item in command.inputs]
        source, target, reference = (
            color.read_rgb8_png(path).astype(float) / 255 for path in paths[:3]
        )
        output = Path(command.parameters["output"])
        result = verify_persisted_relighting(source, target, reference, output)
        return receipt(
            command,
            output,
            result[0],
            "PERSISTED_COLOR_AND_STRUCTURE_VERIFIED",
            {"structure_score": str(result[3])},
        )

    def spatial(command: Any) -> Any:
        dry, rir = (np_load(item) for item in command.inputs[:2])
        rendered, telemetry = acoustic.render_spatial_dialogue(dry, rir)
        output = Path(command.parameters["output"])
        np_save(output, rendered)
        return receipt(
            command,
            output,
            hashlib.sha256(output.read_bytes()).hexdigest(),
            "SPATIAL_ROOM_IR_RENDERED",
            {"drr_db": str(telemetry.drr_db)},
        )

    def lipsync(command: Any) -> Any:
        first, second = (np_load(item) for item in command.inputs[:2])
        result = acoustic.measure_cepstral_timbre_consistency(
            first, second, int(command.parameters["sample_rate_hz"])
        )
        output = Path(command.parameters["output"])
        output.write_text(str(result), encoding="utf-8")
        return receipt(
            command,
            output,
            hashlib.sha256(output.read_bytes()).hexdigest(),
            "ACOUSTIC_TIMBRE_CONSISTENCY",
            {"status": result.status, "distance": str(result.mean_distance_db)},
        )

    def np_load(path: str) -> Any:
        import numpy as np

        return np.load(path, allow_pickle=False)

    def np_save(path: Path, value: Any) -> None:
        import numpy as np

        with path.open("wb") as stream:
            np.save(stream, value, allow_pickle=False)

    return {
        "timeline.assemble_wav": wav,
        "timeline.assemble_ffmpeg": ffmpeg,
        "geometry.project_and_verify_scale": scale,
        "color.relight_png": relight,
        "acoustics.render_spatial_dialogue": spatial,
        "acoustics.verify_timbre_and_lipsync": lipsync,
    }


__all__ = ["Continuity", "KernelError", "RationalTimestamp", "build_default_chronos_handlers"]
