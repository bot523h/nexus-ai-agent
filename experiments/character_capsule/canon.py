"""Story canon: immutable facts, mutable facts, and an append-only event ledger.

Visual drift and story drift are different failures, and a system that stores them
in one bag cannot tell them apart. This module therefore separates:

* **Immutable facts** — the character's unchangeable truth ("both eyes are
  grey-blue"; "born in the north"). They may be *added*; they may never be
  *edited* or *removed*. Editing one is :class:`CanonRuleViolation`.
* **Mutable facts** — beliefs, titles, allegiances. They change through a
  revision and (in a real deployment) an approval.
* **Story events** — an append-only ledger of things that happened and their
  permanent effects ("injury at scene 3 leaves a scar on the left cheek"; "dies
  at scene 9"). Effects persist for every later scene, which is exactly what
  makes "the scar disappeared in episode 4" detectable.

:meth:`CanonEngine.check_shot` is the bridge to production: given a shot's
declared canon assertions, it returns typed conflicts (``CANON_CONFLICT`` in the
verdict vocabulary) instead of a similarity score.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, ClassVar

from .errors import CapsuleError, ValidationError
from .model import COLOUR_RE, CharacterCapsule, Scope, _Module, _require
from .wardrobe import WardrobeModule

FACT_ID_RE = re.compile(r"^FACT-\d{3}$")
EVENT_ID_RE = re.compile(r"^EVT-\d{3}$")


class CanonRuleViolation(CapsuleError):
    """A canon edit that the rules forbid (e.g. rewriting an immutable fact)."""

    code = "canon_rule_violation"


class FactKind(str, Enum):
    IMMUTABLE = "IMMUTABLE"
    MUTABLE = "MUTABLE"


@dataclass(frozen=True)
class CanonFact:
    """One asserted truth about the character (or a relationship)."""

    fact_id: str
    predicate: str
    value: str
    kind: FactKind = FactKind.IMMUTABLE
    added_at_scene: int | None = None

    def __post_init__(self) -> None:
        if not FACT_ID_RE.match(self.fact_id):
            raise ValidationError("canon fact ids look like FACT-001", value=self.fact_id)
        _require(bool(self.predicate.strip()), "canon fact needs a predicate")
        _require(bool(self.value.strip()), "canon fact needs a value")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data


@dataclass(frozen=True)
class StoryEvent:
    """Something that happened, and the permanent effect it leaves behind."""

    event_id: str
    kind: str
    scene_order: int
    description: str
    effects: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not EVENT_ID_RE.match(self.event_id):
            raise ValidationError("story event ids look like EVT-001", value=self.event_id)
        _require(
            self.kind in {"injury", "death", "transformation", "wardrobe", "other"},
            f"unsupported story event kind {self.kind!r}",
        )
        _require(int(self.scene_order) >= 1, "story event needs a scene order >= 1")
        _require(bool(self.description.strip()), "story event needs a description")
        object.__setattr__(self, "effects", dict(self.effects or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "scene_order": self.scene_order,
            "description": self.description,
            "effects": dict(self.effects),
        }


@dataclass(frozen=True)
class CanonConflict:
    """A shot/story contradiction, machine-readable."""

    code: str
    detail: str
    shot_id: str = ""
    fact_id: str = ""
    event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CanonModule(_Module):
    """Canon: facts, story events, biography, personality, relationships."""

    MODULE: ClassVar[str] = "canon"
    LABEL: ClassVar[str] = "Canon"
    FIELD_SCOPES: ClassVar[dict[str, Scope]] = {
        "revision": Scope.REVISION,
        "facts": Scope.REVISION,
        "story_events": Scope.REVISION,
        "biography": Scope.REVISION,
        "personality": Scope.REVISION,
        "relationships": Scope.REVISION,
    }

    def __init__(
        self,
        *,
        revision: int = 1,
        facts: Mapping[str, CanonFact] | None = None,
        story_events: tuple[StoryEvent, ...] = (),
        biography: str = "",
        personality: tuple[str, ...] = (),
        relationships: Mapping[str, str] | None = None,
    ) -> None:
        self.revision = int(revision)
        self.facts = dict(facts or {})
        self.story_events = tuple(story_events)
        self.biography = biography
        self.personality = tuple(personality)
        self.relationships = dict(relationships or {})
        self._validate()

    def _validate(self) -> None:
        _require(self.revision >= 1, "canon revision must be >= 1")
        predicates: dict[str, str] = {}
        for key, fact in self.facts.items():
            if key != fact.fact_id:
                raise ValidationError("facts must be keyed by fact_id", key=key)
            if fact.predicate in predicates:
                raise CanonRuleViolation(
                    "two canon facts assert the same predicate",
                    predicate=fact.predicate,
                    first=predicates[fact.predicate],
                    second=fact.fact_id,
                )
            predicates[fact.predicate] = fact.fact_id
        seen: set[str] = set()
        for event in self.story_events:
            if event.event_id in seen:
                raise CanonRuleViolation("duplicate story event id", event_id=event.event_id)
            seen.add(event.event_id)

    def with_revision(self, revision: int) -> CanonModule:
        return CanonModule(
            revision=int(revision),
            facts=self.facts,
            story_events=self.story_events,
            biography=self.biography,
            personality=self.personality,
            relationships=self.relationships,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "facts": {key: fact.to_dict() for key, fact in sorted(self.facts.items())},
            "story_events": [event.to_dict() for event in self.story_events],
            "biography": self.biography,
            "personality": list(self.personality),
            "relationships": dict(sorted(self.relationships.items())),
        }

    def updated(self, updates: Mapping[str, Any]) -> CanonModule:
        """Apply updates while enforcing canon rules.

        * ``facts`` may add facts and change MUTABLE ones; an IMMUTABLE fact that
          disappears or changes value is refused.
        * ``story_events`` is append-only: existing events may not be dropped or
          rewritten.
        """
        allowed = set(self.FIELD_SCOPES) - {"revision"}
        for key in updates:
            if key not in allowed:
                raise ValidationError(f"unknown field {key!r} on module canon", module="canon")
        facts = dict(self.facts)
        if "facts" in updates:
            facts = self._merge_facts(dict(updates["facts"]))
        events = self.story_events
        if "story_events" in updates:
            candidate = tuple(updates["story_events"])
            current = {event.event_id: event for event in self.story_events}
            next_events = {event.event_id: event for event in candidate}
            for event_id, event in current.items():
                if event_id not in next_events:
                    raise CanonRuleViolation(
                        "story events are append-only; an event cannot be removed",
                        event_id=event_id,
                    )
                if next_events[event_id] != event:
                    raise CanonRuleViolation(
                        "story events are append-only; an event cannot be rewritten",
                        event_id=event_id,
                    )
            events = candidate
        return CanonModule(
            revision=self.revision + 1,
            facts=facts,
            story_events=events,
            biography=str(updates.get("biography", self.biography)),
            personality=tuple(updates.get("personality", self.personality)),
            relationships=dict(updates.get("relationships", self.relationships)),
        )

    def _merge_facts(self, candidate: dict[str, CanonFact]) -> dict[str, CanonFact]:
        for fact_id, fact in candidate.items():
            if fact_id != fact.fact_id:
                raise ValidationError("facts must be keyed by fact_id", key=fact_id)
        for fact_id, current in self.facts.items():
            if fact_id not in candidate:
                raise CanonRuleViolation(
                    "canon facts cannot be removed", fact_id=fact_id, kind=current.kind.value
                )
            proposed = candidate[fact_id]
            if proposed == current:
                continue
            if current.kind is FactKind.IMMUTABLE:
                raise CanonRuleViolation(
                    "an IMMUTABLE canon fact cannot be changed",
                    fact_id=fact_id,
                    predicate=current.predicate,
                    was=current.value,
                    proposed=proposed.value,
                )
            if proposed.predicate != current.predicate:
                raise CanonRuleViolation(
                    "a fact cannot change its predicate; add a new fact instead",
                    fact_id=fact_id,
                )
        predicates = {fact.predicate: fact_id for fact_id, fact in candidate.items()}
        if len(predicates) != len(candidate):
            raise CanonRuleViolation("two canon facts assert the same predicate")
        return candidate

    def with_fact(self, fact: CanonFact) -> CanonModule:
        facts = dict(self.facts)
        facts[fact.fact_id] = fact
        return self.updated({"facts": facts})

    def with_story_event(self, event: StoryEvent) -> CanonModule:
        return self.updated({"story_events": (*self.story_events, event)})


def _effect_strings(effects: Mapping[str, str]) -> dict[str, str]:
    return {str(key): str(value) for key, value in effects.items()}


class CanonEngine:
    """Evaluates a shot against one capsule's canon (pure, no I/O)."""

    def __init__(self, capsule: CharacterCapsule) -> None:
        module = capsule.modules.get("canon")
        if not isinstance(module, CanonModule):
            raise ValidationError("capsule has no canon module")
        self.capsule = capsule
        self.canon: CanonModule = module

    # -- individual rules -------------------------------------------------
    def immutable_contradictions(
        self, assertions: Mapping[str, str], *, shot_id: str = ""
    ) -> list[CanonConflict]:
        conflicts: list[CanonConflict] = []
        for fact in self.canon.facts.values():
            if fact.kind is not FactKind.IMMUTABLE:
                continue
            if fact.predicate in assertions and assertions[fact.predicate] != fact.value:
                conflicts.append(
                    CanonConflict(
                        code="IMMUTABLE_FACT_CONTRADICTION",
                        detail=(
                            f"canon says {fact.predicate}={fact.value!r} but the shot asserts "
                            f"{assertions[fact.predicate]!r}"
                        ),
                        fact_id=fact.fact_id,
                        shot_id=shot_id,
                    )
                )
        return conflicts

    def story_effects(
        self, *, scene_order: int, assertions: Mapping[str, str], shot_id: str
    ) -> list[CanonConflict]:
        conflicts: list[CanonConflict] = []
        for event in self.canon.story_events:
            effects = _effect_strings(event.effects)
            if event.kind == "death":
                if event.scene_order < scene_order:
                    conflicts.append(
                        CanonConflict(
                            code="POST_DEATH_PRESENCE",
                            detail=(
                                f"character died at scene {event.scene_order} but appears in "
                                f"scene {scene_order}"
                            ),
                            event_id=event.event_id,
                            shot_id=shot_id,
                        )
                    )
                continue
            if event.scene_order > scene_order or not effects:
                continue
            for predicate, value in sorted(effects.items()):
                if predicate not in assertions:
                    # The shot is silent about the effect. Silence is not a
                    # contradiction: it is missing evidence and is reported for
                    # review rather than as a canon violation.
                    conflicts.append(
                        CanonConflict(
                            code="STORY_EFFECT_NOT_ASSERTED",
                            detail=(
                                f"event {event.event_id} ({event.description}) requires "
                                f"{predicate}={value!r} from scene {event.scene_order}; "
                                "the shot does not assert it"
                            ),
                            event_id=event.event_id,
                            shot_id=shot_id,
                        )
                    )
                    continue
                if assertions.get(predicate) != value:
                    conflicts.append(
                        CanonConflict(
                            code="STORY_EVENT_NOT_HONOURED",
                            detail=(
                                f"event {event.event_id} ({event.description}) requires "
                                f"{predicate}={value!r} from scene {event.scene_order}; the shot "
                                f"asserts {assertions.get(predicate)!r}"
                            ),
                            event_id=event.event_id,
                            shot_id=shot_id,
                        )
                    )
        return conflicts

    @staticmethod
    def outfit_era_conflicts(
        wardrobe: WardrobeModule, *, outfit_id: str, scene_order: int, shot_id: str
    ) -> list[CanonConflict]:
        outfit = wardrobe.outfits.get(outfit_id)
        if outfit is None:
            return [
                CanonConflict(
                    code="UNKNOWN_OUTFIT",
                    detail=f"outfit {outfit_id!r} is not registered in the wardrobe",
                    shot_id=shot_id,
                )
            ]
        if outfit.valid_at(scene_order):
            return []
        return [
            CanonConflict(
                code="OUTFIT_ERA_CONFLICT",
                detail=(
                    f"outfit {outfit_id} is declared for scenes "
                    f"{outfit.era_from_scene}..{outfit.era_to_scene} but appears at scene "
                    f"{scene_order}"
                ),
                shot_id=shot_id,
            )
        ]

    # -- one call for the verifier ---------------------------------------
    def check_shot(
        self,
        *,
        shot_id: str,
        scene_order: int,
        assertions: Mapping[str, str],
        outfit_id: str | None = None,
    ) -> tuple[CanonConflict, ...]:
        """All canon conflicts for one shot, in a stable order."""
        conflicts: list[CanonConflict] = []
        conflicts.extend(self.immutable_contradictions(assertions, shot_id=shot_id))
        conflicts.extend(
            self.story_effects(scene_order=scene_order, assertions=assertions, shot_id=shot_id)
        )
        wardrobe = self.capsule.modules.get("wardrobe")
        if outfit_id is not None and isinstance(wardrobe, WardrobeModule):
            conflicts.extend(
                self.outfit_era_conflicts(
                    wardrobe, outfit_id=outfit_id, scene_order=scene_order, shot_id=shot_id
                )
            )
        return tuple(
            sorted(
                conflicts,
                key=lambda item: (item.code, item.shot_id, item.fact_id, item.event_id),
            )
        )


def colour_fact(fact_id: str, predicate: str, colour: str) -> CanonFact:
    """Helper for the common 'irreversible appearance' canon fact."""
    if not COLOUR_RE.match(colour):
        raise ValidationError("canon colour facts need #rrggbb", value=colour)
    return CanonFact(fact_id=fact_id, predicate=predicate, value=colour)
