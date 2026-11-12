"""NAGAR AETHEL — temporal composition package.

* :mod:`rational_time` — exact multi-rate rational time (no IEEE-754 drift);
* :mod:`dag` — a multi-track TimelineDAG with Kahn scheduling, a monotone
  cubic-Bezier time warp (Newton + bisection inverse) and J/L cut algebra;
* :mod:`compositor_2d` — linear-radiance premultiplied-alpha Porter-Duff
  compositing (12 operators + PLUS/MULTIPLY/SCREEN).

The package is a *pure analysis/composition layer*: it renders nothing, owns no
project state, and dispatches no command.
"""

from __future__ import annotations

from .compositor_2d import (
    ALL_OPERATORS,
    OPERATOR_FACTORS,
    PorterDuff,
    check_premultiplied,
    composite,
    linear_to_srgb,
    srgb_to_linear,
)
from .dag import (
    WARP_TOLERANCE,
    Edge,
    JLCut,
    Node,
    NodeKind,
    TimelineDAG,
    TimeWarp,
    compile_atlas_plan_to_dag,
    crossfade_gains,
)
from .errors import (
    CompositeError,
    CompositionError,
    CyclicGraphError,
    RateMismatchError,
    RationalTimeError,
    TimeWarpError,
    UnknownNodeError,
)
from .rational_time import (
    CANONICAL_RATES,
    RATE_23_976,
    RATE_24,
    RATE_25,
    RATE_29_97,
    RATE_30,
    RATE_59_94,
    RATE_48000,
    RATE_96000,
    RationalTime,
    TimeRange,
    common_time_base,
    frames_per_second,
)

__all__ = [
    "ALL_OPERATORS",
    "CANONICAL_RATES",
    "OPERATOR_FACTORS",
    "RATE_23_976",
    "RATE_24",
    "RATE_25",
    "RATE_29_97",
    "RATE_30",
    "RATE_48000",
    "RATE_59_94",
    "RATE_96000",
    "WARP_TOLERANCE",
    "CompositeError",
    "CompositionError",
    "CyclicGraphError",
    "Edge",
    "JLCut",
    "Node",
    "NodeKind",
    "PorterDuff",
    "RateMismatchError",
    "RationalTime",
    "RationalTimeError",
    "TimeRange",
    "TimeWarp",
    "TimeWarpError",
    "TimelineDAG",
    "UnknownNodeError",
    "check_premultiplied",
    "common_time_base",
    "compile_atlas_plan_to_dag",
    "composite",
    "crossfade_gains",
    "frames_per_second",
    "linear_to_srgb",
    "srgb_to_linear",
]
