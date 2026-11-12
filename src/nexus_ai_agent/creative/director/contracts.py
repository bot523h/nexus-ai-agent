"""Typed contracts for the AETHEL director package.

Every type here is a *local, advisory* representation owned by the director
package. Nothing in this module mints an ``AssetVersion``, a passport, or any
project state; the package analyses and proposes, it does not own the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction

#: Hard input bounds — no ambient/unbounded parsing.
MAX_SCREENPLAY_LINES = 200_000
MAX_SCREENPLAY_CHARS = 8_000_000
MAX_SCENES = 20_000
MAX_BLOCKS_PER_SCENE = 5_000
MAX_ID_LEN = 120


class BlockKind(str, Enum):
    """The closed set of Fountain block kinds."""

    SCENE_HEADING = "scene_heading"
    ACTION = "action"
    CHARACTER = "character"
    PARENTHETICAL = "parenthetical"
    DIALOGUE = "dialogue"
    TRANSITION = "transition"
    SECTION = "section"
    SYNOPSIS = "synopsis"
    NOTE = "note"
    PAGE_BREAK = "page_break"
    TITLE = "title"


class ShotSize(str, Enum):
    """Canonical shot sizes, ordered wide -> tight."""

    ESTABLISHING = "establishing"
    MASTER = "master"
    FULL = "full"
    MEDIUM = "medium"
    OTS = "over_the_shoulder"
    CLOSE_UP = "close_up"
    EXTREME_CLOSE_UP = "extreme_close_up"
    INSERT = "insert"


class HallBand(str, Enum):
    """Edward T. Hall interpersonal distance bands (metres)."""

    INTIMATE = "intimate"  # < 0.45 m
    PERSONAL = "personal"  # 0.45 .. 1.20 m
    SOCIAL = "social"  # 1.20 .. 3.60 m
    PUBLIC = "public"  # > 3.60 m


@dataclass(frozen=True)
class SourceLine:
    """Line-number provenance for one parsed element."""

    line: int
    text: str


@dataclass(frozen=True)
class DialogueBlock:
    character: str
    parenthetical: str
    dialogue: str
    line: int
    #: Syllable-count timing estimate in whole seconds (documented heuristic).
    estimated_seconds: Fraction = Fraction(0)


@dataclass(frozen=True)
class SceneHeading:
    interior: bool
    location: str
    time_of_day: str
    line: int

    @property
    def slugline(self) -> str:
        kind = "INT" if self.interior else "EXT"
        return f"{kind}. {self.location} - {self.time_of_day}"


@dataclass(frozen=True)
class ParsedBlock:
    kind: BlockKind
    text: str
    line: int


@dataclass(frozen=True)
class ParsedScene:
    heading: SceneHeading
    blocks: tuple[ParsedBlock, ...]
    dialogue: tuple[DialogueBlock, ...]
    index: int


@dataclass(frozen=True)
class ParsedScreenplay:
    title: str
    scenes: tuple[ParsedScene, ...]
    sections: tuple[str, ...]
    synopses: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ActorPlacement:
    """A 2D stage position and facing for one actor in a scene."""

    actor_id: str
    x: float
    y: float
    facing_deg: float = 0.0  # 0 = +x screen-right


@dataclass(frozen=True)
class CameraSetup:
    """One deterministic camera setup in a shot sequence."""

    setup_id: str
    shot_size: ShotSize
    subject_id: str
    cam_x: float
    cam_y: float
    focal_mm: int
    axis_side: int  # -1 or +1: which half-plane the camera sits in
    is_axis_reset: bool = False


@dataclass(frozen=True)
class ShotSequence:
    scene_index: int
    setups: tuple[CameraSetup, ...]
    axis_side: int
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EyelineResult:
    a_looks_right: bool
    b_looks_left: bool
    matched: bool


def hall_band(distance_m: float) -> HallBand:
    """Classify an interpersonal distance into a Hall band."""
    if distance_m < 0:
        raise ValueError("distance must be non-negative")
    if distance_m < 0.45:
        return HallBand.INTIMATE
    if distance_m < 1.20:
        return HallBand.PERSONAL
    if distance_m <= 3.60:
        return HallBand.SOCIAL
    return HallBand.PUBLIC


__all__ = [
    "BlockKind",
    "ShotSize",
    "HallBand",
    "SourceLine",
    "DialogueBlock",
    "SceneHeading",
    "ParsedBlock",
    "ParsedScene",
    "ParsedScreenplay",
    "ActorPlacement",
    "CameraSetup",
    "ShotSequence",
    "EyelineResult",
    "hall_band",
    "MAX_SCREENPLAY_LINES",
    "MAX_SCREENPLAY_CHARS",
    "MAX_SCENES",
    "MAX_BLOCKS_PER_SCENE",
    "MAX_ID_LEN",
]
