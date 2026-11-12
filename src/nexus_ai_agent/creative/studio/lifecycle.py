"""Capability lifecycle: the 4-state machine + fail-closed pack gate (session 3).

Session-2 audit finding (NAG-003 / PR#64 review): every pack operation
declares ``required_packs`` on its spec, and *nothing checked it* — a typo'd
pack id passed silently.  This module is the single authority for the
maturity axis and the gate that enforces it:

* :class:`LifecycleState` — ``STUB < EXPERIMENTAL < AVAILABLE`` plus
  ``RETIRED``.  ``STUB`` exists (documented, addressable) but executes
  nothing; ``EXPERIMENTAL`` executes only with explicit opt-in;
  ``AVAILABLE`` executes normally; ``RETIRED`` resolves but refuses new
  executions.
* :func:`check_required_packs` — the fail-closed gate: every pack a command
  names must resolve to ``AVAILABLE`` (or ``EXPERIMENTAL`` with
  ``allow_experimental=True``); anything else raises
  :class:`PackRequirementError` *before* any work starts.  Unknown names,
  ``STUB``, and ``RETIRED`` all refuse — the error names the pack, the
  state found, and the state required.

Evidence axis (OMEGA-01/02)
---------------------------
``OMEGA-01`` added a second, orthogonal ladder —
``PLANNED → IMPLEMENTED → EXECUTION_PROVEN → ARTIFACT_VERIFIED →
PRODUCTION_PROVEN`` (:mod:`nexus_ai_agent.creative.studio.evidence`) — that
answers a different question: *what, exactly, has been proven about this
operation, and by which evidence?*  A record now carries the state that its
evidence supports (:attr:`PackLifecycle.evidence_state`), so a pack cannot claim
``PRODUCTION_PROVEN`` in prose while its table says otherwise.  The maturity
axis above still decides *whether* a command may run; the evidence axis decides
what may be *claimed* about the result.  The evidence for a state is not stored
in this table — it is re-derived from the artifacts on every assessment, and the
table only records the ceiling those measurements reached.

Two axes, two authorities — do not conflate them:

* **lifecycle** (this module): pack *maturity* — may this pack execute here
  at all?  Static table, checked at dispatch (bus step 3.5).
* **availability** (``packs/availability.py``): pack *runnability* — its six
  states (``REGISTERED``/``AVAILABLE``/``MISSING_DEPENDENCY``/
  ``MISSING_BINARY``/``DISABLED``/``FAILED``) answer "can it execute *right
  now*", with injected binary/dependency probes.  Checked at
  render/preflight, where resolvers exist — the bus owns none.

Lifecycle states describe the pack's *contract maturity*, not per-operation
media truth (that is ``execution.py``'s job: a ``STATE_ONLY`` operation that
delivers its documented state edit is fulfilling its contract).  A pack is
``AVAILABLE`` when its flagship paths execute end-to-end or fail loud;
``EXPERIMENTAL`` while media realization is partly unmapped; no current
pack is ``STUB`` or ``RETIRED`` — those states exist for packs that
lose/gain engines, and their refusal behavior is pinned by
``tests/unit/test_capability_lifecycle.py`` against constructed records.

Change a state here only with the tests that prove the new behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from nexus_ai_agent.creative.studio.evidence import OperationEvidenceState
from nexus_ai_agent.creative.studio.models import NagarError


class LifecycleState(str, Enum):
    """The four lifecycle states, in increasing maturity (``RETIRED`` excepted)."""

    STUB = "stub"  # documented + addressable, executes nothing
    EXPERIMENTAL = "experimental"  # executes only with explicit opt-in
    AVAILABLE = "available"  # executes normally
    RETIRED = "retired"  # resolves, refuses new executions


class PackRequirementError(NagarError):
    """A command's ``required_packs`` gate refused to open (fail-closed)."""


@dataclass(frozen=True)
class PackLifecycle:
    """Lifecycle record for one pack: state + why + what to do instead."""

    pack_id: str
    state: LifecycleState
    reason: str
    successor: str | None = None
    #: The highest state the pack's *measured* evidence supports.  Defaults to
    #: ``IMPLEMENTED`` for an executable pack (its handlers exist and its media
    #: paths were reviewed) and ``PLANNED`` for a ``STUB``; a deployment raises
    #: it only by presenting verifier corroborations (``studio.evidence``).
    evidence_state: OperationEvidenceState = OperationEvidenceState.IMPLEMENTED

    @property
    def executable(self) -> bool:
        return self.state in (LifecycleState.AVAILABLE, LifecycleState.EXPERIMENTAL)

    def require_executable(self, *, allow_experimental: bool) -> None:
        if self.state is LifecycleState.AVAILABLE:
            return
        if self.state is LifecycleState.EXPERIMENTAL and allow_experimental:
            return
        need = "AVAILABLE (or EXPERIMENTAL with opt-in)" if allow_experimental else "AVAILABLE"
        raise PackRequirementError(
            f"pack {self.pack_id!r} is {self.state.value} "
            f"({self.reason}); gate requires {need}"
            + (f"; successor: {self.successor}" if self.successor else "")
        )


#: Session-3 lifecycle table, keyed by manifest pack id (the exact strings
#: operations declare in ``required_packs``).  States must match observable
#: behavior — see the module docstring for the promotion rule.
PACK_LIFECYCLE: dict[str, PackLifecycle] = {
    "nexus.slideshow.compose": PackLifecycle(
        pack_id="nexus.slideshow.compose",
        state=LifecycleState.AVAILABLE,
        reason="real ffmpeg Ken Burns encode path with ffprobe evidence",
    ),
    "nexus.language.caption": PackLifecycle(
        pack_id="nexus.language.caption",
        state=LifecycleState.AVAILABLE,
        reason="SRT/ASS document bytes + libass burn-in twin with RTL proof; "
        "engine-backed ops fail loud when the engine is absent",
    ),
    "nexus.edit.timeline": PackLifecycle(
        pack_id="nexus.edit.timeline",
        state=LifecycleState.AVAILABLE,
        reason="editorial state edits fulfilled; media realization via lane twins where mapped",
    ),
    "nexus.audio.studio": PackLifecycle(
        pack_id="nexus.audio.studio",
        state=LifecycleState.EXPERIMENTAL,
        # The reason is evidence-bound, not historical: beat detection,
        # loudness normalization and denoise all run through the native engine
        # and publish verifier-approved bytes, so the old "DSP/beat media paths
        # unmapped" sentence had stopped being true. The state stays EXPERIMENTAL
        # because a production run has not corroborated it with a second
        # verifier key (PRODUCTION_PROVEN is earned, never assigned).
        reason="native engine: loudness/denoise/beats run real DSP and publish "
        "verifier-approved artifacts (ARTIFACT_VERIFIED); awaiting independent "
        "production corroboration for PRODUCTION_PROVEN",
    ),
    "nexus.motion.graphics": PackLifecycle(
        pack_id="nexus.motion.graphics",
        state=LifecycleState.EXPERIMENTAL,
        reason="title twin proven; transitions/particles/masks unmapped",
    ),
    "nexus.color.delivery": PackLifecycle(
        pack_id="nexus.color.delivery",
        state=LifecycleState.EXPERIMENTAL,
        reason="exposure/LUT twins + OTIO export real; the 4k master is a spec, not a render",
    ),
    "nexus.vision.portrait": PackLifecycle(
        pack_id="nexus.vision.portrait",
        state=LifecycleState.EXPERIMENTAL,
        reason="deterministic spec derivation pinned; zero pixel paths",
    ),
    "nexus.vision.scene": PackLifecycle(
        pack_id="nexus.vision.scene",
        state=LifecycleState.EXPERIMENTAL,
        reason="deterministic spec derivation pinned; zero pixel paths",
    ),
}


#: Evidence ceilings measured for this checkout, per pack.
#:
#: Every entry below is the *ceiling* the pack's canonical evidence reaches, and
#: each is produced by re-decoding a published artifact (not by a handler's own
#: claim): ``audio.normalize_loudness`` lands on its locked target and passes the
#: independent true-peak gate, ``audio.remove_noise`` publishes a verified
#: artifact, ``audio.detect_beats`` measures a real tempo.  The canonical audio
#: target set carries exactly **one** signing key id, so the honest ceiling is
#: ``ARTIFACT_VERIFIED``; ``PRODUCTION_PROVEN`` additionally requires two
#: independent verifier key ids over the same artifact hash and is therefore
#: not claimed anywhere in this table.
PACK_EVIDENCE: dict[str, OperationEvidenceState] = {
    "nexus.slideshow.compose": OperationEvidenceState.EXECUTION_PROVEN,
    "nexus.language.caption": OperationEvidenceState.EXECUTION_PROVEN,
    "nexus.edit.timeline": OperationEvidenceState.EXECUTION_PROVEN,
    "nexus.audio.studio": OperationEvidenceState.ARTIFACT_VERIFIED,
    "nexus.motion.graphics": OperationEvidenceState.EXECUTION_PROVEN,
    "nexus.color.delivery": OperationEvidenceState.EXECUTION_PROVEN,
    "nexus.vision.portrait": OperationEvidenceState.IMPLEMENTED,
    "nexus.vision.scene": OperationEvidenceState.IMPLEMENTED,
}


def pack_evidence_state(pack_id: str) -> OperationEvidenceState:
    """The evidence ceiling for ``pack_id`` (unknown → fail-closed ``PLANNED``).

    Unlike :func:`pack_lifecycle` this never raises: it is a *report*, and an
    unknown pack has proven nothing, which is exactly ``PLANNED``.
    """
    if pack_id not in PACK_LIFECYCLE:
        return OperationEvidenceState.PLANNED
    return PACK_EVIDENCE.get(pack_id, OperationEvidenceState.IMPLEMENTED)


def pack_lifecycle(pack_id: str) -> PackLifecycle:
    """Return the lifecycle record for ``pack_id`` (unknown → fail-closed)."""
    try:
        record = PACK_LIFECYCLE[pack_id]
    except KeyError:
        raise PackRequirementError(
            f"unknown pack {pack_id!r} (known: {sorted(PACK_LIFECYCLE)})"
        ) from None
    evidence = pack_evidence_state(pack_id)
    if record.evidence_state is evidence:
        return record
    return replace(record, evidence_state=evidence)


def check_required_packs(
    required_packs: tuple[str, ...] | list[str],
    *,
    allow_experimental: bool = False,
) -> tuple[PackLifecycle, ...]:
    """Fail-closed gate: every named pack must be executable *now*.

    Returns the resolved records (so callers can log states) or raises
    :class:`PackRequirementError` naming the first refusal.  Empty input
    passes (no requirements, no gate).  Duplicate names are collapsed.
    """
    resolved: list[PackLifecycle] = []
    seen: set[str] = set()
    for pack_id in required_packs:
        if pack_id in seen:
            continue
        seen.add(pack_id)
        record = pack_lifecycle(pack_id)
        record.require_executable(allow_experimental=allow_experimental)
        resolved.append(record)
    return tuple(resolved)


__all__ = [
    "LifecycleState",
    "PACK_EVIDENCE",
    "PACK_LIFECYCLE",
    "PackLifecycle",
    "PackRequirementError",
    "check_required_packs",
    "pack_evidence_state",
    "pack_lifecycle",
]
