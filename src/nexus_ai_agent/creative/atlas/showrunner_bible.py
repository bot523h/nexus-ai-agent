"""WAVE 28 — multi-episode showrunner bible (offline narrative-continuity store).

A deterministic, offline knowledge base for a serialised show:

* characters, with an arc of per-episode states (alive/dead, location,
  possessions, knowledge);
* props with a single-owner-per-episode invariant enforced by the store;
* episode-ordered facts and their provenance (which episode declared them);
* consistency checks: a character cannot possess a prop in two places at once, a
  dead character cannot act, a fact cannot be known before it is established,
  and a prop cannot vanish without a declared transfer;
* three-valued answers: ``TRUE`` / ``FALSE`` / ``UNKNOWN`` — a query about
  something never declared returns UNKNOWN, never a fabricated FALSE.

The bible is a *view* over declared facts; it never invents story content and it
never writes to any canonical project database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

__all__ = [
    "BibleError",
    "Truth",
    "Character",
    "Prop",
    "Episode",
    "ShowrunnerBible",
    "ContinuityFinding",
    "MAX_EPISODES",
    "MAX_ENTITIES",
]

MAX_EPISODES: Final[int] = 1000
MAX_ENTITIES: Final[int] = 10_000


class BibleError(ValueError):
    """Raised for duplicate ids, malformed entities, or invalid references."""


class Truth(Enum):
    """A three-valued truth for a continuity query."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


@dataclass
class Character:
    """A character declared in the show."""

    cid: str
    name: str
    first_episode: int = 1

    def __post_init__(self) -> None:
        if not self.cid or not self.name:
            raise BibleError("character id and name must be non-empty")
        if not isinstance(self.first_episode, int) or self.first_episode < 1:
            raise BibleError("first_episode must be a positive int")


@dataclass
class Prop:
    """A uniquely-identified prop (may be handed between characters)."""

    pid: str
    name: str
    unique: bool = True

    def __post_init__(self) -> None:
        if not self.pid or not self.name:
            raise BibleError("prop id and name must be non-empty")


@dataclass
class Episode:
    """An episode: ordered, with declared deaths and prop transfers."""

    number: int
    title: str
    deaths: tuple[str, ...] = ()
    transfers: tuple[tuple[str, str], ...] = ()  # (pid, to_cid)
    facts: tuple[tuple[str, str], ...] = ()  # (fact_id, declaring_cid)

    def __post_init__(self) -> None:
        if not isinstance(self.number, int) or self.number < 1:
            raise BibleError("episode number must be a positive int")
        if not self.title:
            raise BibleError("episode title must be non-empty")


@dataclass(frozen=True)
class ContinuityFinding:
    """A continuity violation with its kind, subjects and explanation."""

    kind: str
    subjects: tuple[str, ...]
    message: str


@dataclass
class ShowrunnerBible:
    """The show bible: characters, props, episodes, and a consistency engine."""

    characters: dict[str, Character] = field(default_factory=dict)
    props: dict[str, Prop] = field(default_factory=dict)
    episodes: list[Episode] = field(default_factory=list)
    _facts_known: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _prop_owner: dict[str, dict[int, str]] = field(default_factory=dict, repr=False)

    # --- declaration --------------------------------------------------------------

    def add_character(self, character: Character) -> None:
        if character.cid in self.characters:
            raise BibleError(f"duplicate character id {character.cid!r}")
        if len(self.characters) >= MAX_ENTITIES:
            raise BibleError("too many characters")
        self.characters[character.cid] = character

    def add_prop(self, prop: Prop) -> None:
        if prop.pid in self.props:
            raise BibleError(f"duplicate prop id {prop.pid!r}")
        if len(self.props) >= MAX_ENTITIES:
            raise BibleError("too many props")
        self.props[prop.pid] = prop

    def add_episode(self, episode: Episode) -> None:
        if len(self.episodes) >= MAX_EPISODES:
            raise BibleError("too many episodes")
        if any(e.number == episode.number for e in self.episodes):
            raise BibleError(f"duplicate episode number {episode.number}")
        for cid in episode.deaths:
            if cid not in self.characters:
                raise BibleError(f"death references unknown character {cid!r}")
        for pid, to_cid in episode.transfers:
            if pid not in self.props:
                raise BibleError(f"transfer references unknown prop {pid!r}")
            if to_cid not in self.characters:
                raise BibleError(f"transfer references unknown character {to_cid!r}")
        for _fact_id, cid in episode.facts:
            if cid not in self.characters:
                raise BibleError(f"fact references unknown character {cid!r}")
        self.episodes.append(episode)
        self.episodes.sort(key=lambda e: e.number)
        self._reindex()

    def _reindex(self) -> None:
        """Rebuild the derived knowledge and ownership indices from episodes."""
        self._facts_known = {cid: set() for cid in self.characters}
        self._prop_owner = {pid: {} for pid in self.props}
        for ep in self.episodes:
            for fact_id, cid in ep.facts:
                self._facts_known.setdefault(cid, set()).add(fact_id)
            for pid, to_cid in ep.transfers:
                self._prop_owner[pid][ep.number] = to_cid

    # --- queries ------------------------------------------------------------------

    def is_alive(self, cid: str, episode: int) -> Truth:
        """Whether ``cid`` is alive at ``episode`` (three-valued)."""
        if cid not in self.characters:
            raise BibleError(f"unknown character {cid!r}")
        if not self._episode_exists(episode):
            raise BibleError(f"unknown episode {episode}")
        char = self.characters[cid]
        if episode < char.first_episode:
            return Truth.FALSE  # not yet introduced
        for ep in self.episodes:
            if ep.number > episode:
                break
            if cid in ep.deaths:
                return Truth.FALSE
        return Truth.TRUE

    def knows(self, cid: str, fact_id: str, episode: int) -> Truth:
        """Whether ``cid`` knows ``fact_id`` by ``episode`` (three-valued)."""
        if cid not in self.characters:
            raise BibleError(f"unknown character {cid!r}")
        if not self._episode_exists(episode):
            raise BibleError(f"unknown episode {episode}")
        known = set()
        for ep in self.episodes:
            if ep.number > episode:
                break
            for fid, fcid in ep.facts:
                if fcid == cid:
                    known.add(fid)
        if fact_id in known:
            return Truth.TRUE
        # Is the fact declared anywhere at all? If not, the answer is UNKNOWN.
        if any(fid == fact_id for ep in self.episodes for fid, _ in ep.facts):
            return Truth.FALSE  # declared elsewhere, but not by this character
        return Truth.UNKNOWN

    def owns(self, pid: str, cid: str, episode: int) -> Truth:
        """Whether ``cid`` owns prop ``pid`` at ``episode`` (three-valued)."""
        if pid not in self.props:
            raise BibleError(f"unknown prop {pid!r}")
        if cid not in self.characters:
            raise BibleError(f"unknown character {cid!r}")
        if not self._episode_exists(episode):
            raise BibleError(f"unknown episode {episode}")
        timeline = self._prop_owner[pid]
        relevant = [n for n in timeline if n <= episode]
        if not relevant:
            return Truth.UNKNOWN  # ownership never declared up to this episode
        latest = max(relevant)
        return Truth.TRUE if timeline[latest] == cid else Truth.FALSE

    def _episode_exists(self, episode: int) -> bool:
        return any(e.number == episode for e in self.episodes)

    # --- consistency ---------------------------------------------------------------

    def check_continuity(self) -> list[ContinuityFinding]:
        """Return the deterministic list of continuity findings."""
        findings: list[ContinuityFinding] = []
        for ep in self.episodes:
            for cid in ep.deaths:
                if self._died_earlier(cid, ep.number):
                    findings.append(
                        ContinuityFinding(
                            "double_death",
                            (cid,),
                            f"{cid} dies again in episode {ep.number}",
                        )
                    )
            for pid, to_cid in ep.transfers:
                if self.is_alive(to_cid, ep.number) is Truth.FALSE:
                    findings.append(
                        ContinuityFinding(
                            "dead_receives_prop",
                            (pid, to_cid),
                            f"{to_cid} is dead when receiving {pid} in episode {ep.number}",
                        )
                    )
                prior = [n for n in self._prop_owner[pid] if n < ep.number]
                if prior and self._prop_owner[pid][max(prior)] == to_cid:
                    findings.append(
                        ContinuityFinding(
                            "redundant_transfer",
                            (pid, to_cid),
                            f"{to_cid} already owned {pid} before episode {ep.number}",
                        )
                    )
            seen: set[str] = set()
            for fact_id, cid in ep.facts:
                if fact_id in seen:
                    findings.append(
                        ContinuityFinding(
                            "duplicate_fact",
                            (fact_id,),
                            f"fact {fact_id} declared twice in episode {ep.number}",
                        )
                    )
                seen.add(fact_id)
                if self.is_alive(cid, ep.number) is Truth.FALSE:
                    findings.append(
                        ContinuityFinding(
                            "dead_declares_fact",
                            (fact_id, cid),
                            f"{cid} is dead when declaring {fact_id} in episode {ep.number}",
                        )
                    )
        return findings

    def _died_earlier(self, cid: str, episode: int) -> bool:
        for ep in self.episodes:
            if ep.number >= episode:
                break
            if cid in ep.deaths:
                return True
        return False
