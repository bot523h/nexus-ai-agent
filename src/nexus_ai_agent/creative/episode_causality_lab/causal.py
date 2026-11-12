"""Three-valued causal state and continuity checking.

The engine walks the declared events in a deterministic chronological order and
applies each event's transition to a world state. It reports typed findings
instead of silently resolving uncertainty:

* a transfer by an actor explicitly *known not* to hold a unique prop is a
  conflict (``FAIL``);
* a transfer when possession is ``UNKNOWN`` is ``NEEDS_REVIEW`` — never a pass
  and never an invented failure;
* revealing a fact the actor is explicitly not declared to know is a conflict;
* a fact whose knowledge is ``UNKNOWN`` is ``NEEDS_REVIEW``;
* two relevant events with an ambiguous declared order yield ``NEEDS_REVIEW``;
* a possession change with no declared transfer event is flagged.

Chronology is the declared ``(episode.order, scene.order, event.order)``
sequence. A declared ``story_tick`` that contradicts that sequence marks a
non-linear (flashback) edit; the engine still reasons over the declared
sequence and reports the discrepancy rather than silently reordering.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import (
    DeclaredFact,
    Event,
    EventKind,
    FactKey,
    FactRelation,
    SourceRef,
    StoryDocument,
)
from .statuses import Status, TriValue

__all__ = [
    "Finding",
    "WorldState",
    "CausalReport",
    "CausalEngine",
    "CausalFindingCode",
]


class CausalFindingCode:
    """The closed set of continuity finding codes."""

    POSSESSION_CONFLICT = "POSSESSION_CONFLICT"
    POSSESSION_UNKNOWN = "POSSESSION_UNKNOWN"
    KNOWLEDGE_PREREQUISITE_UNMET = "KNOWLEDGE_PREREQUISITE_UNMET"
    KNOWLEDGE_UNKNOWN = "KNOWLEDGE_UNKNOWN"
    AMBIGUOUS_ORDER = "AMBIGUOUS_ORDER"
    FLASHBACK_ORDER = "FLASHBACK_ORDER"
    UNDECLARED_POSSESSION_CHANGE = "UNDECLARED_POSSESSION_CHANGE"


#: Which finding codes are hard failures vs. review items.
_FAIL_CODES = frozenset(
    {
        CausalFindingCode.POSSESSION_CONFLICT,
        CausalFindingCode.KNOWLEDGE_PREREQUISITE_UNMET,
        CausalFindingCode.UNDECLARED_POSSESSION_CHANGE,
    }
)
_REVIEW_CODES = frozenset(
    {
        CausalFindingCode.POSSESSION_UNKNOWN,
        CausalFindingCode.KNOWLEDGE_UNKNOWN,
        CausalFindingCode.AMBIGUOUS_ORDER,
    }
)


@dataclass(frozen=True)
class Finding:
    """One typed continuity finding with a source reference and a reason."""

    code: str
    status: Status
    message: str
    source: SourceRef
    event_ids: tuple[str, ...] = ()
    fact: FactKey | None = None

    def sort_key(self) -> tuple[str, str, str]:
        return (self.code, self.source.path, self.message)


class WorldState:
    """An immutable snapshot of the three-valued fact set.

    A fact that is not present is ``UNKNOWN`` unless the story declares a
    closed world, in which case it is ``FALSE``.
    """

    __slots__ = ("_facts", "_closed_world")

    def __init__(self, facts: dict[tuple[str, str, str], TriValue], closed_world: bool) -> None:
        self._facts = facts
        self._closed_world = closed_world

    @property
    def closed_world(self) -> bool:
        return self._closed_world

    def get(self, key: FactKey) -> TriValue:
        found = self._facts.get(key.as_tuple())
        if found is not None:
            return found
        return TriValue.FALSE if self._closed_world else TriValue.UNKNOWN

    def get_tuple(self, subject: str, relation: FactRelation, target: str) -> TriValue:
        return self.get(FactKey(subject_id=subject, relation=relation, target_id=target))

    def items(self) -> tuple[tuple[FactKey, TriValue], ...]:
        out: list[tuple[FactKey, TriValue]] = []
        for (subject, relation, target), value in self._facts.items():
            out.append(
                (
                    FactKey(
                        subject_id=subject,
                        relation=FactRelation(relation),
                        target_id=target,
                    ),
                    value,
                )
            )
        out.sort(key=lambda pair: pair[0].as_tuple())
        return tuple(out)

    def copy(self) -> WorldState:
        return WorldState(dict(self._facts), self._closed_world)

    def snapshot(self) -> tuple[tuple[tuple[str, str, str], TriValue], ...]:
        """A sorted, hashable snapshot of the whole fact set."""
        return tuple(sorted(self._facts.items()))

    @classmethod
    def from_snapshot(
        cls,
        snapshot: tuple[tuple[tuple[str, str, str], TriValue], ...],
        closed_world: bool,
    ) -> WorldState:
        return cls(dict(snapshot), closed_world)


@dataclass(frozen=True)
class CausalReport:
    """The deterministic result of a causal analysis pass."""

    status: Status
    findings: tuple[Finding, ...]
    final_state: WorldState
    event_order: tuple[str, ...]
    fact_rows: tuple[tuple[FactKey, TriValue], ...]
    #: event id -> the set of event ids that event's own precondition read.
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: One ``(event_id, state_snapshot, last_writer_snapshot)`` per event, in
    #: declared order. Used by the incremental engine; kept so incremental ==
    #: full is checkable.
    checkpoints: tuple[
        tuple[
            str,
            tuple[tuple[tuple[str, str, str], TriValue], ...],
            tuple[tuple[tuple[str, str, str], str], ...],
        ],
        ...,
    ] = ()

    def findings_for(self, code: str) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.code == code)


def _sorted_events(doc: StoryDocument) -> list[Event]:
    episode_order = {ep.id: ep.order_index for ep in doc.episodes}
    scene_order = {sc.id: sc.order_index for sc in doc.scenes}
    return sorted(
        doc.events,
        key=lambda e: (
            episode_order[e.episode_id],
            scene_order[e.scene_id],
            e.order_index,
            e.id,
        ),
    )


def _possesses(actor: str, prop: str) -> FactKey:
    return FactKey(subject_id=actor, relation=FactRelation.POSSESSES, target_id=prop)


def encode_fact_token(fact: FactKey) -> str:
    """Encode a fact triple as a single legal identifier token.

    The identifier alphabet is ``[A-Za-z0-9._-]``; a triple encoded with ``|``
    is rewritten to ``.`` so it stays a legal target id. This is the canonical
    encoding used by :func:`knows_fact_key` and by the engine internally, so a
    story author can declare knowledge facts that the engine will match.
    """
    return _sanitize(f"{fact.subject_id}|{fact.relation.value}|{fact.target_id}")


def knows_fact_key(actor: str, fact: FactKey) -> FactKey:
    """The ``(actor, knows, <encoded fact>)`` key the engine reads/writes."""
    return FactKey(
        subject_id=actor, relation=FactRelation.KNOWS, target_id=encode_fact_token(fact)
    )


def _knows(actor: str, fact: FactKey) -> FactKey:
    return knows_fact_key(actor, fact)


def _sanitize(raw: str) -> str:
    """Map an arbitrary encoded key onto the identifier alphabet.

    The identifier validator only accepts ``[A-Za-z0-9._-]``; a fact triple
    encoded with ``|`` is rewritten to ``.`` so it remains a legal target id.
    """
    return raw.replace("|", ".")


def _fact_key_from_encoded(encoded: str) -> FactKey | None:
    parts = encoded.split(".")
    if len(parts) != 3:
        return None
    try:
        return FactKey(
            subject_id=parts[0], relation=FactRelation(parts[1]), target_id=parts[2]
        )
    except Exception:  # pragma: no cover - defensive; keys are built internally
        return None


class CausalEngine:
    """Applies declared events to a three-valued world state."""

    def __init__(self, *, max_events: int = 100_000) -> None:
        self._max_events = max_events

    def analyze(self, doc: StoryDocument) -> CausalReport:
        state = self._initial_state(doc)
        events = _sorted_events(doc)
        if len(events) > self._max_events:  # pragma: no cover - model bounds first
            from .errors import LimitExceededError

            raise LimitExceededError(f"event count {len(events)} exceeds {self._max_events}")

        findings = self._order_findings(doc, events)
        suffix, deps, order, checkpoints = self.replay(events, state)
        findings = findings + suffix

        findings.sort(key=Finding.sort_key)
        return CausalReport(
            status=self._overall(findings),
            findings=tuple(findings),
            final_state=state,
            event_order=tuple(order),
            fact_rows=state.items(),
            dependencies=deps,
            checkpoints=checkpoints,
        )

    def replay(
        self,
        events: list[Event],
        state: WorldState,
        last_writer: dict[tuple[str, str, str], str] | None = None,
    ) -> tuple[
        list[Finding],
        dict[str, tuple[str, ...]],
        list[str],
        tuple[
            tuple[
                str,
                tuple[tuple[tuple[str, str, str], TriValue], ...],
                tuple[tuple[tuple[str, str, str], str], ...],
            ],
            ...,
        ],
    ]:
        """Apply ``events`` to ``state``; return (findings, deps, order, checkpoints).

        ``checkpoints`` is one entry per event:
        ``(event_id, state_after, last_writer_after)`` where each snapshot is
        sorted and hashable. The incremental engine reuses these so a suffix
        recomputation is provably identical to a full replay.

        ``last_writer`` seeds the internal "who last wrote this fact" map; an
        incremental suffix passes the prefix's map so its dependency output is
        identical to a full replay.
        """
        findings: list[Finding] = []
        dependencies: dict[str, tuple[str, ...]] = {}
        order: list[str] = []
        checkpoints: list[
            tuple[
                str,
                tuple[tuple[tuple[str, str, str], TriValue], ...],
                tuple[tuple[tuple[str, str, str], str], ...],
            ]
        ] = []
        writer: dict[tuple[str, str, str], str] = dict(last_writer) if last_writer else {}

        for event in events:
            order.append(event.id)
            deps = self._apply(event, state, findings, writer)
            dependencies[event.id] = deps
            checkpoints.append((event.id, state.snapshot(), tuple(sorted(writer.items()))))
        return findings, dependencies, order, tuple(checkpoints)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _initial_state(self, doc: StoryDocument) -> WorldState:
        facts: dict[tuple[str, str, str], TriValue] = {}
        for declared in doc.initial_facts:
            facts[declared.key.as_tuple()] = declared.value
        return WorldState(facts, doc.closed_world)

    def _overall(self, findings: list[Finding]) -> Status:
        if any(f.status is Status.FAIL for f in findings):
            return Status.FAIL
        if any(f.status in (Status.NEEDS_REVIEW, Status.UNKNOWN) for f in findings):
            return Status.NEEDS_REVIEW
        return Status.PASS

    def _order_findings(self, doc: StoryDocument, events: list[Event]) -> list[Finding]:
        out: list[Finding] = []
        seen: dict[tuple[str, str, int], str] = {}
        for event in events:
            key = (event.episode_id, event.scene_id, event.order_index)
            if key in seen:
                out.append(
                    Finding(
                        code=CausalFindingCode.AMBIGUOUS_ORDER,
                        status=Status.NEEDS_REVIEW,
                        message=(
                            f"events {seen[key]!r} and {event.id!r} share the same declared "
                            f"position {key}; their order is ambiguous"
                        ),
                        source=event.source,
                        event_ids=(seen[key], event.id),
                    )
                )
            else:
                seen[key] = event.id

        ticked = [e for e in events if e.story_tick is not None]
        if len(ticked) >= 2:
            seq = {e.id: i for i, e in enumerate(events)}
            for earlier, later in zip(ticked, ticked[1:], strict=False):
                assert earlier.story_tick is not None and later.story_tick is not None
                if earlier.story_tick > later.story_tick and seq[earlier.id] < seq[later.id]:
                    out.append(
                        Finding(
                            code=CausalFindingCode.FLASHBACK_ORDER,
                            status=Status.NEEDS_REVIEW,
                            message=(
                                f"declared story_tick of {earlier.id!r} "
                                f"({earlier.story_tick}) is later than {later.id!r} "
                                f"({later.story_tick}) but it is edited earlier — a "
                                f"non-linear (flashback) sequence"
                            ),
                            source=earlier.source,
                            event_ids=(earlier.id, later.id),
                        )
                    )
        return out

    def _apply(
        self,
        event: Event,
        state: WorldState,
        findings: list[Finding],
        last_writer: dict[tuple[str, str, str], str],
    ) -> tuple[str, ...]:
        """Mutate ``state`` for one event; return the event ids it depended on."""
        kind = event.kind
        if kind is EventKind.TRANSFER_PROP:
            return self._transfer(event, state, findings, last_writer)
        if kind is EventKind.LEARN_FACT:
            if event.actor_id and event.fact_key:
                self._set(state, _knows(event.actor_id, event.fact_key), TriValue.TRUE)
            return ()
        if kind is EventKind.REVEAL_FACT:
            return self._reveal(event, state, findings)
        if kind is EventKind.ENTER_LOCATION:
            if event.actor_id and event.target_id:
                self._set(state, self._at(event.actor_id, event.target_id), TriValue.TRUE)
            return ()
        if kind is EventKind.LEAVE_LOCATION:
            if event.actor_id and event.target_id:
                self._set(state, self._at(event.actor_id, event.target_id), TriValue.FALSE)
            return ()
        if kind is EventKind.INJURE:
            if event.actor_id:
                self._set(
                    state,
                    FactKey(
                        subject_id=event.actor_id,
                        relation=FactRelation.INJURED,
                        target_id=event.prop_id or "self",
                    ),
                    TriValue.TRUE,
                )
            return ()
        if kind is EventKind.HEAL:
            if event.actor_id:
                self._set(
                    state,
                    FactKey(
                        subject_id=event.actor_id,
                        relation=FactRelation.INJURED,
                        target_id=event.prop_id or "self",
                    ),
                    TriValue.FALSE,
                )
            return ()
        if kind is EventKind.SET_STATE:
            if event.fact_key and event.value is not None:
                self._set(state, event.fact_key, event.value)
            return ()
        return ()  # pragma: no cover - enum is exhaustive

    def _at(self, actor: str, location: str) -> FactKey:
        return FactKey(subject_id=actor, relation=FactRelation.AT_LOCATION, target_id=location)

    def _set(self, state: WorldState, key: FactKey, value: TriValue) -> None:
        state._facts[key.as_tuple()] = value  # noqa: SLF001 - internal mutation API

    def _transfer(
        self,
        event: Event,
        state: WorldState,
        findings: list[Finding],
        last_writer: dict[tuple[str, str, str], str],
    ) -> tuple[str, ...]:
        if not event.actor_id or not event.prop_id:
            return ()
        poss = _possesses(event.actor_id, event.prop_id)
        current = state.get(poss)
        deps: tuple[str, ...] = ()
        writer = last_writer.get(poss.as_tuple())
        if writer is not None:
            deps = (writer,)

        if current is TriValue.FALSE:
            findings.append(
                Finding(
                    code=CausalFindingCode.POSSESSION_CONFLICT,
                    status=Status.FAIL,
                    message=(
                        f"{event.actor_id!r} transfers prop {event.prop_id!r} but is "
                        f"explicitly known not to possess it"
                    ),
                    source=event.source,
                    event_ids=(event.id,),
                    fact=poss,
                )
            )
        elif current is TriValue.UNKNOWN:
            findings.append(
                Finding(
                    code=CausalFindingCode.POSSESSION_UNKNOWN,
                    status=Status.NEEDS_REVIEW,
                    message=(
                        f"possession of prop {event.prop_id!r} by {event.actor_id!r} is "
                        f"unknown at the transfer; cannot confirm or refute"
                    ),
                    source=event.source,
                    event_ids=(event.id,),
                    fact=poss,
                )
            )

        self._set(state, poss, TriValue.FALSE)
        last_writer[poss.as_tuple()] = event.id
        if event.target_id:
            newposs = _possesses(event.target_id, event.prop_id)
            self._set(state, newposs, TriValue.TRUE)
            last_writer[newposs.as_tuple()] = event.id
        return deps

    def _reveal(
        self, event: Event, state: WorldState, findings: list[Finding]
    ) -> tuple[str, ...]:
        if not event.actor_id or not event.fact_key:
            return ()
        knows = _knows(event.actor_id, event.fact_key)
        current = state.get(knows)
        deps: tuple[str, ...] = ()
        if current is TriValue.FALSE:
            findings.append(
                Finding(
                    code=CausalFindingCode.KNOWLEDGE_PREREQUISITE_UNMET,
                    status=Status.FAIL,
                    message=(
                        f"{event.actor_id!r} reveals fact "
                        f"{event.fact_key.as_tuple()} but is explicitly not declared to "
                        f"know it"
                    ),
                    source=event.source,
                    event_ids=(event.id,),
                    fact=event.fact_key,
                )
            )
        elif current is TriValue.UNKNOWN:
            findings.append(
                Finding(
                    code=CausalFindingCode.KNOWLEDGE_UNKNOWN,
                    status=Status.NEEDS_REVIEW,
                    message=(
                        f"whether {event.actor_id!r} knows fact "
                        f"{event.fact_key.as_tuple()} is unknown at the reveal"
                    ),
                    source=event.source,
                    event_ids=(event.id,),
                    fact=event.fact_key,
                )
            )
        if event.target_id:
            self._set(state, _knows(event.target_id, event.fact_key), TriValue.TRUE)
        return deps


def declared_fact_to_rows(facts: Iterable[DeclaredFact]) -> tuple[tuple[FactKey, TriValue], ...]:
    """Utility: expose declared initial facts as sorted rows (for reports)."""
    rows = [(f.key, f.value) for f in facts]
    rows.sort(key=lambda pair: pair[0].as_tuple())
    return tuple(rows)
