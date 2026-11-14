"""Deterministic recursive-descent Fountain screenplay parser.

This parses the *industry-standard Fountain* plain-text markup into a typed AST
with line-number provenance. It understands:

* title pages (``Title:`` / ``Author:`` … key-value lines);
* scene headings (``INT.``/``EXT.``/``INT./EXT.`` + location + time-of-day);
* action blocks;
* character cues (uppercase, optionally ``(V.O.)``/``(CONT'D)``-style extensions);
* parentheticals and dialogue blocks, with a syllable-based timing estimate;
* transitions (``CUT TO:`` … ``FADE OUT.``);
* ``#`` sections, ``=`` synopses, ``[[notes]]``, and forced ``>`` transitions.

It is deliberately bounded (see ``contracts``) and refuses malformed input with
typed errors. It never guesses: a line that fits no rule is reported as
``ScreenplayParseError`` with its line number rather than silently dropped.
"""

from __future__ import annotations

import re
from fractions import Fraction

from .contracts import (
    MAX_BLOCKS_PER_SCENE,
    MAX_SCENES,
    MAX_SCREENPLAY_CHARS,
    MAX_SCREENPLAY_LINES,
    BlockKind,
    DialogueBlock,
    ParsedBlock,
    ParsedScene,
    ParsedScreenplay,
    SceneHeading,
)
from .errors import InputBoundError, ScreenplayParseError

__all__ = ["parse_fountain", "estimate_syllables", "ESTIMATED_SECONDS_PER_SYLLABLE"]

#: A conservative, documented delivery rate for the timing estimate. It is an
#: *estimate* for planning only — never a claim about an actual performance.
ESTIMATED_SECONDS_PER_SYLLABLE = 0.25

_SCENE_RE = re.compile(
    r"^(?P<kind>INT\./EXT\.|INT/EXT|INT\.|EXT\.|I/E\.)\s*"
    r"(?P<loc>[^\-]+?)"
    r"(?:\s*-\s*(?P<tod>[A-Za-z0-9 ./]+?))?\s*$"
)
_CHARACTER_RE = re.compile(r"^(?P<name>[A-Z][A-Z0-9 .'\-]*?)(?:\s*\((?P<ext>[^)]*)\))?\s*$")
_PARENTHETICAL_RE = re.compile(r"^\((?P<inner>.*)\)$")
_TRANSITION_RE = re.compile(
    r"^(?:>\s*)?[A-Z][A-Z ]*TO:|^FADE (?:IN|OUT)[:.]?$|^CUT TO:$|^SMASH CUT TO:$"
)
_SECTION_RE = re.compile(r"^(?P<hashes>#{1,6})\s*(?P<title>.+?)\s*$")
_SYNOPSIS_RE = re.compile(r"^=\s*(?P<body>.+?)\s*$")
_NOTE_RE = re.compile(r"\[\[(?P<body>.*?)\]\]")
_TITLE_KV_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z ]*?):\s*(?P<value>.*)$")

_TITLE_KEYS = {"title", "author", "authors", "credit", "source", "draft date", "contact", "notes"}


def estimate_syllables(text: str) -> int:
    """A deterministic, documented syllable estimate for one dialogue line.

    This is a *heuristic* used only to size a timing budget; it is not a claim
    about the true spoken syllable count. It groups adjacent vowels and counts
    each group, subtracting a trailing silent ``e``.
    """
    words = re.findall(r"[A-Za-z]+", text)
    total = 0
    for word in words:
        w = word.lower()
        groups = re.findall(r"[aeiouy]+", w)
        count = len(groups)
        if w.endswith("e") and count > 1:
            count -= 1
        total += max(1, count)
    return total


def _estimate_seconds(text: str) -> int:
    syllables = estimate_syllables(text)
    return int(round(syllables * ESTIMATED_SECONDS_PER_SYLLABLE))


def _check_bounds(lines: list[str], text: str) -> None:
    if len(text) > MAX_SCREENPLAY_CHARS:
        raise InputBoundError(f"screenplay is {len(text)} chars > {MAX_SCREENPLAY_CHARS}")
    if len(lines) > MAX_SCREENPLAY_LINES:
        raise InputBoundError(f"screenplay has {len(lines)} lines > {MAX_SCREENPLAY_LINES}")


def _parse_title_page(lines: list[str]) -> tuple[str, int]:
    """Return ``(title, index_of_first_body_line)``.

    A title page is recognised only when the *first* non-blank line is a
    ``Title:`` key-value line; otherwise the body starts at line 0.
    """
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return "", idx
    first = lines[idx].strip()
    match = _TITLE_KV_RE.match(first)
    if not match or match.group("key").strip().lower() not in _TITLE_KEYS:
        return "", 0

    title = ""
    while idx < len(lines):
        raw = lines[idx].strip()
        if not raw:
            idx += 1
            # a blank line after the title block ends the page
            if title and idx < len(lines) and not _TITLE_KV_RE.match(lines[idx].strip()):
                break
            continue
        kv = _TITLE_KV_RE.match(raw)
        if not kv:
            break
        key = kv.group("key").strip().lower()
        value = kv.group("value").strip()
        if key == "title" and not title:
            title = value
        idx += 1
    return title, idx


def parse_fountain(text: str) -> ParsedScreenplay:
    """Parse Fountain markup into a typed :class:`ParsedScreenplay`."""
    if not isinstance(text, str):
        raise ScreenplayParseError("screenplay must be a string")
    lines = text.splitlines()
    _check_bounds(lines, text)

    title, start = _parse_title_page(lines)

    scenes: list[ParsedScene] = []
    sections: list[str] = []
    synopses: list[str] = []
    notes: list[str] = []

    cur_heading: SceneHeading | None = None
    cur_blocks: list[ParsedBlock] = []
    cur_dialogue: list[DialogueBlock] = []

    def flush_scene() -> None:
        nonlocal cur_heading, cur_blocks, cur_dialogue
        if cur_heading is None:
            return
        if len(cur_blocks) > MAX_BLOCKS_PER_SCENE:
            raise InputBoundError(
                f"scene at line {cur_heading.line} has {len(cur_blocks)} blocks "
                f"> {MAX_BLOCKS_PER_SCENE}"
            )
        scenes.append(
            ParsedScene(
                heading=cur_heading,
                blocks=tuple(cur_blocks),
                dialogue=tuple(cur_dialogue),
                index=len(scenes),
            )
        )
        if len(scenes) > MAX_SCENES:
            raise InputBoundError(f"screenplay has more than {MAX_SCENES} scenes")
        cur_heading = None
        cur_blocks = []
        cur_dialogue = []

    i = start
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()
        lineno = i + 1

        # notes may appear anywhere
        for note_match in _NOTE_RE.finditer(line):
            notes.append(note_match.group("body"))

        if not stripped:
            i += 1
            continue

        # section
        sec = _SECTION_RE.match(stripped)
        if sec:
            sections.append(sec.group("title"))
            cur_blocks.append(ParsedBlock(BlockKind.SECTION, sec.group("title"), lineno))
            i += 1
            continue

        # page break (must precede the synopsis rule, which also starts with '=')
        if stripped == "===":
            cur_blocks.append(ParsedBlock(BlockKind.PAGE_BREAK, "===", lineno))
            i += 1
            continue

        # synopsis
        syn = _SYNOPSIS_RE.match(stripped)
        if syn:
            synopses.append(syn.group("body"))
            cur_blocks.append(ParsedBlock(BlockKind.SYNOPSIS, syn.group("body"), lineno))
            i += 1
            continue

        # scene heading
        head = _SCENE_RE.match(stripped)
        if head:
            flush_scene()
            interior = head.group("kind") in {"INT.", "INT/EXT", "I/E."}
            loc = head.group("loc").strip()
            tod = (head.group("tod") or "UNKNOWN").strip().upper()
            cur_heading = SceneHeading(
                interior=interior, location=loc, time_of_day=tod, line=lineno
            )
            i += 1
            continue

        # transition (forced with leading '>' or a canonical form)
        if _TRANSITION_RE.match(stripped):
            cur_blocks.append(
                ParsedBlock(BlockKind.TRANSITION, stripped.lstrip(">").strip(), lineno)
            )
            i += 1
            continue

        # character cue followed by dialogue: uppercase line, then dialogue
        char = _CHARACTER_RE.match(stripped)
        if char and stripped == stripped.upper() and any(c.isalpha() for c in stripped):
            name = char.group("name").strip()
            extension = (char.group("ext") or "").strip()
            dialogue_lines: list[str] = []
            parenthetical = extension
            j = i + 1
            block_lineno = lineno
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt:
                    break
                paren = _PARENTHETICAL_RE.match(nxt)
                if paren:
                    parenthetical = paren.group("inner").strip()
                    j += 1
                    continue
                if _CHARACTER_RE.match(nxt) and nxt == nxt.upper():
                    break
                dialogue_lines.append(nxt)
                j += 1
            dialogue_text = " ".join(dialogue_lines).strip()
            cur_blocks.append(ParsedBlock(BlockKind.CHARACTER, name, block_lineno))
            if dialogue_text or parenthetical:
                cur_dialogue.append(
                    DialogueBlock(
                        character=name,
                        parenthetical=parenthetical,
                        dialogue=dialogue_text,
                        line=block_lineno,
                        estimated_seconds=Fraction(_estimate_seconds(dialogue_text)),
                    )
                )
            i = j
            continue

        # otherwise: action
        cur_blocks.append(ParsedBlock(BlockKind.ACTION, stripped, lineno))
        i += 1

    flush_scene()
    return ParsedScreenplay(
        title=title,
        scenes=tuple(scenes),
        sections=tuple(sections),
        synopses=tuple(synopses),
        notes=tuple(notes),
    )
