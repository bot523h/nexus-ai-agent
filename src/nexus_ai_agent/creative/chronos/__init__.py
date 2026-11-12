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
        biomechanics_motion,
        compositor_25d,
        episode_pipeline,
        geometry,
        identity_lock,
        merkle_ledger,
        scene_portrait_ops,
        temporal_optics,
        timeline,
        weather_and_grading,
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
        if op == "motion.solve_ik":
            data = np.load(command.inputs[0], allow_pickle=False)
            limits = (
                float(p.get("joint_min_rad", "-3.141592653589793")),
                float(p.get("joint_max_rad", "3.141592653589793")),
            )
            result = biomechanics_motion.solve_two_bone_or_chain_ik(
                data["root"],
                tuple(float(value) for value in p["segment_lengths_m"].split(",")),
                data["target"],
                joint_angle_limits_rad=limits,
                clamp_unreachable=p.get("clamp_unreachable", "false").lower() == "true",
            )
            output.write_text(repr(result), encoding="utf-8")
            return file_receipt(
                command,
                output,
                "DAMPED_LEAST_SQUARES_IK",
                {"residual_m": str(result.target_residual_m), "reachable": str(result.reachable)},
            )
        if op == "motion.foot_lock":
            from fractions import Fraction

            data = np.load(command.inputs[0], allow_pickle=False)
            pts = tuple(int(value) for value in p["pts"].split(","))
            time_base = Fraction(int(p["time_base_num"]), int(p["time_base_den"]))
            stamps = tuple(RationalTimestamp(value, time_base) for value in pts)
            result = biomechanics_motion.enforce_stance_foot_lock(
                stamps,
                data["positions"],
                data["stance"].astype(bool),
                transition_frames=int(p.get("transition_frames", "2")),
            )
            output.write_text(repr(result), encoding="utf-8")
            return file_receipt(
                command,
                output,
                "STANCE_FOOT_LOCKED",
                {
                    "slip_before_mm": str(result.max_foot_slip_mm_before),
                    "slip_after_mm": str(result.max_foot_slip_mm_after),
                },
            )
        if op == "motion.verlet_chain":
            from fractions import Fraction

            data = np.load(command.inputs[0], allow_pickle=False)
            pts = tuple(int(value) for value in p["pts"].split(","))
            time_base = Fraction(int(p["time_base_num"]), int(p["time_base_den"]))
            stamps = tuple(RationalTimestamp(value, time_base) for value in pts)
            result = biomechanics_motion.simulate_secondary_verlet_chain(
                stamps,
                data["root_positions"],
                data["initial_chain"],
                gravity_m_s2=tuple(float(v) for v in p.get("gravity", "0,0,-9.81").split(",")),
                wind_m_s2=tuple(float(v) for v in p.get("wind", "0,0,0").split(",")),
                drag=float(p.get("drag", "0.02")),
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("wb") as stream:
                np.savez(
                    stream,
                    positions=np.asarray(result.positions),
                    strain=result.max_segment_strain,
                    rest_lengths=np.asarray(result.rest_lengths_m),
                )
            return file_receipt(
                command,
                output,
                "VERLET_CHAIN_SIMULATED",
                {"max_segment_strain": str(result.max_segment_strain)},
            )
        if op == "motion.shutter_blur":
            result = biomechanics_motion.render_shutter_motion_blur_linear(
                np_load(command.inputs[0]),
                (float(p["velocity_x_px_per_frame"]), float(p["velocity_y_px_per_frame"])),
                shutter_angle_deg=float(p.get("shutter_angle_deg", "180")),
                subframe_samples=int(p.get("subframe_samples", "9")),
            )
            np_save(output, result.image)
            return file_receipt(
                command,
                output,
                "SCENE_LINEAR_SHUTTER_BLUR",
                {
                    "energy_relative_error": str(result.energy_relative_error),
                    "shutter_fraction": str(result.shutter_fraction),
                },
            )
        if op == "temporal.phase_corr":
            result = temporal_optics.estimate_subpixel_translation_phase_corr(
                np_load(command.inputs[0]), np_load(command.inputs[1])
            )
            output.write_text(repr(result), encoding="utf-8")
            return file_receipt(
                command,
                output,
                "PHASE_CORRELATION_TRANSLATION",
                {"dx_px": str(result.translation_x_px), "dy_px": str(result.translation_y_px)},
            )
        if op == "temporal.deflicker":
            result = temporal_optics.stabilize_temporal_exposure_flicker(
                np_load(command.inputs[0]), smoothing_radius=int(p.get("smoothing_radius", "3"))
            )
            np_save(output, result.frames)
            return file_receipt(
                command,
                output,
                "TEMPORAL_EXPOSURE_STABILIZED",
                {
                    "variance_before": str(result.temporal_flicker_variance_before),
                    "variance_after": str(result.temporal_flicker_variance_after),
                    "meets_target": str(result.meets_variance_target),
                },
            )
        if op == "temporal.rectify_rolling":
            result = temporal_optics.rectify_rolling_shutter_skew(
                np_load(command.inputs[0]),
                (float(p["velocity_x_px_per_second"]), float(p["velocity_y_px_per_second"])),
                readout_duration_s=float(p["readout_duration_s"]),
                frame_period_s=float(p["frame_period_s"]),
            )
            np_save(output, result.frame)
            return file_receipt(
                command,
                output,
                "ROLLING_SHUTTER_RECTIFIED",
                {
                    "max_warp_px": str(result.max_warp_px),
                    "readout_fraction": str(result.readout_fraction),
                },
            )
        if op == "weather.precipitation":
            result = weather_and_grading.render_physical_precipitation_layer(
                int(p["width_px"]),
                int(p["height_px"]),
                float(p["focal_length_y_px"]),
                float(p["particle_depth_m"]),
                float(p["terminal_velocity_m_s"]),
                frame_rate_fps=float(p["frame_rate_fps"]),
                shutter_angle_deg=float(p.get("shutter_angle_deg", "180")),
                wind_velocity_m_s=tuple(float(v) for v in p.get("wind_m_s", "0,0").split(",")),
                particle_count=int(p.get("particle_count", "64")),
                extinction_per_m=float(p.get("extinction_per_m", "0")),
                seed=int(p.get("seed", "0")),
            )
            np_save(output, result.layer)
            return file_receipt(
                command,
                output,
                "PHYSICAL_PRECIPITATION_LAYER",
                {
                    "vertical_streak_length_px": str(result.vertical_streak_length_px),
                    "beer_lambert_attenuation": str(result.beer_lambert_attenuation),
                },
            )
        if op == "grading.apply_cdl_lut":

            def vector(name: str, default: str) -> tuple[float, ...]:
                return tuple(float(value) for value in p.get(name, default).split(","))

            lut = (
                weather_and_grading.parse_cube_lut(
                    Path(command.inputs[1]).read_text(encoding="utf-8")
                )
                if len(command.inputs) > 1
                else None
            )
            result = weather_and_grading.apply_asc_cdl_and_3d_lut(
                np_load(command.inputs[0]),
                slope=vector("slope", "1,1,1"),
                offset=vector("offset", "0,0,0"),
                power=vector("power", "1,1,1"),
                saturation=float(p.get("saturation", "1")),
                lut=lut,
            )
            np_save(output, result)
            return file_receipt(
                command, output, "ASC_CDL_AND_3D_LUT", {"lut_size": str(lut.size if lut else 0)}
            )
        if op == "grading.serialize_cube":
            lut = weather_and_grading.parse_cube_lut(
                Path(command.inputs[0]).read_text(encoding="utf-8")
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(weather_and_grading.serialize_cube_lut(lut), encoding="utf-8")
            return file_receipt(command, output, "CUBE_LUT_SERIALIZED", {"size": str(lut.size)})
        if op == "grading.legalize_ebu_r103":
            result = weather_and_grading.legalize_broadcast_rgb_ebu_r103(np_load(command.inputs[0]))
            np_save(output, result.image)
            return file_receipt(
                command,
                output,
                "EBU_R103_LEGALIZED",
                {
                    "illegal_before": str(result.illegal_pixel_fraction_before),
                    "illegal_after": str(result.illegal_pixel_fraction_after),
                    "sobel_correlation": str(result.sobel_structure_correlation),
                },
            )
        if op == "episode.verify_show_bible":
            from dataclasses import asdict

            manifest_path = Path(command.inputs[0])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            bible = episode_pipeline.ShowBibleCharacterBible(**payload["show_bible"])
            observations = [
                episode_pipeline.ShowBibleShotObservation(**item)
                for item in payload["observations"]
            ]
            result = episode_pipeline.verify_cross_episode_show_bible(bible, observations)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(asdict(result), sort_keys=True), encoding="utf-8")
            return file_receipt(
                command,
                output,
                "SHOW_BIBLE_CONTINUITY",
                {"shots": str(result.observations_checked)},
            )
        if op == "episode.run_verified":
            from dataclasses import asdict
            from fractions import Fraction

            manifest_path = Path(command.inputs[0]).resolve()
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            bible = episode_pipeline.ShowBibleCharacterBible(**payload["show_bible"])

            def asset(value: str) -> Path:
                item = Path(value)
                return item if item.is_absolute() else manifest_path.parent / item

            shots = []
            for record in payload["shots"]:
                stamps = tuple(
                    RationalTimestamp(
                        int(item["pts"]),
                        Fraction(int(item["time_base_num"]), int(item["time_base_den"])),
                    )
                    for item in record["foot_timestamps"]
                )
                shots.append(
                    episode_pipeline.EpisodeShotInput(
                        shot_index=int(record["shot_index"]),
                        scene_linear_rgb=np.asarray(record["scene_linear_rgb"], dtype=np.float64),
                        alpha=np.asarray(record["alpha"], dtype=np.float64),
                        depth_m=float(record["depth_m"]),
                        canonical_landmarks_m=np.asarray(
                            record["canonical_landmarks_m"], dtype=np.float64
                        ),
                        observed_landmarks_3d_m=np.asarray(
                            record["observed_landmarks_3d_m"], dtype=np.float64
                        ),
                        observed_landmarks_px=np.asarray(
                            record["observed_landmarks_px"], dtype=np.float64
                        ),
                        intrinsics=np.asarray(record["intrinsics"], dtype=np.float64),
                        camera_depth_m=float(record["camera_depth_m"]),
                        top_px=cast(tuple[float, float], tuple(float(v) for v in record["top_px"])),
                        bottom_px=cast(
                            tuple[float, float], tuple(float(v) for v in record["bottom_px"])
                        ),
                        camera_a=np.asarray(record["camera_a"], dtype=np.float64),
                        camera_b=np.asarray(record["camera_b"], dtype=np.float64),
                        actor_a=np.asarray(record["actor_a"], dtype=np.float64),
                        actor_b=np.asarray(record["actor_b"], dtype=np.float64),
                        gaze_a=np.asarray(record["gaze_a"], dtype=np.float64),
                        gaze_b=np.asarray(record["gaze_b"], dtype=np.float64),
                        ik_root_position=np.asarray(record["ik_root_position"], dtype=np.float64),
                        ik_segment_lengths_m=tuple(
                            float(v) for v in record["ik_segment_lengths_m"]
                        ),
                        ik_effector_target=np.asarray(
                            record["ik_effector_target"], dtype=np.float64
                        ),
                        foot_timestamps=stamps,
                        ankle_positions_m=np.asarray(record["ankle_positions_m"], dtype=np.float64),
                        stance_contact=np.asarray(record["stance_contact"], dtype=bool),
                        cepstral_envelope=tuple(float(v) for v in record["cepstral_envelope"]),
                        skin_tone_lab=cast(
                            tuple[float, float, float],
                            tuple(float(v) for v in record["skin_tone_lab"]),
                        ),
                        dialogue_wav_path=asset(record["dialogue_wav_path"]),
                        ambience_wav_path=asset(record["ambience_wav_path"]),
                        speaker_position_m=cast(
                            tuple[float, float, float],
                            tuple(float(v) for v in record["speaker_position_m"]),
                        ),
                        camera_position_m=cast(
                            tuple[float, float, float],
                            tuple(float(v) for v in record["camera_position_m"]),
                        ),
                        camera_right_unit=cast(
                            tuple[float, float, float],
                            tuple(float(v) for v in record.get("camera_right_unit", (1, 0, 0))),
                        ),
                        room_dimensions_m=cast(
                            tuple[float, float, float],
                            tuple(float(v) for v in record.get("room_dimensions_m", (6, 5, 3))),
                        ),
                        room_absorption=float(record.get("room_absorption", 0.35)),
                    )
                )
            raw_dag = payload.get("shot_dag")
            dag = (
                None
                if raw_dag is None
                else episode_pipeline.ShotDAG(
                    Continuity(raw_dag.get("continuity", "OK")),
                    raw_dag.get("previous_input_hashes", {}),
                    raw_dag.get("current_input_hashes", {}),
                    {key: tuple(value) for key, value in raw_dag.get("dependencies", {}).items()},
                )
            )
            result = episode_pipeline.run_verified_episode_continuity_pipeline(
                int(payload["episode_index"]), shots, bible, p["staging_dir"], shot_dag=dag
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(asdict(result), sort_keys=True), encoding="utf-8")
            return file_receipt(
                command,
                output,
                "VERIFIED_EPISODE_CONTINUITY_PASSPORT",
                {
                    "merkle_root_sha256": result.merkle_root_sha256,
                    "published_wav_sha256": result.published_wav_sha256,
                },
            )
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
        "motion.solve_ik": wave2,
        "motion.foot_lock": wave2,
        "motion.verlet_chain": wave2,
        "motion.shutter_blur": wave2,
        "temporal.phase_corr": wave2,
        "temporal.deflicker": wave2,
        "temporal.rectify_rolling": wave2,
        "weather.precipitation": wave2,
        "grading.apply_cdl_lut": wave2,
        "grading.serialize_cube": wave2,
        "grading.legalize_ebu_r103": wave2,
        "episode.verify_show_bible": wave2,
        "episode.run_verified": wave2,
    }


__all__ = ["Continuity", "KernelError", "RationalTimestamp", "build_default_chronos_handlers"]
