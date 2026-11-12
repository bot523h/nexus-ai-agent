"""Deterministic, offline-first media physics kernels and concrete handler factory."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from .contracts import Continuity, KernelError, RationalTimestamp


def build_default_chronos_handlers() -> dict[str, Callable[..., Any]]:
    """Build local adapters for the Studio port's six canonical Wave-1 operations.

    The Studio module stays dependency-free: composition direction is Chronos adapter
    to pure Studio port. Each adapter accepts the immutable command protocol.
    """
    import hashlib
    import json
    from pathlib import Path

    import numpy as np

    from nexus_ai_agent.creative.studio.chronos_port import ChronosExecutionReceipt

    from . import acoustics_lipsync as acoustic
    from . import (
        audio_continuity,
        compositor_25d,
        geometry,
        identity_lock,
        merkle_ledger,
        scene_portrait_ops,
        timeline,
    )
    from . import color_science as color
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

    def file_receipt(command: Any, output: Path, claim: str, metadata: dict[str, str]) -> Any:
        output.parent.mkdir(parents=True, exist_ok=True)
        return receipt(
            command, output, hashlib.sha256(output.read_bytes()).hexdigest(), claim, metadata
        )

    def wave2(command: Any) -> Any:
        p = command.parameters
        op = command.operation
        result: Any
        output = Path(p.get("output", p.get("tiled_output", "chronos-output")))
        if op == "audio.declick_wav":
            result = audio_continuity.declick_pcm_wav_splices(
                command.inputs[0],
                output,
                [int(v) for v in p["splice_frames"].split(",")],
                bridge_frames=int(p.get("bridge_frames", "32")),
            )
            return file_receipt(
                command,
                output,
                "PCM_SPLICE_DECLICKED",
                {
                    "sample_frames": str(result["sample_frames"]),
                    "max_step_after": str(result["max_step_after"]),
                },
            )
        if op == "audio.duck_ambience":
            result, snr = audio_continuity.duck_ambience_under_dialogue(
                np_load(command.inputs[0]), np_load(command.inputs[1]), int(p["sample_rate_hz"])
            )
            np_save(output, result)
            return file_receipt(
                command, output, "DIALOGUE_SIDECHAIN_DUCKED", {"snr_gain_db": str(snr)}
            )
        if op == "identity.verify_pose":
            result = identity_lock.verify_pose_invariant_identity(
                np_load(command.inputs[0]),
                np_load(command.inputs[1]),
                np_load(command.inputs[2]),
                float(p["depth_m"]),
            )
            output.write_text(repr(result), encoding="utf-8")
            return file_receipt(
                command,
                output,
                result.scope,
                {
                    "rms_mm": str(result.rms_residual_mm),
                    "mahalanobis": str(result.mahalanobis_distance),
                },
            )
        if op == "identity.verify_eyeline":
            vectors = [np_load(item) for item in command.inputs[:6]]
            result = identity_lock.verify_eyeline_and_180_rule(
                *vectors, angular_tolerance_deg=float(p.get("tolerance_deg", "3.5"))
            )
            output.write_text(repr(result), encoding="utf-8")
            return file_receipt(
                command,
                output,
                "EYELINE_VERIFIED",
                {"accepted": str(result.accepted), "miss_m": str(result.gaze_ray_miss_distance_m)},
            )
        if op.startswith("compositor."):
            data = np.load(command.inputs[0], allow_pickle=False)
            layer_list = [
                (data[f"rgb{i}"], data[f"alpha{i}"], float(data[f"depth{i}"]))
                for i in range(int(p["layer_count"]))
            ]
            haze = np_load(command.inputs[1])
            focus = cast(
                tuple[float, float, float, float, int],
                tuple(float(p[k]) for k in ("focus_m", "focal_mm", "f_number", "sensor_mm"))
                + (int(p["width_px"]),),
            )
            if op == "compositor.render_25d":
                result = compositor_25d.render_25d_multiplane_composite(
                    layer_list,
                    haze,
                    output,
                    beta=cast(
                        tuple[float, float, float],
                        tuple(float(v) for v in p["beta_rgb"].split(",")),
                    ),
                    focus=focus,
                    tile_size_px=int(p.get("tile_size_px", "32")),
                )
            else:
                result = compositor_25d.verify_tiled_compositor_equivalence(
                    layer_list,
                    haze,
                    output,
                    Path(p["full_output"]),
                    beta=float(p["beta"]),
                    focus=focus,
                    tile_size_px=int(p.get("tile_size_px", "32")),
                )
            return receipt(
                command,
                output,
                result.sha256,
                "TILED_COMPOSITE_VERIFIED",
                {
                    "equivalent": str(result.equivalent),
                    "working_bytes": str(result.max_tile_window_working_bytes),
                },
            )
        if op in {"scene.inpaint", "scene.retouch"}:
            image, mask = np_load(command.inputs[0]), np_load(command.inputs[1]).astype(bool)
            if op == "scene.inpaint":
                inpaint_receipt = scene_portrait_ops.inpaint_masked_region_pde(image, mask)
                result = inpaint_receipt.image
                meta = {
                    "dirichlet_energy_before": str(inpaint_receipt.dirichlet_energy_before),
                    "dirichlet_energy_after": str(inpaint_receipt.dirichlet_energy_after),
                    "biharmonic_rms_residual": str(inpaint_receipt.biharmonic_rms_residual),
                }
            else:
                result = scene_portrait_ops.retouch_portrait_frequency_separation(image, mask)
                meta = {}
            np_save(output, result)
            return file_receipt(command, output, "SCENE_IMAGE_OPERATION", meta)
        if op == "scene.sky":
            result = scene_portrait_ops.synthesize_physical_sky_dome(
                int(p["width"]),
                int(p["height"]),
                float(p["solar_elevation_deg"]),
                camera_pitch_deg=float(p.get("camera_pitch_deg", "0")),
            )
            np_save(output, result)
            return file_receipt(command, output, "PROCEDURAL_SKY", {})
        if op == "scene.tonemap":
            result = scene_portrait_ops.tonemap_scene_linear_filmic(
                np_load(command.inputs[0]), halation_strength=float(p.get("halation_strength", "0"))
            )
            np_save(output, result)
            return file_receipt(command, output, "FILMIC_TONE_MAPPED", {})
        if op == "merkle.root":
            leaves = json.loads(Path(command.inputs[0]).read_text(encoding="utf-8"))
            output.write_text(merkle_ledger.merkle_root(leaves).hex(), encoding="ascii")
            return file_receipt(command, output, "MERKLE_ROOT", {"leaf_count": str(len(leaves))})
        payload = json.loads(Path(command.inputs[0]).read_text(encoding="utf-8"))
        raw = payload["proof"]
        proof = merkle_ledger.InclusionProof(
            int(raw["leaf_index"]),
            int(raw["tree_size"]),
            tuple((bool(left), bytes.fromhex(digest)) for left, digest in raw["audit_path"]),
        )
        verified = merkle_ledger.verify_inclusion_proof(
            payload["receipt"], proof, bytes.fromhex(payload["root"])
        )
        output.write_text(str(verified), encoding="ascii")
        return file_receipt(command, output, "MERKLE_INCLUSION", {"verified": str(verified)})

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
        "audio.declick_wav": wave2,
        "audio.duck_ambience": wave2,
        "identity.verify_pose": wave2,
        "identity.verify_eyeline": wave2,
        "compositor.render_25d": wave2,
        "compositor.verify_equivalence": wave2,
        "scene.inpaint": wave2,
        "scene.retouch": wave2,
        "scene.sky": wave2,
        "scene.tonemap": wave2,
        "merkle.root": wave2,
        "merkle.verify": wave2,
    }


__all__ = ["Continuity", "KernelError", "RationalTimestamp", "build_default_chronos_handlers"]
