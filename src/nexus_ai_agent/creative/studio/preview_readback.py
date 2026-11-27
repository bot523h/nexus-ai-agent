"""WYSIWYS structured preview readback in exact rational time.

The approval a human gives must describe *exactly* what the worker will execute,
in numbers that survive both a round trip through JSON and a 23.976 fps timeline.
Two representation bugs are eliminated here before they can reach an approval:

* **Floats in timeline math.** ``fractions.Fraction`` only — never ``float``.
  NTSC ``30000/1001`` is a repeating decimal; adding it as a float and expecting
  a 10,000-cut timeline to land on a frame boundary is an arithmetic fiction.
  Every span here is an exact ``Fraction`` of seconds and every boundary is
  exact to the microsecond.
* **A preview that is not what it claims.** :class:`StructuredPreviewReadback`
  owns a single ``preview_digest``: the RFC 8785 JCS SHA-256 of *every* field of
  the readback.  :func:`verify_wysiwys_readback` re-derives it and compares with
  ``hmac.compare_digest``, so a UI that displays one cut set while the plan pins
  another cannot also present a matching digest — tampering with any field,
  including the bilingual title, the timebase or the operation order, fails.

:func:`compare_revisions` diffs two readbacks in exact rational arithmetic and
reports zero drift when the two revisions are identical, which is the property a
"no-op edit" must have.

The readback is deliberately *closed*: it carries no callbacks, no file handles
and no floats, so the exact bytes that were shown are the exact bytes that can be
hashed, stored beside the revision and re-verified by a worker.
"""

from __future__ import annotations

import hmac
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
)

__all__ = [
    "BILINGUAL_LANGUAGES",
    "ParameterReadbackDelta",
    "PreviewReadbackError",
    "RationalTime",
    "RationalTimeSpan",
    "RevisionComparisonReport",
    "StructuredPreviewReadback",
    "StudioPreviewReadbackBuilder",
    "TimelineOperationPreview",
    "compare_revisions",
    "frames_for",
    "lcm_timebase",
    "verify_wysiwys_readback",
]

#: The two languages every human-facing line must be rendered in.
BILINGUAL_LANGUAGES: tuple[str, str] = ("fa", "en")

_SCHEMA = "nexus.studio.preview_readback.v1"
_COMPARISON_SCHEMA = "nexus.studio.revision_comparison.v1"


class PreviewReadbackError(ValueError):
    """Typed, fail-closed refusal from the preview readback."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
# Exact rational time
# --------------------------------------------------------------------------- #
def _as_fraction(value: Any, *, field_name: str) -> Fraction:
    """Coerce ``int``/``Fraction``/``"n/d"`` to an exact Fraction.

    Floats are refused outright: accepting one would silently reintroduce the
    rounding this module exists to remove (``float(1)/3`` is not ``1/3``).
    """
    if isinstance(value, bool):
        raise PreviewReadbackError("invalid_time", f"{field_name} must not be a bool")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise PreviewReadbackError("invalid_time", f"{field_name} must not be empty")
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise PreviewReadbackError(
                "invalid_time", f"{field_name}={value!r} is not an exact rational ({exc})"
            ) from exc
    raise PreviewReadbackError(
        "invalid_time",
        f"{field_name} must be an int, Fraction or 'n/d' string, got {type(value).__name__}",
    )


def _fraction_text(value: Fraction) -> str:
    """``3/2`` for non-integers, ``2`` for integers — never a decimal expansion."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


@dataclass(frozen=True)
class RationalTime:
    """One exact instant in seconds, stored as a reduced ``Fraction``."""

    seconds: Fraction

    def __post_init__(self) -> None:
        object.__setattr__(self, "seconds", _as_fraction(self.seconds, field_name="seconds"))

    @classmethod
    def of(cls, value: Any) -> RationalTime:
        """Build from an ``int``, ``Fraction`` or ``"n/d"`` string."""
        return cls(seconds=_as_fraction(value, field_name="seconds"))

    @classmethod
    def from_microseconds(cls, microseconds: int) -> RationalTime:
        if isinstance(microseconds, bool) or not isinstance(microseconds, int):
            raise PreviewReadbackError("invalid_time", "microseconds must be an int")
        return cls(seconds=Fraction(microseconds, 1_000_000))

    @property
    def microseconds(self) -> int:
        """Exact microsecond value; refuses a time that is not whole microseconds."""
        scaled = self.seconds * 1_000_000
        if scaled.denominator != 1:
            raise PreviewReadbackError(
                "sub_microsecond_time",
                f"{_fraction_text(self.seconds)}s is finer than one microsecond",
            )
        return int(scaled)

    def to_seconds_text(self) -> str:
        return _fraction_text(self.seconds)

    def __add__(self, other: RationalTime) -> RationalTime:
        return RationalTime(seconds=self.seconds + other.seconds)

    def __lt__(self, other: RationalTime) -> bool:
        return self.seconds < other.seconds

    def __le__(self, other: RationalTime) -> bool:
        return self.seconds <= other.seconds


@dataclass(frozen=True)
class RationalTimeSpan:
    """A half-open ``[start, end)`` span in exact rational seconds."""

    start: RationalTime
    end: RationalTime

    def __post_init__(self) -> None:
        if not isinstance(self.start, RationalTime) or not isinstance(self.end, RationalTime):
            raise PreviewReadbackError(
                "invalid_time", "RationalTimeSpan requires RationalTime endpoints"
            )
        if self.start.seconds >= self.end.seconds:
            raise PreviewReadbackError(
                "invalid_span",
                f"start {self.start.to_seconds_text()}s must precede end "
                f"{self.end.to_seconds_text()}s",
            )

    @classmethod
    def of(cls, start: Any, end: Any) -> RationalTimeSpan:
        return cls(start=RationalTime.of(start), end=RationalTime.of(end))

    @classmethod
    def from_microseconds(cls, start_us: int, end_us: int) -> RationalTimeSpan:
        return cls(
            start=RationalTime.from_microseconds(start_us),
            end=RationalTime.from_microseconds(end_us),
        )

    @property
    def duration(self) -> RationalTime:
        return RationalTime(seconds=self.end.seconds - self.start.seconds)

    def contains(self, instant: RationalTime) -> bool:
        """Half-open containment: ``start <= instant < end``."""
        return self.start.seconds <= instant.seconds < self.end.seconds

    def overlaps(self, other: RationalTimeSpan) -> bool:
        """Half-open overlap; touching endpoints do not overlap."""
        return self.start.seconds < other.end.seconds and other.start.seconds < self.end.seconds

    def to_dict(self) -> dict[str, str]:
        return {
            "start": self.start.to_seconds_text(),
            "end": self.end.to_seconds_text(),
            "duration": self.duration.to_seconds_text(),
        }


def _timebase(value: Any) -> Fraction:
    """``(numerator, denominator)``, ``"n/d"`` or already-exact → frame rate."""
    if isinstance(value, Fraction):
        # Already normalised (``__post_init__`` re-runs the coercion on the
        # stored field, so this path is the idempotent one).
        if value <= 0:
            raise PreviewReadbackError("invalid_timebase", "timebase must be positive")
        return value
    if isinstance(value, bool):
        raise PreviewReadbackError("invalid_timebase", "a bool is not a timebase")
    if isinstance(value, tuple):
        if len(value) != 2:
            raise PreviewReadbackError("invalid_timebase", "timebase tuple must be (num, den)")
        numerator, denominator = value
    elif isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
    else:
        raise PreviewReadbackError(
            "invalid_timebase", "timebase must be a (numerator, denominator) pair or 'n/d'"
        )
    try:
        num = int(numerator)
        den = int(denominator)
    except (TypeError, ValueError) as exc:
        raise PreviewReadbackError(
            "invalid_timebase", f"timebase components must be integers ({exc})"
        ) from exc
    if num <= 0 or den <= 0:
        raise PreviewReadbackError(
            "invalid_timebase", f"timebase {num}/{den} must be strictly positive"
        )
    return Fraction(num, den)


def frames_for(span: RationalTimeSpan, timebase: Fraction) -> int:
    """Exact whole frames covered by ``span`` at ``timebase`` (rounded to nearest).

    Uses ``Fraction`` throughout and the half-up integer rule
    ``(2*a*den + b*num) // (2*b*den)`` so it never touches floating point.
    """
    if timebase <= 0:
        raise PreviewReadbackError("invalid_timebase", "timebase must be positive")
    seconds = span.duration.seconds
    # frames = seconds * timebase, kept as an exact rational.
    numerator = seconds.numerator * timebase.numerator
    denominator = seconds.denominator * timebase.denominator
    # round(numerator / denominator) half-up, in integers.
    return (2 * numerator + denominator) // (2 * denominator)


# --------------------------------------------------------------------------- #
# Operation preview
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParameterReadbackDelta:
    """One parameter's before/after, rendered in both languages.

    ``before``/``after`` are stored as exact rational *text* (never floats) so a
    numeric parameter (opacity, gain, rate) survives the round trip unchanged,
    and a non-numeric parameter is carried verbatim.
    """

    parameter: str
    before: str
    after: str
    labels: Mapping[str, str] = field(default_factory=dict)
    unit: str = ""
    is_numeric: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.parameter, str) or not self.parameter.strip():
            raise PreviewReadbackError("invalid_field", "parameter must be a non-empty string")
        for name in ("before", "after"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PreviewReadbackError("invalid_field", f"{name} must be a non-empty string")
        object.__setattr__(self, "labels", _bilingual_labels(self.labels, "labels"))
        object.__setattr__(self, "unit", str(self.unit))
        object.__setattr__(self, "is_numeric", bool(self.is_numeric))
        if self.is_numeric:
            # Exact parse (Fraction accepts "0.5" without loss of the *literal*,
            # and "1/3" exactly); a float-typed input is already impossible here
            # because the values are strings.
            for name in ("before", "after"):
                try:
                    Fraction(getattr(self, name))
                except (ValueError, ZeroDivisionError) as exc:
                    raise PreviewReadbackError(
                        "invalid_numeric_parameter",
                        f"{self.parameter}.{name}={getattr(self, name)!r} is not numeric ({exc})",
                    ) from exc

    @classmethod
    def numeric(
        cls,
        parameter: str,
        before: Any,
        after: Any,
        *,
        labels: Mapping[str, str],
        unit: str = "",
    ) -> ParameterReadbackDelta:
        """Numeric delta from exact values (``int``/``Fraction``/``"n/d"``)."""
        return cls(
            parameter=parameter,
            before=_fraction_text(_as_fraction(before, field_name=f"{parameter}.before")),
            after=_fraction_text(_as_fraction(after, field_name=f"{parameter}.after")),
            labels=labels,
            unit=unit,
            is_numeric=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "before": self.before,
            "after": self.after,
            "unit": self.unit,
            "is_numeric": self.is_numeric,
            "labels": {key: self.labels[key] for key in sorted(self.labels)},
        }


def _bilingual_labels(labels: Mapping[str, str], field_name: str) -> dict[str, str]:
    """Every human-facing string must exist in both ``fa`` and ``en``."""
    if not isinstance(labels, Mapping):
        raise PreviewReadbackError(
            "missing_translation", f"{field_name} must be a mapping of language -> text"
        )
    resolved: dict[str, str] = {}
    for language in BILINGUAL_LANGUAGES:
        text = labels.get(language)
        if not isinstance(text, str) or not text.strip():
            raise PreviewReadbackError(
                "missing_translation",
                f"{field_name} is missing the {language!r} rendering",
            )
        resolved[language] = text
    return resolved


@dataclass(frozen=True)
class TimelineOperationPreview:
    """What one operation will do: where, to what, and how it reads in both languages."""

    operation: str
    target_clip_id: str
    span: RationalTimeSpan
    deltas: tuple[ParameterReadbackDelta, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    reversible: bool = True
    destructive: bool = False

    def __post_init__(self) -> None:
        for name in ("operation", "target_clip_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PreviewReadbackError("invalid_field", f"{name} must be a non-empty string")
        if not isinstance(self.span, RationalTimeSpan):
            raise PreviewReadbackError("invalid_field", "span must be a RationalTimeSpan")
        object.__setattr__(
            self, "labels", _bilingual_labels(self.labels, f"{self.operation}.labels")
        )
        resolved = tuple(self.deltas)
        for delta in resolved:
            if not isinstance(delta, ParameterReadbackDelta):
                raise PreviewReadbackError(
                    "invalid_field", "deltas must be ParameterReadbackDelta instances"
                )
        seen: set[str] = set()
        for delta in resolved:
            if delta.parameter in seen:
                raise PreviewReadbackError(
                    "duplicate_parameter",
                    f"{self.operation} declares {delta.parameter!r} twice",
                )
            seen.add(delta.parameter)
        object.__setattr__(self, "deltas", resolved)
        object.__setattr__(self, "reversible", bool(self.reversible))
        object.__setattr__(self, "destructive", bool(self.destructive))
        if self.destructive and self.reversible:
            raise PreviewReadbackError(
                "destructive_is_irreversible",
                f"{self.operation} cannot be both destructive and reversible",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target_clip_id": self.target_clip_id,
            "span": self.span.to_dict(),
            "deltas": [delta.to_dict() for delta in self.deltas],
            "labels": {key: self.labels[key] for key in sorted(self.labels)},
            "reversible": self.reversible,
            "destructive": self.destructive,
        }


# --------------------------------------------------------------------------- #
# The readback itself
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StructuredPreviewReadback:
    """The exact, hashable description of an edit a human is approving."""

    readback_id: str
    tenant_id: str
    project_id: str
    revision_id: str
    revision_number: int
    principal_id: str
    timebase: Fraction
    operations: tuple[TimelineOperationPreview, ...]
    summary: Mapping[str, str] = field(default_factory=dict)
    language_order: tuple[str, ...] = BILINGUAL_LANGUAGES
    preview_digest: str = ""
    schema: str = _SCHEMA

    def __post_init__(self) -> None:
        for name in ("readback_id", "tenant_id", "project_id", "revision_id", "principal_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PreviewReadbackError("invalid_field", f"{name} must be a non-empty string")
        if (
            isinstance(self.revision_number, bool)
            or not isinstance(self.revision_number, int)
            or self.revision_number < 1
        ):
            raise PreviewReadbackError(
                "invalid_revision_number",
                f"revision_number must be an int >= 1, got {self.revision_number!r}",
            )
        object.__setattr__(self, "timebase", _timebase(self.timebase))
        if self.schema != _SCHEMA:
            raise PreviewReadbackError("invalid_schema", f"unsupported schema {self.schema!r}")
        resolved_ops = tuple(self.operations)
        for operation in resolved_ops:
            if not isinstance(operation, TimelineOperationPreview):
                raise PreviewReadbackError(
                    "invalid_operation",
                    "operations must be TimelineOperationPreview instances",
                )
        object.__setattr__(self, "operations", resolved_ops)
        object.__setattr__(self, "summary", _bilingual_labels(self.summary, "summary"))
        order = tuple(self.language_order)
        if sorted(order) != sorted(BILINGUAL_LANGUAGES):
            raise PreviewReadbackError(
                "invalid_language_order",
                f"language_order must be a permutation of {BILINGUAL_LANGUAGES}, got {order}",
            )
        object.__setattr__(self, "language_order", order)
        computed = self.compute_digest()
        if not self.preview_digest:
            object.__setattr__(self, "preview_digest", computed)
        elif self.preview_digest != computed:
            raise PreviewReadbackError(
                "preview_digest_mismatch",
                "the supplied preview_digest does not describe this readback",
            )

    # -- digest ------------------------------------------------------------ #

    def canonical_payload(self) -> dict[str, Any]:
        """Every field except the digest itself, in canonical form."""
        return {
            "schema": self.schema,
            "readback_id": self.readback_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "principal_id": self.principal_id,
            "timebase": _fraction_text(self.timebase),
            "language_order": list(self.language_order),
            "summary": {key: self.summary[key] for key in sorted(self.summary)},
            "operations": [operation.to_dict() for operation in self.operations],
        }

    def compute_digest(self) -> str:
        """RFC 8785 JCS SHA-256 over the whole readback."""
        return canonical_digest(self.canonical_payload(), CanonicalizationScheme.JCS_RFC8785_V2)

    def verify_integrity(self) -> bool:
        """Re-derive the digest and compare it in constant time.

        Fail-closed: a readback too corrupted even to *re-hash* (a byte-forged
        timebase, a non-serialisable field planted by a compromised process) is
        not verified — verification must answer True/False, never raise into the
        approval path.
        """
        try:
            computed = self.compute_digest()
        except Exception:  # noqa: BLE001 - fail-closed by construction
            return False
        return hmac.compare_digest(computed, self.preview_digest)

    # -- timing ------------------------------------------------------------ #

    @property
    def total_span(self) -> RationalTimeSpan:
        """Half-open span from the earliest cut start to the latest cut end."""
        if not self.operations:
            return RationalTimeSpan.of(0, 1)
        start = min(operation.span.start.seconds for operation in self.operations)
        end = max(operation.span.end.seconds for operation in self.operations)
        return RationalTimeSpan(start=RationalTime(seconds=start), end=RationalTime(seconds=end))

    def frame_count(self) -> int:
        """Exact whole-frame coverage of the whole readback."""
        if not self.operations:
            return 0
        return frames_for(self.total_span, self.timebase)

    def cut_points(self) -> tuple[RationalTime, ...]:
        """Every distinct operation boundary, in exact ascending order."""
        points = {operation.span.start.seconds for operation in self.operations}
        points |= {operation.span.end.seconds for operation in self.operations}
        return tuple(RationalTime(seconds=value) for value in sorted(points))

    def to_dict(self) -> dict[str, Any]:
        """The canonical readback plus its digest (what a UI may display)."""
        payload = self.canonical_payload()
        payload["preview_digest"] = self.preview_digest
        return payload

    def render(self, language: str) -> str:
        """Human-readable rendering in one language (never the source of truth)."""
        if language not in BILINGUAL_LANGUAGES:
            raise PreviewReadbackError(
                "unsupported_language", f"{language!r} is not one of {BILINGUAL_LANGUAGES}"
            )
        lines = [self.summary[language]]
        for operation in self.operations:
            lines.append(f"- {operation.labels[language]} ({operation.span.to_dict()['start']}s)")
            for delta in operation.deltas:
                suffix = delta.unit
                lines.append(
                    f"    {delta.labels[language]}: {delta.before}{suffix} -> {delta.after}{suffix}"
                )
        return "\n".join(lines)


def verify_wysiwys_readback(
    readback: object,
    *,
    expected_digest: str = "",
) -> bool:
    """True only if the readback still hashes to what was shown/approved.

    Fails closed on a non-readback input, on any tampered field, and on a digest
    that disagrees with ``expected_digest`` (the value the approval recorded).
    """
    if not isinstance(readback, StructuredPreviewReadback):
        return False
    if not readback.verify_integrity():
        return False
    if expected_digest:
        return hmac.compare_digest(readback.preview_digest, expected_digest)
    return True


# --------------------------------------------------------------------------- #
# Revision comparison
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RevisionComparisonReport:
    """The exact delta between two readbacks (a no-op edit yields zero drift)."""

    base_revision_id: str
    head_revision_id: str
    base_revision_number: int
    head_revision_number: int
    added_operations: tuple[str, ...]
    removed_operations: tuple[str, ...]
    changed_operations: tuple[str, ...]
    frame_drift: int
    duration_drift_seconds: str
    duration_drift_us: int
    duration_drift_us_exact: bool
    timebase_changed: bool
    labels: Mapping[str, str] = field(default_factory=dict)
    summary: Mapping[str, str] = field(default_factory=dict)
    comparison_digest: str = ""
    schema: str = _COMPARISON_SCHEMA

    def __post_init__(self) -> None:
        for name in ("base_revision_id", "head_revision_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PreviewReadbackError("invalid_field", f"{name} must be a non-empty string")
        for name in ("added_operations", "removed_operations", "changed_operations"):
            ordered = tuple(sorted(getattr(self, name)))
            object.__setattr__(self, name, ordered)
        object.__setattr__(self, "frame_drift", int(self.frame_drift))
        object.__setattr__(self, "duration_drift_us", int(self.duration_drift_us))
        object.__setattr__(
            self,
            "duration_drift_seconds",
            _fraction_text(
                _as_fraction(self.duration_drift_seconds, field_name="duration_drift_seconds")
            ),
        )
        object.__setattr__(self, "duration_drift_us_exact", bool(self.duration_drift_us_exact))
        object.__setattr__(self, "timebase_changed", bool(self.timebase_changed))
        object.__setattr__(self, "labels", _bilingual_labels(self.labels, "labels"))
        object.__setattr__(self, "summary", _bilingual_labels(self.summary, "summary"))
        if self.schema != _COMPARISON_SCHEMA:
            raise PreviewReadbackError("invalid_schema", f"unsupported schema {self.schema!r}")
        computed = self.compute_digest()
        if not self.comparison_digest:
            object.__setattr__(self, "comparison_digest", computed)
        elif self.comparison_digest != computed:
            raise PreviewReadbackError(
                "comparison_digest_mismatch",
                "the supplied comparison_digest does not describe this report",
            )

    @property
    def identical(self) -> bool:
        """True when nothing about the edit changed (the no-op property)."""
        return (
            not self.added_operations
            and not self.removed_operations
            and not self.changed_operations
            and self.frame_drift == 0
            and self.duration_drift_seconds == "0"
            and not self.timebase_changed
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "base_revision_id": self.base_revision_id,
            "head_revision_id": self.head_revision_id,
            "base_revision_number": self.base_revision_number,
            "head_revision_number": self.head_revision_number,
            "added_operations": list(self.added_operations),
            "removed_operations": list(self.removed_operations),
            "changed_operations": list(self.changed_operations),
            "frame_drift": self.frame_drift,
            "duration_drift_seconds": self.duration_drift_seconds,
            "duration_drift_us": self.duration_drift_us,
            "duration_drift_us_exact": self.duration_drift_us_exact,
            "timebase_changed": self.timebase_changed,
            "labels": {key: self.labels[key] for key in sorted(self.labels)},
            "summary": {key: self.summary[key] for key in sorted(self.summary)},
        }

    def compute_digest(self) -> str:
        return canonical_digest(self.canonical_payload(), CanonicalizationScheme.JCS_RFC8785_V2)

    def to_dict(self) -> dict[str, Any]:
        payload = self.canonical_payload()
        payload["comparison_digest"] = self.comparison_digest
        payload["identical"] = self.identical
        return payload


def _operation_signature(operation: TimelineOperationPreview) -> dict[str, Any]:
    """Everything that makes two operations the *same* edit."""
    return {
        "operation": operation.operation,
        "target_clip_id": operation.target_clip_id,
        "span": {
            "start": operation.span.start.to_seconds_text(),
            "end": operation.span.end.to_seconds_text(),
        },
        "deltas": [
            {"parameter": delta.parameter, "before": delta.before, "after": delta.after}
            for delta in operation.deltas
        ],
        "reversible": operation.reversible,
        "destructive": operation.destructive,
    }


def compare_revisions(
    base: StructuredPreviewReadback,
    head: StructuredPreviewReadback,
    *,
    labels: Mapping[str, str] | None = None,
) -> RevisionComparisonReport:
    """Diff two readbacks in exact rational arithmetic.

    Operations are matched by ``operation`` *and* ``target_clip_id``: a cut that
    moved is ``changed`` (never silently an add plus a remove), and an edit that
    moved nothing produces ``identical=True`` with both drifts at zero.
    """
    for candidate in (base, head):
        if not isinstance(candidate, StructuredPreviewReadback):
            raise PreviewReadbackError(
                "invalid_readback", "compare_revisions requires two StructuredPreviewReadback"
            )
        if not candidate.verify_integrity():
            raise PreviewReadbackError(
                "readback_tampered",
                f"readback {candidate.readback_id!r} no longer matches its preview_digest",
            )

    base_map = {
        (operation.operation, operation.target_clip_id): operation for operation in base.operations
    }
    head_map = {
        (operation.operation, operation.target_clip_id): operation for operation in head.operations
    }
    added = tuple(sorted(key[0] + "@" + key[1] for key in head_map if key not in base_map))
    removed = tuple(sorted(key[0] + "@" + key[1] for key in base_map if key not in head_map))
    changed = tuple(
        sorted(
            key[0] + "@" + key[1]
            for key in base_map
            if key in head_map
            and _operation_signature(base_map[key]) != _operation_signature(head_map[key])
        )
    )

    # Exact duration drift: head total minus base total, as an exact Fraction.
    # The microsecond figure is a *projection* for display: an NTSC one-frame move
    # is 1001/30000 s = 33366.6... µs, which must round rather than be refused —
    # refusing it would make comparison unusable on the timelines this exists for.
    drift_seconds = head.total_span.duration.seconds - base.total_span.duration.seconds
    scaled = drift_seconds * 1_000_000
    exact_us = scaled.denominator == 1
    # Round half-up on integers, never through float.
    drift_us = (2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)
    frame_drift = head.frame_count() - base.frame_count()
    timebase_changed = base.timebase != head.timebase

    resolved_labels = (
        labels
        if labels is not None
        else _default_comparison_labels(
            added=added,
            removed=removed,
            changed=changed,
            frame_drift=frame_drift,
            timebase_changed=timebase_changed,
        )
    )
    return RevisionComparisonReport(
        base_revision_id=base.revision_id,
        head_revision_id=head.revision_id,
        base_revision_number=base.revision_number,
        head_revision_number=head.revision_number,
        added_operations=added,
        removed_operations=removed,
        changed_operations=changed,
        frame_drift=frame_drift,
        duration_drift_seconds=_fraction_text(drift_seconds),
        duration_drift_us=drift_us,
        duration_drift_us_exact=exact_us,
        timebase_changed=timebase_changed,
        labels=resolved_labels,
        summary=resolved_labels,
    )


def _default_comparison_labels(
    *,
    added: Sequence[str],
    removed: Sequence[str],
    changed: Sequence[str],
    frame_drift: int,
    timebase_changed: bool,
) -> dict[str, str]:
    """Deterministic bilingual wording for a comparison report."""
    parts_fa = [f"{len(added)} افزوده", f"{len(removed)} حذف", f"{len(changed)} تغییر"]
    parts_en = [f"{len(added)} added", f"{len(removed)} removed", f"{len(changed)} changed"]
    if frame_drift:
        parts_fa.append(f"اختلاف {frame_drift} فریم")
        parts_en.append(f"{frame_drift} frames apart")
    if timebase_changed:
        parts_fa.append("تغییر تایم‌بیس")
        parts_en.append("timebase changed")
    return {"fa": "، ".join(parts_fa), "en": ", ".join(parts_en)}


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
class StudioPreviewReadbackBuilder:
    """Accumulates operations, then seals them into a digest-bearing readback.

    Sealing is not optional: :class:`StructuredPreviewReadback` refuses any
    instance whose ``preview_digest`` disagrees with its own content, so a
    builder cannot emit a preview that does not describe itself.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        project_id: str,
        revision_id: str,
        revision_number: int,
        principal_id: str,
        timebase: Any = (30, 1),
        readback_id: str = "",
    ) -> None:
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._revision_id = revision_id
        self._revision_number = revision_number
        self._principal_id = principal_id
        self._timebase = _timebase(timebase)
        self._readback_id = readback_id
        self._operations: list[TimelineOperationPreview] = []
        self._summary: dict[str, str] = {}
        self._sequence = 0

    @property
    def operation_count(self) -> int:
        return len(self._operations)

    def with_readback_id(self, readback_id: str) -> StudioPreviewReadbackBuilder:
        """Pin the identity (defaults to a deterministic per-build value)."""
        if not isinstance(readback_id, str) or not readback_id.strip():
            raise PreviewReadbackError("invalid_field", "readback_id must be non-empty")
        self._readback_id = readback_id
        return self

    def add_operation(
        self,
        *,
        operation: str,
        target_clip_id: str,
        span: RationalTimeSpan | tuple[Any, Any],
        deltas: Iterable[ParameterReadbackDelta] = (),
        labels: Mapping[str, str] | None = None,
        reversible: bool = True,
        destructive: bool = False,
    ) -> StudioPreviewReadbackBuilder:
        """Append one operation preview (order is part of the digest)."""
        if isinstance(span, RationalTimeSpan):
            resolved_span = span
        elif isinstance(span, tuple) and len(span) == 2:
            resolved_span = RationalTimeSpan.of(span[0], span[1])
        else:
            raise PreviewReadbackError(
                "invalid_span", "span must be a RationalTimeSpan or a (start, end) pair"
            )
        if labels is None:
            text = f"{operation} {target_clip_id}"
            labels = {"fa": f"{operation} روی {target_clip_id}", "en": text}
        self._sequence += 1
        self._operations.append(
            TimelineOperationPreview(
                operation=operation,
                target_clip_id=target_clip_id,
                span=resolved_span,
                deltas=tuple(deltas),
                labels=labels,
                reversible=reversible,
                destructive=destructive,
            )
        )
        return self

    def with_summary(self, summary: Mapping[str, str]) -> StudioPreviewReadbackBuilder:
        self._summary = _bilingual_labels(summary, "summary")
        return self

    def build_readback(self) -> StructuredPreviewReadback:
        """Seal the accumulated state into an immutable, hashable readback."""
        summary = dict(self._summary) or {
            "fa": f"ویرایش {len(self._operations)} عملیات",
            "en": f"Edit of {len(self._operations)} operations",
        }
        readback_id = self._readback_id or self._derived_readback_id(summary)
        return StructuredPreviewReadback(
            readback_id=readback_id,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            revision_id=self._revision_id,
            revision_number=self._revision_number,
            principal_id=self._principal_id,
            timebase=self._timebase,
            operations=tuple(self._operations),
            summary=summary,
        )

    def _derived_readback_id(self, summary: Mapping[str, str]) -> str:
        """Deterministic identity over the accumulated design (no clock, no uuid)."""
        material = {
            "tenant_id": self._tenant_id,
            "project_id": self._project_id,
            "revision_id": self._revision_id,
            "revision_number": self._revision_number,
            "principal_id": self._principal_id,
            "timebase": _fraction_text(self._timebase),
            "summary": {key: summary[key] for key in sorted(summary)},
            "operations": [operation.to_dict() for operation in self._operations],
        }
        digest = canonical_digest(material, CanonicalizationScheme.JCS_RFC8785_V2)
        return "rdb_" + digest.split(":", 1)[1]


def lcm_timebase(*timebases: Any) -> Fraction:
    """Smallest common exact frame rate across ``timebases`` (mixed-rate edits).

    For rationals, ``lcm(a/b, c/d) = lcm(a, c) / gcd(b, d)`` when the operands are
    reduced — the smallest rational that is an integer multiple of both.  An edit
    mixing ``30000/1001`` (NTSC) and ``25/1`` (PAL) therefore resolves to ``30000``
    fps (an exact multiple of 25), and NTSC with ``24000/1001`` (film) to
    ``120000`` fps, with no float step anywhere.

    (Note the reduced-fraction case: ``Fraction(25, 1) * 1200 == 30000``.)
    """
    if not timebases:
        raise PreviewReadbackError("invalid_timebase", "at least one timebase is required")
    resolved = [_timebase(value) for value in timebases]
    numerator = 1
    denominator = 1
    for rate in resolved:
        numerator = math.lcm(numerator, rate.numerator)
        denominator = math.gcd(denominator, rate.denominator)
    return Fraction(numerator, denominator)
