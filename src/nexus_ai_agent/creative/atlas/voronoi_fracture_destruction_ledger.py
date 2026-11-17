"""WAVE 16 — bounded Voronoi fracture with an exact mass/energy ledger and a
tamper-evident Merkle destruction chain.

Destruction shots need fragments that (a) split a body into a *deterministic*
set of convex cells, (b) conserve mass exactly, (c) spend a bounded impact
energy, and (d) remain auditable frame-to-frame so a later edit cannot silently
teleport or duplicate debris. This module provides, in pure NumPy + stdlib:

* :func:`fracture_voronoi_cells` — a bounded voxel Voronoi tessellation: every
  voxel of the body is assigned to its nearest seed (ties broken by the lowest
  seed index), so the per-cell voxel counts are integers and the fragment masses
  sum to the body mass with **zero** drift;
* :func:`impact_energy_budget` — the kinetic energy of an impactor versus the
  fracture surface energy ``E_s = gamma * A`` and kinetic-fragment budget, with a
  typed FEASIBLE / INFEASIBLE verdict;
* :class:`DestructionLedger` — an append-only Merkle hash chain over per-frame
  fragment states (canonical integer serialisation); :meth:`verify` recomputes
  every frame hash and the chain links, so a tampered frame is detected.

Bounded, validated, offline and deterministic; malformed inputs raise
:class:`FractureError`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Final

import numpy as np

__all__ = [
    "MAX_VOXELS",
    "MAX_SEEDS",
    "FractureError",
    "FractureSeed",
    "FractureCell",
    "FractureResult",
    "fracture_voronoi_cells",
    "ImpactEnergyReport",
    "impact_energy_budget",
    "DestructionLedger",
    "LedgerVerification",
]

MAX_VOXELS: Final[int] = 8_388_608
MAX_SEEDS: Final[int] = 4096
_EPS: Final[float] = 1e-12


class FractureError(ValueError):
    """Raised for malformed fracture/ledger inputs."""


@dataclass(frozen=True)
class FractureSeed:
    """A Voronoi seed: a 3-D position with a material density weight."""

    position: np.ndarray
    density: float = 1.0

    def __post_init__(self) -> None:
        pos = np.asarray(self.position, dtype=np.float64)
        if pos.shape != (3,) or not np.all(np.isfinite(pos)):
            raise FractureError("seed position must be a finite 3-vector")
        if not np.isfinite(self.density) or self.density <= 0.0:
            raise FractureError("seed density must be positive and finite")
        object.__setattr__(self, "position", pos)


@dataclass(frozen=True)
class FractureCell:
    """One Voronoi fragment with exact integer voxel counts."""

    index: int
    voxel_count: int
    mass: float
    centroid: np.ndarray
    seed: np.ndarray


@dataclass
class FractureResult:
    """Result of the voxel Voronoi fracture."""

    cells: list[FractureCell]
    assignment: np.ndarray  # (X, Y, Z) integer cell index per voxel
    total_voxels: int
    total_mass: float
    body_mass: float
    mass_drift: float  # |total_mass - body_mass| (must be ~0)
    seed_count: int


def fracture_voronoi_cells(
    shape: tuple[int, int, int],
    seeds: list[FractureSeed],
    *,
    voxel_volume: float = 1.0,
    body_mask: np.ndarray | None = None,
) -> FractureResult:
    """Tessellate a bounded voxel body into nearest-seed Voronoi cells.

    Every voxel ``(i, j, k)`` of ``shape`` (optionally restricted by
    ``body_mask``) is assigned to the seed minimising the Euclidean distance;
    ties are broken by the lowest seed index, making the assignment fully
    deterministic. ``voxel_volume`` scales the per-voxel mass
    ``m = density * voxel_volume``. Fragment masses sum to the body mass exactly
    (the reported ``mass_drift`` is the residual, expected to be ``0``).
    """
    if len(shape) != 3 or min(shape) < 1:
        raise FractureError("shape must be a positive 3-tuple")
    nx, ny, nz = shape
    if nx * ny * nz > MAX_VOXELS:
        raise FractureError("voxel body exceeds the bounded budget")
    if not seeds:
        raise FractureError("at least one seed is required")
    if len(seeds) > MAX_SEEDS:
        raise FractureError(f"seed count must be <= {MAX_SEEDS}")
    if not np.isfinite(voxel_volume) or voxel_volume <= 0.0:
        raise FractureError("voxel_volume must be positive and finite")

    mask: np.ndarray
    if body_mask is None:
        mask = np.ones(shape, dtype=bool)
    else:
        m = np.asarray(body_mask, dtype=bool)
        if m.shape != shape:
            raise FractureError("body_mask must match shape")
        mask = m
    if not mask.any():
        raise FractureError("body_mask contains no solid voxels")

    coords = np.stack(
        np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij"), axis=-1
    ).astype(np.float64)
    seed_positions = np.stack([s.position for s in seeds], axis=0)  # (S, 3)
    densities = np.array([s.density for s in seeds], dtype=np.float64)

    flat = coords.reshape(-1, 3)
    # Distance to each seed; nearest with a stable (lowest-index) tie-break.
    dist = np.linalg.norm(flat[:, None, :] - seed_positions[None, :, :], axis=-1)
    assignment_flat = np.argmin(dist, axis=1)
    assignment = assignment_flat.reshape(shape)
    assignment = np.where(mask, assignment, -1)

    cells: list[FractureCell] = []
    total_mass = 0.0
    total_voxels = 0
    for idx in range(len(seeds)):
        sel = assignment == idx
        count = int(np.count_nonzero(sel))
        if count == 0:
            continue
        mass = float(densities[idx] * voxel_volume * count)
        centroid = coords[sel].mean(axis=0)
        cells.append(
            FractureCell(
                index=idx,
                voxel_count=count,
                mass=mass,
                centroid=centroid,
                seed=seed_positions[idx].copy(),
            )
        )
        total_mass += mass
        total_voxels += count

    body_mass = float(np.sum(densities[assignment[mask]]) * voxel_volume)
    return FractureResult(
        cells=cells,
        assignment=assignment,
        total_voxels=total_voxels,
        total_mass=total_mass,
        body_mass=body_mass,
        mass_drift=abs(total_mass - body_mass),
        seed_count=len(seeds),
    )


@dataclass(frozen=True)
class ImpactEnergyReport:
    """Energy balance of an impact against a fracture surface budget."""

    kinetic_energy: float
    surface_energy: float
    verdict: str  # "FEASIBLE" | "INFEASIBLE"
    fragment_budget: float


def impact_energy_budget(
    *,
    impactor_mass: float,
    impactor_speed: float,
    fracture_toughness: float,
    new_surface_area: float,
    fragment_energy_fraction: float = 0.5,
) -> ImpactEnergyReport:
    """Compare impactor kinetic energy ``E_k = ½ m v²`` to fracture cost.

    The fracture cost is ``E_s = G_c * A`` (toughness × new surface area); the
    remaining energy is available to the fragments as kinetic energy. The
    verdict is FEASIBLE only when ``E_k >= E_s``. Values are reported as-is (no
    probability, no artistic score).
    """
    if impactor_mass <= 0.0 or not np.isfinite(impactor_mass):
        raise FractureError("impactor_mass must be positive and finite")
    if impactor_speed < 0.0 or not np.isfinite(impactor_speed):
        raise FractureError("impactor_speed must be non-negative and finite")
    if fracture_toughness < 0.0 or not np.isfinite(fracture_toughness):
        raise FractureError("fracture_toughness must be non-negative and finite")
    if new_surface_area < 0.0 or not np.isfinite(new_surface_area):
        raise FractureError("new_surface_area must be non-negative and finite")
    if not (0.0 <= fragment_energy_fraction <= 1.0):
        raise FractureError("fragment_energy_fraction must lie in [0, 1]")

    kinetic = 0.5 * impactor_mass * impactor_speed * impactor_speed
    surface = fracture_toughness * new_surface_area
    budget = max(kinetic - surface, 0.0) * fragment_energy_fraction
    verdict = "FEASIBLE" if kinetic + _EPS >= surface else "INFEASIBLE"
    return ImpactEnergyReport(
        kinetic_energy=kinetic,
        surface_energy=surface,
        verdict=verdict,
        fragment_budget=budget,
    )


def _cell_id(entry: dict[str, object]) -> int:
    value = entry["id"]
    assert isinstance(value, int)
    return value


@dataclass(frozen=True)
class LedgerVerification:
    """Result of verifying the destruction ledger chain."""

    valid: bool
    frames_checked: int
    first_bad_frame: int | None
    reason: str


@dataclass
class DestructionLedger:
    """Append-only Merkle hash chain over per-frame fragment states.

    Each frame records the frame index, the integer voxel counts and the
    quantised (integer-micron) fragment centroids of every live cell. A frame's
    hash is ``sha256(prev_hash || canonical_payload)``; the genesis frame chains
    from a fixed zero hash. :meth:`verify` recomputes every frame hash and the
    links, so a mutated payload, a reordered frame or a broken link is detected.
    """

    _frames: list[dict[str, object]] = field(default_factory=list)
    _hashes: list[str] = field(default_factory=list)
    _genesis: str = field(default="0" * 64)

    @staticmethod
    def _canonical(frame: dict[str, object]) -> bytes:
        return json.dumps(frame, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def _frame_hash(cls, prev_hash: str, frame: dict[str, object]) -> str:
        h = hashlib.sha256()
        h.update(prev_hash.encode("ascii"))
        h.update(b"|")
        h.update(cls._canonical(frame))
        return h.hexdigest()

    def append_frame(
        self, frame_index: int, cells: list[FractureCell], *, positions: np.ndarray | None = None
    ) -> str:
        """Append a frame; return its hash.

        ``positions`` (optional ``(K, 3)`` float array, in metres) overrides the
        centroids for the payload and is quantised to integer microns for a
        canonical, hash-stable representation.
        """
        if frame_index != len(self._frames):
            raise FractureError("frames must be appended in contiguous order")
        if not cells:
            raise FractureError("a frame must carry at least one live cell")
        payload_cells: list[dict[str, object]] = []
        if positions is None:
            for c in cells:
                payload_cells.append(
                    {
                        "id": c.index,
                        "voxels": c.voxel_count,
                        "microns": [int(round(v * 1e6)) for v in c.centroid],
                    }
                )
        else:
            pos = np.asarray(positions, dtype=np.float64)
            if pos.shape != (len(cells), 3) or not np.all(np.isfinite(pos)):
                raise FractureError("positions must be (K, 3) and finite")
            for c, p in zip(cells, pos, strict=True):
                payload_cells.append(
                    {
                        "id": c.index,
                        "voxels": c.voxel_count,
                        "microns": [int(round(float(v) * 1e6)) for v in p],
                    }
                )
        payload_cells.sort(key=lambda d: _cell_id(d))
        frame: dict[str, object] = {"frame": frame_index, "cells": payload_cells}
        prev = self._hashes[-1] if self._hashes else self._genesis
        digest = self._frame_hash(prev, frame)
        self._frames.append(frame)
        self._hashes.append(digest)
        return digest

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    @property
    def head(self) -> str:
        return self._hashes[-1] if self._hashes else self._genesis

    def hash_at(self, frame_index: int) -> str:
        if frame_index < 0 or frame_index >= len(self._hashes):
            raise FractureError("frame index out of range")
        return self._hashes[frame_index]

    def verify(self) -> LedgerVerification:
        """Recompute the whole chain; report the first bad frame if any."""
        prev = self._genesis
        for i, frame in enumerate(self._frames):
            digest = self._frame_hash(prev, frame)
            if digest != self._hashes[i]:
                return LedgerVerification(
                    valid=False,
                    frames_checked=i + 1,
                    first_bad_frame=i,
                    reason="frame hash mismatch (payload tampered)",
                )
            prev = digest
        return LedgerVerification(
            valid=True,
            frames_checked=len(self._frames),
            first_bad_frame=None,
            reason="chain intact",
        )

    def tamper_frame(self, frame_index: int, payload: dict[str, object]) -> None:
        """Test-only: overwrite a stored frame payload without touching hashes."""
        if frame_index < 0 or frame_index >= len(self._frames):
            raise FractureError("frame index out of range")
        self._frames[frame_index] = payload
