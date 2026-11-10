"""Deterministic relative-scope intent parser for the IBPC Delta Dialogue slice.

Every user utterance that references the *current* scope relatively —
"trim 1 more second from the end", "یک ثانیه از تهش ببر" — is parsed
into a :class:`DeltaOp` (anchor + signed microseconds) and immediately
*resolved* into a standard :class:`TypedCreativeAST` by applying the delta
to the last-committed :class:`VideoTimeScope`.

This module is a pure function library. It imports from ``models``,
``canonical``, and ``intent`` (read-only helpers), and explicitly must NOT
import from ``service``, ``session``, ``approval``, or ``footprint``.

Design constraints:
- No LLM, no external API, no new pip dependencies.
- Persian digit normalisation reuses ``_to_ascii_digits`` and ``_NUMBER_RE``
  patterns from ``intent`` (imported lazily to avoid circular dependency at
  module level).
- Zero delta → ``Refusal("zero_delta")``.
- Resolved scope inverted or empty → ``Refusal("delta_out_of_range")``.
- No prior scope (``current_scope is None``) → ``Clarification``.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import TypedCreativeAST, VideoTimeScope

if TYPE_CHECKING:
    pass

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
# Pattern tables (deterministic, no LLM)
# ---------------------------------------------------------------------------

# Each pattern group: (compiled_regex, anchor, sign)
# sign +1 means delta_us is positive (scope boundary moves *later*),
# sign -1 means delta_us is negative (scope boundary moves *earlier*).

_END_TRIM_RE = [
    # "trim/cut/remove X s from the end / از تهش / از آخرش"
    re.compile(
        r"(?:trim|cut|remove|ببر|برش|حذف|کوتاه).*?(\d+(?:\.\d+)?)\s*"
        r"(?:s\b|sec\b|secs?\b|second[s]?\b|ثانیه)?.*?"
        r"(?:end|tail|تهش|آخرش|انتها|پایانش)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|second[s]?\b|ثانیه)?\s*"
        r"(?:از\s*)?(?:تهش|آخرش|انتهاش|from\s+the\s+end|off\s+the\s+end)",
        re.IGNORECASE,
    ),
]  # → OUT_POINT, sign=-1

_START_TRIM_RE = [
    # "trim X s from the start / از اولش / از ابتداش"
    re.compile(
        r"(?:trim|cut|remove|ببر|برش|حذف|کوتاه).*?(\d+(?:\.\d+)?)\s*"
        r"(?:s\b|sec\b|second[s]?\b|ثانیه)?.*?"
        r"(?:start|beginning|اولش|ابتداش|ابتدا|اول)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|second[s]?\b|ثانیه)?\s*"
        r"(?:از\s*)?(?:اولش|ابتداش|from\s+the\s+start|off\s+the\s+start)",
        re.IGNORECASE,
    ),
]  # → IN_POINT, sign=+1  (start moves right → delta positive)

_END_ADD_RE = [
    # "add X s to/at the end / اضافه کن به آخر"
    re.compile(
        r"(?:add|extend|اضافه).*?(\d+(?:\.\d+)?)\s*"
        r"(?:s\b|sec\b|second[s]?\b|ثانیه)?.*?"
        r"(?:end|tail|back|آخر|تهش|انتها)",
        re.IGNORECASE,
    ),
]  # → OUT_POINT, sign=+1

_START_ADD_RE = [
    # "add X s to/at the start / اضافه کن به اول"
    re.compile(
        r"(?:add|extend|اضافه).*?(\d+(?:\.\d+)?)\s*"
        r"(?:s\b|sec\b|second[s]?\b|ثانیه)?.*?"
        r"(?:start|beginning|back\s+at\s+start|اول|ابتدا|ابتداش)",
        re.IGNORECASE,
    ),
]  # → IN_POINT, sign=-1  (start moves left → delta negative)

# Move anchor earlier/later
_MOVE_OUT_EARLIER_RE = re.compile(
    r"move\s+(?:out|end)\s+(?:point\s+)?earlier\s+by\s+(\d+(?:\.\d+)?)\s*"
    r"(?:s\b|sec\b|second[s]?\b)?",
    re.IGNORECASE,
)  # → OUT_POINT, sign=-1

_MOVE_OUT_LATER_RE = re.compile(
    r"move\s+(?:out|end)\s+(?:point\s+)?later\s+by\s+(\d+(?:\.\d+)?)\s*"
    r"(?:s\b|sec\b|second[s]?\b)?",
    re.IGNORECASE,
)  # → OUT_POINT, sign=+1

_MOVE_IN_EARLIER_RE = re.compile(
    r"move\s+(?:in|start)\s+(?:point\s+)?earlier\s+by\s+(\d+(?:\.\d+)?)\s*"
    r"(?:s\b|sec\b|second[s]?\b)?",
    re.IGNORECASE,
)  # → IN_POINT, sign=-1

_MOVE_IN_LATER_RE = re.compile(
    r"move\s+(?:in|start)\s+(?:point\s+)?later\s+by\s+(\d+(?:\.\d+)?)\s*"
    r"(?:s\b|sec\b|second[s]?\b)?",
    re.IGNORECASE,
)  # → IN_POINT, sign=+1

_PATTERN_TABLE: list[tuple[list[re.Pattern[str]], DeltaAnchor, int]] = [
    (_END_TRIM_RE, DeltaAnchor.OUT_POINT, -1),
    (_START_TRIM_RE, DeltaAnchor.IN_POINT, +1),
    (_END_ADD_RE, DeltaAnchor.OUT_POINT, +1),
    (_START_ADD_RE, DeltaAnchor.IN_POINT, -1),
]

_SINGLE_PATTERN_TABLE: list[tuple[re.Pattern[str], DeltaAnchor, int]] = [
    (_MOVE_OUT_EARLIER_RE, DeltaAnchor.OUT_POINT, -1),
    (_MOVE_OUT_LATER_RE, DeltaAnchor.OUT_POINT, +1),
    (_MOVE_IN_EARLIER_RE, DeltaAnchor.IN_POINT, -1),
    (_MOVE_IN_LATER_RE, DeltaAnchor.IN_POINT, +1),
]


def _to_ascii_digits_local(text: str) -> str:
    """Convert Persian/Arabic-Indic digits to ASCII (local copy, no circular import)."""
    _MAP = {
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        "٫": ".",
    }
    return "".join(_MAP.get(ch, ch) for ch in text)


def _extract_first_number(pattern: re.Pattern[str], text: str) -> float | None:
    m = pattern.search(text)
    if m:
        try:
            return float(m.group(1))
        except (ValueError, IndexError):
            return None
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
) -> "DeltaIntentAST | object":
    """Parse a relative-scope intent into a :class:`DeltaIntentAST`.

    Returns :class:`DeltaIntentAST` on success, or a
    :class:`~nexus_ai_agent.nagar.ibpc.intent.Clarification` /
    :class:`~nexus_ai_agent.nagar.ibpc.intent.Refusal` on ambiguity/error.

    The returned ``resolved_ast`` is a standard ``TypedCreativeAST`` that
    can be passed unchanged to ``compile_ast``.
    """
    # Import lazily to avoid any circular import at module level.
    from nexus_ai_agent.nagar.ibpc.intent import (
        Clarification,
        Refusal,
        intent_digest_of,
        normalize_intent,
    )

    normalized = normalize_intent(text)
    if not normalized:
        return Refusal("empty_intent", "empty delta intent text")

    if current_scope is None:
        return Clarification(
            questions=(
                "No prior scope exists. Please state an absolute range, e.g. "
                "'trim from 2 to 5 seconds'.",
            ),
            reason="no_prior_scope",
        )

    ascii_text = _to_ascii_digits_local(normalized)

    # Try multi-pattern groups
    anchor: DeltaAnchor | None = None
    sign: int = 0
    seconds: float | None = None

    for patterns, _anchor, _sign in _PATTERN_TABLE:
        for pat in patterns:
            val = _extract_first_number(pat, ascii_text)
            if val is not None:
                anchor = _anchor
                sign = _sign
                seconds = val
                break
        if anchor is not None:
            break

    # Try single-pattern table
    if anchor is None:
        for pat, _anchor, _sign in _SINGLE_PATTERN_TABLE:
            val = _extract_first_number(pat, ascii_text)
            if val is not None:
                anchor = _anchor
                sign = _sign
                seconds = val
                break

    if anchor is None or seconds is None:
        return Clarification(
            questions=(
                "Which end should change — start or end? By how many seconds? "
                "Example: 'trim 1 more second from the end'.",
            ),
            reason="ambiguous_delta",
        )

    delta_us = int(round(seconds * 1_000_000)) * sign

    if delta_us == 0:
        return Refusal("zero_delta", f"a delta of {seconds}s resolves to zero microseconds")

    # Apply delta to current scope
    new_start_us = current_scope.start_us
    new_end_us = current_scope.end_us

    if anchor == DeltaAnchor.IN_POINT:
        new_start_us += delta_us
    else:
        new_end_us += delta_us

    # Bounds checks
    if new_start_us < 0:
        return Refusal(
            "delta_out_of_range",
            f"resolved start_us={new_start_us} < 0",
        )
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

    # Reject deltas > half the total duration (likely a misparse)
    max_abs = duration_us // 2
    if abs(delta_us) > max_abs:
        return Refusal(
            "delta_out_of_range",
            f"|delta| {abs(delta_us)} us exceeds half the asset duration {max_abs} us",
        )

    # Build resolved scope and AST
    resolved_scope = VideoTimeScope(
        asset_id=asset_id,
        effect="keep",
        start_us=new_start_us,
        end_us=new_end_us,
    )

    from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope as _VTS

    forbidden = tuple(
        _VTS(
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
    "parse_delta_intent",
]
