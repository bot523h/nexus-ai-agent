"""Deterministic relative-scope intent parser for the IBPC Delta Dialogue zone.

A user utterance that references the *current* scope relatively ("trim 1 more
second from the end", "یک ثانیه از تهش ببر") is parsed into a :class:`DeltaOp`
(anchor + signed microseconds), then resolved against the last committed
:class:`VideoTimeScope` into a standard :class:`TypedCreativeAST`.

Contract (fail-closed, NAGAR-IBPC-TRIAD-CLOSURE-01 Stage 2):

- The parser never executes a partial, negated, contradictory or ambiguous
  instruction. Every such input returns :class:`Clarification` or
  :class:`Refusal`.
- Caller input never raises. Non-string text, invalid revisions and invalid
  identifiers return a typed :class:`Refusal`.
- Feature extraction is explicit: operation verbs, anchor words and quantities
  are counted over the *whole* utterance. A single unambiguous combination is
  resolved; anything else is refused. The first regex match is never trusted.
- Digits and word-numbers are normalised by one policy (see
  :func:`normalize_numeric_text`). The Persian word ``نه`` means "no" and is
  deliberately absent from the word table, so it can never become the digit 9.

This module imports :mod:`intent` at module level. ``intent`` re-exports
:func:`parse_delta_intent` lazily (PEP 562), so there is no import cycle.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_ai_agent.nagar.ibpc.intent import (
    Clarification,
    Refusal,
    _to_ascii_digits,
    intent_digest_of,
    normalize_intent,
)
from nexus_ai_agent.nagar.ibpc.models import TypedCreativeAST, VideoTimeScope

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DeltaAnchor(str, Enum):
    """Which end of the kept window the delta moves."""

    IN_POINT = "in_point"    # start of the kept window
    OUT_POINT = "out_point"  # end of the kept window


class DeltaOp(BaseModel):
    """One relative mutation on the in- or out-point of the last committed scope.

    ``delta_us`` is signed microseconds. Negative means "move earlier in time".
    Zero is allowed by the model but refused by :func:`parse_delta_intent`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor: DeltaAnchor
    delta_us: int   # signed microseconds


class DeltaIntentAST(BaseModel):
    """Typed result of :func:`parse_delta_intent`.

    ``resolved_ast`` is a standard :class:`TypedCreativeAST` ready to pass
    unchanged to :func:`~nexus_ai_agent.nagar.ibpc.compiler.compile_ast`.
    ``base_scope_digest`` records which scope this delta was applied against
    (for auditability — a stale approval cannot replay against a newer scope).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_scope_digest: str    # sha256 of the VideoTimeScope this delta resolved against
    delta_op: DeltaOp
    resolved_ast: TypedCreativeAST
    intent_digest: str


# ---------------------------------------------------------------------------
# Shared numeric policy
# ---------------------------------------------------------------------------

#: Word-numbers the delta parser accepts. ``نه`` (Persian "no") is NOT here.
_WORD_NUMBERS: dict[str, str] = {
    "یک": "1", "دو": "2", "سه": "3", "چهار": "4", "پنج": "5",
    "شش": "6", "هفت": "7", "هشت": "8", "ده": "10", "نیم": "0.5",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "half": "0.5",
}
_WORD_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(w) for w in sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")(?!\w)",
    re.IGNORECASE,
)


def normalize_numeric_text(text: str) -> str:
    """Map Persian/Arabic digits to ASCII, then whole-word numbers to digits.

    Digit conversion is the shared :func:`intent._to_ascii_digits` policy.
    """
    ascii_text = _to_ascii_digits(text)
    return _WORD_RE.sub(lambda m: _WORD_NUMBERS[m.group(1).lower()], ascii_text)


# ---------------------------------------------------------------------------
# Feature extraction (explicit, counted over the whole utterance)
# ---------------------------------------------------------------------------

_NEGATION_RE = re.compile(
    r"(?<!\w)(?:don[’']?t|do\s+not|never|without|not|no)(?!\w)"
    r"|(?:^|(?<=\s))(?:نه|نکن|نکنید|نبر|نزن|بدون|مکن)(?=$|[\s،,.!?؟])",
    re.IGNORECASE,
)

_TRIM_RE = re.compile(r"(?<!\w)(?:trim|cut|remove|shorten|delete)(?!\w)|ببر|برش|حذف|کوتاه|بزن", re.IGNORECASE)
_ADD_RE = re.compile(r"(?<!\w)(?:add|extend|append)(?!\w)|اضافه|برگردون|برگردان", re.IGNORECASE)
_MOVE_RE = re.compile(r"(?<!\w)move(?!\w)", re.IGNORECASE)

_END_RE = re.compile(r"(?<!\w)(?:end|tail)(?!\w)|تهش|آخرش|انتهاش|انتها|پایانش", re.IGNORECASE)
_START_RE = re.compile(r"(?<!\w)(?:start|beginning)(?!\w)|اولش|ابتداش|ابتدا|اول", re.IGNORECASE)

# Quantity = a numeric token that is not glued to letters or a decimal dot.
_QUANTITY_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)")

_MOVE_DIRECTIVE_RE = re.compile(
    r"(?<!\w)move\s+(out|end|in|start)\s+(?:point\s+)?(earlier|later)\s+by\s+(\d+(?:\.\d+)?)(?!\w)",
    re.IGNORECASE,
)

#: (operation, anchor word-class) -> (anchor, sign of delta_us)
_SIGN_TABLE: dict[tuple[str, str], tuple[DeltaAnchor, int]] = {
    ("trim", "end"): (DeltaAnchor.OUT_POINT, -1),
    ("trim", "start"): (DeltaAnchor.IN_POINT, +1),
    ("add", "end"): (DeltaAnchor.OUT_POINT, +1),
    ("add", "start"): (DeltaAnchor.IN_POINT, -1),
}

_MOVE_ANCHOR = {"out": DeltaAnchor.OUT_POINT, "end": DeltaAnchor.OUT_POINT,
                "in": DeltaAnchor.IN_POINT, "start": DeltaAnchor.IN_POINT}
_MOVE_SIGN = {"earlier": -1, "later": +1}

DeltaParseResult = DeltaIntentAST | Clarification | Refusal


def _clarify(reason: str, *questions: str) -> Clarification:
    return Clarification(questions=questions or ("Please rephrase the change you want.",), reason=reason)


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

    Returns :class:`DeltaIntentAST` only for one unambiguous, un-negated,
    single-operation, single-anchor, single-quantity instruction whose resolved
    scope is inside the source. Everything else is a :class:`Clarification`
    (ask) or :class:`Refusal` (reject). Never raises for caller input.
    """
    # --- caller-input validation (typed refusals, never exceptions) --------
    if not isinstance(text, str):
        return Refusal("invalid_text_type", "delta intent text must be a string")
    if not isinstance(source_revision, int) or source_revision < 0:
        return Refusal("invalid_source_revision", f"source_revision={source_revision!r}")
    if not normalize_intent(text):
        return Refusal("empty_intent", "empty delta intent text")
    if duration_us <= 0:
        return Refusal("unknown_duration", "source duration is not measured yet")
    if current_scope is None:
        return _clarify(
            "no_prior_scope",
            "No prior scope exists. Please state an absolute range, e.g. 'trim from 2 to 5 seconds'.",
        )
    if current_scope.asset_id != asset_id:
        return Refusal(
            "asset_mismatch",
            f"current scope belongs to {current_scope.asset_id!r}, request targets {asset_id!r}",
        )
    if current_scope.end_us > duration_us:
        return Refusal(
            "scope_out_of_bounds",
            f"current scope end {current_scope.end_us} exceeds duration {duration_us}",
        )

    normalized = normalize_intent(text)
    lowered = normalize_numeric_text(normalized).lower()

    # --- fail-closed: negation and multi-operation are never executed -------
    if _NEGATION_RE.search(normalized):
        return _clarify(
            "negated_constraint",
            "Which exact change may happen? Negated instructions cannot be compiled automatically.",
            "Please state the change positively, e.g. 'trim 1 more second from the end'.",
        )

    ops = [name for name, rx in (("trim", _TRIM_RE), ("add", _ADD_RE), ("move", _MOVE_RE))
           if rx.search(normalized)]
    if not ops:
        return _clarify(
            "ambiguous_delta",
            "Which end should change — start or end? By how many seconds? "
            "Example: 'trim 1 more second from the end'.",
        )
    if len(ops) > 1:
        return _clarify(
            "contradictory_operations",
            f"The request mixes operations {ops}; please ask for one change at a time.",
        )

    quantities = _QUANTITY_RE.findall(lowered)
    if not quantities:
        return _clarify("ambiguous_delta", "By how many seconds should it change?")
    if len(quantities) > 1:
        return _clarify(
            "multiple_quantities",
            f"I found {len(quantities)} quantities ({quantities}); please ask for one change at a time.",
        )

    operation = ops[0]
    if operation == "move":
        directive = _MOVE_DIRECTIVE_RE.search(lowered)
        if directive is None:
            return _clarify(
                "ambiguous_delta",
                "Use the form 'move out point earlier by N seconds' or 'move in point later by N seconds'.",
            )
        anchor = _MOVE_ANCHOR[directive.group(1).lower()]
        sign = _MOVE_SIGN[directive.group(2).lower()]
        seconds = float(directive.group(3))
    else:
        has_start = bool(_START_RE.search(normalized))
        has_end = bool(_END_RE.search(normalized))
        if has_start and has_end:
            return _clarify(
                "mixed_anchor",
                "The request names both the start and the end; which one should change?",
            )
        if not (has_start or has_end):
            return _clarify("missing_anchor", "Should the change apply to the start or the end?")
        anchor, sign = _SIGN_TABLE[(operation, "start" if has_start else "end")]
        seconds = float(quantities[0])

    delta_us = int(round(seconds * 1_000_000)) * sign
    if delta_us == 0:
        return Refusal("zero_delta", f"a delta of {seconds}s resolves to zero microseconds")

    # --- resolve against the committed scope and bound-check ---------------
    new_start_us = current_scope.start_us
    new_end_us = current_scope.end_us
    if anchor == DeltaAnchor.IN_POINT:
        new_start_us += delta_us
    else:
        new_end_us += delta_us

    if new_start_us < 0:
        return Refusal("delta_out_of_range", f"resolved start_us={new_start_us} < 0")
    if new_end_us > duration_us:
        return Refusal("delta_out_of_range", f"resolved end_us={new_end_us} > duration {duration_us}")
    if new_start_us >= new_end_us:
        return Refusal("delta_out_of_range", f"resolved scope inverted or empty: {new_start_us}..{new_end_us}")
    max_abs = duration_us // 2
    if abs(delta_us) > max_abs:
        return Refusal(
            "delta_out_of_range",
            f"|delta| {abs(delta_us)} us exceeds half the asset duration {max_abs} us",
        )

    # --- build the executable AST (typed refusal on invalid identifiers) ---
    try:
        resolved_scope = VideoTimeScope(
            asset_id=asset_id,
            effect="keep",
            start_us=new_start_us,
            end_us=new_end_us,
        )
        forbidden = tuple(
            VideoTimeScope(asset_id=asset_id, effect="modify", start_us=r.start_us, end_us=r.end_us)
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
        return Refusal("invalid_request", f"identifiers or scope rejected: {exc.error_count()} error(s)")

    return DeltaIntentAST(
        base_scope_digest=current_scope.scope_digest,
        delta_op=DeltaOp(anchor=anchor, delta_us=delta_us),
        resolved_ast=resolved_ast,
        intent_digest=intent_digest_of(normalized),
    )


__all__ = [
    "DeltaAnchor",
    "DeltaIntentAST",
    "DeltaOp",
    "DeltaParseResult",
    "normalize_numeric_text",
    "parse_delta_intent",
]
