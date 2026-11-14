"""WAVE 28 — tests for the multi-episode showrunner bible."""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.atlas import showrunner_bible as s


def _bible() -> s.ShowrunnerBible:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("a", "Alice"))
    b.add_character(s.Character("b", "Bob"))
    b.add_prop(s.Prop("sword", "Sword"))
    b.add_episode(s.Episode(1, "Pilot", transfers=(("sword", "a"),), facts=(("secret", "a"),)))
    b.add_episode(
        s.Episode(2, "Fall", deaths=("a",), transfers=(("sword", "b"),), facts=(("secret", "b"),))
    )
    return b


def test_is_alive_three_valued() -> None:
    b = _bible()
    assert b.is_alive("a", 1) is s.Truth.TRUE
    assert b.is_alive("a", 2) is s.Truth.FALSE
    assert b.is_alive("b", 2) is s.Truth.TRUE


def test_character_before_first_episode_is_false() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("late", "Late", first_episode=3))
    b.add_episode(s.Episode(3, "E3"))
    assert b.is_alive("late", 3) is s.Truth.TRUE


def test_knows_three_valued() -> None:
    b = _bible()
    assert b.knows("a", "secret", 1) is s.Truth.TRUE
    assert b.knows("b", "secret", 1) is s.Truth.FALSE  # declared elsewhere but not by b yet
    assert b.knows("b", "secret", 2) is s.Truth.TRUE
    assert b.knows("a", "never_declared", 1) is s.Truth.UNKNOWN


def test_owns_three_valued() -> None:
    b = _bible()
    assert b.owns("sword", "a", 1) is s.Truth.TRUE
    assert b.owns("sword", "b", 1) is s.Truth.FALSE
    assert b.owns("sword", "b", 2) is s.Truth.TRUE


def test_owns_unknown_before_first_declaration() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("a", "Alice"))
    b.add_prop(s.Prop("p", "Prop"))
    b.add_episode(s.Episode(1, "E1"))
    b.add_episode(s.Episode(2, "E2", transfers=(("p", "a"),)))
    assert b.owns("p", "a", 1) is s.Truth.UNKNOWN
    assert b.owns("p", "a", 2) is s.Truth.TRUE


def test_episode_order_is_normalised() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("a", "A"))
    b.add_episode(s.Episode(2, "Second"))
    b.add_episode(s.Episode(1, "First"))
    assert [e.number for e in b.episodes] == [1, 2]


def test_continuity_detects_dead_receives_prop() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("x", "X"))
    b.add_character(s.Character("y", "Y"))
    b.add_prop(s.Prop("g", "Gun"))
    b.add_episode(s.Episode(1, "D", deaths=("x",)))
    b.add_episode(s.Episode(2, "T", transfers=(("g", "x"),)))
    kinds = [f.kind for f in b.check_continuity()]
    assert "dead_receives_prop" in kinds


def test_continuity_detects_dead_declares_fact() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("x", "X"))
    b.add_episode(s.Episode(1, "D", deaths=("x",)))
    b.add_episode(s.Episode(2, "T", facts=(("f", "x"),)))
    kinds = [f.kind for f in b.check_continuity()]
    assert "dead_declares_fact" in kinds


def test_continuity_detects_double_death() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("x", "X"))
    b.add_episode(s.Episode(1, "D", deaths=("x",)))
    b.add_episode(s.Episode(2, "D2", deaths=("x",)))
    kinds = [f.kind for f in b.check_continuity()]
    assert "double_death" in kinds


def test_continuity_detects_duplicate_fact() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("x", "X"))
    b.add_episode(s.Episode(1, "E1", facts=(("f", "x"), ("f", "x"))))
    kinds = [f.kind for f in b.check_continuity()]
    assert "duplicate_fact" in kinds


def test_continuity_detects_redundant_transfer() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("x", "X"))
    b.add_prop(s.Prop("p", "P"))
    b.add_episode(s.Episode(1, "E1", transfers=(("p", "x"),)))
    b.add_episode(s.Episode(2, "E2", transfers=(("p", "x"),)))
    kinds = [f.kind for f in b.check_continuity()]
    assert "redundant_transfer" in kinds


def test_clean_bible_has_no_findings() -> None:
    assert _bible().check_continuity() == []


def test_findings_are_deterministic() -> None:
    b = _bible()
    assert b.check_continuity() == b.check_continuity()


# --- adversarial / refusal --------------------------------------------------------


def test_refuse_duplicate_character() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("a", "A"))
    with pytest.raises(s.BibleError):
        b.add_character(s.Character("a", "A2"))


def test_refuse_duplicate_prop() -> None:
    b = s.ShowrunnerBible()
    b.add_prop(s.Prop("p", "P"))
    with pytest.raises(s.BibleError):
        b.add_prop(s.Prop("p", "P2"))


def test_refuse_duplicate_episode() -> None:
    b = s.ShowrunnerBible()
    b.add_episode(s.Episode(1, "E1"))
    with pytest.raises(s.BibleError):
        b.add_episode(s.Episode(1, "E1b"))


def test_refuse_death_unknown_character() -> None:
    b = s.ShowrunnerBible()
    with pytest.raises(s.BibleError):
        b.add_episode(s.Episode(1, "E1", deaths=("ghost",)))


def test_refuse_transfer_unknown_prop() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("a", "A"))
    with pytest.raises(s.BibleError):
        b.add_episode(s.Episode(1, "E1", transfers=(("nope", "a"),)))


def test_refuse_transfer_unknown_character() -> None:
    b = s.ShowrunnerBible()
    b.add_prop(s.Prop("p", "P"))
    with pytest.raises(s.BibleError):
        b.add_episode(s.Episode(1, "E1", transfers=(("p", "ghost"),)))


def test_refuse_fact_unknown_character() -> None:
    b = s.ShowrunnerBible()
    with pytest.raises(s.BibleError):
        b.add_episode(s.Episode(1, "E1", facts=(("f", "ghost"),)))


def test_refuse_empty_character_ids() -> None:
    with pytest.raises(s.BibleError):
        s.Character("", "Name")
    with pytest.raises(s.BibleError):
        s.Character("id", "")


def test_refuse_bad_first_episode() -> None:
    with pytest.raises(s.BibleError):
        s.Character("id", "Name", first_episode=0)


def test_refuse_empty_prop_ids() -> None:
    with pytest.raises(s.BibleError):
        s.Prop("", "Name")


def test_refuse_bad_episode_number() -> None:
    with pytest.raises(s.BibleError):
        s.Episode(0, "E")


def test_refuse_empty_episode_title() -> None:
    with pytest.raises(s.BibleError):
        s.Episode(1, "")


def test_refuse_query_unknown_character() -> None:
    b = _bible()
    with pytest.raises(s.BibleError):
        b.is_alive("ghost", 1)
    with pytest.raises(s.BibleError):
        b.knows("ghost", "secret", 1)
    with pytest.raises(s.BibleError):
        b.owns("sword", "ghost", 1)


def test_refuse_query_unknown_episode() -> None:
    b = _bible()
    with pytest.raises(s.BibleError):
        b.is_alive("a", 99)


def test_refuse_query_unknown_prop() -> None:
    b = _bible()
    with pytest.raises(s.BibleError):
        b.owns("excalibur", "a", 1)


def test_refuse_too_many_episodes() -> None:
    b = s.ShowrunnerBible()
    b.episodes = [s.Episode(i + 1, f"E{i + 1}") for i in range(s.MAX_EPISODES)]
    with pytest.raises(s.BibleError):
        b.add_episode(s.Episode(s.MAX_EPISODES + 1, "overflow"))


def test_refuse_too_many_characters() -> None:
    b = s.ShowrunnerBible()
    b.characters = {f"c{i}": s.Character(f"c{i}", f"C{i}") for i in range(s.MAX_ENTITIES)}
    with pytest.raises(s.BibleError):
        b.add_character(s.Character("overflow", "Overflow"))


def test_refuse_too_many_props() -> None:
    b = s.ShowrunnerBible()
    b.props = {f"p{i}": s.Prop(f"p{i}", f"P{i}") for i in range(s.MAX_ENTITIES)}
    with pytest.raises(s.BibleError):
        b.add_prop(s.Prop("overflow", "Overflow"))


def test_character_not_yet_introduced_is_false() -> None:
    b = s.ShowrunnerBible()
    b.add_character(s.Character("late", "Late", first_episode=3))
    b.add_episode(s.Episode(1, "E1"))
    b.add_episode(s.Episode(3, "E3"))
    assert b.is_alive("late", 1) is s.Truth.FALSE


def test_refuse_unknown_episode_in_knows() -> None:
    b = _bible()
    with pytest.raises(s.BibleError):
        b.knows("a", "secret", 99)


def test_refuse_unknown_episode_in_owns() -> None:
    b = _bible()
    with pytest.raises(s.BibleError):
        b.owns("sword", "a", 99)


def test_died_earlier_walks_multiple_episodes() -> None:
    # A death in a later episode: earlier episodes have no death (the loop body
    # runs and does not return), then the death episode is reached.
    b = s.ShowrunnerBible()
    b.add_character(s.Character("x", "X"))
    b.add_episode(s.Episode(1, "E1"))
    b.add_episode(s.Episode(2, "E2", deaths=("x",)))
    b.add_episode(s.Episode(3, "E3"))
    assert b._died_earlier("x", 3) is True
    assert b._died_earlier("x", 2) is False


def test_died_earlier_loop_exhaustion() -> None:
    # A character who never dies: the loop runs over every episode and falls
    # through to the final return without a break.
    b = s.ShowrunnerBible()
    b.add_character(s.Character("x", "X"))
    b.add_episode(s.Episode(1, "E1"))
    b.add_episode(s.Episode(2, "E2"))
    assert b._died_earlier("x", 5) is False
