"""Bridge: compile a parsed Fountain screenplay into an ATLAS ``StoryDocument``.

This is the seam between the AETHEL director package and the ATLAS
``episode_causality_lab`` engine. It translates *declared* screenplay structure
into ATLAS contracts so the existing causal engine, temporal network, planner
and incremental engine can be reused directly — the director package never
re-implements them.

Everything it emits is *declared*: a character entering a scene is an
``ENTER_LOCATION`` event, a prop interaction found in an action line is a
``TRANSFER_PROP``, and scene order becomes explicit ``scene:<id>`` temporal
anchors. Nothing is inferred beyond the documented, deterministic patterns; a
prop interaction the patterns do not match is simply not emitted (the causal
engine then treats it as ``UNKNOWN``).
"""

from __future__ import annotations

import re

from nexus_ai_agent.creative.episode_causality_lab import (
    SCHEMA_VERSION,
    Character,
    Episode,
    Event,
    EventKind,
    Location,
    Prop,
    Scene,
    SourceRef,
    StoryBeat,
    StoryDocument,
    TemporalConstraint,
    TimeOfDay,
)

from .contracts import MAX_ID_LEN, ParsedScreenplay
from .errors import InputBoundError

__all__ = ["compile_fountain_to_story_document", "slugify_identifier", "TICK_PER_SCENE"]

#: Deterministic story-tick spacing between consecutive scenes.
TICK_PER_SCENE = 1000

_TIME_OF_DAY = {
    "DAY": TimeOfDay.DAY,
    "NIGHT": TimeOfDay.NIGHT,
    "DAWN": TimeOfDay.DAWN,
    "DUSK": TimeOfDay.DUSK,
}

#: Documented, deterministic prop-interaction patterns on action lines.
#: ``actor takes the <prop>`` / ``actor picks up the <prop>`` / ``actor hands
#: the <prop> to <target>``. Case-insensitive; anything else is left UNKNOWN.
_PROP_WORD = r"[A-Za-z][A-Za-z0-9'\-]*"
_PROP_PHRASE = rf"{_PROP_WORD}(?:\s+{_PROP_WORD})?"
_TAKE_RE = re.compile(
    rf"\b(?P<actor>{_PROP_WORD})\s+(?:takes|grabs|picks up)\s+"
    rf"(?:the\s+|a\s+|an\s+)?(?P<prop>{_PROP_PHRASE})",
    re.IGNORECASE,
)
_HAND_RE = re.compile(
    rf"\b(?P<actor>{_PROP_WORD})\s+hands\s+(?:the\s+|a\s+|an\s+)?"
    rf"(?P<prop>{_PROP_PHRASE})\s+to\s+(?P<target>{_PROP_WORD})",
    re.IGNORECASE,
)

_RESERVED = {"the", "a", "an", "to", "and", "of"}
#: Pronouns are never treated as declared character subjects: a pronoun's
#: antecedent is not established in the text, so the interaction stays UNKNOWN.
_PRONOUNS = {"she", "he", "they", "it", "we", "i", "you", "his", "her", "their"}


def slugify_identifier(name: str) -> str:
    """A safe, deterministic identifier from a human name (bounded length)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name.strip()).strip("_").lower()
    if not slug:
        slug = "unnamed"
    if slug[0].isdigit():
        slug = "n_" + slug
    if len(slug) > MAX_ID_LEN:
        raise InputBoundError(f"identifier {name!r} exceeds {MAX_ID_LEN} chars")
    return slug


def _is_prop_token(token: str) -> bool:
    first = token.split()[0].lower() if token.split() else ""
    return bool(first) and first not in _RESERVED


def _is_actor_token(token: str) -> bool:
    return token.lower() not in _PRONOUNS


def _collect_characters(script: ParsedScreenplay) -> dict[str, str]:
    """Map slug -> canonical display name, preserving first-seen order."""
    out: dict[str, str] = {}
    for scene in script.scenes:
        for dlg in scene.dialogue:
            slug = slugify_identifier(dlg.character)
            out.setdefault(slug, dlg.character)
    return out


def compile_fountain_to_story_document(script: ParsedScreenplay, episode_id: str) -> StoryDocument:
    """Compile a parsed screenplay into a versioned ATLAS ``StoryDocument``."""
    ep_slug = slugify_identifier(episode_id)
    character_map = _collect_characters(script)

    locations: dict[str, str] = {}
    for scene in script.scenes:
        loc_slug = slugify_identifier(scene.heading.location)
        locations.setdefault(loc_slug, scene.heading.location)

    props: dict[str, str] = {}
    scenes: list[Scene] = []
    events: list[Event] = []
    beats: list[StoryBeat] = []
    temporal: list[TemporalConstraint] = []

    prev_scene_slug: str | None = None
    for idx, parsed in enumerate(script.scenes):
        scene_id = f"s{idx}_{slugify_identifier(parsed.heading.location)}"
        loc_slug = slugify_identifier(parsed.heading.location)
        tod = _TIME_OF_DAY.get(parsed.heading.time_of_day.upper(), TimeOfDay.UNKNOWN)
        scenes.append(
            Scene(
                id=scene_id,
                episode_id=ep_slug,
                location_id=loc_slug,
                order_index=idx,
                time_of_day=tod,
            )
        )
        src = SourceRef(path=f"{ep_slug}/{scene_id}", line=parsed.heading.line)
        tick = idx * TICK_PER_SCENE

        seen_actors: set[str] = set()
        event_order = 0
        for dlg in parsed.dialogue:
            actor = slugify_identifier(dlg.character)
            if actor not in seen_actors:
                seen_actors.add(actor)
                events.append(
                    Event(
                        id=f"{scene_id}_enter_{actor}",
                        episode_id=ep_slug,
                        scene_id=scene_id,
                        order_index=event_order,
                        story_tick=tick,
                        kind=EventKind.ENTER_LOCATION,
                        actor_id=actor,
                        target_id=loc_slug,
                        source=SourceRef(path=src.path, line=dlg.line),
                    )
                )
                event_order += 1

        for block in parsed.blocks:
            if block.kind.value != "action":
                continue
            for match in _HAND_RE.finditer(block.text):
                if not _is_actor_token(match.group("actor")) or not _is_actor_token(
                    match.group("target")
                ):
                    continue
                actor = slugify_identifier(match.group("actor"))
                target = slugify_identifier(match.group("target"))
                prop_raw = match.group("prop")
                if not _is_prop_token(prop_raw):
                    continue
                prop_slug = slugify_identifier(prop_raw)
                props.setdefault(prop_slug, prop_raw)
                events.append(
                    Event(
                        id=f"{scene_id}_hand_{prop_slug}_{event_order}",
                        episode_id=ep_slug,
                        scene_id=scene_id,
                        order_index=event_order,
                        story_tick=tick,
                        kind=EventKind.TRANSFER_PROP,
                        actor_id=actor,
                        target_id=target,
                        prop_id=prop_slug,
                        source=SourceRef(path=src.path, line=block.line),
                    )
                )
                event_order += 1
            for match in _TAKE_RE.finditer(block.text):
                if not _is_actor_token(match.group("actor")):
                    continue
                actor = slugify_identifier(match.group("actor"))
                prop_raw = match.group("prop")
                if not _is_prop_token(prop_raw):
                    continue
                prop_slug = slugify_identifier(prop_raw)
                props.setdefault(prop_slug, prop_raw)
                events.append(
                    Event(
                        id=f"{scene_id}_take_{prop_slug}_{event_order}",
                        episode_id=ep_slug,
                        scene_id=scene_id,
                        order_index=event_order,
                        story_tick=tick,
                        kind=EventKind.SET_STATE,
                        actor_id=actor,
                        prop_id=prop_slug,
                        source=SourceRef(path=src.path, line=block.line),
                    )
                )
                event_order += 1

        beats.append(
            StoryBeat(
                id=f"beat_{scene_id}",
                episode_id=ep_slug,
                order_index=idx,
                description=parsed.heading.slugline,
            )
        )

        scene_anchor = f"scene:{scene_id}"
        if prev_scene_slug is not None:
            temporal.append(
                TemporalConstraint(
                    id=f"tc_{prev_scene_slug}_{scene_id}",
                    a=f"scene:{prev_scene_slug}",
                    b=scene_anchor,
                    lower=1,
                    upper=10_000_000,
                    source=SourceRef(path="timeline", line=idx),
                )
            )
        prev_scene_slug = scene_id

    return StoryDocument(
        schema_version=SCHEMA_VERSION,
        characters=tuple(
            Character(id=slug, display_name=name) for slug, name in character_map.items()
        ),
        props=tuple(Prop(id=slug, display_name=name) for slug, name in props.items()),
        locations=tuple(Location(id=slug, display_name=name) for slug, name in locations.items()),
        episodes=(Episode(id=ep_slug, order_index=0, title=script.title or ep_slug),),
        scenes=tuple(scenes),
        initial_facts=(),
        events=tuple(events),
        temporal_constraints=tuple(temporal),
        beats=tuple(beats),
        shot_candidates=(),
    )
