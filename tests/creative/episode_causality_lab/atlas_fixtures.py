"""Shared builders for the ATLAS lab tests.

This module is imported as a sibling (``import atlas_fixtures``) — never as
``tests.*`` — so the suite-hygiene guard
(``tests/architecture/test_test_suite_hygiene.py``) stays green.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.episode_causality_lab import (
    SCHEMA_VERSION,
    Character,
    DeclaredFact,
    Episode,
    Event,
    EventKind,
    FactKey,
    FactRelation,
    Location,
    Prop,
    Scene,
    ShotCandidate,
    SourceRef,
    StoryBeat,
    StoryDocument,
    TemporalConstraint,
    TimeOfDay,
    TriValue,
)

SRC = SourceRef(path="ep01/sc01", line=1)


def fact(subject: str, relation: FactRelation, target: str, value: TriValue) -> DeclaredFact:
    return DeclaredFact(
        key=FactKey(subject_id=subject, relation=relation, target_id=target),
        value=value,
        source=SRC,
    )


def event(
    eid: str,
    kind: EventKind,
    *,
    order: int = 0,
    actor: str | None = None,
    target: str | None = None,
    prop: str | None = None,
    fact_key: FactKey | None = None,
    value: TriValue | None = None,
    story_tick: int | None = None,
    scene: str = "sc1",
) -> Event:
    return Event(
        id=eid,
        episode_id="ep1",
        scene_id=scene,
        order_index=order,
        story_tick=story_tick,
        kind=kind,
        actor_id=actor,
        target_id=target,
        prop_id=prop,
        fact_key=fact_key,
        value=value,
        source=SRC,
    )


def base_doc(**overrides: Any) -> StoryDocument:
    """A minimal valid two-character story; override any collection."""
    fields: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "characters": (Character(id="alice"), Character(id="bob")),
        "props": (Prop(id="gun"), Prop(id="key")),
        "locations": (Location(id="room"), Location(id="street")),
        "episodes": (Episode(id="ep1", order_index=0), Episode(id="ep2", order_index=1)),
        "scenes": (
            Scene(
                id="sc1",
                episode_id="ep1",
                location_id="room",
                order_index=0,
                time_of_day=TimeOfDay.DAY,
            ),
            Scene(
                id="sc2",
                episode_id="ep2",
                location_id="street",
                order_index=0,
                time_of_day=TimeOfDay.NIGHT,
            ),
        ),
        "initial_facts": (),
        "events": (),
        "temporal_constraints": (),
        "beats": (),
        "shot_candidates": (),
    }
    fields.update(overrides)
    return StoryDocument(**fields)


def raw_doc(**overrides: Any) -> dict[str, Any]:
    """A JSON-ready story document (dict) for parse_story/loads_story tests."""
    doc = base_doc(**overrides)
    return doc.model_dump(mode="json")


def constraint(cid: str, a: str, b: str, lower: int, upper: int) -> TemporalConstraint:
    return TemporalConstraint(id=cid, a=a, b=b, lower=lower, upper=upper, source=SRC)


def beat(bid: str, order: int) -> StoryBeat:
    return StoryBeat(id=bid, episode_id="ep1", order_index=order)


def shot(
    sid: str,
    covers: tuple[str, ...],
    frames: int,
    *,
    preference: int = 0,
    verified: bool = True,
) -> ShotCandidate:
    return ShotCandidate(
        id=sid,
        asset_ref=f"asset://{sid}",
        covers_beats=covers,
        frames=frames,
        preference=preference,
        evidence_status=TriValue.TRUE if verified else TriValue.UNKNOWN,
        evidence_ref=f"evidence://{sid}" if verified else "",
        source=SRC,
    )
