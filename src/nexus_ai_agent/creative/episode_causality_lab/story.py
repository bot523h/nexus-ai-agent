"""Story loading and structural validation.

Loading is deliberately two-staged: :func:`parse_story` accepts an
already-decoded ``dict`` (the caller owns I/O), and :func:`loads_story` decodes
bytes with the standard library ``json`` only — never ``eval``, ``pickle`` or
any executable deserialiser. Structural validation then checks referential
integrity, which a pure schema model cannot: duplicate identifiers, dangling
references, and impossible episode/scene links.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .errors import (
    DuplicateIdentifierError,
    LimitExceededError,
    StoryValidationError,
    UnknownReferenceError,
    UnsupportedSchemaError,
)
from .models import SCHEMA_VERSION, StoryDocument

__all__ = ["parse_story", "loads_story", "validate_references"]

#: Largest accepted raw document, before parsing. A guard against a hostile or
#: accidental multi-gigabyte payload; the field-level caps in ``models`` apply
#: afterwards.
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024


def parse_story(data: dict[str, Any]) -> StoryDocument:
    """Validate a decoded mapping into a :class:`StoryDocument`.

    Raises a typed error for a malformed document, an unsupported schema
    version, a duplicate identifier, or a dangling reference.
    """
    if not isinstance(data, dict):
        raise StoryValidationError("story document must be a JSON object")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"unsupported schema_version {version!r}; this lab implements {SCHEMA_VERSION}"
        )
    try:
        doc = StoryDocument.model_validate(data)
    except ValidationError as exc:
        raise StoryValidationError(f"story document is invalid: {exc}") from exc
    validate_references(doc)
    return doc


def loads_story(raw: bytes | str) -> StoryDocument:
    """Decode JSON bytes/str and validate. Never executes dynamic code."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise LimitExceededError(f"story document is {len(raw)} bytes > {MAX_DOCUMENT_BYTES}")
        text = raw.decode("utf-8")
    else:
        text = raw
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise StoryValidationError(f"story document is not valid JSON: {exc}") from exc
    return parse_story(data)


def _unique(items: tuple[Any, ...], namespace: str) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for item in items:
        if item.id in seen:
            raise DuplicateIdentifierError(f"duplicate {namespace} id {item.id!r}")
        seen[item.id] = item
    return seen


def validate_references(doc: StoryDocument) -> None:
    """Check every cross-reference resolves; raise a typed error otherwise.

    Also enforces the nested identifier namespaces (a scene id may not collide
    with an event id is *not* required — they are separate namespaces — but a
    reference must point at the right kind of entity).
    """
    characters = _unique(doc.characters, "character")
    props = _unique(doc.props, "prop")
    locations = _unique(doc.locations, "location")
    episodes = _unique(doc.episodes, "episode")
    scenes = _unique(doc.scenes, "scene")
    _unique(doc.events, "event")
    _unique(doc.beats, "beat")
    _unique(doc.shot_candidates, "shot")
    _unique(doc.temporal_constraints, "temporal constraint")

    if not doc.episodes:
        raise StoryValidationError("story declares no episodes")

    episode_orders: dict[int, str] = {}
    for ep in doc.episodes:
        if ep.order_index in episode_orders:
            raise DuplicateIdentifierError(
                f"two episodes share order_index {ep.order_index}: "
                f"{episode_orders[ep.order_index]!r} and {ep.id!r}"
            )
        episode_orders[ep.order_index] = ep.id

    scene_orders: dict[tuple[str, int], str] = {}
    for scene in doc.scenes:
        if scene.episode_id not in episodes:
            raise UnknownReferenceError(
                f"scene {scene.id!r} references unknown episode {scene.episode_id!r}"
            )
        if scene.location_id not in locations:
            raise UnknownReferenceError(
                f"scene {scene.id!r} references unknown location {scene.location_id!r}"
            )
        key = (scene.episode_id, scene.order_index)
        if key in scene_orders:
            raise DuplicateIdentifierError(
                f"two scenes in {scene.episode_id!r} share order_index {scene.order_index}"
            )
        scene_orders[key] = scene.id

    for fact in doc.initial_facts:
        _check_fact_refs(fact.key.subject_id, fact.key.target_id, characters, props, locations)

    for event in doc.events:
        if event.episode_id not in episodes:
            raise UnknownReferenceError(
                f"event {event.id!r} references unknown episode {event.episode_id!r}"
            )
        if event.scene_id not in scenes:
            raise UnknownReferenceError(
                f"event {event.id!r} references unknown scene {event.scene_id!r}"
            )
        if scenes[event.scene_id].episode_id != event.episode_id:
            raise UnknownReferenceError(
                f"event {event.id!r}: scene {event.scene_id!r} is not in episode "
                f"{event.episode_id!r}"
            )
        if event.actor_id is not None and event.actor_id not in characters:
            raise UnknownReferenceError(
                f"event {event.id!r} references unknown character {event.actor_id!r}"
            )
        if event.prop_id is not None and event.prop_id not in props:
            raise UnknownReferenceError(
                f"event {event.id!r} references unknown prop {event.prop_id!r}"
            )
        if event.target_id is not None and event.target_id not in characters:
            raise UnknownReferenceError(
                f"event {event.id!r} references unknown target character {event.target_id!r}"
            )
        if event.fact_key is not None:
            _check_fact_refs(
                event.fact_key.subject_id,
                event.fact_key.target_id,
                characters,
                props,
                locations,
            )

    for beat in doc.beats:
        if beat.episode_id not in episodes:
            raise UnknownReferenceError(
                f"beat {beat.id!r} references unknown episode {beat.episode_id!r}"
            )

    for shot in doc.shot_candidates:
        for beat_id in shot.covers_beats:
            if beat_id not in {b.id for b in doc.beats}:
                raise UnknownReferenceError(f"shot {shot.id!r} covers unknown beat {beat_id!r}")

    _validate_temporal_nodes(doc, scenes)


def _check_fact_refs(
    subject_id: str,
    target_id: str,
    characters: dict[str, Any],
    props: dict[str, Any],
    locations: dict[str, Any],
) -> None:
    known = set(characters) | set(props) | set(locations)
    for ref in (subject_id, target_id):
        if ref not in known:
            raise UnknownReferenceError(f"fact references unknown entity {ref!r}")


def _validate_temporal_nodes(doc: StoryDocument, scenes: dict[str, Any]) -> None:
    event_ids = {e.id for e in doc.events}
    episode_ids = {e.id for e in doc.episodes}
    for con in doc.temporal_constraints:
        for node in (con.a, con.b):
            if node in event_ids:
                continue
            if node.startswith("episode:") and node[len("episode:") :] in episode_ids:
                continue
            if node.startswith("scene:") and node[len("scene:") :] in scenes:
                continue
            raise UnknownReferenceError(
                f"temporal constraint {con.id!r} references unknown node {node!r}"
            )
