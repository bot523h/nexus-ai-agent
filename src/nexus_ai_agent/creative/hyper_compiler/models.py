"""Immutable mathematical data structures for the hyper-compiler timeline.

Every type here is a **value**: a frozen dataclass that describes a piece of
media or a node of the timeline DAG.  They carry no I/O and no subprocesses —
pure, side-effect free, and hashable so the segment cache in
:mod:`differential_filtergraph_compiler` can key on them.

Time is always kept in integer microseconds.  Frame rates are exact
:class:`fractions.Fraction` values so VFR/CFR detection never loses precision.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final, Literal

ColorSpace = Literal["SDR", "HDR"]

SDR: Final[ColorSpace] = "SDR"
"""Standard dynamic range (Rec.709 / sRGB)."""
HDR: Final[ColorSpace] = "HDR"
"""High dynamic range."""

_SH256_REF = r"^sha256:[0-9a-f]{64}$"

_IDENTITY_ROTATION: Final = ((1.0, 0.0), (0.0, 1.0))


def _is_so2(matrix: tuple[tuple[float, float], tuple[float, float]]) -> bool:
    (a, b), (c, d) = matrix
    # Columns must be unit and orthogonal; determinant must be +1.
    col0_norm = a * a + c * c
    col1_norm = b * b + d * d
    dot = a * b + c * d
    det = a * d - b * c
    return (
        abs(col0_norm - 1.0) < 1e-6
        and abs(col1_norm - 1.0) < 1e-6
        and abs(dot) < 1e-6
        and abs(det - 1.0) < 1e-6
    )


def _validate_sha256(value: str) -> str:
    import re

    if not isinstance(value, str) or not re.match(_SH256_REF, value):
        raise ValueError(f"sha256 must be 'sha256:<64 hex>', got {value!r}")
    return value


def canonical_params_hash(params: object) -> str:
    """``sha256:<hex>`` over canonical JSON of *params*.

    Sorting keys and forbidding NaN makes the hash stable across runs and
    processes, which is what the content-addressed segment cache keys on.
    """
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MediaStreamProbe:
    """A single ffprobe measurement of one media file.

    ``rotation`` is the :math:`\\mathrm{SO}(2)` rotation matrix extracted from
    the stream ``side_data_list`` / ``displaymatrix``.  ``sha256`` is the
    content hash of the source file.
    """

    width: int
    height: int
    r_frame_rate: Fraction
    avg_frame_rate: Fraction
    aspect_ratio: float
    rotation: tuple[tuple[float, float], tuple[float, float]]
    color_space: ColorSpace
    has_audio: bool
    sha256: str

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"invalid dimensions {self.width}x{self.height}")
        if self.r_frame_rate < 0 or self.avg_frame_rate < 0:
            raise ValueError("frame rates must be non-negative")
        if self.aspect_ratio <= 0:
            raise ValueError("aspect ratio must be positive")
        if not _is_so2(self.rotation):
            raise ValueError("rotation must be a valid SO(2) matrix")
        object.__setattr__(self, "sha256", _validate_sha256(self.sha256))

    @property
    def rotation_degrees(self) -> float:
        """Clockwise rotation in degrees, normalised to ``[0, 360)``."""
        (a, b), (c, d) = self.rotation
        # Pure rotation [[cos, -sin], [sin, cos]] -> sin = c, cos = a.
        theta = math.atan2(c, a)
        return math.degrees(theta) % 360.0

    @property
    def is_variable_frame_rate(self) -> bool:
        """True when instantaneous and average frame rate diverge > 1%."""
        if self.avg_frame_rate <= 0:
            return False
        return (
            abs(float(self.r_frame_rate) - float(self.avg_frame_rate))
            / max(float(self.avg_frame_rate), 1e-6)
        ) > 0.01

    @property
    def needs_rotation(self) -> bool:
        return abs(self.rotation_degrees) > 1e-3


@dataclass(frozen=True)
class TimelineDifferentialNode:
    """One node in the differential timeline DAG.

    ``speed_curve`` holds the four control points of the cubic Bézier

    .. math::
        B(t) = (1-t)^3 P_0 + 3(1-t)^2 t P_1 + 3(1-t) t^2 P_2 + t^3 P_3

    used for speed ramping (slow-motion, fast-motion, continuous, reverse).
    The phase integral is :math:`\\tau(t) = \\int_0^t v(s)\\,ds`.
    """

    node_id: str
    input_sha256: str
    start_us: int
    end_us: int
    speed_curve: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ]
    oklab_matrix: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    image_layers: tuple[str, ...] = field(default_factory=tuple)
    audio_layers: tuple[str, ...] = field(default_factory=tuple)
    zoom_box: tuple[float, float, float, float] | None = None
    reverse: bool = False

    def __post_init__(self) -> None:
        if self.end_us <= self.start_us:
            raise ValueError(f"node {self.node_id}: end_us must exceed start_us")
        object.__setattr__(self, "input_sha256", _validate_sha256(self.input_sha256))
        matrix = self.oklab_matrix
        if len(matrix) != 3 or any(len(row) != 3 for row in matrix):
            raise ValueError(f"node {self.node_id}: oklab_matrix must be 3x3")

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def speed_at(self, t: float) -> float:
        """Instantaneous speed ``v(t)`` at normalised time ``t`` in ``[0, 1]``.

        The derivative of the Bézier gives the speed curve; the x component of
        the derivative is the rate of progress through the segment and the y
        component is the playback speed.
        """
        t = min(1.0, max(0.0, float(t)))
        p0, p1, p2, p3 = self.speed_curve
        # B'(t) = 3(1-t)^2(P1-P0) + 6(1-t)t(P2-P1) + 3t^2(P3-P2)
        mt = 1.0 - t
        dx = (
            3 * mt * mt * (p1[0] - p0[0])
            + 6 * mt * t * (p2[0] - p1[0])
            + 3 * t * t * (p3[0] - p2[0])
        )
        dy = (
            3 * mt * mt * (p1[1] - p0[1])
            + 6 * mt * t * (p2[1] - p1[1])
            + 3 * t * t * (p3[1] - p2[1])
        )
        if abs(dx) < 1e-9:
            return 1.0
        return dy / dx

    def phase_integral(self, t: float, samples: int = 64) -> float:
        """Approximate :math:`\\tau(t) = \\int_0^t v(s)\\,ds` by trapezoids."""
        t = min(1.0, max(0.0, float(t)))
        if t <= 0.0:
            return 0.0
        total = 0.0
        prev_s = 0.0
        prev_v = self.speed_at(0.0)
        for i in range(1, samples + 1):
            s = t * i / samples
            v = self.speed_at(s)
            total += 0.5 * (prev_v + v) * (s - prev_s)
            prev_s, prev_v = s, v
        return total


@dataclass(frozen=True)
class TimelineDAG:
    """A directed acyclic graph of timeline nodes.

    ``total_duration_us`` is the wall-clock length of the assembled timeline in
    microseconds; ``fps``/``width``/``height`` describe the canonical raster.
    """

    nodes: tuple[TimelineDifferentialNode, ...]
    total_duration_us: int
    fps: Fraction
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.total_duration_us <= 0:
            raise ValueError("total_duration_us must be positive")
        if self.fps <= 0:
            raise ValueError("fps must be positive")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"invalid raster {self.width}x{self.height}")

    @property
    def params_hash(self) -> str:
        """Content hash of every node's editable parameters (cache key)."""
        payload = [
            {
                "node_id": n.node_id,
                "start_us": n.start_us,
                "end_us": n.end_us,
                "speed_curve": n.speed_curve,
                "oklab_matrix": n.oklab_matrix,
                "image_layers": n.image_layers,
                "audio_layers": n.audio_layers,
                "zoom_box": n.zoom_box,
                "reverse": n.reverse,
            }
            for n in self.nodes
        ]
        return canonical_params_hash(
            {"nodes": payload, "total_duration_us": self.total_duration_us}
        )
