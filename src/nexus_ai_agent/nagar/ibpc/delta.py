"""Deterministic relative-scope intent parser for the IBPC Delta Dialogue slice.

Every user utterance that references the *current* scope relatively —
"trim 1 more second from the end", "یک ثانیه از تهش ببر" — is parsed into one
or more :class:`DeltaOp` (anchor + signed microseconds) and immediately
*resolved* into a standard :class:`TypedCreativeAST` by applying every op to the
last-committed :class:`VideoTimeScope`.

This module is a pure function library. It imports from ``models``, ``canonical``
and ``intent`` (read-only helpers), and explicitly must NOT import from
``service``, ``session``, ``approval`` or ``footprint``.

Deterministic contract (no LLM, no external API, no new pip dependencies):

* The request is split into clauses on conjunctions (``and`` / ``,`` / ``و`` …).
  **Every** clause must resolve to exactly one unambiguous operation; a clause
  that carries an anchor but no number (or a number but no anchor/direction) is
  ambiguous and fails the *whole* request closed with a ``Clarification`` — no
  operation is ever silently dropped.
* A negation anywhere in the request (``don't`` / ``do not`` / ``نکن`` / ``نبر``
  / ``نمی`` …) is honoured: the request is refused, never executed.
* Two operations that move the *same* anchor in *opposite* directions are a
  contradiction and are refused (``contradictory_delta``).
* Persian word-numbers are normalised **context-safely**: the bare word ``نه``
  (which means both "no" and, rarely, "nine") is deliberately **not** mapped to
  ``9`` — see ``docs/architecture/adr`` — so a negated utterance can never be
  turned into a spurious positive delta.
* The asset identity must match the scope's asset; the source revision must be a
  non-negative integer. Both are validated *here*, so no raw ``ValidationError``
  ever crosses this public boundary.

Sign convention (the boundary moves by ``delta_us``; negative = earlier in time):

    | utterance                          | anchor    | sign |
    |------------------------------------|-----------|------|
    | trim N more seconds from the end   | OUT_POINT |  -   |
    | add N seconds back at the end      | OUT_POINT |  +   |
    | trim N more seconds from the start | IN_POINT  |  +   |
    | add N seconds back at the start    | IN_POINT  |  -   |
    | move <in/out> earlier by N         | IN/OUT    |  -   |
    | move <in/out> later by N           | IN/OUT    |  +   |
"""

from __future__ import annotations

import re
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexus_ai_agent.nagar.ibpc.models import TypedCreativeAST, VideoTimeScope

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DeltaAnchor(str, Enum):
    """Which end of the kept window the delta moves."""

    IN_POINT = "in_point"  # start of the kept window
    OUT_POINT = "out_point"  # end of the kept window


class DeltaOp(BaseModel):
    """One relative mutation on the in- or out-point of the last committed scope.

    ``delta_us`` is signed microseconds. Negative means "move earlier in time".
    Zero is allowed by the model but refused by :func:`parse_delta_intent`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor: DeltaAnchor
    delta_us: int  # signed microseconds


class DeltaIntentAST(BaseModel):
    """Typed result of :func:`parse_delta_intent`.

    ``resolved_ast`` is a standard :class:`TypedCreativeAST` ready to pass
    unchanged to :func:`~nexus_ai_agent.nagar.ibpc.compiler.compile_ast`.
    ``base_scope_digest`` records which scope this delta was applied against
    (for auditability — a stale approval cannot replay against a newer scope).

    ``delta_ops`` holds **every** resolved operation (a combined request such as
    "trim 1s from the start and add 2s to the end" yields two). ``delta_op`` is a
    read-only convenience returning the first operation for single-op callers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_scope_digest: str  # sha256 of the VideoTimeScope this delta resolved against
    delta_ops: tuple[DeltaOp, ...] = Field(min_length=1)
    resolved_ast: TypedCreativeAST
    intent_digest: str

    @property
    def delta_op(self) -> DeltaOp:
        """The first (primary) operation — convenience for single-op callers."""
        return self.delta_ops[0]


# ---------------------------------------------------------------------------
# Deterministic lexicons (no LLM)
# ---------------------------------------------------------------------------

_SEGMENT_SPLIT_RE = re.compile(
    r"(?:\band\b|\bthen\b|\bplus\b|,|،|;|،|(?<=\s)و(?=\s))",
    re.IGNORECASE,
)

# Start / end anchors — matched as whole words so "extend" never reads as "end".
_START_ANCHOR_WORDS = ("start", "beginning", "begin", "in point", "inpoint", "in-point")
_END_ANCHOR_WORDS = ("end", "tail", "out point", "outpoint", "out-point", "last")
_START_ANCHOR_FA = ("اول", "اولش", "ابتدا", "ابتداش", "شروع")
_END_ANCHOR_FA = ("ته", "تهش", "آخر", "آخرش", "انتها", "انتهاش", "پایان")

# Verbs that *shrink* the window at the referenced anchor.
_REDUCE_WORDS = ("trim", "cut", "remove", "delete", "shorten", "reduce")
_REDUCE_FA = ("ببر", "برش", "حذف", "کوتاه", "بزن")
# Verbs that *grow* the window at the referenced anchor.
_EXTEND_WORDS = ("add", "extend", "restore", "append")
_EXTEND_FA = ("اضافه", "برگردان", "بازگردان", "بیشتر", "بلندتر", "طولانی")

# Explicit move direction.
_EARLIER_WORDS = ("earlier", "sooner")
_EARLIER_FA = ("زودتر", "جلوتر", "قبلتر")
_LATER_WORDS = ("later",)
_LATER_FA = ("دیرتر", "بعدتر", "عقبتر")

# Negation markers. English and the standalone Persian "نه" are whole-word
# matched; Persian verbs are substring matched (they are agglutinative:
# "نبر"/"نکن"/"نمی…"). "نه" MUST be whole-word: as a substring it appears
# inside unrelated words, and as a *number* it must never become 9.
_NEGATION_WORDS = ("don't", "do not", "does not", "never", "without", "cannot", "no", "not")
_NEGATION_FA_WORDS = ("نه",)
_NEGATION_FA = ("نکن", "نبر", "نزن", "نخواه", "نمی", "بدون")

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

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

# NOTE: the bare word ``نه`` (Persian for "no"/"nine") is intentionally absent —
# mapping it to 9 turns negated utterances into spurious positive deltas.
_WORD_NUMBERS = {
    "یک": "1",
    "دو": "2",
    "سه": "3",
    "چهار": "4",
    "پنج": "5",
    "شش": "6",
    "هفت": "7",
    "هشت": "8",
    "ده": "10",
    "نیم": "0.5",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "half": "0.5",
}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _to_ascii_digits(text: str) -> str:
    """Convert Persian/Arabic-Indic digits to ASCII (no word-number mapping)."""
    return "".join(_PERSIAN_DIGITS.get(ch, ch) for ch in text)


def _normalize_word_numbers(segment: str) -> str:
    """Replace Persian/English word-numbers with digits, whole-word only.

    Context-safety: the ambiguous Persian word ``نه`` is never mapped.
    """
    out = segment
    for word, digit in _WORD_NUMBERS.items():
        out = re.sub(r"(?<!\w)" + re.escape(word) + r"(?!\w)", digit, out, flags=re.IGNORECASE)
    return out


def _has_any_word(haystack: str, words: tuple[str, ...]) -> bool:
    return any(
        re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", haystack, re.IGNORECASE) for w in words
    )


def _has_any_substring(haystack: str, fragments: tuple[str, ...]) -> bool:
    return any(f in haystack for f in fragments)


def _has_negation(text: str) -> bool:
    low = text.lower()
    if _has_any_word(low, _NEGATION_WORDS):
        return True
    if _has_any_word(low, _NEGATION_FA_WORDS):
        return True
    return _has_any_substring(low, _NEGATION_FA)


def _split_segments(text: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_SPLIT_RE.split(text) if s.strip()]


def _anchors_in(segment: str) -> frozenset[DeltaAnchor]:
    """Every end a clause names. Whole-word matching in both languages: a
    substring match would read the "ته" inside "گذشته" as the end anchor."""
    low = segment.lower()
    found: set[DeltaAnchor] = set()
    if _has_any_word(low, _START_ANCHOR_WORDS) or _has_any_word(low, _START_ANCHOR_FA):
        found.add(DeltaAnchor.IN_POINT)
    if _has_any_word(low, _END_ANCHOR_WORDS) or _has_any_word(low, _END_ANCHOR_FA):
        found.add(DeltaAnchor.OUT_POINT)
    return frozenset(found)


def _detect_move_sign(segment: str) -> int | None:
    low = segment.lower()
    if _has_any_word(low, _EARLIER_WORDS) or _has_any_substring(low, _EARLIER_FA):
        return -1
    if _has_any_word(low, _LATER_WORDS) or _has_any_substring(low, _LATER_FA):
        return +1
    return None


def _detect_reduce_extend(segment: str) -> str | None:
    """Return ``"reduce"``, ``"extend"`` or ``None`` for one clause."""
    low = segment.lower()
    if _has_any_word(low, _REDUCE_WORDS) or _has_any_substring(low, _REDUCE_FA):
        return "reduce"
    if _has_any_word(low, _EXTEND_WORDS) or _has_any_substring(low, _EXTEND_FA):
        return "extend"
    return None


def _first_number(segment: str) -> float | None:
    m = _NUMBER_RE.search(segment)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public parse function
# ---------------------------------------------------------------------------


def parse_delta_intent(
    text: str,
    *,
    current_scope: VideoTimeScope | None,
    project_id: str,
    asset_id: str,
    source_revision: int,
    duration_us: int,
    channel_binding_id: str,
) -> DeltaIntentAST | Clarification | Refusal:
    """Parse a relative-scope intent into a :class:`DeltaIntentAST`.

    Returns :class:`DeltaIntentAST` on success, or a
    :class:`~nexus_ai_agent.nagar.ibpc.intent.Clarification` /
    :class:`~nexus_ai_agent.nagar.ibpc.intent.Refusal` on ambiguity/error.

    The returned ``resolved_ast`` is a standard ``TypedCreativeAST`` that
    can be passed unchanged to ``compile_ast``.
    """
    if not isinstance(text, str):
        return Refusal(
            "invalid_text_type", f"intent text must be a string, got {type(text).__name__}"
        )
    normalized = normalize_intent(text)
    if not normalized:
        return Refusal("empty_intent", "empty delta intent text")

    # Caller-supplied identity guards (validated here so no raw ValidationError
    # ever escapes this boundary).
    if isinstance(source_revision, bool) or not isinstance(source_revision, int):
        return Refusal(
            "invalid_source_revision", f"source_revision must be an int, got {source_revision!r}"
        )
    if source_revision < 0:
        return Refusal(
            "invalid_source_revision", f"source_revision must be >= 0, got {source_revision}"
        )
    if duration_us <= 0:
        return Refusal("unknown_duration", "source duration is not measured yet")

    if current_scope is None:
        return Clarification(
            questions=(
                "No prior scope exists. Please state an absolute range, e.g. "
                "'trim from 2 to 5 seconds'.",
            ),
            reason="no_prior_scope",
        )

    if current_scope.start_us < 0 or current_scope.end_us > duration_us:
        return Refusal(
            "scope_out_of_bounds",
            f"the current scope {current_scope.start_us}..{current_scope.end_us}us lies outside "
            f"the source duration {duration_us}us",
        )

    if asset_id != current_scope.asset_id:
        return Refusal(
            "asset_mismatch",
            f"asset_id {asset_id!r} does not match the current scope's asset "
            f"{current_scope.asset_id!r}",
        )

    ascii_text = _to_ascii_digits(normalized)
    if _has_negation(ascii_text):
        return Refusal(
            "negated_delta",
            "a negated relative instruction must not be executed; restate the change you do want",
        )

    # --- per-clause, fully-explicit parse (no operation silently dropped) ---
    ops: list[DeltaOp] = []
    for segment in _split_segments(ascii_text):
        normalized_segment = _normalize_word_numbers(segment)
        seconds = _first_number(normalized_segment)
        anchors = _anchors_in(normalized_segment)
        if len(anchors) > 1:
            return Clarification(
                questions=(
                    "One clause names both the start and the end. Give each end its own "
                    "instruction, e.g. 'trim 1 second from the start and 1 second from the end'.",
                ),
                reason="mixed_anchor",
            )
        anchor = next(iter(anchors)) if anchors else None
        move_sign = _detect_move_sign(normalized_segment)

        if seconds is None or anchor is None:
            return Clarification(
                questions=(
                    "Every clause must name an end (start/end) and a duration in "
                    "seconds. Example: 'trim 1 more second from the end'.",
                    f"Could not fully resolve the clause: {segment!r}.",
                ),
                reason="ambiguous_delta",
            )

        if move_sign is not None:
            sign = move_sign
        else:
            mode = _detect_reduce_extend(normalized_segment)
            if mode is None:
                return Clarification(
                    questions=(
                        "Say whether to trim (shrink) or add (grow) at that end, "
                        "e.g. 'trim 1 second from the end'.",
                        f"No direction found in the clause: {segment!r}.",
                    ),
                    reason="ambiguous_delta",
                )
            if mode == "reduce":
                sign = +1 if anchor is DeltaAnchor.IN_POINT else -1
            else:  # extend
                sign = -1 if anchor is DeltaAnchor.IN_POINT else +1

        delta_us = int(round(seconds * 1_000_000)) * sign
        ops.append(DeltaOp(anchor=anchor, delta_us=delta_us))

    if not ops:
        return Clarification(
            questions=(
                "Which end should change — start or end? By how many seconds? "
                "Example: 'trim 1 more second from the end'.",
            ),
            reason="ambiguous_delta",
        )

    # --- contradiction: same anchor moved in opposite directions ---
    for target in (DeltaAnchor.IN_POINT, DeltaAnchor.OUT_POINT):
        signs = {1 if op.delta_us > 0 else -1 for op in ops if op.anchor is target}
        if len(signs) > 1:
            return Refusal(
                "contradictory_delta",
                f"the request moves the same {target.value} in opposite directions",
            )

    net_in = sum(op.delta_us for op in ops if op.anchor is DeltaAnchor.IN_POINT)
    net_out = sum(op.delta_us for op in ops if op.anchor is DeltaAnchor.OUT_POINT)

    if net_in == 0 and net_out == 0:
        return Refusal("zero_delta", "the request resolves to a zero-microsecond change")

    # Reject any single misparsed delta larger than half the asset duration.
    max_abs = duration_us // 2
    if any(abs(op.delta_us) > max_abs for op in ops):
        return Refusal(
            "delta_out_of_range",
            f"a delta exceeds half the asset duration ({max_abs} us)",
        )

    # --- apply ---
    new_start_us = current_scope.start_us + net_in
    new_end_us = current_scope.end_us + net_out

    if new_start_us < 0:
        return Refusal("delta_out_of_range", f"resolved start_us={new_start_us} < 0")
    if new_end_us > duration_us:
        return Refusal(
            "delta_out_of_range",
            f"resolved end_us={new_end_us} > duration {duration_us}",
        )
    if new_start_us >= new_end_us:
        return Refusal(
            "delta_out_of_range",
            f"resolved scope inverted or empty: {new_start_us}..{new_end_us}",
        )

    try:
        resolved_scope = VideoTimeScope(
            asset_id=asset_id,
            effect="keep",
            start_us=new_start_us,
            end_us=new_end_us,
        )
        forbidden = tuple(
            VideoTimeScope(
                asset_id=asset_id,
                effect="modify",
                start_us=r.start_us,
                end_us=r.end_us,
            )
            for r in resolved_scope.forbidden_ranges(duration_us)
        )
        resolved_ast = TypedCreativeAST(
            project_id=project_id,
            asset_id=asset_id,
            channel_binding_id=channel_binding_id,
            source_revision=source_revision,
            operation="timeline.trim",
            params={
                "clip_asset_id": asset_id,
                "in_point_us": new_start_us,
                "out_point_us": new_end_us,
            },
            scope=resolved_scope,
            forbidden_scopes=forbidden,
            ambiguities=(),
        )
    except ValidationError as exc:
        return Refusal(
            "invalid_request",
            f"request identifiers or scope failed validation ({exc.error_count()} error(s))",
        )

    return DeltaIntentAST(
        base_scope_digest=current_scope.scope_digest,
        delta_ops=tuple(ops),
        resolved_ast=resolved_ast,
        intent_digest=intent_digest_of(normalized),
    )


# Runtime import after the definitions: each of intent/delta completes its own
# body before importing the other, so either import order is cycle-safe.
from nexus_ai_agent.nagar.ibpc.intent import (  # noqa: E402
    Clarification,
    Refusal,
    intent_digest_of,
    normalize_intent,
)

__all__ = [
    "DeltaAnchor",
    "DeltaIntentAST",
    "DeltaOp",
    "parse_delta_intent",
]
