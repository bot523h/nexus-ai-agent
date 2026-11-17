from __future__ import annotations

import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos.acoustics_lipsync import (
    measure_rir_acoustics,
    solve_jaw_trajectory,
)
from nexus_ai_agent.creative.chronos.chronos_port import (
    ChronosCommand,
    ChronosExecutionReceipt,
    ChronosHandler,
    ChronosPort,
)
from nexus_ai_agent.creative.chronos.color_science import (
    cct_kelvin_to_xyz,
    match_linear_rgb_gaussian_ot,
)
from nexus_ai_agent.creative.chronos.contracts import Continuity, KernelError, RationalTimestamp
from nexus_ai_agent.creative.chronos.geometry import (
    backproject_character_height_m,
    intrinsics,
    project,
    undistort_normalized_point,
)
from nexus_ai_agent.creative.chronos.timeline import (
    assemble_pcm_wav_stream_copy,
    verify_pcm_wav_assembly,
)


def _port_handler(command: ChronosCommand) -> ChronosExecutionReceipt:
    return ChronosExecutionReceipt(
        command.operation,
        command.parameters.get("output", "/tmp/fake-output"),
        "a" * 64,
        "TEST_HANDLER",
        {"operation": command.operation},
    )


def _port_handlers() -> dict[str, ChronosHandler]:
    return {operation: _port_handler for operation in ChronosPort.OPERATIONS}


def _port() -> ChronosPort:
    return ChronosPort(_port_handlers())


def _write_wav(path: Path, frames: np.ndarray, rate: int = 48000) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(frames.astype("<i2").tobytes())


def test_real_disk_three_shot_wav_conservation(tmp_path: Path) -> None:
    sources = []
    for index in range(3):
        path = tmp_path / f"shot-{index}.wav"
        _write_wav(path, np.arange(index * 100, (index + 1) * 100, dtype=np.int16))
        sources.append(path)
    output = tmp_path / "episode.wav"
    staging = tmp_path / "staging"
    receipt = assemble_pcm_wav_stream_copy(sources, output, staging)
    assert receipt.verification_mode == "REAL_DISK_PCM_WAV"
    assert receipt.total_sample_frames == 300
    assert receipt.splice_boundaries_verified == 2
    assert receipt.duration_seconds == Fraction(1, 160)
    assert len(receipt.sha256) == 64
    assert output.exists() and not list(staging.glob("*.staged.wav"))
    with wave.open(str(output), "rb") as reader:
        assert reader.getnframes() == 300
        assert reader.readframes(300) == b"".join(path.read_bytes()[44:] for path in sources)


def test_wav_refusals_and_bad_staging(tmp_path: Path) -> None:
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    _write_wav(first, np.ones(16, dtype=np.int16))
    _write_wav(second, np.ones(16, dtype=np.int16), 44100)
    output = tmp_path / "bad.wav"
    with pytest.raises(KernelError):
        assemble_pcm_wav_stream_copy([first, second], output, tmp_path / "stage")
    assert not output.exists()
    bad = tmp_path / "corrupt.wav"
    bad.write_bytes(b"broken")
    with pytest.raises(KernelError):
        assemble_pcm_wav_stream_copy([first, bad], output, tmp_path / "stage")
    assert not output.exists()
    valid = tmp_path / "valid.wav"
    _write_wav(valid, np.ones(16, dtype=np.int16), 44100)
    with pytest.raises(KernelError):
        assemble_pcm_wav_stream_copy([first, valid], output, tmp_path / "stage")
    staged = tmp_path / "wrong.wav"
    _write_wav(staged, np.ones(4, dtype=np.int16))
    with pytest.raises(KernelError):
        verify_pcm_wav_assembly(staged, (1, 2, 48000, "NONE"), 9, 18, [4])


def test_lens_undistortion_metric_height_and_scale_contracts() -> None:
    distortion = (0.02, -0.001, 0.0001, 0.0002, -0.0001)
    x, y = 0.1, -0.2
    r2 = x * x + y * y
    k1, k2, k3, p1, p2 = distortion
    rad = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    xd = x * rad + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    yd = y * rad + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    ux, uy, residual = undistort_normalized_point(xd, yd, distortion)
    assert (ux, uy) == pytest.approx((x, y), abs=1e-9)
    assert residual < 1e-9
    for focal, depth in ((24.0, 2.5), (85.0, 7.0)):
        k = intrinsics(focal, 1920, 1080, 36, 20.25)
        endpoints = project(
            np.array([[0.0, 0.9, depth], [0.0, -0.9, depth]]), np.eye(3), np.zeros(3), k
        )
        receipt = backproject_character_height_m(tuple(endpoints[0]), tuple(endpoints[1]), depth, k)
        assert receipt.world_height_m == pytest.approx(1.8, abs=1e-6)
        assert receipt.world_space_contract == "WORLD_SPACE_CHARACTER_SCALE"
        assert receipt.image_space_contract == "IMAGE_SPACE_PROJECTED_SCALE"


def test_cct_gaussian_ot_rir_measurement_and_jaw_receipt() -> None:
    xyz = cct_kelvin_to_xyz(6500)
    assert xyz[1] == 1.0 and all(np.isfinite(xyz))
    with pytest.raises(KernelError):
        cct_kelvin_to_xyz(1000)
    rng = np.random.default_rng(42)
    source = rng.normal(size=(128, 3))
    target = source @ np.diag([2.0, 0.5, 1.2]) + np.array([1.0, -2.0, 0.5])
    transferred = match_linear_rgb_gaussian_ot(source, target)
    assert np.allclose(transferred.mean(axis=0), target.mean(axis=0), atol=1e-5)
    sample_rate = 48000
    t = np.arange(15000) / sample_rate
    rir = np.exp(-10 * t)
    rir[0] += 1.0
    measured = measure_rir_acoustics(rir, sample_rate)
    assert np.isfinite(measured.measured_drr_db)
    assert measured.measured_rt60_seconds > 0
    stamps = [(RationalTimestamp(i, Fraction(1, 30)), float(4 <= i < 12)) for i in range(20)]
    trajectory = solve_jaw_trajectory(stamps, 1000)
    assert np.all((trajectory.aperture >= 0) & (trajectory.aperture <= 1))
    assert abs(trajectory.peak_lag_frames) < 0.5


def test_chronos_port_routes_allowlisted_commands_and_fails_closed(tmp_path: Path) -> None:
    command = ChronosCommand(
        "timeline.assemble_wav", ("shot-a", "shot-b"), {"output": str(tmp_path / "out.wav")}
    )
    receipt = _port().execute(command)
    assert receipt.operation == command.operation
    assert receipt.quality_claim == "TEST_HANDLER"
    assert receipt.sha256 == "a" * 64
    with pytest.raises(ValueError, match="unsupported Chronos operation"):
        _port().execute(ChronosCommand("unknown.operation", ("x",), {}))
    with pytest.raises(ValueError, match="UNKNOWN continuity"):
        _port().execute(ChronosCommand("timeline.assemble_wav", ("x",), {"continuity": "UNKNOWN"}))
    with pytest.raises(ValueError, match="no Chronos handler"):
        ChronosPort({}).execute(command)
    with pytest.raises(ValueError):
        ChronosCommand("", ("x",), {})


def test_color_ot_and_kelvin_domains() -> None:
    from nexus_ai_agent.creative.chronos.color_science import srgb_decode

    for kelvin in (2000.0, 3000.0, 4000.0, 6500.0, 15000.0):
        assert all(np.isfinite(cct_kelvin_to_xyz(kelvin)))
    for value in (1999.0, 15001.0):
        with pytest.raises(KernelError):
            cct_kelvin_to_xyz(value)
    constant = np.ones((4, 3), dtype=np.float64)
    mapped = match_linear_rgb_gaussian_ot(constant, constant * 2)
    assert np.isfinite(mapped).all()
    with pytest.raises(KernelError):
        match_linear_rgb_gaussian_ot(np.ones((1, 3)), np.ones((2, 3)))
    with pytest.raises(KernelError):
        match_linear_rgb_gaussian_ot(np.ones((2, 3)), np.ones((2, 3)), 0)
    with pytest.raises(KernelError):
        srgb_decode(np.array([2.0]))


def test_geometry_undistortion_refusals() -> None:
    with pytest.raises(KernelError):
        undistort_normalized_point(float("nan"), 0.0)
    with pytest.raises(KernelError):
        undistort_normalized_point(0.1, 0.2, tolerance=0.0)
    with pytest.raises(KernelError):
        undistort_normalized_point(0.1, 0.2, (1.0, 0.0, 0.0, 0.0, 0.0), max_iterations=1)
    with pytest.raises(KernelError):
        backproject_character_height_m((1, 1), (2, 2), 0, np.eye(3))
    with pytest.raises(KernelError):
        backproject_character_height_m((1, 1), (2, 2), 2, np.eye(3), pixel_uncertainty=-1)


def test_wav_verifier_and_atomic_failure_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.chronos import timeline

    with pytest.raises(KernelError):
        timeline._wav_signature(object())
    empty = tmp_path / "empty.wav"
    _write_wav(empty, np.array([], dtype=np.int16))
    with pytest.raises(KernelError):
        verify_pcm_wav_assembly(empty, (1, 2, 48000, "NONE"), 0, 0, [])
    with pytest.raises(KernelError):
        verify_pcm_wav_assembly(empty, (1, 2, 44100, "NONE"), 0, 0, [])

    one, two = tmp_path / "one.wav", tmp_path / "two.wav"
    _write_wav(one, np.ones(4, dtype=np.int16))
    _write_wav(two, np.ones(4, dtype=np.int16))
    destination = tmp_path / "atomic.wav"
    staging = tmp_path / "stage-existing"
    staging.mkdir()
    (staging / "atomic.staged.wav").write_bytes(b"stale")

    def refuse_verification(*args: object, **kwargs: object) -> tuple[Fraction, tuple[float, ...]]:
        raise KernelError("injected staged verification failure")

    monkeypatch.setattr(timeline, "verify_pcm_wav_assembly", refuse_verification)
    with pytest.raises(KernelError):
        assemble_pcm_wav_stream_copy([one, two], destination, staging)
    assert not destination.exists()
    assert not (staging / "atomic.staged.wav").exists()
    destination.write_bytes(b"pre-existing")
    with pytest.raises(KernelError):
        assemble_pcm_wav_stream_copy([one, two], destination, staging)
    assert destination.read_bytes() == b"pre-existing"


def test_timeline_wave_truncation_and_precondition_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import wave
    from types import SimpleNamespace

    from nexus_ai_agent.creative.chronos import timeline

    with pytest.raises(KernelError):
        timeline._fraction_duration({"format": {"duration": "bad"}})
    with pytest.raises(KernelError):
        timeline._fraction_duration({"format": {"duration": "-1"}})
    original_run = subprocess.run

    def timeout_run(*args: object, **kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired("ffprobe", 1)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    with pytest.raises(KernelError):
        timeline._probe(tmp_path / "media", "ffprobe", packets=False)
    monkeypatch.setattr(subprocess, "run", original_run)

    truncated = tmp_path / "truncated.wav"
    _write_wav(truncated, np.arange(8, dtype=np.int16))
    truncated.write_bytes(truncated.read_bytes()[:-12])
    with pytest.raises(KernelError):
        verify_pcm_wav_assembly(truncated, (1, 2, 48000, "NONE"), 8, 2, [])
    with pytest.raises(KernelError):
        assemble_pcm_wav_stream_copy([truncated, truncated], tmp_path / "x.wav", tmp_path / "stage")
    valid = tmp_path / "v.wav"
    _write_wav(valid, np.arange(8, dtype=np.int16))
    existing = tmp_path / "existing.wav"
    existing.write_bytes(b"preserve")
    with pytest.raises(KernelError):
        assemble_pcm_wav_stream_copy([valid, valid], existing, tmp_path / "stage")
    assert existing.read_bytes() == b"preserve"
    with wave.open(str(valid), "rb") as reader:
        assert timeline._wav_signature(reader) == (1, 2, 48000, "NONE")


def test_timeline_packet_and_staged_wav_error_branches(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos import timeline

    with pytest.raises(KernelError):
        timeline._check_packets({})
    with pytest.raises(KernelError):
        timeline._check_packets({"packets": [None]})
    corrupt = tmp_path / "not-wave.wav"
    corrupt.write_bytes(b"not a RIFF file")
    with pytest.raises(KernelError):
        verify_pcm_wav_assembly(corrupt, (1, 2, 48000, "NONE"), 1, 2, [])
    valid = tmp_path / "small.wav"
    _write_wav(valid, np.arange(4, dtype=np.int16))
    with pytest.raises(KernelError):
        verify_pcm_wav_assembly(valid, (1, 2, 48000, "NONE"), 4, 8, [0])
    dag = timeline.ShotDAG({"left": b"l", "middle": b"m", "right": b"r"})
    dag.set_continuity("middle", "right", Continuity.OK)
    assert dag.verify("middle", "right")
    dag.mutate("middle", b"new")
    assert not dag.verify("middle", "right")


def test_probe_signature_success_and_jacobian_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import subprocess
    from types import SimpleNamespace

    from nexus_ai_agent.creative.chronos.geometry import projected_height_jacobian
    from nexus_ai_agent.creative.chronos.timeline import ffprobe_signature

    payload = {
        "streams": [
            {
                "codec_name": "h264",
                "codec_type": "video",
                "profile": "High",
                "pix_fmt": "yuv420p",
                "time_base": "1/90000",
                "extradata_hash": "hash",
                "width": 1920,
                "height": 1080,
            }
        ]
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps(payload)
        ),
    )
    signature = ffprobe_signature(Path("/absolute/video.mp4"))
    assert signature.codec == "h264" and signature.time_base == Fraction(1, 90000)
    with pytest.raises(KernelError):
        projected_height_jacobian(1000, 1.8, 0)


def test_studio_port_registry_tamper_and_receipt_validation() -> None:
    from nexus_ai_agent.creative.chronos.chronos_port import (
        ChronosCommand,
        ChronosExecutionReceipt,
        ChronosPort,
        execute_chronos_command,
    )

    with pytest.raises(ValueError, match="unsupported Chronos operation"):
        ChronosPort({"evil.operation": _port_handler})
    command = ChronosCommand("timeline.assemble_wav", ("a",), {})
    assert execute_chronos_command(command, _port_handlers()).operation == command.operation

    def invalid_receipt(item: ChronosCommand) -> ChronosExecutionReceipt:
        return ChronosExecutionReceipt(item.operation, "x", "", "none", {})

    with pytest.raises(ValueError, match="invalid execution receipt"):
        ChronosPort({command.operation: invalid_receipt}).execute(command)
    tampered = object.__new__(ChronosCommand)
    object.__setattr__(tampered, "operation", command.operation)
    object.__setattr__(tampered, "inputs", command.inputs)
    object.__setattr__(tampered, "parameters", command.parameters)
    object.__setattr__(tampered, "deterministic", False)
    object.__setattr__(tampered, "external_api_allowed", False)
    with pytest.raises(ValueError, match="nondeterministic"):
        ChronosPort(_port_handlers()).execute(tampered)


def test_ffprobe_signature_stream_count_and_bad_timebase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.chronos import timeline

    monkeypatch.setattr(timeline, "_probe", lambda *args, **kwargs: {"streams": [{}, {}]})
    with pytest.raises(KernelError, match="exactly one"):
        timeline.ffprobe_signature(tmp_path / "multi")
    monkeypatch.setattr(timeline, "_probe", lambda *args, **kwargs: {"streams": [{}]})
    monkeypatch.setattr(
        timeline,
        "_stream_key",
        lambda stream: (
            None,
            "codec",
            None,
            None,
            None,
            None,
            None,
            "bad",
            None,
            None,
            None,
            "hash",
        ),
    )
    with pytest.raises(KernelError, match="rational time base"):
        timeline.ffprobe_signature(tmp_path / "invalid-time-base")


def test_concat_preassembly_signature_refusals_and_pts_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.chronos import timeline

    base_stream: dict[str, object] = {
        "codec_type": "audio",
        "codec_name": "pcm_s16le",
        "profile": None,
        "sample_fmt": "s16",
        "time_base": "1/48000",
        "sample_rate": "48000",
        "channels": 1,
        "channel_layout": "mono",
        "extradata_hash": "hash",
    }
    data_a: dict[str, object] = {"streams": [base_stream]}
    data_b: dict[str, object] = {"streams": [{**base_stream, "codec_name": "pcm_f32le"}]}
    a, b = tmp_path / "sig-a.wav", tmp_path / "sig-b.wav"
    a.touch()
    b.touch()
    monkeypatch.setattr(
        timeline, "_probe", lambda path, *args, **kwargs: data_a if path.name == a.name else data_b
    )
    with pytest.raises(KernelError, match="incompatible stream signature"):
        timeline.concat_copy([a, b], tmp_path / "signature.wav")
    data_b["streams"] = [base_stream, base_stream]
    with pytest.raises(KernelError, match="stream count mismatch"):
        timeline.concat_copy([a, b], tmp_path / "stream-count.wav")
    counts, timestamps = timeline._check_packets({"packets": [{"stream_index": 0, "dts": 5}]})
    assert counts == {0: 1} and timestamps == {0: 5}
