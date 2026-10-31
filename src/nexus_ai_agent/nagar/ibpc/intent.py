"""Deterministic free-text → typed AST parser for the IBPC slice.

This parser is deliberately **not** an LLM and never will be the authority: it
maps a bounded, explicit request to the one canonical operation this slice can
honestly execute (``timeline.trim`` — registered, AVAILABLE, REVERSIBLE, with
a real runtime path), and it **asks instead of guessing**: any ambiguity
(missing range, more than two numbers, negations, an unsupported operation)
returns a typed :class:`Clarification` or :class:`Refusal`, never a fabricated
AST.

Raw intent text is treated as untrusted input: the capsule stores only its
``intent_digest`` (sha256 of the normalized text), never the text itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import TypedCreativeAST, VideoTimeScope

#: The only canonical operation this slice parses. Closed constant — the
#: parser can never emit an operation the registry does not know; compile-time
#: registry validation is a second, independent gate.
SUPPORTED_OPERATION: Literal["timeline.trim"] = "timeline.trim"

MAX_INTENT_CHARS = 500

_PERSIAN_DIGITS = {
    "۰": "0",
    "۱": "1",
    "۲": "2",
    "۳": "3",
    "۴": "4",
    "۵": "5",
    "۶": "6",
    "۷": "7",
    "۸": "8",
    "۹": "9",
    "٠": "0",
    "١": "1",
    "٢": "2",
    "٣": "3",
    "٤": "4",
    "٥": "5",
    "٦": "6",
    "٧": "7",
    "٨": "8",
    "٩": "9",
    "٫": ".",
}

_TRIM_MARKERS = (
    "trim",
    "cut",
    "remove",
    "delete",
    "shorten",
    "ببر",
    "برش",
    "حذف",
    "کوتاه",
    "بزن",
)
_NEGATION_MARKERS = ("don't", "do not", "without", "never", "بدون", "حذف نکن", "نکن")
_UNSUPPORTED_HINTS = (
    "brighten",
    "brighter",
    "exposure",
    "grade",
    "color",
    "colour",
    "louder",
    "caption",
    "subtitle",
    "reverse",
    "speed",
    "proxy",
    "music",
    "voice",
    "روشن",
    "نور",
    "رنگی",
    "صدا",
    "زیرنویس",
    "عقب",
    "سرعت",
)
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds|ثانیه)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class IntentAST:
    """A fully-specified typed AST plus the digest of the raw intent."""

    ast: TypedCreativeAST
    intent_digest: str


@dataclass(frozen=True)
class Clarification:
    """The honest answer when the scope/intent is ambiguous: questions, not guesses."""

    questions: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Refusal:
    """A typed refusal — nothing was parsed into an executable AST."""

    reason: str
    detail: str = ""


ParseResult = IntentAST | Clarification | Refusal


def normalize_intent(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("intent text must be a string")
    return " ".join(text.split())[:MAX_INTENT_CHARS]


def intent_digest_of(normalized_text: str) -> str:
    return digest_of({"intent_text": normalized_text})


def _to_ascii_digits(text: str) -> str:
    return "".join(_PERSIAN_DIGITS.get(ch, ch) for ch in text)


def _extract_seconds(text: str) -> list[float]:
    values: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        try:
            values.append(float(match.group(1)))
        except ValueError:
            continue
    return values


def parse_intent(
    text: str,
    *,
    project_id: str,
    asset_id: str,
    source_revision: int,
    duration_us: int,
) -> ParseResult:
    """Parse one bounded free-text intent into a typed AST or a typed question.

    Never raises for caller input; never guesses a scope.
    """
    normalized = normalize_intent(text)
    if not normalized:
        return Refusal("empty_intent", "empty intent text")
    digest = intent_digest_of(normalized)
    lowered = _to_ascii_digits(normalized).lower()

    if any(marker in lowered for marker in _NEGATION_MARKERS):
        return Clarification(
            questions=(
                "Which exact range may change? (negations cannot be compiled automatically)",
                "Please state the range explicitly, e.g. 'trim from 1 to 2 seconds'.",
            ),
            reason="negated_constraint",
        )

    has_trim_marker = any(marker in lowered for marker in _TRIM_MARKERS)
    unsupported = next((h for h in _UNSUPPORTED_HINTS if h in lowered), None)
    if unsupported and not has_trim_marker:
        return Refusal(
            "unsupported_operation_in_slice",
            f"this slice executes only {SUPPORTED_OPERATION}; {unsupported!r} is not wired here",
        )
    if not has_trim_marker:
        return Clarification(
            questions=(
                "What change do you want? This slice supports exactly one: "
                "'trim from <start> to <end> seconds' (remove that range, keep the rest).",
            ),
            reason="no_recognized_operation",
        )

    numbers = _extract_seconds(lowered)
    if len(numbers) == 0:
        return Clarification(
            questions=(
                "What is the start of the range to remove (in seconds)?",
                "What is the end of the range to remove (in seconds)?",
            ),
            reason="missing_range",
        )
    if len(numbers) > 2:
        return Clarification(
            questions=(
                f"I found {len(numbers)} numbers ({numbers}); which two are the start and end?",
            ),
            reason="ambiguous_range",
        )
    if len(numbers) == 1:
        return Clarification(
            questions=("You gave one number; a trim needs both a start and an end.",),
            reason="single_point",
        )

    lo, hi = sorted(numbers)
    in_us = int(round(lo * 1_000_000))
    out_us = int(round(hi * 1_000_000))
    if in_us >= out_us:
        return Refusal("invalid_range", f"empty trim range: {lo}..{hi} seconds")
    if duration_us > 0 and out_us > duration_us:
        return Refusal(
            "out_of_range",
            f"range {lo}..{hi}s exceeds the source duration {duration_us / 1_000_000:.3f}s",
        )
    if duration_us <= 0:
        return Refusal("unknown_duration", "source duration is not measured yet")

    # canonical timeline.trim semantics: in/out points are a KEEP window —
    # the kept range is preserved, the complement is removed
    scope = VideoTimeScope(asset_id=asset_id, effect="keep", start_us=in_us, end_us=out_us)
    ast = TypedCreativeAST(
        project_id=project_id,
        asset_id=asset_id,
        source_revision=source_revision,
        operation=SUPPORTED_OPERATION,
        params={
            "clip_asset_id": asset_id,
            "in_point_us": in_us,
            "out_point_us": out_us,
        },
        scope=scope,
        forbidden_scopes=tuple(
            VideoTimeScope(
                asset_id=asset_id,
                effect="modify",
                start_us=r.start_us,
                end_us=r.end_us,
            )
            for r in scope.forbidden_ranges(duration_us)
        ),  # keep-scope: the forbidden (preserved) region IS the kept window
        ambiguities=(),
    )
    return IntentAST(ast=ast, intent_digest=digest)


__all__ = [
    "SUPPORTED_OPERATION",
    "Clarification",
    "IntentAST",
    "ParseResult",
    "Refusal",
    "intent_digest_of",
    "normalize_intent",
    "parse_intent",
]
