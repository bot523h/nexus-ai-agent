import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos import compositor_25d, identity_lock
from nexus_ai_agent.creative.chronos import timeline as timeline_module
from nexus_ai_agent.creative.chronos.acoustics_lipsync import (
    _voiced_envelopes,
    fractional_impulse,
    jaw_aperture,
    measure_cepstral_timbre_consistency,
    measure_rir_acoustics,
    render_spatial_dialogue,
    sabine_rt60,
    shoebox_rir,
    synthesize_stereo_room_ir,
    woodworth_itd,
)
from nexus_ai_agent.creative.chronos.color_science import (
    bradford_adaptation,
    bt709_decode,
    bt709_encode,
    delta_e_2000,
    preserve_structure,
    read_rgb8_png,
    rgb_to_lab,
    srgb_decode,
    srgb_encode,
    write_rgb8_png,
)
from nexus_ai_agent.creative.chronos.contracts import Continuity, KernelError, RationalTimestamp
from nexus_ai_agent.creative.chronos.geometry import (
    coc_diameter_px,
    intrinsics,
    project,
    projected_height_jacobian,
)
from nexus_ai_agent.creative.chronos.timeline import (
    ShotDAG,
    StreamSignature,
    _wav_signature,
    assemble_pcm_wav_stream_copy,
    concat_copy,
    content_hash,
    ffprobe_signature,
    require_compatible,
)
from nexus_ai_agent.creative.chronos.verifier import (
    continuity_gate,
    gradient_correlation,
    verify_persisted_relighting,
    verify_structure,
)


def test_rational_and_dag() -> None:
    t = RationalTimestamp(0, Fraction(1, 24000))
    for _ in range(10001):
        t = t + 1001
    assert t.seconds == Fraction(1001 * 10001, 24000)
    dag = ShotDAG({str(i): str(i).encode() for i in range(4)})
    assert dag.verify("0", "1") is False
    dag.set_continuity("0", "1", Continuity.OK)
    assert dag.verify("0", "1")
    old = dag.digest("2")
    assert dag.mutate("2", b"changed") == ("1", "2", "3")
    assert dag.digest("2") != old


def test_timeline_pcm_assembly_refuses_invalid_inputs(tmp_path: Path) -> None:
    with pytest.raises(KernelError, match="invalid WAV reader"):
        _wav_signature(object())
    source = tmp_path / "bad.wav"
    source.write_bytes(b"not a wave")
    with pytest.raises(KernelError, match="invalid input WAV"):
        assemble_pcm_wav_stream_copy([source, source], tmp_path / "out.wav", tmp_path / "stage")
    with pytest.raises(KernelError, match="at least two WAV shots"):
        assemble_pcm_wav_stream_copy([], tmp_path / "empty.wav", tmp_path / "stage")


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("stream-count", "assembled stream count mismatch"),
        ("signature", "assembled stream signature mismatch"),
        ("packets", "packet conservation failed"),
        ("duration", "duration conservation failed"),
    ],
)
def test_timeline_concat_copy_verifies_final_output(
    fault: str, message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    inputs = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in inputs:
        path.write_bytes(b"source")
    stream = {
        "codec_type": "video",
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "width": 16,
        "height": 16,
        "r_frame_rate": "24/1",
        "time_base": "1/24",
        "extradata_hash": "sha256:codec",
    }
    packet = {"stream_index": 0, "pts": 0, "dts": 0}
    source_data = {"format": {"duration": "1"}, "streams": [stream], "packets": [packet]}
    output_streams = [dict(stream)]
    output_packets = [dict(packet), dict(packet)]
    duration = "2"
    if fault == "stream-count":
        output_streams.append(dict(stream))
    elif fault == "signature":
        output_streams[0]["codec_name"] = "different"
    elif fault == "packets":
        output_packets = []
    elif fault == "duration":
        duration = "3"
    output_data = {
        "format": {"duration": duration},
        "streams": output_streams,
        "packets": output_packets,
    }
    probes = iter((source_data, source_data, output_data))
    monkeypatch.setattr(timeline_module, "_probe", lambda *_args, **_kwargs: next(probes))
    calls = 0

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(command[-1]).write_bytes(b"staged media")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(KernelError, match=message):
        concat_copy(inputs, tmp_path / "assembled.mp4")


def test_stream_compatibility_fails_closed() -> None:
    sig = StreamSignature("h264", "High", "yuv420p", Fraction(1, 90000), None, None, "abc")
    assert require_compatible([sig, sig]) == sig
    with pytest.raises(KernelError):
        require_compatible(
            [sig, StreamSignature("h264", "High", "yuv420p", Fraction(1, 90000), None, None, "bad")]
        )
    assert content_hash(b"x") == content_hash(b"x")


def test_geometry_projection_coc_and_domain() -> None:
    k = intrinsics(50, 1920, 1080, 36, 20.25)
    p = project(np.array([[0.0, 0.0, 2.0]]), np.eye(3), np.zeros(3), k)
    assert np.allclose(p[0], [960, 540])
    assert projected_height_jacobian(1000, 1.7, 2) == pytest.approx(-425)
    assert coc_diameter_px(50, 2, 2000, 2000, 0.005) == pytest.approx(0)
    assert coc_diameter_px(50, 2, 2000, 1000, 0.005) > 0
    with pytest.raises(KernelError):
        project(np.array([[0.0, 0.0, -1.0]]), np.eye(3), np.zeros(3), k)
    with pytest.raises(KernelError):
        project(np.array([[0.0, 0.0, 1.0]]), np.diag([1.0, 1.0, -1.0]), np.zeros(3), k)


def test_color_transfers_bradford_ot_and_ciede() -> None:
    x = np.linspace(0, 1, 101)
    assert np.max(np.abs(srgb_encode(srgb_decode(x)) - x)) < 1e-6
    assert np.max(np.abs(bt709_encode(bt709_decode(x)) - x)) < 1e-6
    adaptation = bradford_adaptation(np.array([0.95, 1.0, 1.09]), np.array([0.95, 1.0, 1.09]))
    assert np.allclose(adaptation, np.eye(3), atol=1e-5)
    assert np.all(np.isfinite(preserve_structure(np.ones((5, 5)), np.ones((5, 5)))))
    lab = np.array([50.0, 2.6772, -79.7751])
    assert float(delta_e_2000(lab, np.array([50.0, 0.0, -82.7485]))) == pytest.approx(
        2.0425, abs=0.001
    )
    assert np.all(np.isfinite(rgb_to_lab(np.array([[0.2, 0.3, 0.4]]))))


def test_acoustics_lipsync_refusal_and_edge_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _voiced_envelopes(np.zeros(2048), 8000) == []
    assert _voiced_envelopes(np.random.default_rng(1).normal(size=2048), 8000) == []
    with pytest.raises(KernelError, match="nonzero direct"):
        measure_rir_acoustics(np.zeros(128), 8000)
    with pytest.raises(KernelError, match="nonzero direct"):
        measure_rir_acoustics(np.r_[1.0, np.zeros(127)], 8000)
    abrupt_tail = np.zeros(128)
    abrupt_tail[0], abrupt_tail[50] = 1.0, 0.01
    with pytest.raises(KernelError, match="too short"):
        measure_rir_acoustics(abrupt_tail, 8000)
    monkeypatch.setattr(np, "polyfit", lambda *_args, **_kwargs: np.array([0.0, 1.0]))
    signal = np.r_[1.0, np.zeros(31), np.exp(-np.arange(1, 129) / 15)]
    with pytest.raises(KernelError, match="negative slope"):
        measure_rir_acoustics(signal, 8000)

    zero_path = shoebox_rir(
        np.array([1.0, 1.0, 1.0]),
        np.array([1.0, 1.0, 1.0]),
        np.array([4.0, 5.0, 3.0]),
        0.2,
        8000,
        order=0,
    )
    assert not np.any(zero_path)
    with pytest.raises(KernelError, match="camera right vector"):
        synthesize_stereo_room_ir(
            np.array([4.0, 5.0, 3.0]),
            np.array([2.0, 2.0, 1.0]),
            np.array([1.0, 1.0, 1.0]),
            np.array([0.0, 0.0, 0.0]),
            8000,
            0.2,
        )
    with pytest.raises(KernelError, match="invalid dry signal"):
        render_spatial_dialogue(np.array([]), np.ones((2, 2)))


def test_identity_lock_degenerate_geometry_and_svd_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = np.array(
        [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.0, 0.1, 0.0], [0.0, 0.0, 0.1]],
        dtype=np.float64,
    )
    degenerate = reference.copy()
    degenerate[1] = degenerate[0]
    with pytest.raises(KernelError, match="canonical cranial baseline"):
        identity_lock.restore_cranial_landmarks_to_anchor(
            degenerate, degenerate, left_index=0, right_index=1
        )
    with pytest.raises(KernelError, match="reference cranial baseline"):
        identity_lock.verify_cranial_baseline(degenerate, reference, left_index=0, right_index=1)
    assert identity_lock._brown_conrady_jacobian(0.1, -0.2, (0.01, 0, 0, 0, 0)).shape == (2, 2)

    def fail_svd(*_args: object, **_kwargs: object) -> None:
        raise np.linalg.LinAlgError("forced decomposition failure")

    monkeypatch.setattr(np.linalg, "svd", fail_svd)
    with pytest.raises(KernelError, match="SVD failed"):
        identity_lock.restore_cranial_landmarks_to_anchor(reference, reference)


def test_verifier_and_compositor_refusal_branches(tmp_path: Path) -> None:
    constant = np.ones((4, 4), dtype=np.float64)
    changed = np.full((4, 4), 2.0, dtype=np.float64)
    assert gradient_correlation(constant, constant) == 1.0
    assert gradient_correlation(constant, changed) == 0.0
    with pytest.raises(KernelError, match="identical non-empty shape"):
        gradient_correlation(np.empty((0, 0)), np.empty((0, 0)))
    with pytest.raises(KernelError, match="identical non-empty shape"):
        gradient_correlation(constant, np.ones((2, 2)))

    haze = np.zeros((4, 4, 3), dtype=np.float64)
    layer = np.ones_like(haze)
    alpha = np.ones((4, 4), dtype=np.float64)
    focus = (2.0, 50.0, 2.8, 36.0, 4)
    with pytest.raises(KernelError, match="layers required"):
        compositor_25d._composite([], haze, 0.1, focus)
    with pytest.raises(KernelError, match="shapes must match"):
        compositor_25d._composite([(layer, alpha, 1.0)], np.zeros((4, 4)), 0.1, focus)
    with pytest.raises(KernelError, match="extinction"):
        compositor_25d._composite([(layer, alpha, 1.0)], haze, -0.1, focus)
    with pytest.raises(KernelError, match="invalid 2.5D layer"):
        compositor_25d._composite([(layer, alpha, -1.0)], haze, 0.1, focus)
    with pytest.raises(KernelError, match="tile size"):
        compositor_25d.render_25d_multiplane_composite(
            [(layer, alpha, 1.0)],
            haze,
            tmp_path / "invalid.png",
            beta=0.1,
            focus=focus,
            tile_size_px=0,
        )
    with pytest.raises(KernelError, match="haze must be RGB"):
        compositor_25d.render_25d_multiplane_composite(
            [(layer, alpha, 1.0)],
            np.zeros((4, 4, 1)),
            tmp_path / "invalid-haze.png",
            beta=0.1,
            focus=focus,
        )


def test_acoustics_visemes_and_verifier() -> None:
    impulse = fractional_impulse(12.25, 64)
    assert np.argmax(impulse) in (12, 13)
    rir = shoebox_rir(
        np.array([1.0, 1.0, 1.0]),
        np.array([2.0, 1.0, 1.0]),
        np.array([5.0, 4.0, 3.0]),
        0.4,
        8000,
        0,
    )
    assert rir.max() > 0
    assert sabine_rt60(100, 200, 0.2) > 0
    stamps = [(RationalTimestamp(i, Fraction(1, 100)), float(i > 3)) for i in range(20)]
    y = jaw_aperture(stamps, 5)
    assert np.all((y >= 0) & (y <= 1))
    a = np.arange(100, dtype=float).reshape(10, 10)
    assert gradient_correlation(a, a) == pytest.approx(1)
    verify_structure(a, a)
    with pytest.raises(KernelError, match="structural verification failed"):
        verify_structure(a, -a)
    assert continuity_gate(Continuity.OK) and not continuity_gate(Continuity.UNKNOWN)
    with pytest.raises(KernelError):
        verify_structure(a, np.zeros_like(a))


def test_png_disk_roundtrip_and_corruption(tmp_path: Path) -> None:

    pixels = np.arange(6 * 7 * 3, dtype=np.uint8).reshape(6, 7, 3)
    path = tmp_path / "roundtrip.png"
    write_rgb8_png(path, pixels)
    assert np.array_equal(read_rgb8_png(path), pixels)
    data = bytearray(path.read_bytes())
    data[-8] ^= 1
    path.write_bytes(data)
    with pytest.raises(KernelError):
        read_rgb8_png(path)


def test_persisted_relighting_verified_from_disk(tmp_path: Path) -> None:

    yy, xx = np.mgrid[0:32, 0:32]
    plane = 0.4 + 0.2 * np.sin(xx / 5) * np.cos(yy / 6)
    base = np.repeat(plane[:, :, None], 3, axis=2)
    source = base * 0.8 + 0.05
    target = base
    output = tmp_path / "relit.png"
    digest, before, after, structure = verify_persisted_relighting(source, target, target, output)
    assert len(digest) == 64 and after < before and structure >= 0.985


def test_stereo_room_and_renderer() -> None:
    from numpy.typing import NDArray

    assert woodworth_itd(0.0) == 0.0
    assert woodworth_itd(0.5) == pytest.approx(-woodworth_itd(-0.5))
    room, telemetry = synthesize_stereo_room_ir(
        np.array([8.0, 6.0, 3.0]),
        np.array([3.0, 2.0, 1.5]),
        np.array([2.0, 2.0, 1.5]),
        np.array([1.0, 0.0, 0.0]),
        8000,
        0.35,
        1,
    )
    assert room.ndim == 2 and room.shape[1] == 2
    assert np.isfinite(telemetry.drr_db) and telemetry.rt60_seconds > 0
    audio: NDArray[np.float64] = np.zeros(80, dtype=np.float64)
    audio[0] = 1.0
    rendered, receipt = render_spatial_dialogue(audio, room[:100], 32)
    assert rendered.shape == (audio.size + min(100, len(room)) - 1, 2)
    assert np.isfinite(rendered).all() and np.isfinite(receipt.drr_db)


def test_timeline_probe_and_concat_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import subprocess
    from types import SimpleNamespace

    from nexus_ai_agent.creative.chronos import timeline

    stream = {
        "index": 0,
        "codec_type": "audio",
        "codec_name": "pcm_s16le",
        "profile": None,
        "sample_fmt": "s16",
        "time_base": "1/48000",
        "sample_rate": "48000",
        "channels": 1,
        "channel_layout": "mono",
        "extradata_hash": "abc",
    }

    def packet(pts: int) -> dict[str, object]:
        return {"stream_index": 0, "pts": pts, "dts": pts, "duration": 1}

    inputs_data = {
        "a.wav": {
            "format": {"duration": "1/10"},
            "streams": [stream],
            "packets": [packet(0), packet(1)],
        },
        "b.wav": {
            "format": {"duration": "1/10"},
            "streams": [stream],
            "packets": [packet(0), packet(1)],
        },
    }
    output_data = {
        "format": {"duration": "1/5"},
        "streams": [stream],
        "packets": [packet(0), packet(1), packet(2), packet(3)],
    }

    def run(args: list[str], **kwargs: object) -> SimpleNamespace:
        if args[0] == "ffprobe":
            media_path = Path(args[-1])
            data = (
                output_data
                if media_path.name.startswith("assembled")
                else inputs_data[media_path.name]
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        if "concat" in args:
            Path(args[-1]).write_bytes(b"staged-media")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    source_a, source_b = tmp_path / "a.wav", tmp_path / "b.wav"
    source_a.touch()
    source_b.touch()
    dest = tmp_path / "out.wav"
    receipt = timeline.concat_copy([source_a, source_b], dest)
    assert dest.read_bytes() == b"staged-media"
    assert receipt.quality_claim == "NO_ADDITIONAL_REENCODE_LOSS_AT_ASSEMBLY"
    assert receipt.duration_seconds == Fraction(1, 5)
    assert receipt.packet_counts == {0: 4} and receipt.verified_splice_count == 1
    assert len(receipt.sha256) == 64
    with pytest.raises(KernelError):
        timeline.concat_copy([source_a], tmp_path / "no.wav")
    quote = tmp_path / "unsafe'path.wav"
    quote.touch()
    with pytest.raises(KernelError):
        timeline.concat_copy([quote, source_b], tmp_path / "unsafeout.wav")


def test_timestamp_rescale_and_contract_failures() -> None:
    with pytest.raises(KernelError):
        RationalTimestamp(1, Fraction(0))
    stamp = RationalTimestamp(2, Fraction(1, 4))
    assert stamp.rescale(Fraction(1, 2)) == RationalTimestamp(1, Fraction(1, 2))
    with pytest.raises(KernelError):
        stamp.rescale(Fraction(1, 3))
    with pytest.raises(KernelError):
        stamp.rescale(Fraction(-1, 3))
    with pytest.raises(KeyError):
        ShotDAG({"a": b"a"}).mutate("missing", b"x")


def test_geometry_color_acoustic_error_branches() -> None:
    with pytest.raises(KernelError):
        intrinsics(0, 10, 10, 1, 1)
    with pytest.raises(KernelError):
        coc_diameter_px(50, 2, 30, 40, 0.01)
    with pytest.raises(KernelError):
        bradford_adaptation(np.zeros(3), np.ones(3))
    with pytest.raises(KernelError):
        preserve_structure(np.zeros((2, 2)), np.ones((3, 3)))
    with pytest.raises(KernelError):
        srgb_decode(np.array([-0.1]))
    with pytest.raises(KernelError):
        sabine_rt60(10, 20, 0)
    with pytest.raises(KernelError):
        fractional_impulse(-1, 10)
    result = measure_cepstral_timbre_consistency(np.zeros(100), np.zeros(100), 16000)
    assert result.status == "INSUFFICIENT_VOICED_FRAMES"
    with pytest.raises(KernelError):
        render_spatial_dialogue(np.ones(3), np.ones((3, 1)))
    with pytest.raises(KernelError):
        jaw_aperture([], 5)


def test_voiced_cepstrum_and_acoustic_refusals() -> None:
    from nexus_ai_agent.creative.chronos.acoustics_lipsync import (
        synthesize_stereo_room_ir,
    )

    rate = 16000
    time = np.arange(rate) / rate
    voiced = (0.2 * np.sin(2 * np.pi * 120 * time)).astype(np.float64)
    measured = measure_cepstral_timbre_consistency(voiced, voiced * 2, rate)
    assert measured.status == "OK"
    assert measured.metric_scope == "ACOUSTIC_TIMBRE_CONSISTENCY"
    assert measured.mean_distance_db is not None and measured.mean_distance_db < 2.0
    with pytest.raises(KernelError):
        measure_cepstral_timbre_consistency(voiced, voiced, 0)
    with pytest.raises(KernelError):
        sabine_rt60(0, 10, 0.2)
    with pytest.raises(KernelError):
        sabine_rt60(10, 20, 1)
    with pytest.raises(KernelError):
        shoebox_rir(np.zeros(2), np.zeros(3), np.ones(3), 0.2, 8000)
    with pytest.raises(KernelError):
        jaw_aperture([(RationalTimestamp(0, Fraction(1, 30)), 1.2)], 4)
    with pytest.raises(KernelError):
        synthesize_stereo_room_ir(
            np.ones(3),
            np.array([2.0, 2.0, 1.0]),
            np.array([1.0, 1.0, 1.0]),
            np.array([0.0, 1.0, 0.0]),
            8000,
            0.2,
        )
    with pytest.raises(KernelError):
        synthesize_stereo_room_ir(
            np.ones(3),
            np.array([1.0, 1.0, 1.0]),
            np.array([1.0, 1.0, 1.0]),
            np.array([1.0, 0.0, 0.0]),
            8000,
            0.2,
        )


def test_png_writer_and_probe_refusal_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    from types import SimpleNamespace

    with pytest.raises(KernelError):
        write_rgb8_png(tmp_path / "bad.png", np.zeros((2, 2, 4), dtype=np.uint8))
    with pytest.raises(KernelError):
        write_rgb8_png(tmp_path / "empty.png", np.zeros((0, 2, 3), dtype=np.uint8))
    for payload in (b"bad", b"\x89PNG\r\n\x1a\n"):
        path = tmp_path / "broken.png"
        path.write_bytes(payload)
        with pytest.raises(KernelError):
            read_rgb8_png(path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout='{"streams":[]}'),
    )
    with pytest.raises(KernelError):
        ffprobe_signature(tmp_path / "none")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stderr="",
            stdout='{"streams":[{"codec_name":"x","time_base":"1/1"}]}',
        ),
    )
    with pytest.raises(KernelError):
        ffprobe_signature(tmp_path / "missing-metadata")


def test_persisted_relighting_refusals(tmp_path: Path) -> None:
    a = np.zeros((4, 4, 3), dtype=np.float64)
    with pytest.raises(KernelError):
        verify_persisted_relighting(a, a, a, tmp_path / "same.png")
    with pytest.raises(KernelError):
        verify_persisted_relighting(a, a, np.ones((3, 3, 3)), tmp_path / "shape.png")
    with pytest.raises(KernelError):
        verify_structure(a, np.ones_like(a), 1.1)


def test_timeline_internal_validation_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import subprocess
    from types import SimpleNamespace

    from nexus_ai_agent.creative.chronos import timeline

    with pytest.raises(KernelError):
        timeline._streams({"streams": []})
    with pytest.raises(KernelError):
        timeline._fraction_duration({})
    with pytest.raises(KernelError):
        timeline._fraction_duration({"format": {"duration": "0"}})
    with pytest.raises(KernelError):
        timeline._stream_key({"codec_name": "x"})
    with pytest.raises(KernelError):
        timeline._check_packets(
            {
                "packets": [
                    {"stream_index": 0, "pts": 2, "dts": 2},
                    {"stream_index": 0, "pts": 1, "dts": 1},
                ]
            }
        )
    with pytest.raises(KernelError):
        timeline._check_packets({"packets": [{"stream_index": 0}]})
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="failure", stdout=""),
    )
    with pytest.raises(KernelError):
        timeline._probe(Path("/absolute/fixture"), "ffprobe", packets=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout="not-json"),
    )
    with pytest.raises(KernelError):
        timeline._probe(Path("/absolute/fixture"), "ffprobe", packets=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps({"streams": []})
        ),
    )
    with pytest.raises(KernelError):
        timeline._probe(Path("/absolute/fixture"), "ffprobe", packets=False)


def test_concat_failure_cleans_staging_and_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from nexus_ai_agent.creative.chronos import timeline

    audio_stream: dict[str, object] = {
        "codec_type": "audio",
        "codec_name": "pcm_s16le",
        "profile": None,
        "sample_fmt": "s16",
        "time_base": "1/48000",
        "sample_rate": "48000",
        "channels": 1,
        "channel_layout": "mono",
        "extradata_hash": "pcm",
    }
    data: dict[str, object] = {
        "streams": [audio_stream],
        "format": {"duration": "1/2"},
        "packets": [{"stream_index": 0, "pts": 0, "dts": 0}],
    }
    monkeypatch.setattr(timeline, "_probe", lambda *args, **kwargs: data)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: type("Completed", (), {"returncode": 1, "stderr": "bad"})(),
    )
    first, second = tmp_path / "first.wav", tmp_path / "second.wav"
    first.touch()
    second.touch()
    destination = tmp_path / "failed.wav"
    with pytest.raises(KernelError):
        timeline.concat_copy([first, second], destination)
    assert not destination.exists()
