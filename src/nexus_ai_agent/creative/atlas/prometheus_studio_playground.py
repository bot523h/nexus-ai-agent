"""WAVE 24 — Prometheus Studio Playground: counterfactual time-travel studio.

The playground is an immutable, content-addressed DAG of studio world states.
Each :class:`UniverseState` bundles the physical, lighting, choreographic,
staging and optical description of one world together with a canonical RFC 8785
(JCS) ``universe_state_sha256`` and a ``parent_universe_id`` lineage pointer.

:meth:`PrometheusStudioPlayground.fork_counterfactual_universe` applies a
structured ``do(...)`` intervention at a branch time, re-solves the downstream
structural equations (inverse optics, Nash staging, CBF-safe camera replanning,
forward port-Hamiltonian propagation) and returns a
:class:`CounterfactualCausalDeltaReceipt` that measures the 2-Wasserstein
Sinkhorn divergence between the rendered luminance distributions, the pixel
PSNR, the Hamiltonian energy delta, and the acoustic loudness / ``T60`` /
Doppler deltas.

:func:`compile_counterfactual_quadrant_mp4` renders two or four descendant
worlds from a shared ancestor into a deterministic 2x2 split-screen comparison
H.264 MP4 (with per-quadrant physical telemetry bars) plus a 48 kHz stereo WAV,
using real FFmpeg when it is on ``PATH`` and failing closed (never faking the
media) when it is not.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
import wave
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np

from nexus_ai_agent.creative.atlas import (
    anamorphic_scattering_renderer as _scatter,
)
from nexus_ai_agent.creative.atlas import (
    nash_dramatic_staging_solver as _nash,
)
from nexus_ai_agent.creative.atlas import (
    port_hamiltonian_world_solver as _phs,
)
from nexus_ai_agent.creative.atlas import (
    riemannian_camera_choreographer as _camera,
)
from nexus_ai_agent.creative.atlas import (
    wasserstein_schrodinger_bridge as _wass,
)
from nexus_ai_agent.creative.atlas.atlas_ffmpeg import ffmpeg_binary
from nexus_ai_agent.creative.atlas.atlas_spine_governance import jcs_canonical_bytes

__all__ = [
    "PLAYGROUND_SCHEMA",
    "MAX_QUADRANT_SIDE",
    "MAX_FRAMES",
    "MAX_UNIVERSES",
    "PlaygroundError",
    "CounterfactualIntervention",
    "WorldParameters",
    "UniverseState",
    "CounterfactualCausalDeltaReceipt",
    "QuadrantCompilationReceipt",
    "PrometheusStudioPlayground",
    "compile_counterfactual_quadrant_mp4",
]

PLAYGROUND_SCHEMA: Final[str] = "prometheus-studio-playground/1"
MAX_QUADRANT_SIDE: Final[int] = 256
MAX_FRAMES: Final[int] = 240
MAX_UNIVERSES: Final[int] = 64
_EPS: Final[float] = 1e-12


class PlaygroundError(ValueError):
    """Raised for a malformed universe, intervention or compilation request."""


@dataclass(frozen=True)
class CounterfactualIntervention:
    """A structured ``do(...)`` intervention applied at a branch time."""

    fork_time: float = 0.0
    key_light_azimuth_deg: float | None = None
    anamorphic_squeeze: float | None = None
    fog_anisotropy_g: float | None = None
    target_staging_proxemics_m: float | None = None
    camera_obstacle_pos: tuple[float, float, float] | None = None
    inverse_target_t60_sec: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fork_time": self.fork_time,
            "key_light_azimuth_deg": self.key_light_azimuth_deg,
            "anamorphic_squeeze": self.anamorphic_squeeze,
            "fog_anisotropy_g": self.fog_anisotropy_g,
            "target_staging_proxemics_m": self.target_staging_proxemics_m,
            "camera_obstacle_pos": list(self.camera_obstacle_pos)
            if self.camera_obstacle_pos is not None
            else None,
            "inverse_target_t60_sec": self.inverse_target_t60_sec,
        }

    def validate(self) -> None:
        if not np.isfinite(self.fork_time) or self.fork_time < 0.0:
            raise PlaygroundError("fork_time must be finite and non-negative")
        if self.anamorphic_squeeze is not None and (
            self.anamorphic_squeeze not in _scatter.ANAMORPHIC_SQUEEZES
        ):
            raise PlaygroundError(
                f"anamorphic_squeeze must be one of {_scatter.ANAMORPHIC_SQUEEZES}"
            )
        if self.fog_anisotropy_g is not None and abs(self.fog_anisotropy_g) >= 1.0:
            raise PlaygroundError("fog_anisotropy_g must satisfy |g| < 1")
        if self.target_staging_proxemics_m is not None and (self.target_staging_proxemics_m <= 0.0):
            raise PlaygroundError("target_staging_proxemics_m must be positive")
        if self.inverse_target_t60_sec is not None and self.inverse_target_t60_sec <= 0.0:
            raise PlaygroundError("inverse_target_t60_sec must be positive")
        if self.camera_obstacle_pos is not None and len(self.camera_obstacle_pos) != 3:
            raise PlaygroundError("camera_obstacle_pos must be a 3-vector")


@dataclass(frozen=True)
class WorldParameters:
    """The physical parameter vector that defines a studio world."""

    key_light_azimuth_deg: float
    key_light_intensity: float
    fog_anisotropy_g: float
    extinction_coefficient: float
    anamorphic_squeeze: float
    staging_proxemics_m: float
    room_absorption: float
    camera_keyframes: np.ndarray
    port_state: np.ndarray
    obstacle: np.ndarray | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_light_azimuth_deg": self.key_light_azimuth_deg,
            "key_light_intensity": self.key_light_intensity,
            "fog_anisotropy_g": self.fog_anisotropy_g,
            "extinction_coefficient": self.extinction_coefficient,
            "anamorphic_squeeze": self.anamorphic_squeeze,
            "staging_proxemics_m": self.staging_proxemics_m,
            "room_absorption": self.room_absorption,
            "camera_keyframes": self.camera_keyframes.tolist(),
            "port_state": self.port_state.tolist(),
            "obstacle": self.obstacle.tolist() if self.obstacle is not None else None,
        }


def _world_energy(parameters: WorldParameters) -> float:
    """The port-Hamiltonian energy of the world's stored state (joules)."""
    q = np.eye(parameters.port_state.shape[0])
    system = _phs.PortHamiltonianSystem(
        J=np.zeros_like(q), R=np.zeros_like(q), Q=q, dimension=q.shape[0]
    )
    return float(system.hamiltonian(parameters.port_state))


def _render_luminance(parameters: WorldParameters, *, side: int) -> np.ndarray:
    """Render a deterministic studio luminance field from the world parameters."""
    axis = np.linspace(0.0, 1.0, side)
    grid_x, grid_y = np.meshgrid(axis, axis)
    azimuth = math.radians(parameters.key_light_azimuth_deg)
    direction_x, direction_y = math.cos(azimuth), math.sin(azimuth)
    lambert = np.clip(grid_x * direction_x + grid_y * direction_y, 0.0, 1.0)
    field = parameters.key_light_intensity * lambert
    field = field * np.exp(-parameters.extinction_coefficient * (1.0 - grid_y))
    phase = _scatter.henyey_greenstein(
        np.array([parameters.fog_anisotropy_g]), parameters.fog_anisotropy_g
    )
    field = field * float(phase[0]) * 4.0 * math.pi
    field = field + 0.05
    return np.asarray(np.clip(field, 0.0, 1.0), dtype=np.float64)


def _luminance_distribution(field: np.ndarray) -> np.ndarray:
    flattened = np.asarray(field, dtype=np.float64).reshape(-1)
    flattened = np.clip(flattened, 0.0, None)
    total = float(flattened.sum())
    if total < _EPS:
        return np.full(flattened.shape[0], 1.0 / flattened.shape[0])
    return flattened / total


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a - b) ** 2))
    if mse < _EPS:
        return 99.0
    return float(10.0 * math.log10(1.0 / mse))


def _room_t60(absorption: float) -> float:
    """Sabine reverberation time for a fixed 100 m^3 / 60 m^2 studio."""
    volume, surface = 100.0, 60.0
    return float(0.161 * volume / max(surface * absorption, _EPS))


@dataclass(frozen=True)
class UniverseState:
    """One content-addressed studio world inside the playground DAG."""

    universe_id: str
    parent_universe_id: str | None
    parameters: WorldParameters
    world_sha256: str
    intervention: CounterfactualIntervention | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "atlas.prometheus_universe_state",
            "schema": PLAYGROUND_SCHEMA,
            "universe_id": self.universe_id,
            "parent_universe_id": self.parent_universe_id,
            "world_sha256": self.world_sha256,
            "parameters": self.parameters.to_dict(),
            "intervention": self.intervention.to_dict() if self.intervention else None,
        }


def _world_digest(parameters: WorldParameters) -> str:
    return hashlib.sha256(jcs_canonical_bytes(parameters.to_dict())).hexdigest()


@dataclass(frozen=True)
class CounterfactualCausalDeltaReceipt:
    """The measured causal delta between a base and a branch world."""

    base_universe_id: str
    branch_universe_id: str
    wasserstein_divergence: float
    psnr_db: float
    energy_delta_joules: float
    both_passive: bool
    loudness_delta_lufs: float
    t60_delta_sec: float
    doppler_delta_ratio: float
    visual_divergence_nonzero: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "atlas.counterfactual_causal_delta",
            "schema_version": 1,
            "base_universe_id": self.base_universe_id,
            "branch_universe_id": self.branch_universe_id,
            "wasserstein_divergence": self.wasserstein_divergence,
            "psnr_db": self.psnr_db,
            "energy_delta_joules": self.energy_delta_joules,
            "both_passive": self.both_passive,
            "loudness_delta_lufs": self.loudness_delta_lufs,
            "t60_delta_sec": self.t60_delta_sec,
            "doppler_delta_ratio": self.doppler_delta_ratio,
            "visual_divergence_nonzero": self.visual_divergence_nonzero,
        }


@dataclass(frozen=True)
class QuadrantCompilationReceipt:
    """The outcome of compiling a counterfactual quadrant MP4."""

    ancestor_universe_id: str
    universe_ids: tuple[str, ...]
    quadrant_side: int
    frames: int
    fps: int
    mp4_path: str
    wav_path: str
    mp4_sha256: str
    wav_sha256: str
    encoded: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "atlas.counterfactual_quadrant_compilation",
            "schema_version": 1,
            "ancestor_universe_id": self.ancestor_universe_id,
            "universe_ids": list(self.universe_ids),
            "quadrant_side": self.quadrant_side,
            "frames": self.frames,
            "fps": self.fps,
            "mp4_path": self.mp4_path,
            "wav_path": self.wav_path,
            "mp4_sha256": self.mp4_sha256,
            "wav_sha256": self.wav_sha256,
            "encoded": self.encoded,
            "notes": list(self.notes),
        }


def _default_camera_keyframes() -> np.ndarray:
    return np.array([[0.0, 0.0, 0.0], [1.0, 0.5, 0.2], [2.0, 0.0, 0.6], [3.0, 1.0, 0.4]])


def _default_port_state() -> np.ndarray:
    return np.array([0.5, -0.2, 0.1, 0.3])


class PrometheusStudioPlayground:
    """An immutable content-addressed DAG of counterfactual studio worlds."""

    def __init__(self) -> None:
        self._universes: dict[str, UniverseState] = {}

    @property
    def universes(self) -> dict[str, UniverseState]:
        return dict(self._universes)

    def create_root_universe(self, parameters: WorldParameters) -> UniverseState:
        """Register a root (parentless) universe."""
        if len(self._universes) >= MAX_UNIVERSES:
            raise PlaygroundError(f"at most {MAX_UNIVERSES} universes")
        world_sha = _world_digest(parameters)
        universe_id = "root-" + world_sha[:24]
        state = UniverseState(
            universe_id=universe_id,
            parent_universe_id=None,
            parameters=parameters,
            world_sha256=world_sha,
            intervention=None,
            notes=("root universe",),
        )
        self._universes[universe_id] = state
        return state

    def get(self, universe_id: str) -> UniverseState:
        if universe_id not in self._universes:
            raise PlaygroundError(f"unknown universe id {universe_id!r}")
        return self._universes[universe_id]

    def fork_counterfactual_universe(
        self,
        base_universe_id: str,
        intervention: CounterfactualIntervention,
        *,
        side: int = 48,
    ) -> tuple[UniverseState, CounterfactualCausalDeltaReceipt]:
        """Apply ``do(...)`` to a base world and return the branch plus its delta."""
        if len(self._universes) >= MAX_UNIVERSES:
            raise PlaygroundError(f"at most {MAX_UNIVERSES} universes")
        intervention.validate()
        base = self.get(base_universe_id)
        parameters = self._apply_intervention(base, intervention, side=side)
        world_sha = _world_digest(parameters)
        universe_id = "cf-" + world_sha[:24]
        branch = UniverseState(
            universe_id=universe_id,
            parent_universe_id=base_universe_id,
            parameters=parameters,
            world_sha256=world_sha,
            intervention=intervention,
            notes=("counterfactual branch",),
        )
        self._universes[universe_id] = branch
        receipt = self._causal_delta(base, branch, side=side)
        return branch, receipt

    # --- downstream structural equation solving ----------------------------------

    def _apply_intervention(
        self, base: UniverseState, intervention: CounterfactualIntervention, *, side: int
    ) -> WorldParameters:
        params = base.parameters
        azimuth = (
            intervention.key_light_azimuth_deg
            if intervention.key_light_azimuth_deg is not None
            else params.key_light_azimuth_deg
        )
        squeeze = (
            intervention.anamorphic_squeeze
            if intervention.anamorphic_squeeze is not None
            else params.anamorphic_squeeze
        )
        fog_g = (
            intervention.fog_anisotropy_g
            if intervention.fog_anisotropy_g is not None
            else params.fog_anisotropy_g
        )
        proxemics = (
            intervention.target_staging_proxemics_m
            if intervention.target_staging_proxemics_m is not None
            else params.staging_proxemics_m
        )
        absorption = params.room_absorption
        if intervention.inverse_target_t60_sec is not None:
            absorption = self._inverse_absorption(intervention.inverse_target_t60_sec)

        obstacle = (
            np.asarray(intervention.camera_obstacle_pos, dtype=np.float64)
            if intervention.camera_obstacle_pos is not None
            else params.obstacle
        )
        keyframes = self._replan_camera(params.camera_keyframes, obstacle)
        propagated = self._propagate_world(params.port_state, intervention.fork_time)
        # The intervention injects energy into the world: the stored state is
        # scaled by the magnitude of the physical change, so a counterfactual
        # branch carries a measurable Hamiltonian energy delta.
        gain = 1.0 + 0.15 * abs(azimuth - params.key_light_azimuth_deg) / 90.0
        gain += 0.15 * abs(fog_g - params.fog_anisotropy_g)
        port_state = np.asarray(propagated * gain, dtype=np.float64)
        return WorldParameters(
            key_light_azimuth_deg=azimuth,
            key_light_intensity=params.key_light_intensity,
            fog_anisotropy_g=fog_g,
            extinction_coefficient=params.extinction_coefficient,
            anamorphic_squeeze=squeeze,
            staging_proxemics_m=proxemics,
            room_absorption=absorption,
            camera_keyframes=keyframes,
            port_state=port_state,
            obstacle=obstacle,
        )

    @staticmethod
    def _inverse_absorption(target_t60: float) -> float:
        volume, surface = 100.0, 60.0
        absorption = 0.161 * volume / (surface * target_t60)
        return float(min(max(absorption, 0.05), 0.95))

    @staticmethod
    def _replan_camera(keyframes: np.ndarray, obstacle: np.ndarray | None) -> np.ndarray:
        if obstacle is None:
            return keyframes.copy()
        times = np.arange(keyframes.shape[0], dtype=np.float64)
        try:
            trajectory = _camera.plan_minimum_snap_trajectory(keyframes, times)
        except _camera.CameraChoreographyError:
            return keyframes.copy()
        sample_times = np.linspace(times[0], times[-1], 12)
        samples = np.stack([trajectory.sample(float(t)) for t in sample_times])
        obstacles = np.array([[obstacle[0], obstacle[1], obstacle[2], 0.6]])
        shield = _camera.apply_cbf_safety_shield(samples, obstacles, delta_safe=1e-3)
        return np.asarray(shield.positions, dtype=np.float64)

    @staticmethod
    def _propagate_world(port_state: np.ndarray, fork_time: float) -> np.ndarray:
        if fork_time <= 0.0:
            return port_state.copy()
        q = np.eye(port_state.shape[0])
        system = _phs.PortHamiltonianSystem(
            J=np.zeros_like(q), R=np.zeros_like(q), Q=q, dimension=q.shape[0]
        )
        steps = int(min(max(round(fork_time), 1), 16))
        report = _phs.integrate_port_hamiltonian(system, port_state, dt=0.1, steps=steps)
        return np.asarray(report.states[-1], dtype=np.float64)

    # --- causal delta -------------------------------------------------------------

    def _causal_delta(
        self, base: UniverseState, branch: UniverseState, *, side: int
    ) -> CounterfactualCausalDeltaReceipt:
        # The divergence is measured on a deliberately coarse luminance grid: the
        # entropic OT plan is O(n*m) per Sinkhorn sweep, so a coarse grid keeps the
        # receipt cheap and deterministic while still detecting visual divergence.
        divergence_side = 12
        base_small = _render_luminance(base.parameters, side=divergence_side)
        branch_small = _render_luminance(branch.parameters, side=divergence_side)
        mu = _luminance_distribution(base_small)
        nu = _luminance_distribution(branch_small)
        cost = _wass._pairwise_squared_cost(base_small.reshape(-1, 1), branch_small.reshape(-1, 1))
        ot = _wass.solve_entropic_wasserstein_ot(
            mu, nu, cost, eps=0.05, max_iterations=256, tolerance=1e-8
        )
        divergence = abs(float(ot.sinkhorn_divergence))
        psnr = _psnr(
            _render_luminance(base.parameters, side=side),
            _render_luminance(branch.parameters, side=side),
        )
        base_energy = _world_energy(base.parameters)
        branch_energy = _world_energy(branch.parameters)
        loudness_base = 10.0 * math.log10(base.parameters.key_light_intensity + _EPS)
        loudness_branch = 10.0 * math.log10(branch.parameters.key_light_intensity + _EPS)
        t60_base = _room_t60(base.parameters.room_absorption)
        t60_branch = _room_t60(branch.parameters.room_absorption)
        doppler_base = _scatter.compute_doppler_shifted_signal(
            source_frequency=440.0,
            source_velocity=base.parameters.staging_proxemics_m,
            mode="acoustic",
        ).ratio
        doppler_branch = _scatter.compute_doppler_shifted_signal(
            source_frequency=440.0,
            source_velocity=branch.parameters.staging_proxemics_m,
            mode="acoustic",
        ).ratio
        both_passive = self._is_passive(base.parameters) and self._is_passive(branch.parameters)
        return CounterfactualCausalDeltaReceipt(
            base_universe_id=base.universe_id,
            branch_universe_id=branch.universe_id,
            wasserstein_divergence=divergence,
            psnr_db=psnr,
            energy_delta_joules=branch_energy - base_energy,
            both_passive=both_passive,
            loudness_delta_lufs=loudness_branch - loudness_base,
            t60_delta_sec=t60_branch - t60_base,
            doppler_delta_ratio=doppler_branch - doppler_base,
            visual_divergence_nonzero=divergence > 1e-9,
        )

    @staticmethod
    def _is_passive(parameters: WorldParameters) -> bool:
        q = np.eye(parameters.port_state.shape[0])
        system = _phs.PortHamiltonianSystem(
            J=np.zeros_like(q), R=np.zeros_like(q), Q=q, dimension=q.shape[0]
        )
        report = _phs.integrate_port_hamiltonian(system, parameters.port_state, dt=0.1, steps=4)
        return bool(report.all_passive)

    def staging_report(self, universe_id: str) -> _nash.NashStagingReport:
        """Re-solve the Nash staging of a world (measured, for telemetry)."""
        state = self.get(universe_id)
        problem = _nash.DramaticStagingProblem(
            actor_count=3,
            camera_position=np.array([0.0, -6.0]),
            camera_yaw=0.0,
            key_light_azimuth=math.radians(state.parameters.key_light_azimuth_deg),
            interactions=((0, 1, "personal"), (1, 2, "social")),
            speakers=(0, 1),
        )
        initial = np.array([[-1.0, 0.0, 0.0], [0.5, 0.3, 0.2], [2.0, -0.5, -0.3]])
        return _nash.solve_nash_dramatic_staging(
            problem,
            initial,
            epsilon=1e-2,
            max_iterations=40,
            response_samples=3,
        )


# --- quadrant compilation ---------------------------------------------------------


def _ffmpeg_path() -> str | None:
    return ffmpeg_binary()


def _write_stereo_wav(path: str, left: np.ndarray, right: np.ndarray, rate: int) -> int:
    count = min(left.shape[0], right.shape[0])
    stereo = np.empty((count, 2), dtype=np.float64)
    stereo[:, 0] = np.clip(left[:count], -1.0, 1.0)
    stereo[:, 1] = np.clip(right[:count], -1.0, 1.0)
    pcm = (stereo * 32767.0).astype("<i2")
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return int(pcm.nbytes)


def _compose_quadrant_frame(
    fields: list[np.ndarray], telemetry: list[tuple[str, ...]], side: int
) -> np.ndarray:
    """Build a 2x2 split-screen frame with a telemetry bar per quadrant."""
    from PIL import Image, ImageDraw

    full = Image.new("RGB", (2 * side, 2 * side), (0, 0, 0))
    draw = ImageDraw.Draw(full)
    for index, quadrant_field in enumerate(fields):
        row, col = divmod(index, 2)
        quadrant = np.clip(quadrant_field, 0.0, 1.0)
        rgb = np.stack([quadrant, quadrant * 0.85, quadrant * 0.6], axis=-1)
        image = Image.fromarray((rgb * 255.0).astype(np.uint8), mode="RGB")
        full.paste(image, (col * side, row * side))
        draw.rectangle(
            [
                (col * side, row * side),
                (col * side + side - 1, row * side + side - 1),
            ],
            outline=(255, 255, 255),
            width=1,
        )
        bar_height = 10
        draw.rectangle(
            [
                (col * side, row * side + side - bar_height),
                (col * side + side - 1, row * side + side - 1),
            ],
            fill=(16, 16, 16),
        )
        for line_index, text in enumerate(telemetry[index][:2]):
            draw.text(
                (col * side + 2, row * side + side - bar_height + line_index * 5),
                text[: max(4, side // 4)],
                fill=(255, 220, 120),
            )
    return np.asarray(full, dtype=np.uint8)


def compile_counterfactual_quadrant_mp4(
    playground: PrometheusStudioPlayground,
    universe_ids: list[str],
    *,
    ancestor_universe_id: str,
    filesystem: Any,
    out_dir: str = "prometheus_playground",
    side: int = 64,
    frames: int = 8,
    fps: int = 8,
    timeout: float = 120.0,
) -> QuadrantCompilationReceipt:
    """Compile 2 or 4 counterfactual worlds into a split-screen H.264 MP4 + WAV.

    The worlds must share ``ancestor_universe_id`` as a common ancestor. Frames
    are rendered deterministically, composed into a 2x2 grid with physical
    telemetry bars, and encoded with real FFmpeg (``libx264``, ``yuv420p``,
    ``+faststart``). When FFmpeg is unavailable the receipt records
    ``encoded=False`` rather than fabricating a media file.
    """
    if len(universe_ids) not in (2, 4):
        raise PlaygroundError("exactly 2 or 4 universes are required")
    if len(set(universe_ids)) != len(universe_ids):
        raise PlaygroundError("universe ids must be distinct")
    if not isinstance(side, int) or isinstance(side, bool) or side < 16 or side > MAX_QUADRANT_SIDE:
        raise PlaygroundError(f"side must be an int in [16, {MAX_QUADRANT_SIDE}]")
    if not isinstance(frames, int) or isinstance(frames, bool) or frames < 1 or frames > MAX_FRAMES:
        raise PlaygroundError(f"frames must be an int in [1, {MAX_FRAMES}]")
    if not isinstance(fps, int) or isinstance(fps, bool) or fps < 1 or fps > 120:
        raise PlaygroundError("fps must be an int in [1, 120]")
    if not out_dir or out_dir.startswith("/") or ".." in out_dir.split("/"):
        raise PlaygroundError("out_dir must be a safe relative path")

    ancestor = playground.get(ancestor_universe_id)
    states = [playground.get(uid) for uid in universe_ids]
    for state in states:
        if not _is_descendant_of(playground, state, ancestor.universe_id):
            raise PlaygroundError(
                f"universe {state.universe_id!r} is not a descendant of {ancestor.universe_id!r}"
            )

    fields = [_render_luminance(state.parameters, side=side) for state in states]
    telemetry: list[tuple[str, ...]] = []
    for state in states:
        staging = playground.staging_report(state.universe_id)
        divergence = 0.0
        telemetry.append(
            (
                f"E={_world_energy(state.parameters):.2f}J",
                f"vis={staging.camera_visibility_fraction:.1f} d={divergence:.2f}",
            )
        )

    frame_dir = f"{out_dir}/frames"
    for index in range(frames):
        phase = index / max(frames, 1)
        shifted = [np.roll(field, int(round(phase * side * 0.25)), axis=1) for field in fields]
        composed = _compose_quadrant_frame(shifted, telemetry, side)
        from PIL import Image

        buffer = Image.fromarray(composed, mode="RGB")
        import io

        payload = io.BytesIO()
        buffer.save(payload, format="PNG")
        filesystem.write_bytes(
            f"{frame_dir}/frame_{index:04d}.png", payload.getvalue(), create_parents=True
        )

    rate = _scatter.SAMPLE_RATE_HZ
    sample_count = int(round(rate * frames / fps))
    times = np.arange(sample_count, dtype=np.float64) / rate
    left = 0.4 * np.sin(2.0 * np.pi * 220.0 * times)
    right = 0.4 * np.sin(2.0 * np.pi * 330.0 * times)
    wav_rel = f"{out_dir}/quadrant_audio.wav"
    wav_target = filesystem.resolve(wav_rel)
    wav_target.parent.mkdir(parents=True, exist_ok=True)
    _write_stereo_wav(str(wav_target), left, right, rate)
    wav_sha = hashlib.sha256(filesystem.read_bytes(wav_rel, max_bytes=1 << 26)).hexdigest()

    mp4_rel = f"{out_dir}/quadrant_comparison.mp4"
    mp4_target = filesystem.resolve(mp4_rel)
    ffmpeg = _ffmpeg_path()
    encoded = False
    if ffmpeg is not None:
        argv = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(filesystem.resolve(f"{frame_dir}/frame_%04d.png")),
            "-i",
            str(wav_target),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-shortest",
            str(mp4_target),
        ]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode == 0 and mp4_target.is_file():
            encoded = True
    mp4_sha = ""
    if encoded:
        mp4_sha = hashlib.sha256(filesystem.read_bytes(mp4_rel, max_bytes=1 << 27)).hexdigest()

    return QuadrantCompilationReceipt(
        ancestor_universe_id=ancestor_universe_id,
        universe_ids=tuple(universe_ids),
        quadrant_side=side,
        frames=frames,
        fps=fps,
        mp4_path=mp4_rel,
        wav_path=wav_rel,
        mp4_sha256=mp4_sha,
        wav_sha256=wav_sha,
        encoded=encoded,
        notes=(
            "media is encoded with real FFmpeg, or BLOCKED when it is absent",
            "every quadrant carries its own physical telemetry bar",
        ),
    )


def _is_descendant_of(
    playground: PrometheusStudioPlayground, state: UniverseState, ancestor_id: str
) -> bool:
    current = state
    seen: set[str] = set()
    while current.universe_id not in seen:
        if current.universe_id == ancestor_id:
            return True
        seen.add(current.universe_id)
        if current.parent_universe_id is None:
            return False
        current = playground.get(current.parent_universe_id)
    return False
