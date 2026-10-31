"""``nexus.apply.lane`` — from the packs' pure IR to a real master file.

Wave 8 turns pack operations into executed media through the same staged shape
as the proven slideshow lane: pick typed :class:`LaneOp` values, bind them to
a :class:`LaneIR`, compile a deterministic filtergraph + argv (pure), then run
exactly one FFmpeg process with staging + atomic publish and probed evidence.
"""

from nexus_ai_agent.creative.rendering.compiler import (
    CompiledLane,
    MeasuredLoudness,
    compile_assembly,
    compile_lane,
    compile_measure,
)
from nexus_ai_agent.creative.rendering.executor import (
    LaneArtifact,
    LaneExecutionError,
    encode_lane,
    measure_loudness,
    render_lane,
)
from nexus_ai_agent.creative.rendering.ir import (
    LANE_PACKAGE_ID,
    AssemblyGap,
    DuckOp,
    ExposureOp,
    FreezeOp,
    LaneAssembly,
    LaneError,
    LaneIR,
    LaneOp,
    LaneProfile,
    LaneSource,
    LoudnormOp,
    ReverseOp,
    SpeedOp,
    TitleOp,
    TrimOp,
    XfadeOp,
    ev_to_gamma,
    lane_ir_from_project,
    tint_to_gm,
)

__all__ = [
    "LANE_PACKAGE_ID",
    "AssemblyGap",
    "CompiledLane",
    "DuckOp",
    "ExposureOp",
    "FreezeOp",
    "LaneArtifact",
    "LaneAssembly",
    "LaneError",
    "LaneExecutionError",
    "LaneIR",
    "LaneOp",
    "LaneProfile",
    "LaneSource",
    "LoudnormOp",
    "MeasuredLoudness",
    "ReverseOp",
    "SpeedOp",
    "TitleOp",
    "TrimOp",
    "XfadeOp",
    "compile_assembly",
    "compile_lane",
    "compile_measure",
    "encode_lane",
    "ev_to_gamma",
    "lane_ir_from_project",
    "measure_loudness",
    "render_lane",
    "tint_to_gm",
]
