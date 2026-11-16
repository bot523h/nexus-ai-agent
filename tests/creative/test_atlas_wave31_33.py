"""WAVE 31-33 — falsifiable tests for the physical ATLAS ocean and render path.

What these tests refuse to accept:

* a JONSWAP spectrum whose fetch does not move the peak frequency, the energy or the
  surface RMS (fetch 1 km versus 100 km at a fixed 10 m/s wind and fixed seed);
* a real height field with energy in the Nyquist bins, or one that is not real;
* a rendered or encoded video whose pixels do not depend on the physical inputs;
* any ``testsrc`` / ``lavfi`` test pattern in the encode path, in the source or in the
  FFmpeg argument list;
* a streaming encode that holds more than 32 MiB of Python heap (the clip is never
  buffered whole).

The FFmpeg-backed tests run a real ``ffmpeg``/``ffprobe`` and are skipped when those
binaries are not on PATH. Nothing here shells out to a test pattern.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import media_sine as media
from nexus_ai_agent.creative.atlas import olympus_master as om
from nexus_ai_agent.creative.atlas import physical_frame_stream as physical
from nexus_ai_agent.creative.atlas import spectral_ocean_swe as ocean
from nexus_ai_agent.creative.atlas.showrunner_bible import Character, Episode, ShowrunnerBible
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

requires_ffmpeg = pytest.mark.skipif(
    not media.ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH"
)
ATLAS_SRC = Path(physical.__file__).resolve().parent
MEMORY_CAP_BYTES = 32 * 1024 * 1024


@pytest.fixture()
def workspace(tmp_path: Path) -> WorkspaceFilesystem:
    return WorkspaceFilesystem(tmp_path)


def _spectrum(fetch: float, wind: float = 10.0, grid: int = 256, extent: float = 180.0) -> Any:
    kx, ky = ocean.fft_wavenumber_grid(grid, grid, extent)
    return ocean.jonswap_spectrum(kx, ky, wind_speed=wind, fetch=fetch)


# --- W31: fetch changes the physical sea ---------------------------------------------


def test_fetch_lengthens_the_dominant_wave_and_raises_energy() -> None:
    short = _spectrum(1.0e3)
    long_ = _spectrum(1.0e5)
    assert long_.peak_wavenumber < short.peak_wavenumber
    assert long_.peak_omega < short.peak_omega
    assert long_.variance > 5.0 * short.variance
    assert long_.significant_wave_height > 5.0 * short.significant_wave_height
    k_grid = np.hypot(*ocean.fft_wavenumber_grid(256, 256, 180.0))
    k_short = k_grid[np.unravel_index(np.argmax(short.amplitudes), short.amplitudes.shape)]
    k_long = k_grid[np.unravel_index(np.argmax(long_.amplitudes), long_.amplitudes.shape)]
    assert k_long < k_short


def test_fetch_changes_the_surface_rms_and_height_field() -> None:
    short = _spectrum(1.0e3)
    long_ = _spectrum(1.0e5)
    h_short = ocean.spectral_height_field(short, seed=7, extent=180.0, time=0.0)
    h_long = ocean.spectral_height_field(long_, seed=7, extent=180.0, time=0.0)
    rms_short = float(h_short.std())
    rms_long = float(h_long.std())
    assert rms_long > 4.0 * rms_short
    assert not np.array_equal(h_short, h_long)


def test_wind_speed_and_fetch_formulas_are_pinned() -> None:
    spec = _spectrum(1.0e4, wind=8.0)
    omega_p = 22.0 * (ocean.GRAVITY**2 / (8.0 * 1.0e4)) ** (1.0 / 3.0)
    x_tilde = ocean.GRAVITY * 1.0e4 / 64.0
    assert spec.peak_omega == pytest.approx(omega_p, rel=1e-12)
    assert spec.phillips_alpha == pytest.approx(0.076 * x_tilde**-0.22, rel=1e-12)
    assert spec.peak_wavenumber == pytest.approx(omega_p**2 / ocean.GRAVITY, rel=1e-12)


def test_spectrum_energy_is_integral_of_jonswap_not_fixed_hs() -> None:
    # The total energy must move with fetch at fixed wind: there is no fixed-H_s
    # renormalisation left to cancel the fetch dependence.
    variances = [_spectrum(f).variance for f in (1.0e3, 1.0e4, 1.0e5)]
    assert variances[0] < variances[1] < variances[2]


# --- W31: the Hermitian real-field construction (golden rule 6) ----------------------


def test_height_field_is_real_and_finite() -> None:
    h = ocean.spectral_height_field(_spectrum(1.0e5), seed=3, extent=180.0, time=2.5)
    assert h.dtype == np.float64
    assert np.all(np.isfinite(h))


def test_nyquist_bins_are_zero_for_even_dimensions() -> None:
    spec = _spectrum(1.0e5, grid=64, extent=180.0)
    h = ocean.spectral_height_field(spec, seed=11, extent=180.0, time=0.7)
    spectrum = np.fft.fft2(h)
    scale = float(np.abs(spectrum).max())
    assert scale > 0.0
    assert float(np.abs(spectrum[:, 32]).max()) < 1e-9 * scale  # kx Nyquist column
    assert float(np.abs(spectrum[32, :]).max()) < 1e-9 * scale  # ky Nyquist row


def test_odd_dimensions_keep_every_bin() -> None:
    kx, ky = ocean.fft_wavenumber_grid(33, 33, 180.0)
    spec = ocean.jonswap_spectrum(kx, ky, wind_speed=10.0, fetch=1.0e5)
    h = ocean.spectral_height_field(spec, seed=5, extent=180.0, time=0.0)
    assert h.shape == (33, 33)
    assert np.all(np.isfinite(h))


def test_height_field_evolves_in_time() -> None:
    spec = _spectrum(1.0e5)
    h0 = ocean.spectral_height_field(spec, seed=7, extent=180.0, time=0.0)
    h1 = ocean.spectral_height_field(spec, seed=7, extent=180.0, time=1.0)
    assert not np.allclose(h0, h1)


# --- W32: frames depend on the physical inputs ----------------------------------------


def _frame(ocean_state: physical.OceanState, index: int = 3, side: int = 64) -> np.ndarray:
    raw = physical.generate_physical_atlas_frame_rgb24(
        index, 24, side, side, ocean_state, physical.AtmosphereState(), seed=7
    )
    return np.frombuffer(raw, dtype=np.uint8).reshape(side, side, 3)


def test_frame_bytes_have_the_declared_geometry() -> None:
    raw = physical.generate_physical_atlas_frame_rgb24(
        0, 2, 48, 32, physical.OceanState(), physical.AtmosphereState(), seed=1
    )
    assert len(raw) == 48 * 32 * 3


def test_frames_are_deterministic_and_change_over_time() -> None:
    state = physical.OceanState()
    a = _frame(state, index=3)
    assert np.array_equal(a, _frame(state, index=3))
    assert not np.array_equal(a, _frame(state, index=4))


def test_frames_change_with_wind_speed() -> None:
    calm = _frame(physical.OceanState(wind_speed=6.0))
    gale = _frame(physical.OceanState(wind_speed=14.0))
    assert float(np.mean(np.abs(calm.astype(np.int16) - gale.astype(np.int16)))) > 2.0


def test_frames_change_with_fetch() -> None:
    short = _frame(physical.OceanState(fetch=1.0e3))
    long_ = _frame(physical.OceanState(fetch=1.0e5))
    assert float(np.mean(np.abs(short.astype(np.int16) - long_.astype(np.int16)))) > 2.0


def test_sky_is_brighter_at_noon_than_at_dusk() -> None:
    noon = _frame(physical.OceanState())
    dusk = np.frombuffer(
        physical.generate_physical_atlas_frame_rgb24(
            3,
            24,
            64,
            64,
            physical.OceanState(),
            physical.AtmosphereState(sun_elevation_deg=4.0),
            seed=7,
        ),
        dtype=np.uint8,
    ).reshape(64, 64, 3)
    assert float(noon[:32].mean()) > float(dusk[:32].mean())
    # Dusk sky is red-shifted: the red channel dominates the blue channel above the horizon.
    assert float(dusk[:32, :, 0].mean()) > float(dusk[:32, :, 2].mean())


def test_invalid_frame_requests_are_refused() -> None:
    state = physical.OceanState()
    atmos = physical.AtmosphereState()
    with pytest.raises(physical.FrameError):
        physical.generate_physical_atlas_frame_rgb24(5, 5, 32, 32, state, atmos, seed=1)
    with pytest.raises(physical.FrameError):
        physical.generate_physical_atlas_frame_rgb24(0, 1, 4, 32, state, atmos, seed=1)
    with pytest.raises(ocean.OceanError):
        physical.generate_physical_atlas_frame_rgb24(0, 1, 32, 32, state, atmos, seed=-1)


def test_unresolvable_fetch_is_refused_not_aliased() -> None:
    # fetch 1 km with a 16-cell grid puts the peak far above Nyquist/2.
    with pytest.raises(physical.FrameError, match="Nyquist"):
        physical.OceanState(fetch=1.0e3, grid=16)


def test_atmosphere_bounds_are_enforced() -> None:
    with pytest.raises(physical.FrameError):
        physical.AtmosphereState(sun_elevation_deg=0.0)
    with pytest.raises(physical.FrameError):
        physical.AtmosphereState(turbidity=0.0)
    with pytest.raises(physical.FrameError):
        physical.AtmosphereState(sun_intensity=-1.0)
    with pytest.raises(physical.FrameError):
        physical.AtmosphereState(camera_height_m=0.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_physical_and_ocean_inputs_are_refused(bad: float) -> None:
    for kw in (
        {"sun_elevation_deg": bad},
        {"turbidity": bad},
        {"sun_intensity": bad},
        {"camera_height_m": bad},
        {"sun_azimuth_deg": bad},
        {"fov_deg": bad},
    ):
        with pytest.raises(physical.FrameError):
            physical.AtmosphereState(**kw)

    for kw in (
        {"wind_speed": bad},
        {"fetch": bad},
        {"fps": bad},
        {"extent": bad},
        {"wind_dir": (bad, 0.0)},
        {"wind_dir": (1.0, bad)},
    ):
        with pytest.raises(physical.FrameError):
            physical.OceanState(**kw)  # type: ignore[arg-type]

    kx, ky = ocean.fft_wavenumber_grid(16, 16, 100.0)
    for kw in (
        {"wind_speed": bad},
        {"peak_sharpness": bad},
        {"fetch": bad},
        {"gravity": bad},
        {"amplitude_scale": bad},
        {"wind_dir": (bad, 0.0)},
        {"wind_dir": (1.0, bad)},
    ):
        with pytest.raises(ocean.OceanError):
            ocean.jonswap_spectrum(kx, ky, **kw)  # type: ignore[arg-type]

    spec = ocean.jonswap_spectrum(kx, ky)
    with pytest.raises(ocean.OceanError):
        ocean.spectral_height_field(spec, extent=bad)
    with pytest.raises(ocean.OceanError):
        ocean.spectral_height_field(spec, time=bad)


def test_stalled_consumer_pipe_write_aborts_under_one_second(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def _stalled_popen(_argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(media.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media.subprocess, "Popen", _stalled_popen)
    big = b"\x2a" * (256 * 256 * 3)
    t0 = time.monotonic()
    with pytest.raises(media.MediaSineError, match="wall-clock timeout"):
        media.encode_rgb24_stream(
            workspace,
            "stalled.mp4",
            lambda _i: big,
            width=256,
            height=256,
            frames=8,
            fps=8,
            timeout=0.15,
        )
    assert time.monotonic() - t0 < 1.0
    assert spawned and spawned[0].poll() is not None


def test_memory_of_the_frame_stream_stays_under_the_cap() -> None:
    state = physical.OceanState()
    atmos = physical.AtmosphereState()
    physical.spectrum_for.cache_clear()
    tracemalloc.start()
    try:
        for index in range(12):
            physical.generate_physical_atlas_frame_rgb24(index, 12, 256, 256, state, atmos, 7)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < MEMORY_CAP_BYTES, f"peak {peak} bytes exceeds {MEMORY_CAP_BYTES}"


# --- W32: no test pattern anywhere in the encode path ------------------------------


@pytest.mark.parametrize(
    "name",
    ["media_sine.py", "olympus_master.py", "physical_frame_stream.py", "spectral_ocean_swe.py"],
)
def test_no_test_pattern_string_in_the_encode_path(name: str) -> None:
    text = (ATLAS_SRC / name).read_text(encoding="utf-8")
    assert "testsrc" not in text
    assert "lavfi" not in text


@requires_ffmpeg
def test_encode_argv_contains_no_test_pattern_and_reads_stdin(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    real_popen = subprocess.Popen

    def _recording_popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        seen.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(media.subprocess, "Popen", _recording_popen)
    state = physical.OceanState(fps=8.0)
    atmos = physical.AtmosphereState()
    media.encode_rgb24_stream(
        workspace,
        "argv/clip.mp4",
        lambda i: physical.generate_physical_atlas_frame_rgb24(i, 4, 32, 32, state, atmos, 7),
        width=32,
        height=32,
        frames=4,
        fps=8,
    )
    # ffprobe also goes through Popen; the encode argv is the ffmpeg invocation.
    (argv,) = [a for a in seen if a and a[0].endswith("ffmpeg")]
    joined = " ".join(argv)
    assert "testsrc" not in joined and "lavfi" not in joined
    assert argv[argv.index("-f") + 1] == "rawvideo"
    assert argv[argv.index("-pix_fmt") + 1] == "rgb24"
    assert argv[argv.index("-s") + 1] == "32x32"
    assert argv[argv.index("-i") + 1] == "pipe:0"


# --- W32: the decoded video depends on the physical inputs ------------------------


def _decode_rgb24(path: Path, width: int, height: int) -> np.ndarray:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    proc = subprocess.run(  # noqa: S603 - fixed argument list
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return np.frombuffer(proc.stdout, dtype=np.uint8).reshape(-1, height, width, 3)


def _encode_physical(
    workspace: WorkspaceFilesystem, rel: str, state: physical.OceanState, frames: int = 6
) -> media.MediaSineResult:
    atmos = physical.AtmosphereState()
    return media.encode_rgb24_stream(
        workspace,
        rel,
        lambda i: physical.generate_physical_atlas_frame_rgb24(
            i, frames, 64, 64, state, atmos, seed=7
        ),
        width=64,
        height=64,
        frames=frames,
        fps=8,
    )


def _iso_boxes(data: bytes) -> set[str]:
    types: set[str] = set()
    offset = 0
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8].decode("latin-1")
        types.add(kind)
        if size == 1:
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
        if size < 8:
            break
        offset += size
    return types


@requires_ffmpeg
def test_encoded_mp4_has_real_iso_boxes_and_ffprobe_geometry(
    workspace: WorkspaceFilesystem,
) -> None:
    res = _encode_physical(workspace, "iso/clip.mp4", physical.OceanState())
    data = (workspace.root / "iso" / "clip.mp4").read_bytes()
    assert {"ftyp", "moov", "mdat"} <= _iso_boxes(data)
    assert res.probe.codec == "h264"
    assert (res.probe.width, res.probe.height) == (64, 64)
    assert res.probe.nb_frames == 6
    assert res.probe.pix_fmt == "yuv420p"


@requires_ffmpeg
def test_decoded_frames_change_with_wind_speed_and_fetch(workspace: WorkspaceFilesystem) -> None:
    base = physical.OceanState(wind_speed=10.0, fetch=1.0e5)
    windy = physical.OceanState(wind_speed=14.0, fetch=1.0e5)
    short = physical.OceanState(wind_speed=10.0, fetch=1.0e3)
    _encode_physical(workspace, "phys/base.mp4", base)
    _encode_physical(workspace, "phys/windy.mp4", windy)
    _encode_physical(workspace, "phys/short.mp4", short)
    decoded_base = _decode_rgb24(workspace.root / "phys" / "base.mp4", 64, 64)
    decoded_windy = _decode_rgb24(workspace.root / "phys" / "windy.mp4", 64, 64)
    decoded_short = _decode_rgb24(workspace.root / "phys" / "short.mp4", 64, 64)
    assert decoded_base.shape == (6, 64, 64, 3)
    assert float(np.mean(np.abs(decoded_base.astype(np.int16) - decoded_windy))) > 1.0
    assert float(np.mean(np.abs(decoded_base.astype(np.int16) - decoded_short))) > 1.0


@requires_ffmpeg
def test_decoded_frames_are_not_a_repeated_pattern(workspace: WorkspaceFilesystem) -> None:
    _encode_physical(workspace, "phys/seq.mp4", physical.OceanState(), frames=6)
    decoded = _decode_rgb24(workspace.root / "phys" / "seq.mp4", 64, 64)
    consecutive = [
        float(np.mean(np.abs(decoded[i].astype(np.int16) - decoded[i + 1]))) for i in range(5)
    ]
    assert min(consecutive) > 0.05
    assert float(decoded.std()) > 5.0


# --- W33: the master render writes physical frames and streams them verified ------


def _bible() -> ShowrunnerBible:
    bible = ShowrunnerBible()
    bible.add_character(Character("a", "Alice"))
    bible.add_episode(Episode(1, "Pilot"))
    return bible


@requires_ffmpeg
def test_master_render_persists_physical_frames_and_encodes_them(
    workspace: WorkspaceFilesystem,
) -> None:
    ledger = om.render_master(_bible(), workspace, side=64, frames=8, fps=8)
    names = {s.name: s for s in ledger.stages}
    assert names["encode_clip"].status == "COMPUTED"
    frames_dir = workspace.root / "olympus_master" / "frames"
    raws = [
        np.frombuffer((frames_dir / f"frame_{i:04d}.raw").read_bytes(), np.uint8) for i in range(8)
    ]
    assert all(r.size == 64 * 64 * 3 for r in raws)
    assert not np.array_equal(raws[0], raws[1])
    decoded = _decode_rgb24(workspace.root / "olympus_master" / "master_clip.mp4", 64, 64)
    assert decoded.shape == (8, 64, 64, 3)


@requires_ffmpeg
def test_master_render_uses_no_test_pattern(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    real_popen = subprocess.Popen

    def _recording_popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        seen.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(media.subprocess, "Popen", _recording_popen)
    om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    assert seen
    assert all("testsrc" not in " ".join(argv) for argv in seen)


@requires_ffmpeg
def test_streaming_encode_stays_under_the_memory_cap(workspace: WorkspaceFilesystem) -> None:
    # 200 frames of 256x256 rgb24 are ~39 MB; a buffered clip would exceed the cap.
    state = physical.OceanState()
    atmos = physical.AtmosphereState()
    frames = 200
    physical.spectrum_for.cache_clear()
    tracemalloc.start()
    try:
        media.encode_rgb24_stream(
            workspace,
            "stream/clip.mp4",
            lambda i: physical.generate_physical_atlas_frame_rgb24(
                i, frames, 256, 256, state, atmos, 7
            ),
            width=256,
            height=256,
            frames=frames,
            fps=24,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < MEMORY_CAP_BYTES, f"peak {peak} bytes exceeds {MEMORY_CAP_BYTES}"
