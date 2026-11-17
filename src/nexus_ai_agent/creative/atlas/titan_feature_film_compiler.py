"""WAVE 19 — the NAGAR TITAN feature-film compiler.

This is the top-level orchestrator: it consumes an explicit, versioned
:class:`~nexus_ai_agent.creative.episode_causality_lab.models.StoryDocument`,
runs the existing ATLAS analysis (causal + temporal + edit plan) — it does **not**
re-implement any of it — and then attaches a deterministic, typed *render pass*
list built from the Waves 13-18 TITAN operators. The compiler:

1. validates the story and folds the ATLAS statuses (reused verbatim);
2. derives a per-step render-pass graph (optics, atmosphere, portal, crowd,
   fracture, pacing) with a stable, sorted order and a content hash per pass;
3. computes a bounded cost/time estimate from the declared shot frame counts;
4. exports a real, byte-verified ``compiled_film.json`` through the existing
   authorized filesystem boundary (re-opened and independently re-hashed);
5. emits an inert integration handoff for a later authorized PR.

It owns no project state, mints no passport, and dispatches no command. A pass is
a *proposal*, not a rendered artifact: the compiler records
``pass_status = "PROPOSED"`` for every pass and never claims pixels were written.
Anything the inputs do not determine is ``UNKNOWN``/``NEEDS_REVIEW``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Final

from nexus_ai_agent.creative.episode_causality_lab.analysis import (
    AnalysisReport,
    analyze_story,
)
from nexus_ai_agent.creative.episode_causality_lab.models import StoryDocument
from nexus_ai_agent.creative.episode_causality_lab.plan import PlanRequest
from nexus_ai_agent.creative.episode_causality_lab.statuses import Status
from nexus_ai_agent.execution.staging import AttemptStaging
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

__all__ = [
    "MAX_COMPILED_BYTES",
    "CompilerError",
    "RenderPass",
    "CompiledFilm",
    "compile_feature_film",
    "export_compiled_film",
    "compiled_film_handoff",
]

MAX_COMPILED_BYTES: Final[int] = 64 * 1024 * 1024

#: The deterministic operator catalogue the compiler may schedule, in order.
#: ``applies`` is the declared condition under which the pass is scheduled.
_RENDER_PASS_CATALOGUE: Final[tuple[tuple[str, str, str], ...]] = (
    ("titan.wave13.portal_metrics", "scene", "non_euclidean"),
    ("titan.wave14.atmosphere", "scene", "always"),
    ("titan.wave15.crowd_heal", "scene", "crowd"),
    ("titan.wave16.fracture_ledger", "scene", "destruction"),
    ("titan.wave17.anamorphic_optics", "scene", "always"),
    ("titan.wave18.pacing", "editorial", "always"),
)


class CompilerError(ValueError):
    """Raised for malformed compiler inputs."""


@dataclass(frozen=True)
class RenderPass:
    """A proposed, deterministic render pass over one compiled step."""

    step_index: int
    candidate_id: str
    pass_id: str
    stage: str
    applies: str
    frames: int
    content_hash: str
    pass_status: str = "PROPOSED"  # never "RENDERED" unless pixels were written


@dataclass
class CompiledFilm:
    """The compiled feature-film plan and its measured provenance."""

    fingerprint: str
    status: Status
    analysis: AnalysisReport
    passes: tuple[RenderPass, ...]
    total_frames: int
    estimated_seconds: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def pass_count(self) -> int:
        return len(self.passes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "titan.compiled_feature_film",
            "schema_version": 1,
            "story_fingerprint": self.fingerprint,
            "status": self.status.value,
            "total_frames": self.total_frames,
            "estimated_seconds": self.estimated_seconds,
            "analysis_status": self.analysis.status.value,
            "pass_count": self.pass_count,
            "passes": [
                {
                    "step_index": p.step_index,
                    "candidate_id": p.candidate_id,
                    "pass_id": p.pass_id,
                    "stage": p.stage,
                    "applies": p.applies,
                    "frames": p.frames,
                    "content_hash": p.content_hash,
                    "pass_status": p.pass_status,
                }
                for p in self.passes
            ],
            "notes": list(self.notes),
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


def _pass_content_hash(step_index: int, candidate_id: str, pass_id: str, frames: int) -> str:
    h = hashlib.sha256()
    h.update(f"{step_index}|{candidate_id}|{pass_id}|{frames}".encode())
    return h.hexdigest()


def _scheduled_passes(
    step_index: int, candidate_id: str, frames: int, flags: frozenset[str]
) -> list[RenderPass]:
    out: list[RenderPass] = []
    for pass_id, stage, applies in _RENDER_PASS_CATALOGUE:
        if applies != "always" and applies not in flags:
            continue
        out.append(
            RenderPass(
                step_index=step_index,
                candidate_id=candidate_id,
                pass_id=pass_id,
                stage=stage,
                applies=applies,
                frames=frames,
                content_hash=_pass_content_hash(step_index, candidate_id, pass_id, frames),
            )
        )
    return out


def compile_feature_film(
    doc: StoryDocument,
    *,
    plan_request: PlanRequest,
    scene_flags: frozenset[str] = frozenset(),
    frame_rate: float = 24.0,
) -> CompiledFilm:
    """Compile a story document into a deterministic feature-film plan.

    ``scene_flags`` declares which conditional passes apply (e.g.
    ``{"non_euclidean", "crowd", "destruction"}``). The ATLAS analysis runs with
    the supplied ``plan_request``; every planned step receives the scheduled
    render passes for its covered frames. Deterministic: identical inputs give
    byte-identical output. ``estimated_seconds`` is ``total_frames / frame_rate``
    — a linear estimate, not a measured render time.
    """
    if frame_rate <= 0.0 or not (frame_rate == frame_rate) or frame_rate in (float("inf"),):
        raise CompilerError("frame_rate must be a positive finite number")
    analysis = analyze_story(doc, plan_request=plan_request)

    passes: list[RenderPass] = []
    total_frames = 0
    if analysis.plan is not None and analysis.plan.steps:
        for i, step in enumerate(analysis.plan.steps):
            total_frames += step.frames
            passes.extend(_scheduled_passes(i, step.candidate_id, step.frames, scene_flags))

    notes: list[str] = list(analysis.notes)
    if analysis.plan is None:
        notes.append("no plan request supplied: no render passes scheduled")
    elif not analysis.plan.steps:
        notes.append("plan has no steps: no render passes scheduled")
    if not scene_flags:
        notes.append("no scene_flags declared: only 'always' passes scheduled")

    return CompiledFilm(
        fingerprint=doc.fingerprint(),
        status=analysis.status,
        analysis=analysis,
        passes=tuple(passes),
        total_frames=total_frames,
        estimated_seconds=(total_frames / frame_rate) if total_frames else 0.0,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class ExportedFilm:
    """Metadata about a compiled film written and independently re-read."""

    relative_path: str
    sha256: str
    size_bytes: int
    reopened_sha256: str
    verified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "reopened_sha256": self.reopened_sha256,
            "verified": self.verified,
        }


def export_compiled_film(
    film: CompiledFilm,
    filesystem: WorkspaceFilesystem,
    relative_path: str,
    *,
    staging: AttemptStaging | None = None,
) -> ExportedFilm:
    """Write the compiled film, re-open it, and verify the digest independently.

    ``relative_path`` must be a safe relative path inside ``filesystem``'s root;
    the boundary rejects traversal, absolute paths and symlinks. With
    ``staging`` the bytes are written there first and ``publish``-ed (the
    repository's existing two-phase path).
    """
    raw = film.canonical_bytes()
    if len(raw) > MAX_COMPILED_BYTES:
        raise CompilerError(f"compiled film is {len(raw)} bytes > {MAX_COMPILED_BYTES}")
    expected = hashlib.sha256(raw).hexdigest()

    if staging is not None:
        staged_name = "compiled_film.json"
        staging.prepare()
        staging.write(staged_name, raw)
        published = staging.publish(staged_name, relative_path)
        relative_path = str(published.relative_to(filesystem.root))
    else:
        filesystem.write_bytes(relative_path, raw, create_parents=True)

    reopened = filesystem.read_bytes(relative_path, max_bytes=MAX_COMPILED_BYTES)
    reopened_digest = hashlib.sha256(reopened).hexdigest()
    return ExportedFilm(
        relative_path=relative_path,
        sha256=expected,
        size_bytes=len(raw),
        reopened_sha256=reopened_digest,
        verified=(reopened_digest == expected and reopened == raw),
    )


def compiled_film_handoff(film: CompiledFilm, artifact: ExportedFilm) -> dict[str, Any]:
    """Build an inert, typed handoff for a later authorized integration PR.

    The compiler dispatches nothing and holds no authority; the consumer must
    re-check every field against the live pipeline and re-authorize.
    """
    return {
        "kind": "titan.compiled_film_handoff",
        "schema_version": 1,
        "story_fingerprint": film.fingerprint,
        "status": film.status.value,
        "pass_count": film.pass_count,
        "artifact": artifact.as_dict(),
        "authority": "advisory",
        "requires_authorization": True,
        "pass_status_note": "all passes are PROPOSED; no pixels were rendered",
    }
