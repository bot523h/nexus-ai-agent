"""Preview + readback, derived from the *same* compiled plan.

The preview is a deterministic ``plan_derived`` manifest (frame map, expected
output duration/frames, boundary frames). It is computed from the plan — never
from free text — and its digest is what the user's approval binds. The readback
is rendered from the typed AST/plan/preview fields only, so what the user
approves is exactly what the plan contains.
"""

from __future__ import annotations

from nexus_ai_agent.nagar.ibpc.models import (
    CompiledPlan,
    PreviewManifest,
    Readback,
    TypedCreativeAST,
)

MICROSECONDS_PER_SECOND = 1_000_000


def _frames_between(start_us: int, end_us: int, fps: float) -> int:
    """Number of frames with pts in ``[start_us, end_us)`` at ``fps``."""
    if end_us <= start_us or fps <= 0:
        return 0
    import math

    first = math.ceil(start_us * fps / MICROSECONDS_PER_SECOND - 1e-9)
    last = math.ceil(end_us * fps / MICROSECONDS_PER_SECOND - 1e-9)
    return max(0, last - first)


def build_preview(
    plan: CompiledPlan,
    ast: TypedCreativeAST,
    *,
    source_duration_us: int,
    fps: float,
) -> PreviewManifest:
    """Deterministic preview manifest for the plan's single trim step.

    Canonical ``timeline.trim`` semantics: ``[start_us, end_us)`` is the KEEP
    window (preserved), the complement is removed. The manifest records both
    plus the expected output duration/frames and the boundary frames.
    """
    import math

    scope = ast.scope
    kept_us = min(scope.end_us, source_duration_us) - scope.start_us
    output_duration_us = max(0, kept_us)
    source_frames = _frames_between(0, source_duration_us, fps)
    kept_frames = _frames_between(scope.start_us, scope.end_us, fps)
    out_frames = source_frames - sum(
        _frames_between(r.start_us, r.end_us, fps) for r in scope.removed_ranges(source_duration_us)
    )
    in_frame = math.ceil(scope.start_us * fps / MICROSECONDS_PER_SECOND - 1e-9)
    out_frame = math.ceil(scope.end_us * fps / MICROSECONDS_PER_SECOND - 1e-9)
    frame_map = {
        "source_frames": source_frames,
        "keep_range": {"start_us": scope.start_us, "end_us": scope.end_us},
        "kept_frames": kept_frames,
        "removed_ranges_us": [
            {"start_us": r.start_us, "end_us": r.end_us}
            for r in scope.removed_ranges(source_duration_us)
        ],
        "output_frames": out_frames,
        "boundary_frames": {
            "first_kept": in_frame,
            "last_kept": max(in_frame, out_frame - 1),
            "first_removed_before": max(0, in_frame),
            "last_removed_after": max(0, source_frames - out_frame),
        },
    }
    return PreviewManifest(
        plan_digest=plan.plan_digest,
        scope_digest=scope.scope_digest,
        source_duration_us=source_duration_us,
        output_duration_us=output_duration_us,
        frame_map=frame_map,
    )


def render_readback(
    ast: TypedCreativeAST, plan: CompiledPlan, preview: PreviewManifest
) -> Readback:
    """Render the user-facing readback from typed fields only."""
    scope = ast.scope
    preserved = (
        ", ".join(f"[{r.start_us / 1e6:.3f}s..{r.end_us / 1e6:.3f}s]" for r in ast.forbidden_scopes)
        or "(none)"
    )
    removed = (
        ", ".join(
            f"[{r.start_us / 1e6:.3f}s..{r.end_us / 1e6:.3f}s]"
            for r in scope.removed_ranges(preview.source_duration_us)
        )
        or "(nothing)"
    )
    lines = [
        f"asset: {ast.asset_id} @ revision {ast.source_revision}",
        f"operation: {ast.operation} (level {plan.capability.permission_level})",
        f"params: {ast.params}",
        "effect: KEEP window "
        f"[{scope.start_us / 1e6:.3f}s..{scope.end_us / 1e6:.3f}s]; removed: {removed}",
        f"preserved (must be unchanged): {preserved}",
        f"preview: output duration {preview.output_duration_us / 1e6:.3f}s, "
        f"frames {preview.frame_map.get('source_frames')} "
        f"-> {preview.frame_map.get('output_frames')}",
    ]
    if ast.ambiguities:
        lines.append("ambiguities: " + "; ".join(ast.ambiguities))
    return Readback(
        asset_id=ast.asset_id,
        source_revision=ast.source_revision,
        operation=ast.operation,
        params=dict(ast.params),
        scope=scope,
        forbidden_scopes=tuple(ast.forbidden_scopes),
        ambiguities=tuple(ast.ambiguities),
        preview=preview,
        text="\n".join(lines),
    )


__all__ = ["build_preview", "render_readback"]
