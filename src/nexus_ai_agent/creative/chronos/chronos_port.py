"""Pure Studio dispatch boundary for deterministic Chronos kernel handlers.

Concrete kernel bindings are injected by the composition root so this module
remains inside the Wave 1 dependency-free Studio package boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ChronosCommand:
    operation: str
    inputs: tuple[str, ...]
    parameters: Mapping[str, str]
    deterministic: bool = True
    external_api_allowed: bool = False

    def __post_init__(self) -> None:
        if (
            not self.operation
            or not self.inputs
            or not self.deterministic
            or self.external_api_allowed
        ):
            raise ValueError("Chronos commands must be named, deterministic, and zero-egress")


@dataclass(frozen=True, slots=True)
class ChronosExecutionReceipt:
    operation: str
    output_path: str
    sha256: str
    quality_claim: str
    metadata: Mapping[str, str]


class ChronosHandler(Protocol):
    """Synchronous pure dispatch callable bound to an offline kernel adapter."""

    def __call__(self, command: ChronosCommand, /) -> ChronosExecutionReceipt: ...


class ChronosPort:
    """Allowlisted deterministic operation dispatch with fail-closed continuity."""

    OPERATIONS = frozenset(
        {
            "timeline.assemble_wav",
            "timeline.assemble_ffmpeg",
            "geometry.project_and_verify_scale",
            "color.relight_png",
            "acoustics.render_spatial_dialogue",
            "acoustics.verify_timbre_and_lipsync",
            "audio.declick_wav",
            "audio.duck_ambience",
            "identity.verify_pose",
            "identity.verify_eyeline",
            "compositor.render_25d",
            "compositor.verify_equivalence",
            "scene.inpaint",
            "scene.retouch",
            "scene.sky",
            "scene.tonemap",
            "merkle.root",
            "merkle.verify",
            "motion.solve_ik",
            "motion.foot_lock",
            "motion.verlet_chain",
            "motion.shutter_blur",
            "temporal.phase_corr",
            "temporal.deflicker",
            "temporal.rectify_rolling",
            "weather.precipitation",
            "grading.apply_cdl_lut",
            "grading.serialize_cube",
            "grading.legalize_ebu_r103",
            "episode.verify_show_bible",
            "episode.run_verified",
            "chronos.staged_spine_master",
        }
    )

    def __init__(self, handlers: Mapping[str, ChronosHandler]) -> None:
        self._handlers = dict(handlers)
        if not set(self._handlers) <= self.OPERATIONS:
            raise ValueError("handler registry contains an unsupported Chronos operation")

    def execute(self, command: ChronosCommand) -> ChronosExecutionReceipt:
        """Route a canonical command to its injected local kernel implementation."""
        if not command.deterministic or command.external_api_allowed:
            raise ValueError("Chronos rejects nondeterministic or network-enabled commands")
        if command.parameters.get("continuity") == "UNKNOWN":
            raise ValueError("UNKNOWN continuity cannot be dispatched as verified")
        if command.operation not in self.OPERATIONS:
            raise ValueError(f"unsupported Chronos operation: {command.operation}")
        handler = self._handlers.get(command.operation)
        if handler is None:
            raise ValueError(f"no Chronos handler registered for: {command.operation}")
        receipt = handler(command)
        if receipt.operation != command.operation or not receipt.sha256:
            raise ValueError("Chronos handler returned an invalid execution receipt")
        return receipt


def execute_chronos_command(
    command: ChronosCommand, handlers: Mapping[str, ChronosHandler]
) -> ChronosExecutionReceipt:
    """Convenience entrypoint; concrete kernels are explicitly supplied by caller."""
    return ChronosPort(handlers).execute(command)
