"""L0–L4 maturity model — canonical definitions.

Gate 2 must not invent levels from intuition; this module searches repo evidence
first and then defines a minimal, testable ladder.

Repo evidence (2026-09-24):
* No existing L0–L4 definition found in src/ or docs/ (grep returned 0).
* TDD uses "سطح" A/B/C (permission) but not L-levels.
* Runtime packs have pure handlers (domain/reducer) + rendering lane (executor).
* Tests distinguish: defined, registered, reduced, executed, surfaced, proven.

Canonical model (adopted after evidence scan):

* L0 = concept / unimplemented
* L1 = contract or registry presence
* L2 = domain/state behavior proven (pure reducer)
* L3 = real execution/instrument proven (render lane, real encode, FFmpeg probe)
* L4 = end-to-end product/surface proof (Telegram surface, API, user-visible)

This matches the proposal in the Gate 2 mission, but it is now anchored to
repo evidence: registry existence vs handler purity vs render lane vs surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class EvidenceClass(str, Enum):
    """Evidence classification for every claim."""

    VERIFIED = "VERIFIED"
    OBSERVED = "OBSERVED"
    SUPPORTED = "SUPPORTED"
    INFERRED = "INFERRED"
    HYPOTHESIS = "HYPOTHESIS"
    NOT_VERIFIED = "NOT_VERIFIED"
    DOCUMENTED_ONLY = "DOCUMENTED_ONLY"
    MISSING = "MISSING"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class LLevelDefinition:
    level: LLevel
    title: str
    required_evidence: tuple[str, ...]
    allowed_claims: tuple[str, ...]
    transition_criteria: str
    example: str


def canonical_l_levels() -> tuple[LLevelDefinition, ...]:
    return (
        LLevelDefinition(
            level=LLevel.L0,
            title="Concept / Unimplemented",
            required_evidence=("TDD entry or product catalog mention",),
            allowed_claims=("PRODUCT_DEFINED",),
            transition_criteria="TDD defines operation_id, pack, input/output shape — no code",
            example="portrait.smooth_skin defined in TDD but no registry entry",
        ),
        LLevelDefinition(
            level=LLevel.L1,
            title="Contract or Registry Presence",
            required_evidence=(
                "OperationSpec registered in CapabilityRegistry",
                "pack.manifest.json declares capability",
                "TypedCommand operation_id accepted by bus (UnknownOperationError not raised)",
            ),
            allowed_claims=("PRODUCT_DEFINED", "REGISTERED", "COMMAND_CONTRACTED"),
            transition_criteria=(
                "Registry knows operation_id; manifest verifies; no handler required"
            ),
            example="timeline.trim registered, capability present",
        ),
        LLevelDefinition(
            level=LLevel.L2,
            title="Domain / State Behavior Proven",
            required_evidence=(
                "Pure handler (Project, OperationContext) -> OperationOutcome exists",
                "Unit test proves state transition (no I/O)",
                "EffectLayerRef or Timeline patch computed deterministically",
            ),
            allowed_claims=(
                "PRODUCT_DEFINED",
                "REGISTERED",
                "DOMAIN_REDUCER_READY",
                "COMMAND_CONTRACTED",
                "TESTED",
            ),
            transition_criteria="Handler exists, is pure, and has deterministic unit test",
            example="timeline.ripple_delete pure reducer test in test_edit_pack.py",
        ),
        LLevelDefinition(
            level=LLevel.L3,
            title="Real Execution / Instrument Proven",
            required_evidence=(
                "Render lane compiles IR -> filtergraph -> argv (or real audio/caption execution)",
                "Real encode or probe test (imageio-ffmpeg, ffprobe, audio analysis)",
                "No subprocess spawned from pack handler (lane is only executor)",
            ),
            allowed_claims=(
                "PRODUCT_DEFINED",
                "REGISTERED",
                "DOMAIN_REDUCER_READY",
                "EXECUTOR_READY",
                "COMMAND_CONTRACTED",
                "TESTED",
                "PROVEN",
            ),
            transition_criteria="Real execution artifact produced (video, audio, transcript, OTIO)",
            example="delivery.make_proxy_480p renders real mp4 via render lane",
        ),
        LLevelDefinition(
            level=LLevel.L4,
            title="End-to-End Product / Surface Proof",
            required_evidence=(
                "Telegram surface mapping (/edit, /caption, /grade) or API endpoint",
                "Idempotency + auth + locality policy enforced",
                "User-visible output in bot or dashboard",
            ),
            allowed_claims=(
                "PRODUCT_DEFINED",
                "REGISTERED",
                "DOMAIN_REDUCER_READY",
                "EXECUTOR_READY",
                "SURFACE_MAPPED",
                "COMMAND_CONTRACTED",
                "TESTED",
                "PROVEN",
            ),
            transition_criteria="Surface handler calls bus, worker executes, notifier delivers",
            example="media.play wired via bot surface + bus + worker (P0-B)",
        ),
    )


# Helper for matrix: compute L-level from booleans
def compute_l_level(
    *,
    registered: bool,
    domain_reducer_ready: bool,
    executor_ready: bool,
    surface_mapped: bool,
    tested: bool,
    proven: bool,
) -> LLevel:
    if surface_mapped and proven and executor_ready and domain_reducer_ready and registered:
        return LLevel.L4
    if proven and executor_ready and domain_reducer_ready and registered:
        return LLevel.L3
    if domain_reducer_ready and registered:
        return LLevel.L2
    if registered:
        return LLevel.L1
    return LLevel.L0
