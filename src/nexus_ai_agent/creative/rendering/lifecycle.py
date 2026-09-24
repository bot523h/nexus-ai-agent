"""Apply-lane lifecycle: REGISTERED → RUNNABLE, gates only.

This module is the state machine for the FFmpeg apply lane. It never runs a
process. Path existence and typed transitions live here; filter probing and
encode live exclusively in :mod:`executor`.

States:

* ``UNREGISTERED`` — no binary recorded.
* ``REGISTERED`` — a real file path is recorded. Not yet probed. Encode is
  forbidden.
* ``RUNNABLE`` — required filters were observed on that binary. Encode is
  allowed.
* ``FAILED`` — probe or registration produced a typed failure. Encode is
  forbidden. Terminal.

Illegal transitions fail closed with :class:`LaneLifecycleError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from nexus_ai_agent.creative.rendering.ir import LaneError

REPO_ROOT = Path(__file__).resolve().parents[4]
SHIPPED_IDENTITY_LUT = REPO_ROOT / "assets" / "luts" / "identity.cube"
SHIPPED_PERSIAN_FONT = REPO_ROOT / "assets" / "fonts" / "Vazirmatn.ttf"

# Filters the lane actually emits. Presence is probed, never assumed.
# Core filters the compiled lane always may emit. Optional instruments
# (drawtext) are recorded on the runtime and checked per-op.
REQUIRED_FILTERS = frozenset(
    {
        "eq",
        "lut3d",
        "loudnorm",
        "xfade",
        "atempo",
        "format",
        "scale",
    }
)
OPTIONAL_FILTERS = frozenset({"drawtext"})


class LaneLifecycleState(str, Enum):
    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    RUNNABLE = "runnable"
    FAILED = "failed"


class LaneLifecycleError(LaneError):
    """Illegal lifecycle transition or encode before RUNNABLE."""


_ALLOWED_TRANSITIONS: dict[LaneLifecycleState, frozenset[LaneLifecycleState]] = {
    LaneLifecycleState.UNREGISTERED: frozenset(
        {LaneLifecycleState.REGISTERED, LaneLifecycleState.FAILED}
    ),
    LaneLifecycleState.REGISTERED: frozenset(
        {LaneLifecycleState.RUNNABLE, LaneLifecycleState.FAILED}
    ),
    LaneLifecycleState.RUNNABLE: frozenset({LaneLifecycleState.FAILED}),
    LaneLifecycleState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class LaneRuntime:
    """Frozen snapshot of the apply-lane binary lifecycle."""

    state: LaneLifecycleState
    binary: str | None = None
    filters: frozenset[str] = frozenset()
    failure: str | None = None

    def require_runnable(self) -> None:
        if self.state is not LaneLifecycleState.RUNNABLE:
            raise LaneLifecycleError(
                f"encode requires RUNNABLE runtime, got {self.state.value}"
                + (f": {self.failure}" if self.failure else "")
            )


def unregistered() -> LaneRuntime:
    return LaneRuntime(state=LaneLifecycleState.UNREGISTERED)


def fail_unregistered(reason: str) -> LaneRuntime:
    _assert_transition(LaneLifecycleState.UNREGISTERED, LaneLifecycleState.FAILED)
    return LaneRuntime(state=LaneLifecycleState.FAILED, failure=reason)


def register_binary(path: str) -> LaneRuntime:
    """RECORD a binary path. No process. File must exist."""
    candidate = Path(path)
    if not candidate.is_file():
        return LaneRuntime(
            state=LaneLifecycleState.FAILED,
            binary=str(candidate),
            failure=f"ffmpeg binary is not a file: {path!r}",
        )
    return LaneRuntime(
        state=LaneLifecycleState.REGISTERED,
        binary=str(candidate.resolve()),
    )


def apply_probe(runtime: LaneRuntime, filters: frozenset[str]) -> LaneRuntime:
    """REGISTERED → RUNNABLE or FAILED from an already-probed filter set.

    ``filters`` must come from :func:`executor.probe_filter_names`. This
    function does not execute anything.
    """
    _assert_transition(runtime.state, LaneLifecycleState.RUNNABLE)
    if runtime.state is not LaneLifecycleState.REGISTERED or not runtime.binary:
        raise LaneLifecycleError("probe results may only be applied to REGISTERED")
    missing = REQUIRED_FILTERS - filters
    if missing:
        return LaneRuntime(
            state=LaneLifecycleState.FAILED,
            binary=runtime.binary,
            filters=filters,
            failure=f"ffmpeg missing required filters: {sorted(missing)}",
        )
    return LaneRuntime(
        state=LaneLifecycleState.RUNNABLE,
        binary=runtime.binary,
        filters=filters,
    )


def mark_failed(runtime: LaneRuntime, reason: str) -> LaneRuntime:
    _assert_transition(runtime.state, LaneLifecycleState.FAILED)
    return LaneRuntime(
        state=LaneLifecycleState.FAILED,
        binary=runtime.binary,
        filters=runtime.filters,
        failure=reason,
    )


def _assert_transition(current: LaneLifecycleState, target: LaneLifecycleState) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise LaneLifecycleError(f"illegal lifecycle transition {current.value} → {target.value}")


def shipped_identity_lut() -> Path:
    path = SHIPPED_IDENTITY_LUT
    if not path.is_file():
        raise LaneLifecycleError(f"shipped identity LUT missing: {path}")
    return path


def shipped_persian_font() -> Path:
    path = SHIPPED_PERSIAN_FONT
    if not path.is_file():
        raise LaneLifecycleError(f"shipped Persian font missing: {path}")
    return path


__all__ = [
    "REQUIRED_FILTERS",
    "SHIPPED_IDENTITY_LUT",
    "SHIPPED_PERSIAN_FONT",
    "LaneLifecycleError",
    "LaneLifecycleState",
    "LaneRuntime",
    "apply_probe",
    "fail_unregistered",
    "mark_failed",
    "register_binary",
    "shipped_identity_lut",
    "shipped_persian_font",
    "unregistered",
]
