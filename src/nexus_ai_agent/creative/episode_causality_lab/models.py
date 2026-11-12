"""Versioned, bounded, strictly-validated story input for the ATLAS lab.

The lab consumes an *explicit* structured document. It never infers facts from
free text, never invents a character, and never treats a caller-supplied
``verified=True`` as trusted provenance. Everything the analyser reasons about
is declared here.

Design rules enforced by this module:

* **Strict** — unknown fields are rejected (``extra="forbid"``) so a typo cannot
  silently become a default.
* **Bounded** — counts, identifier length, text length, nesting and numeric
  ranges are capped by :data:`INPUT_BOUNDS`; exceeding a bound raises
  :class:`~nexus_ai_agent.creative.episode_causality_lab.errors.LimitExceededError`.
* **Deterministic** — :meth:`StoryDocument.canonical_bytes` yields a stable,
  key-sorted serialisation so two equal stories hash identically.
* **No ambient defaults** — nothing here reads ``Path.cwd()`` or the
  environment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import LimitExceededError, StoryValidationError
from .statuses import TriValue

__all__ = [
    "SCHEMA_VERSION",
    "INPUT_BOUNDS",
    "InputBounds",
    "SourceRef",
    "FactKey",
    "FactRelation",
    "EventKind",
    "TimeOfDay",
    "Character",
    "Prop",
    "Location",
    "Episode",
    "Scene",
    "DeclaredFact",
    "Event",
    "TemporalConstraint",
    "StoryBeat",
    "ShotCandidate",
    "StoryDocument",
]

#: The only schema version this lab implements. Any other value is refused.
SCHEMA_VERSION = 1

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
_NODE_PATTERN = r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*|(?:episode|scene):[A-Za-z0-9][A-Za-z0-9._-]*)$"


@dataclass(frozen=True)
class InputBounds:
    """Hard caps on the size and shape of an accepted story document."""

    max_characters: int = 2_000
    max_props: int = 5_000
    max_locations: int = 2_000
    max_episodes: int = 512
    max_scenes: int = 20_000
    max_events: int = 100_000
    max_facts: int = 100_000
    max_temporal_constraints: int = 100_000
    max_beats: int = 100_000
    max_shot_candidates: int = 50_000
    max_id_length: int = 128
    max_text_length: int = 4_000
    max_list_items: int = 64
    max_abs_time: int = 10**15


INPUT_BOUNDS = InputBounds()


class FactRelation(str, Enum):
    """The closed set of relations the causal model tracks."""

    POSSESSES = "possesses"
    KNOWS = "knows"
    AT_LOCATION = "at_location"
    ALIVE = "alive"
    INJURED = "injured"


class EventKind(str, Enum):
    """Declared event kinds. Each maps to a deterministic state transition."""

    LEARN_FACT = "learn_fact"
    REVEAL_FACT = "reveal_fact"
    TRANSFER_PROP = "transfer_prop"
    ENTER_LOCATION = "enter_location"
    LEAVE_LOCATION = "leave_location"
    SET_STATE = "set_state"
    INJURE = "injure"
    HEAL = "heal"


class TimeOfDay(str, Enum):
    """Scene light state. ``UNKNOWN`` is honest; it is not ``DAY``."""

    DAY = "DAY"
    NIGHT = "NIGHT"
    DAWN = "DAWN"
    DUSK = "DUSK"
    UNKNOWN = "UNKNOWN"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_id(value: str, *, field_name: str) -> str:
    if not value or len(value) > INPUT_BOUNDS.max_id_length:
        raise StoryValidationError(
            f"{field_name} must be 1..{INPUT_BOUNDS.max_id_length} chars, got {len(value)}"
        )
    import re

    if not re.match(_ID_PATTERN, value):
        raise StoryValidationError(f"{field_name}={value!r} is not a safe identifier")
    return value


class SourceRef(_Strict):
    """A stable pointer back to where a declared item came from.

    ``path`` is a logical label (e.g. ``"ep01/sc03"``), never an ambient
    filesystem path the lab resolves.
    """

    path: str = Field(min_length=1, max_length=256)
    line: int = Field(default=0, ge=0, le=INPUT_BOUNDS.max_abs_time)
    note: str = Field(default="", max_length=INPUT_BOUNDS.max_text_length)


class FactKey(_Strict):
    """A ``(subject, relation, target)`` triple identifying one fact slot."""

    subject_id: str
    relation: FactRelation
    target_id: str

    @field_validator("subject_id")
    @classmethod
    def _v_subject(cls, v: str) -> str:
        return _check_id(v, field_name="fact.subject_id")

    @field_validator("target_id")
    @classmethod
    def _v_target(cls, v: str) -> str:
        return _check_id(v, field_name="fact.target_id")

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.subject_id, self.relation.value, self.target_id)


class Character(_Strict):
    id: str
    display_name: str = Field(default="", max_length=INPUT_BOUNDS.max_text_length)

    @field_validator("id")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_id(v, field_name="character.id")


class Prop(_Strict):
    id: str
    display_name: str = Field(default="", max_length=INPUT_BOUNDS.max_text_length)
    unique: bool = True

    @field_validator("id")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_id(v, field_name="prop.id")


class Location(_Strict):
    id: str
    display_name: str = Field(default="", max_length=INPUT_BOUNDS.max_text_length)

    @field_validator("id")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_id(v, field_name="location.id")


class Episode(_Strict):
    id: str
    order_index: int = Field(ge=0, le=INPUT_BOUNDS.max_abs_time)
    title: str = Field(default="", max_length=INPUT_BOUNDS.max_text_length)

    @field_validator("id")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_id(v, field_name="episode.id")


class Scene(_Strict):
    id: str
    episode_id: str
    location_id: str
    order_index: int = Field(ge=0, le=INPUT_BOUNDS.max_abs_time)
    time_of_day: TimeOfDay = TimeOfDay.UNKNOWN

    @field_validator("id", "episode_id", "location_id")
    @classmethod
    def _v(cls, v: str, info: object) -> str:
        return _check_id(v, field_name="scene field")


class DeclaredFact(_Strict):
    key: FactKey
    value: TriValue
    source: SourceRef


class Event(_Strict):
    """One declared story-world event."""

    id: str
    episode_id: str
    scene_id: str
    order_index: int = Field(ge=0, le=INPUT_BOUNDS.max_abs_time)
    story_tick: int | None = Field(default=None, ge=0, le=INPUT_BOUNDS.max_abs_time)
    kind: EventKind
    actor_id: str | None = None
    target_id: str | None = None
    prop_id: str | None = None
    fact_key: FactKey | None = None
    value: TriValue | None = None
    source: SourceRef

    @field_validator("id", "episode_id", "scene_id")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_id(v, field_name="event field")

    @field_validator("actor_id", "target_id", "prop_id")
    @classmethod
    def _v_opt(cls, v: str | None) -> str | None:
        return None if v is None else _check_id(v, field_name="event ref")


class TemporalConstraint(_Strict):
    """A declared bound ``lower <= t_b - t_a <= upper`` in exact integer ticks.

    ``a`` and ``b`` are node identifiers: an event id or a declared timeline
    anchor (``episode:<id>`` / ``scene:<id>``).
    """

    id: str
    a: str
    b: str
    lower: int = Field(ge=-INPUT_BOUNDS.max_abs_time, le=INPUT_BOUNDS.max_abs_time)
    upper: int = Field(ge=-INPUT_BOUNDS.max_abs_time, le=INPUT_BOUNDS.max_abs_time)
    source: SourceRef

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _check_id(v, field_name="temporal.id")

    @field_validator("a", "b")
    @classmethod
    def _v_node(cls, v: str) -> str:
        if not v or len(v) > INPUT_BOUNDS.max_id_length:
            raise StoryValidationError(
                f"temporal node must be 1..{INPUT_BOUNDS.max_id_length} chars"
            )
        import re

        if not re.match(_NODE_PATTERN, v):
            raise StoryValidationError(f"temporal node={v!r} is not a valid node id")
        return v

    @model_validator(mode="after")
    def _ordering(self) -> TemporalConstraint:
        if self.lower > self.upper:
            raise StoryValidationError(
                f"temporal constraint {self.id!r}: lower ({self.lower}) > upper ({self.upper})"
            )
        return self


class StoryBeat(_Strict):
    id: str
    episode_id: str
    order_index: int = Field(ge=0, le=INPUT_BOUNDS.max_abs_time)
    description: str = Field(default="", max_length=INPUT_BOUNDS.max_text_length)

    @field_validator("id", "episode_id")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_id(v, field_name="beat field")


class ShotCandidate(_Strict):
    """A candidate shot/take an editor could use to cover one or more beats.

    ``asset_ref`` is opaque: the lab never resolves it. ``evidence_status`` is
    the caller's *declared* external-continuity verification state, and the
    solver treats anything other than an explicit ``TRUE`` with a non-empty
    ``evidence_ref`` as unverified.
    """

    id: str
    asset_ref: str = Field(min_length=1, max_length=INPUT_BOUNDS.max_id_length)
    covers_beats: tuple[str, ...]
    frames: int = Field(ge=0, le=INPUT_BOUNDS.max_abs_time)
    preference: int = Field(default=0, ge=-INPUT_BOUNDS.max_abs_time, le=INPUT_BOUNDS.max_abs_time)
    evidence_status: TriValue = TriValue.UNKNOWN
    evidence_ref: str = Field(default="", max_length=256)
    source: SourceRef

    @field_validator("id")
    @classmethod
    def _v(cls, v: str) -> str:
        return _check_id(v, field_name="shot.id")

    @field_validator("covers_beats")
    @classmethod
    def _v_beats(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) > INPUT_BOUNDS.max_list_items:
            raise LimitExceededError(
                f"covers_beats has {len(v)} items > {INPUT_BOUNDS.max_list_items}"
            )
        for beat in v:
            _check_id(beat, field_name="shot.covers_beats")
        if len(set(v)) != len(v):
            raise StoryValidationError("covers_beats contains duplicates")
        return v


class StoryDocument(_Strict):
    """The complete, versioned story input."""

    schema_version: int
    closed_world: bool = False
    characters: tuple[Character, ...] = ()
    props: tuple[Prop, ...] = ()
    locations: tuple[Location, ...] = ()
    episodes: tuple[Episode, ...] = ()
    scenes: tuple[Scene, ...] = ()
    initial_facts: tuple[DeclaredFact, ...] = ()
    events: tuple[Event, ...] = ()
    temporal_constraints: tuple[TemporalConstraint, ...] = ()
    beats: tuple[StoryBeat, ...] = ()
    shot_candidates: tuple[ShotCandidate, ...] = ()

    @model_validator(mode="after")
    def _bounds(self) -> StoryDocument:
        b = INPUT_BOUNDS
        for name, count, cap in (
            ("characters", len(self.characters), b.max_characters),
            ("props", len(self.props), b.max_props),
            ("locations", len(self.locations), b.max_locations),
            ("episodes", len(self.episodes), b.max_episodes),
            ("scenes", len(self.scenes), b.max_scenes),
            ("events", len(self.events), b.max_events),
            ("initial_facts", len(self.initial_facts), b.max_facts),
            ("temporal_constraints", len(self.temporal_constraints), b.max_temporal_constraints),
            ("beats", len(self.beats), b.max_beats),
            ("shot_candidates", len(self.shot_candidates), b.max_shot_candidates),
        ):
            if count > cap:
                raise LimitExceededError(f"{name}: {count} exceeds the limit {cap}")
        return self

    def canonical_json(self) -> str:
        """A deterministic, key-sorted serialisation of the document."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    def fingerprint(self) -> str:
        """A SHA-256 over the canonical bytes. Identifies bytes, not truth."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
