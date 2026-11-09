"""Wardrobe continuity: outfits, versions, and an explicit change timeline.

Why wardrobe is its own module
-----------------------------
Costume drift is the most common real continuity failure in episodic production,
and it is *not* the same problem as face drift: an outfit is **supposed** to
change when the story says so. A verifier that cannot tell "planned change" from
"accidental change" either misses every real wardrobe error or rejects every
legitimate scene.

So the model carries the plan explicitly:

* an :class:`Outfit` is a versioned asset (colours, material, accessories, era);
* a :class:`WardrobeChangeEvent` marks the one point in a sequence where a
  change is intentional, with a reason;
* :func:`expected_outfit` answers "what should this shot be wearing?" from the
  plan alone, and :func:`check_wardrobe` turns a mismatch into
  ``WARDROBE_DRIFT``.

"Same outfit id, different content" is refused: that is what versions are for.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, ClassVar

from .errors import ValidationError
from .model import Scope, _Module, _require, _require_colour

OUTFIT_ID_RE = re.compile(r"^OUT-\d{4}$")
EVENT_ID_RE = re.compile(r"^WCE-\d{4}$")


@dataclass(frozen=True)
class Outfit:
    """One immutable content version of one outfit."""

    outfit_id: str
    version: int
    label: str
    top_colour: str
    bottom_colour: str
    accent_colour: str = "#2f2f2f"
    material: str = "cotton"
    accessories: tuple[str, ...] = ()
    era_from_scene: int | None = None
    era_to_scene: int | None = None

    def __post_init__(self) -> None:
        if not OUTFIT_ID_RE.match(self.outfit_id):
            raise ValidationError("outfit ids look like OUT-0001", value=self.outfit_id)
        _require(int(self.version) >= 1, "outfit version must be >= 1")
        _require(bool(self.label.strip()), "outfit label must not be empty")
        _require_colour(self.top_colour, field_name="outfit.top_colour")
        _require_colour(self.bottom_colour, field_name="outfit.bottom_colour")
        _require_colour(self.accent_colour, field_name="outfit.accent_colour")
        _require(
            self.material in {"cotton", "wool", "leather", "silk", "synthetic"},
            f"unsupported outfit material {self.material!r}",
        )
        if self.era_from_scene is not None and self.era_to_scene is not None:
            _require(
                self.era_from_scene <= self.era_to_scene,
                "outfit era_from_scene must be <= era_to_scene",
            )

    def valid_at(self, scene_order: int) -> bool:
        if self.era_from_scene is not None and scene_order < self.era_from_scene:
            return False
        return not (self.era_to_scene is not None and scene_order > self.era_to_scene)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["accessories"] = list(self.accessories)
        return data


@dataclass(frozen=True)
class WardrobeChangeEvent:
    """A planned outfit change inside one sequence."""

    event_id: str
    sequence_id: str
    from_shot_index: int
    outfit_id: str
    reason: str

    def __post_init__(self) -> None:
        if not EVENT_ID_RE.match(self.event_id):
            raise ValidationError("wardrobe event ids look like WCE-0001", value=self.event_id)
        _require(bool(self.sequence_id.strip()), "change event needs a sequence_id")
        _require(int(self.from_shot_index) >= 1, "change event shot index must be >= 1")
        _require(bool(self.outfit_id.strip()), "change event needs an outfit_id")
        _require(bool(self.reason.strip()), "change event needs a reason")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WardrobeFinding:
    """A structured wardrobe verdict for one shot."""

    code: str
    detail: str
    expected_outfit: str
    actual_outfit: str
    sequence_id: str
    shot_index: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WardrobeModule(_Module):
    """Outfits + per-sequence baseline + planned change events."""

    MODULE: ClassVar[str] = "wardrobe"
    LABEL: ClassVar[str] = "Wardrobe"
    FIELD_SCOPES: ClassVar[dict[str, Scope]] = {
        "revision": Scope.REVISION,
        "outfits": Scope.REVISION,
        "sequence_baselines": Scope.REVISION,
        "change_events": Scope.REVISION,
        "scene_rules": Scope.REVISION,
    }

    def __init__(
        self,
        *,
        revision: int = 1,
        outfits: Mapping[str, Outfit] | None = None,
        sequence_baselines: Mapping[str, str] | None = None,
        change_events: tuple[WardrobeChangeEvent, ...] = (),
        scene_rules: Mapping[str, str] | None = None,
    ) -> None:
        self.revision = int(revision)
        self.outfits = dict(outfits or {})
        self.sequence_baselines = dict(sequence_baselines or {})
        self.change_events = tuple(change_events)
        self.scene_rules = dict(scene_rules or {})
        self._validate()

    def _validate(self) -> None:
        _require(self.revision >= 1, "wardrobe revision must be >= 1")
        for key, outfit in self.outfits.items():
            if key != outfit.outfit_id:
                raise ValidationError("outfits must be keyed by their outfit_id", key=key)
        for sequence, outfit_id in self.sequence_baselines.items():
            _require(bool(sequence.strip()), "sequence baseline needs a sequence id")
            _require(
                outfit_id in self.outfits,
                f"sequence {sequence} baseline references unknown outfit {outfit_id}",
            )
        seen: set[tuple[str, int]] = set()
        for event in self.change_events:
            _require(
                event.outfit_id in self.outfits,
                f"change event {event.event_id} references unknown outfit {event.outfit_id}",
            )
            key = (event.sequence_id, event.from_shot_index)
            _require(key not in seen, f"duplicate wardrobe change event at {key}")
            seen.add(key)
        for scene_id, outfit_id in self.scene_rules.items():
            _require(
                outfit_id in self.outfits,
                f"scene rule for {scene_id} references unknown outfit {outfit_id}",
            )

    def payload(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "outfits": {key: outfit.to_dict() for key, outfit in sorted(self.outfits.items())},
            "sequence_baselines": dict(sorted(self.sequence_baselines.items())),
            "change_events": [event.to_dict() for event in self.change_events],
            "scene_rules": dict(sorted(self.scene_rules.items())),
        }

    def updated(self, updates: Mapping[str, Any]) -> WardrobeModule:
        known = set(self.FIELD_SCOPES)
        data: dict[str, Any] = {
            "revision": self.revision,
            "outfits": self.outfits,
            "sequence_baselines": self.sequence_baselines,
            "change_events": self.change_events,
            "scene_rules": self.scene_rules,
        }
        for key, value in updates.items():
            if key not in known:
                raise ValidationError(
                    f"unknown field {key!r} on module wardrobe", module="wardrobe"
                )
            if key == "revision":
                raise ValidationError("revision is managed by the capsule")
            data[key] = value
        data["revision"] = self.revision + 1
        return WardrobeModule(**data)

    # -- convenience constructors ----------------------------------------
    def with_outfit(self, outfit: Outfit) -> WardrobeModule:
        existing = self.outfits.get(outfit.outfit_id)
        if existing is not None and existing == outfit:
            return self
        outfits = dict(self.outfits)
        outfits[outfit.outfit_id] = outfit
        return WardrobeModule(
            revision=self.revision + 1,
            outfits=outfits,
            sequence_baselines=self.sequence_baselines,
            change_events=self.change_events,
            scene_rules=self.scene_rules,
        )

    def with_change_event(self, event: WardrobeChangeEvent) -> WardrobeModule:
        return WardrobeModule(
            revision=self.revision + 1,
            outfits=self.outfits,
            sequence_baselines=self.sequence_baselines,
            change_events=(*self.change_events, event),
            scene_rules=self.scene_rules,
        )

    def with_sequence_baseline(self, sequence_id: str, outfit_id: str) -> WardrobeModule:
        baselines = dict(self.sequence_baselines)
        baselines[sequence_id] = outfit_id
        return WardrobeModule(
            revision=self.revision + 1,
            outfits=self.outfits,
            sequence_baselines=baselines,
            change_events=self.change_events,
            scene_rules=self.scene_rules,
        )


def expected_outfit(module: WardrobeModule, *, sequence_id: str, shot_index: int) -> str:
    """The outfit the plan requires for this shot (fails closed when undefined)."""
    base = module.sequence_baselines.get(sequence_id)
    if base is None:
        raise ValidationError(
            f"sequence {sequence_id!r} has no wardrobe baseline; wardrobe continuity is undefined",
            sequence_id=sequence_id,
        )
    expected = base
    for event in sorted(module.change_events, key=lambda item: item.from_shot_index):
        if event.sequence_id == sequence_id and shot_index >= event.from_shot_index:
            expected = event.outfit_id
    return expected


def check_wardrobe(
    module: WardrobeModule,
    *,
    sequence_id: str,
    shot_index: int,
    actual_outfit_id: str,
) -> WardrobeFinding | None:
    """Return ``WARDROBE_DRIFT`` when a shot's outfit contradicts the plan."""
    expected = expected_outfit(module, sequence_id=sequence_id, shot_index=shot_index)
    if actual_outfit_id == expected:
        return None
    return WardrobeFinding(
        code="WARDROBE_DRIFT",
        detail=(
            f"shot {shot_index} of sequence {sequence_id} wears {actual_outfit_id}; "
            f"the timeline plan requires {expected}"
        ),
        expected_outfit=expected,
        actual_outfit=actual_outfit_id,
        sequence_id=sequence_id,
        shot_index=shot_index,
    )


class WardrobeContinuity(str, Enum):
    """What the plan says about a candidate change."""

    PLANNED = "PLANNED"
    UNPLANNED = "UNPLANNED"


def classify_change(
    module: WardrobeModule,
    *,
    sequence_id: str,
    shot_index: int,
    candidate_outfit_id: str,
) -> WardrobeContinuity:
    """Is switching to ``candidate_outfit_id`` here planned or drift?"""
    if candidate_outfit_id not in module.outfits:
        raise ValidationError(f"unknown outfit {candidate_outfit_id!r}")
    expected = expected_outfit(module, sequence_id=sequence_id, shot_index=shot_index)
    return (
        WardrobeContinuity.PLANNED
        if candidate_outfit_id == expected
        else WardrobeContinuity.UNPLANNED
    )


def validate_outfit_era(module: WardrobeModule, *, outfit_id: str, scene_order: int) -> None:
    """Refuse an outfit outside the era it is declared for (story canon, not drift)."""
    outfit = module.outfits.get(outfit_id)
    if outfit is None:
        raise ValidationError(f"unknown outfit {outfit_id!r}")
    if not outfit.valid_at(scene_order):
        raise ValidationError(
            f"outfit {outfit_id} is not valid at scene order {scene_order}",
            outfit=outfit_id,
            era_from=outfit.era_from_scene,
            era_to=outfit.era_to_scene,
        )


def empty_wardrobe() -> WardrobeModule:
    """A wardrobe module with no plan — continuity is undefined until one exists."""
    return WardrobeModule(
        revision=1, outfits={}, sequence_baselines={}, change_events=(), scene_rules={}
    )
