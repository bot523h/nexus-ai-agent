from __future__ import annotations

import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos.audio_continuity import (
    declick_pcm_wav_splices,
    duck_ambience_under_dialogue,
)
from nexus_ai_agent.creative.chronos.merkle_ledger import (
    build_inclusion_proof,
    merkle_root,
    verify_inclusion_proof,
)


def test_merkle_inclusion_proofs_and_tamper() -> None:
    receipts = [{"shot": i, "sha": f"{i:064x}"} for i in range(7)]
    root = merkle_root(receipts)
    for i, item in enumerate(receipts):
        proof = build_inclusion_proof(receipts, i)
        assert verify_inclusion_proof(item, proof, root)
    altered = dict(receipts[3], shot=88)
    assert not verify_inclusion_proof(altered, build_inclusion_proof(receipts, 3), root)
    with pytest.raises(ValueError):
        build_inclusion_proof(receipts, 7)


def test_audio_declick_preserves_disk_sample_count_and_ducking_improves_snr(
    tmp_path: Path,
) -> None:
    rate, count = 8000, 1000
    signal = np.zeros(count, dtype=np.int16)
    signal[:500] = 12000
    signal[500:] = -12000
    source, output = tmp_path / "in.wav", tmp_path / "out.wav"
    with wave.open(str(source), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(signal.astype("<i2").tobytes())
    result = declick_pcm_wav_splices(source, output, [500], bridge_frames=32)
    assert result["sample_frames"] == count
    after = cast(tuple[float, ...], result["boundary_steps_after"])
    before = cast(tuple[float, ...], result["boundary_steps_before"])
    assert after[0] < before[0]
    with wave.open(str(output), "rb") as reader:
        assert reader.getnframes() == count
    dialogue = np.zeros(2048)
    dialogue[500:1500] = 0.5
    ducked, gain_db = duck_ambience_under_dialogue(dialogue, np.full(2048, 0.1), rate)
    assert ducked.shape == dialogue.shape and gain_db > 0


def test_identity_pose_fit_and_eyeline() -> None:
    from nexus_ai_agent.creative.chronos.identity_lock import (
        verify_eyeline_and_180_rule,
        verify_pose_invariant_identity,
    )

    points = np.array(
        [[0.0, 0.0, 0.0], [0.06, 0.0, 0.0], [0.0, 0.04, 0.0], [0.02, 0.03, 0.0], [0.04, 0.04, 0.0]]
    )
    k = np.array([[1000.0, 0.0, 320.0], [0.0, 1000.0, 240.0], [0.0, 0.0, 1.0]])
    obs = np.column_stack((320 + points[:, 0] * 500, 240 + points[:, 1] * 500))
    receipt = verify_pose_invariant_identity(points, obs, k, 2.0)
    assert receipt.scope == "GEOMETRIC_CRANIOFACIAL_INVARIANCE"
    assert receipt.rms_residual_mm < 1e-6
    assert np.linalg.det(np.asarray(receipt.rotation_so3)) > 0.99
    assert not verify_eyeline_and_180_rule(
        np.array([0.0, -2.0, 1.0]),
        np.array([0.0, -2.0, -1.0]),
        np.array([0.0, 0.0, 0.0]),
        np.array([0.0, 10.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, -1.0, 0.0]),
    )


def test_25d_render_and_scene_portrait_ops(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos.compositor_25d import verify_tiled_compositor_equivalence
    from nexus_ai_agent.creative.chronos.scene_portrait_ops import (
        inpaint_masked_region_pde,
        retouch_portrait_frequency_separation,
        synthesize_physical_sky_dome,
        tonemap_scene_linear_filmic,
    )

    image = np.full((8, 8, 3), 0.3)
    alpha = np.ones((8, 8))
    layer = image.copy()
    layer[3:5, 3:5] = 0.9
    focus = (3.0, 50.0, 2.8, 36.0, 64)
    assert verify_tiled_compositor_equivalence(
        [(layer, alpha, 4.0)],
        image,
        tmp_path / "tile.png",
        tmp_path / "full.png",
        beta=0.01,
        focus=focus,
    )
    mask = np.zeros((8, 8), dtype=bool)
    mask[3:5, 3:5] = True
    fixed = image.copy()
    fixed[3:5, 3:5] = 1.0
    healed, before, after = inpaint_masked_region_pde(fixed, mask, iterations=50)
    assert healed.shape == fixed.shape and after <= before
    assert retouch_portrait_frequency_separation(fixed, mask).shape == fixed.shape
    mapped = tonemap_scene_linear_filmic(synthesize_physical_sky_dome(8, 6, 20.0) * 20)
    assert mapped.min() >= 0 and mapped.max() <= 1


def test_handler_factory_has_port_operations() -> None:
    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.studio.chronos_port import ChronosPort

    handlers = build_default_chronos_handlers()
    assert set(handlers) == ChronosPort.OPERATIONS


def test_default_wav_handler_real_disk(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand, ChronosPort

    files = []
    for n in range(2):
        path = tmp_path / f"{n}.wav"
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(np.arange(16, dtype="<i2").tobytes())
        files.append(str(path))
    output = tmp_path / "joined.wav"
    cmd = ChronosCommand(
        "timeline.assemble_wav",
        tuple(files),
        {"output": str(output), "staging_dir": str(tmp_path / "stage")},
    )
    result = ChronosPort(build_default_chronos_handlers()).execute(cmd)
    assert output.exists() and len(result.sha256) == 64


def test_factory_geometry_color_and_acoustic_adapters(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.chronos.color_science import write_rgb8_png
    from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand, ChronosPort

    handlers = build_default_chronos_handlers()
    port = ChronosPort(handlers)
    scale_out = tmp_path / "scale.txt"
    p = {
        "output": str(scale_out),
        "fx": "1000",
        "fy": "1000",
        "cx": "320",
        "cy": "240",
        "top_u": "320",
        "top_v": "200",
        "bottom_u": "320",
        "bottom_v": "300",
        "depth_m": "2",
    }
    assert port.execute(ChronosCommand("geometry.project_and_verify_scale", ("unused",), p)).sha256
    yy, xx = np.mgrid[0:16, 0:16]
    gray = 0.4 + 0.15 * np.sin(xx / 3) * np.cos(yy / 4)
    tgt = np.repeat(gray[:, :, None], 3, axis=2)
    src = tgt * 0.8 + 0.05
    pngs = [tmp_path / f"c{i}.png" for i in range(3)]
    for path, pixels in zip(pngs, (src, tgt, tgt), strict=True):
        write_rgb8_png(path, np.rint(pixels * 255).astype(np.uint8))
    color_out = tmp_path / "relit.png"
    assert port.execute(
        ChronosCommand("color.relight_png", tuple(map(str, pngs)), {"output": str(color_out)})
    ).sha256
    dry = tmp_path / "dry.npy"
    rir = tmp_path / "rir.npy"
    np.save(dry, np.ones(64))
    impulse = np.zeros((16, 2))
    impulse[0] = 1
    np.save(rir, impulse)
    room_out = tmp_path / "room.npy"
    assert port.execute(
        ChronosCommand(
            "acoustics.render_spatial_dialogue", (str(dry), str(rir)), {"output": str(room_out)}
        )
    ).sha256
    voice = tmp_path / "voice.npy"
    tone = 0.2 * np.sin(2 * np.pi * 120 * np.arange(16000) / 16000)
    np.save(voice, tone)
    jaw_out = tmp_path / "timbre.txt"
    assert port.execute(
        ChronosCommand(
            "acoustics.verify_timbre_and_lipsync",
            (str(voice), str(voice)),
            {"output": str(jaw_out), "sample_rate_hz": "16000"},
        )
    ).sha256


def test_popperian_falsification_20_probes(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos.color_science import cct_kelvin_to_xyz, read_rgb8_png
    from nexus_ai_agent.creative.chronos.compositor_25d import render_25d_multiplane_composite
    from nexus_ai_agent.creative.chronos.contracts import KernelError
    from nexus_ai_agent.creative.chronos.identity_lock import (
        verify_eyeline_and_180_rule,
        verify_pose_invariant_identity,
    )
    from nexus_ai_agent.creative.chronos.scene_portrait_ops import (
        inpaint_masked_region_pde,
        synthesize_physical_sky_dome,
        tonemap_scene_linear_filmic,
    )
    from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand, ChronosPort

    image = np.ones((4, 4, 3))
    alpha = np.ones((4, 4))
    focal = (3.0, 50.0, 2.8, 36.0, 64)
    receipts = [{"n": i} for i in range(3)]
    root = merkle_root(receipts)
    proof = build_inclusion_proof(receipts, 1)
    bad_png = tmp_path / "bad.png"
    bad_png.write_bytes(b"corrupt")
    probes: list[Callable[[], object]] = [
        lambda: verify_pose_invariant_identity(np.ones((3, 3)), np.ones((3, 2)), np.eye(3), 1),
        lambda: verify_pose_invariant_identity(np.ones((4, 3)), np.ones((4, 2)), np.eye(3), 0),
        lambda: cct_kelvin_to_xyz(1000),
        lambda: read_rgb8_png(bad_png),
        lambda: inpaint_masked_region_pde(image, np.zeros((4, 4), bool)),
        lambda: render_25d_multiplane_composite(
            [(image, alpha, 2.0)], image, tmp_path / "neg.png", beta=-1, focus=focal
        ),
        lambda: render_25d_multiplane_composite(
            [(image, alpha, 2.0)],
            image,
            tmp_path / "tile.png",
            beta=0.1,
            focus=focal,
            tile_size_px=0,
        ),
        lambda: tonemap_scene_linear_filmic(-image),
        lambda: synthesize_physical_sky_dome(0, 4, 10),
        lambda: ChronosCommand("x", ("a",), {}, external_api_allowed=True),
        lambda: ChronosCommand("x", ("a",), {}, deterministic=False),
        lambda: ChronosPort({}).execute(ChronosCommand("x", ("a",), {})),
        lambda: ChronosPort({}).execute(
            ChronosCommand("timeline.assemble_wav", ("a",), {"continuity": "UNKNOWN"})
        ),
        lambda: build_inclusion_proof(receipts, 3),
        lambda: declick_pcm_wav_splices(bad_png, tmp_path / "audio.wav", [1]),
        lambda: duck_ambience_under_dialogue(np.ones(3), np.ones(4), 8000),
        lambda: verify_eyeline_and_180_rule(
            np.zeros(2), np.zeros(3), np.zeros(3), np.ones(3), np.ones(3), np.ones(3)
        ),
        lambda: read_rgb8_png(tmp_path / "missing.png"),
        lambda: render_25d_multiplane_composite(
            [], image, tmp_path / "empty.png", beta=0, focus=focal
        ),
        lambda: verify_pose_invariant_identity(
            np.ones((4, 3)), np.ones((4, 2)), np.eye(3), 1, sigma_mm=0
        ),
    ]
    for probe in probes:
        with pytest.raises((KernelError, ValueError, OSError, KeyError)):
            probe()
    assert len(probes) == 20
    assert not verify_inclusion_proof({"n": 99}, proof, root)


def test_audio_codec_widths_and_refusals(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos.audio_continuity import _decode_pcm, _encode_pcm
    from nexus_ai_agent.creative.chronos.contracts import KernelError

    for width in (1, 2, 3, 4):
        encoded = _encode_pcm(np.array([-0.5, 0.0, 0.5]), width)
        assert _decode_pcm(encoded, width).shape == (3,)
    with pytest.raises(KernelError):
        _decode_pcm(b"", 9)
    source = tmp_path / "raw.wav"
    with wave.open(str(source), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(np.tile(np.arange(100, dtype="<i2"), (2, 1)).T.tobytes())
    with pytest.raises(KernelError):
        declick_pcm_wav_splices(source, tmp_path / "short.wav", [1], bridge_frames=32)
    with pytest.raises(KernelError):
        declick_pcm_wav_splices(source, tmp_path / "nobridge.wav", [], bridge_frames=1)
    with pytest.raises(KernelError):
        declick_pcm_wav_splices(tmp_path / "missing.wav", tmp_path / "bad.wav", [])
    with pytest.raises(KernelError):
        duck_ambience_under_dialogue(np.ones(4), np.ones(4), 0)
    with pytest.raises(KernelError):
        duck_ambience_under_dialogue(np.ones(4), np.ones(4), 8000, attack_ms=0)


def test_wave2_refusal_branches_and_factory_ffmpeg_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers, timeline
    from nexus_ai_agent.creative.chronos.color_science import cct_kelvin_to_xyz
    from nexus_ai_agent.creative.chronos.compositor_25d import render_25d_multiplane_composite
    from nexus_ai_agent.creative.chronos.contracts import KernelError
    from nexus_ai_agent.creative.chronos.geometry import circle_of_confusion_px
    from nexus_ai_agent.creative.chronos.identity_lock import (
        verify_eyeline_and_180_rule,
        verify_pose_invariant_identity,
    )
    from nexus_ai_agent.creative.chronos.merkle_ledger import (
        InclusionProof,
        node_hash,
        verify_inclusion_proof,
    )
    from nexus_ai_agent.creative.chronos.scene_portrait_ops import inpaint_masked_region_pde
    from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand, ChronosPort

    with pytest.raises(KernelError):
        circle_of_confusion_px(1, 1, 50, 2, 0, 100)
    with pytest.raises(KernelError):
        cct_kelvin_to_xyz(16000)
    with pytest.raises(KernelError):
        inpaint_masked_region_pde(np.ones((3, 3, 3)), np.ones((2, 2), bool))
    with pytest.raises(KernelError):
        verify_pose_invariant_identity(
            np.ones((4, 3)), np.ones((4, 2)), np.diag([0.0, 1.0, 1.0]), 1.0
        )
    assert not verify_eyeline_and_180_rule(
        np.array([0.0, -2.0, 1.0]),
        np.array([0.0, 2.0, -1.0]),
        np.zeros(3),
        np.array([0.0, 10.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, -1.0, 0.0]),
    )
    eyeline = verify_eyeline_and_180_rule(
        np.array([-2.0, 0.0, 0.0]),
        np.array([-2.0, 10.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 10.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, -1.0, 0.0]),
    )
    assert eyeline and eyeline.gaze_ray_miss_distance_m == 0.0
    assert not verify_inclusion_proof({"a": 1}, InclusionProof(9, 2, ()), b"x")
    with pytest.raises(ValueError):
        node_hash(b"x", b"y")
    with pytest.raises(KernelError):
        render_25d_multiplane_composite(
            [], np.ones((2, 2, 3)), tmp_path / "x.png", beta=0, focus=(2.0, 50.0, 2.8, 36.0, 20)
        )
    monkeypatch.setattr(
        timeline,
        "concat_copy",
        lambda *args, **kwargs: SimpleNamespace(
            sha256="a" * 64, quality_claim="MOCK", duration_seconds=1
        ),
    )
    command = ChronosCommand(
        "timeline.assemble_ffmpeg", ("a", "b"), {"output": str(tmp_path / "fake.mp4")}
    )
    receipt = ChronosPort(build_default_chronos_handlers()).execute(command)
    assert receipt.quality_claim == "MOCK"


def test_merkle_empty_singleton_and_malformed_proof() -> None:
    from nexus_ai_agent.creative.chronos.merkle_ledger import (
        InclusionProof,
        _root,
        leaf_hash,
        merkle_root,
        verify_inclusion_proof,
    )

    assert len(_root([])) == 32
    singleton = [{"id": "only"}]
    assert merkle_root(singleton) == leaf_hash(singleton[0])
    proof = InclusionProof(0, 1, ((False, b"bad"),))
    assert not verify_inclusion_proof(singleton[0], proof, merkle_root(singleton))
    assert not verify_inclusion_proof(singleton[0], InclusionProof(0, 1, ()), b"bad")


def test_wave21_receipts_huber_memory_sor_and_halation(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos.compositor_25d import verify_tiled_compositor_equivalence
    from nexus_ai_agent.creative.chronos.identity_lock import verify_pose_invariant_identity
    from nexus_ai_agent.creative.chronos.scene_portrait_ops import (
        inpaint_masked_region_pde,
        tonemap_scene_linear_filmic,
    )

    points = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.06, 0.0, 0.0],
            [0.0, 0.04, 0.0],
            [0.02, 0.03, 0.0],
            [0.04, 0.04, 0.0],
            [0.01, 0.01, 0.0],
        ]
    )
    k = np.array([[1000.0, 0.0, 32.0], [0.0, 1000.0, 32.0], [0.0, 0.0, 1.0]])
    obs = np.column_stack((32 + points[:, 0] * 500, 32 + points[:, 1] * 500))
    obs[-1] += np.array([80.0, -60.0])
    pose = verify_pose_invariant_identity(points, obs, k, 2.0, huber_delta_mm=2.0)
    assert pose.downweighted_landmark_count >= 1 and abs(np.linalg.det(pose.rotation_so3)) > 0.99
    frame = np.full((64, 64, 3), 0.3)
    alpha = np.ones((64, 64))
    layers = [(frame, alpha, 4.0)]
    result = verify_tiled_compositor_equivalence(
        layers,
        frame,
        tmp_path / "tiles.png",
        tmp_path / "full.png",
        beta=(0.001, 0.002, 0.004),
        focus=(3.0, 50.0, 2.8, 36.0, 64),
        tile_size_px=16,
    )
    assert (
        result.equivalent
        and result.max_tile_window_working_bytes < result.full_frame_equivalent_working_bytes
    )
    assert result.working_memory_reduction_ratio > 1
    damaged = frame.copy()
    damaged[24:40, 24:40] = 0.9
    mask = np.zeros((64, 64), bool)
    mask[24:40, 24:40] = True
    _, energy_before, energy_after = inpaint_masked_region_pde(damaged, mask, iterations=300)
    assert energy_after < energy_before
    hdr = np.zeros((17, 17, 3))
    hdr[7:10, 7:10] = 5.0
    plain = tonemap_scene_linear_filmic(hdr)
    halo = tonemap_scene_linear_filmic(hdr, halation_strength=0.5, halation_threshold=1.0)
    assert halo[..., 0].sum() > plain[..., 0].sum()
    curve = tonemap_scene_linear_filmic(np.repeat(np.linspace(0, 30, 64)[:, None, None], 3, axis=2))
    assert np.all(np.diff(curve[:, 0, 0]) >= 0) and curve.min() >= 0 and curve.max() <= 1


def test_wave2_audio_port_handler_dispatch(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand, ChronosPort

    src = tmp_path / "click.wav"
    with wave.open(str(src), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        vals = np.r_[np.full(100, 12000, dtype=np.int16), np.full(100, -12000, dtype=np.int16)]
        writer.writeframes(vals.astype("<i2").tobytes())
    out = tmp_path / "fixed.wav"
    cmd = ChronosCommand(
        "audio.declick_wav",
        (str(src),),
        {"output": str(out), "splice_frames": "100", "bridge_frames": "16"},
    )
    receipt = ChronosPort(build_default_chronos_handlers()).execute(cmd)
    assert out.exists() and receipt.sha256


def test_all_wave2_default_handler_families(tmp_path: Path) -> None:
    import json

    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.chronos.merkle_ledger import build_inclusion_proof, merkle_root
    from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand, ChronosPort

    port = ChronosPort(build_default_chronos_handlers())

    def save(name: str, data: Any) -> str:
        path = tmp_path / name
        np.save(path, data, allow_pickle=False)
        return str(path)

    dialogue = np.ones(128)
    ambience = np.full(128, 0.1)
    d, a = save("dialogue.npy", dialogue), save("ambience.npy", ambience)
    assert port.execute(
        ChronosCommand(
            "audio.duck_ambience",
            (d, a),
            {"output": str(tmp_path / "duck.npy"), "sample_rate_hz": "8000"},
        )
    ).sha256
    pts = np.array([[0.0, 0.0, 0.0], [0.06, 0.0, 0.0], [0.0, 0.04, 0.0], [0.02, 0.03, 0.0]])
    k = np.array([[1000.0, 0.0, 32.0], [0.0, 1000.0, 32.0], [0.0, 0.0, 1.0]])
    obs = np.column_stack((32 + pts[:, 0] * 500, 32 + pts[:, 1] * 500))
    inputs = (save("pts.npy", pts), save("obs.npy", obs), save("k.npy", k))
    assert port.execute(
        ChronosCommand(
            "identity.verify_pose", inputs, {"output": str(tmp_path / "pose.txt"), "depth_m": "2"}
        )
    ).sha256
    vecs = [
        save(f"vec{i}.npy", v)
        for i, v in enumerate(
            (
                [-2.0, 0.0, 0.0],
                [-2.0, 10.0, 0.0],
                [0.0, 0.0, 0.0],
                [0.0, 10.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, -1.0, 0.0],
            )
        )
    ]
    assert port.execute(
        ChronosCommand(
            "identity.verify_eyeline", tuple(vecs), {"output": str(tmp_path / "eye.txt")}
        )
    ).sha256
    rgb = np.full((32, 32, 3), 0.3)
    alpha = np.ones((32, 32))
    layers = tmp_path / "layers.npz"
    np.savez(layers, rgb0=rgb, alpha0=alpha, depth0=np.array(3.0))
    haze = save("haze.npy", rgb)
    common = {
        "layer_count": "1",
        "focus_m": "3",
        "focal_mm": "50",
        "f_number": "2.8",
        "sensor_mm": "36",
        "width_px": "32",
        "tile_size_px": "8",
    }
    p = {**common, "output": str(tmp_path / "comp.png"), "beta_rgb": "0.001,0.002,0.004"}
    assert port.execute(ChronosCommand("compositor.render_25d", (str(layers), haze), p)).sha256
    p = {
        **common,
        "output": str(tmp_path / "comp2.png"),
        "full_output": str(tmp_path / "full2.png"),
        "beta": "0.001",
    }
    assert (
        port.execute(
            ChronosCommand("compositor.verify_equivalence", (str(layers), haze), p)
        ).metadata["equivalent"]
        == "True"
    )
    image = rgb.copy()
    image[12:20, 12:20] = 0.8
    mask = np.zeros((32, 32), bool)
    mask[12:20, 12:20] = True
    ip, mp = save("image.npy", image), save("mask.npy", mask)
    assert port.execute(
        ChronosCommand("scene.inpaint", (ip, mp), {"output": str(tmp_path / "inpaint.npy")})
    ).sha256
    assert port.execute(
        ChronosCommand("scene.retouch", (ip, mp), {"output": str(tmp_path / "retouch.npy")})
    ).sha256
    assert port.execute(
        ChronosCommand(
            "scene.sky",
            (ip,),
            {
                "output": str(tmp_path / "sky.npy"),
                "width": "8",
                "height": "8",
                "solar_elevation_deg": "20",
            },
        )
    ).sha256
    assert port.execute(
        ChronosCommand(
            "scene.tonemap",
            (ip,),
            {"output": str(tmp_path / "tone.npy"), "halation_strength": ".1"},
        )
    ).sha256
    receipt_list = [{"id": i} for i in range(3)]
    inp = tmp_path / "receipts.json"
    inp.write_text(json.dumps(receipt_list))
    root = merkle_root(receipt_list)
    proof = build_inclusion_proof(receipt_list, 1)
    root_cmd = ChronosCommand("merkle.root", (str(inp),), {"output": str(tmp_path / "root.txt")})
    assert port.execute(root_cmd).metadata["leaf_count"] == "3"
    proof_file = tmp_path / "proof.json"
    proof_file.write_text(
        json.dumps(
            {
                "receipt": receipt_list[1],
                "root": root.hex(),
                "proof": {
                    "leaf_index": proof.leaf_index,
                    "tree_size": proof.tree_size,
                    "audit_path": [[left, digest.hex()] for left, digest in proof.audit_path],
                },
            }
        )
    )
    assert (
        port.execute(
            ChronosCommand(
                "merkle.verify", (str(proof_file),), {"output": str(tmp_path / "proof.txt")}
            )
        ).metadata["verified"]
        == "True"
    )
