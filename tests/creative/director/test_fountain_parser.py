"""Tests for the Fountain AST parser."""

from __future__ import annotations

import director_fixtures as fx
import pytest

from nexus_ai_agent.creative.director import (
    BlockKind,
    InputBoundError,
    ScreenplayParseError,
    estimate_syllables,
    parse_fountain,
)
from nexus_ai_agent.creative.director import fountain_parser as fp


def test_parses_title_scenes_and_provenance() -> None:
    parsed = parse_fountain(fx.SIMPLE_SCRIPT)
    assert parsed.title == "The Key"
    assert len(parsed.scenes) == 2
    first = parsed.scenes[0]
    assert first.heading.interior is True
    assert first.heading.location == "LIGHTHOUSE"
    assert first.heading.time_of_day == "NIGHT"
    assert first.heading.slugline == "INT. LIGHTHOUSE - NIGHT"
    assert first.heading.line > 0
    # every block carries a real line number
    assert all(b.line > 0 for b in first.blocks)


def test_parses_dialogue_and_parenthetical() -> None:
    parsed = parse_fountain(fx.SIMPLE_SCRIPT)
    dlg = parsed.scenes[0].dialogue
    assert [d.character for d in dlg] == ["MARA"]
    assert "keep it safe" in dlg[0].dialogue
    assert dlg[0].estimated_seconds > 0


def test_parses_parenthetical_inline() -> None:
    script = "INT. ROOM - DAY\n\nALICE\n(whispering)\nHello there.\n"
    parsed = parse_fountain(script)
    dlg = parsed.scenes[0].dialogue[0]
    assert dlg.parenthetical == "whispering"
    assert dlg.dialogue == "Hello there."


def test_sections_synopses_notes_and_transitions() -> None:
    parsed = parse_fountain(fx.SIMPLE_SCRIPT)
    assert parsed.sections == ("ACT ONE",)
    assert parsed.synopses == ("Mara decides to hide the key",)
    assert parsed.notes == ("a note about the key",)
    kinds = {b.kind for s in parsed.scenes for b in s.blocks}
    assert BlockKind.TRANSITION in kinds


def test_forced_transition_and_page_break() -> None:
    script = "INT. ROOM - DAY\n\n===\n\n> SMASH CUT TO:\n"
    parsed = parse_fountain(script)
    kinds = [b.kind for b in parsed.scenes[0].blocks]
    assert BlockKind.PAGE_BREAK in kinds
    assert BlockKind.TRANSITION in kinds


def test_multi_scene_script() -> None:
    parsed = parse_fountain(fx.MULTI_SCENE_SCRIPT)
    assert len(parsed.scenes) == 3
    assert parsed.scenes[1].heading.interior is False
    assert parsed.scenes[1].heading.time_of_day == "NIGHT"


def test_unknown_time_of_day_defaults_unknown() -> None:
    parsed = parse_fountain("INT. ROOM\n\nAlice waits.\n")
    assert parsed.scenes[0].heading.time_of_day == "UNKNOWN"


def test_non_string_is_refused() -> None:
    with pytest.raises(ScreenplayParseError):
        parse_fountain(123)  # type: ignore[arg-type]


def test_line_bound_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(fp, "MAX_SCREENPLAY_LINES", 3)
    with pytest.raises(InputBoundError):
        parse_fountain("INT. ROOM - DAY\n" + "action\n" * 10)


def test_char_bound_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(fp, "MAX_SCREENPLAY_CHARS", 5)
    with pytest.raises(InputBoundError):
        parse_fountain("INT. ROOM - DAY\n")


def test_scene_bound_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(fp, "MAX_SCENES", 1)
    with pytest.raises(InputBoundError):
        parse_fountain(fx.MULTI_SCENE_SCRIPT)


def test_block_bound_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(fp, "MAX_BLOCKS_PER_SCENE", 2)
    with pytest.raises(InputBoundError):
        parse_fountain("INT. ROOM - DAY\n" + "line one\nline two\nline three\n")


def test_estimate_syllables_is_deterministic() -> None:
    assert estimate_syllables("hello world") == estimate_syllables("hello world")
    assert estimate_syllables("") == 0
    assert estimate_syllables("the") >= 1


def test_empty_screenplay_yields_no_scenes() -> None:
    parsed = parse_fountain("")
    assert parsed.scenes == ()


def test_dialogue_after_blank_line_ends_block() -> None:
    script = "INT. ROOM - DAY\n\nALICE\nOne line.\n\nAction here.\n"
    parsed = parse_fountain(script)
    assert parsed.scenes[0].dialogue[0].dialogue == "One line."
