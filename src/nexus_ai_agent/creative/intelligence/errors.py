"""Typed failure contract of the Creative Intelligence Plane.

Every failure the plane raises is a :class:`CreativeIRError` subclass, so a
caller can distinguish "this creative document is not well-formed" from "this
document is well-formed but breaks a constraint the user stated" from "the
compiler could not produce a plan" -- three different problems that must never
be collapsed into one generic exception.

The hierarchy is deliberately shallow and closed: the plane is a pure
transformation layer (no I/O, no execution), so it has exactly three ways to
fail -- malformed input, violated semantics, and an unrepresentable result.
"""

from __future__ import annotations


class CreativeIRError(Exception):
    """Base class for every Creative Intelligence Plane failure."""


# ── Malformed input ──────────────────────────────────────────────────────────


class IRValidationError(CreativeIRError):
    """A creative document is structurally invalid.

    Raised by :meth:`~nexus_ai_agent.creative.intelligence.ir.CreativeWork.validate`
    (and by the pydantic validators behind it). A document that raises this can
    never be compiled; the error carries enough detail to fix the *document*,
    not to guess at it.

    :meth:`CreativeWork.validate` reports *every* problem it finds in one
    ``problems`` tuple rather than raising on the first, because a strategy layer
    that produced a broken document needs one actionable list, not one rebuild
    per discovery. The subclasses below are reserved for the failures that are
    fatal at the moment they are detected and cannot usefully be batched.
    """

    def __init__(self, message: str, *, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.problems: tuple[str, ...] = tuple(problems)


class DanglingReferenceError(IRValidationError):
    """A layer, effect, constraint or transition points at an id that is absent."""


class TimingError(IRValidationError):
    """Timeline arithmetic does not close (overlap, inversion, out-of-bounds)."""


class IdentityError(IRValidationError):
    """A declared content address does not match the content it claims to name.

    This is the tamper/staleness detector of the plane: because every id in the
    IR is the hash of its own semantic payload (plus its children's ids), a
    document whose ids were edited by hand -- or produced by an older, drifted
    builder -- fails here instead of silently compiling the wrong thing.
    """


# ── Violated semantics ───────────────────────────────────────────────────────


class ConstraintViolationError(CreativeIRError):
    """A well-formed document breaks a constraint its own brief declared.

    The distinction from :class:`IRValidationError` matters: the document is
    structurally sound, it is simply not *what was asked for*. A compiler must
    refuse to emit a plan for a hard violation and must report a soft one.
    """

    def __init__(self, message: str, *, violations: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.violations: tuple[str, ...] = tuple(violations)


# ── Unrepresentable result ───────────────────────────────────────────────────


class SerializationError(CreativeIRError):
    """A creative document could not be round-tripped through its canonical form."""


class SemanticDiffError(CreativeIRError):
    """A semantic diff could not be constructed honestly from the supplied data."""


class RevisionError(CreativeIRError):
    """A requested revision cannot be represented as a safe plane transformation."""


class RevisionTargetError(RevisionError):
    """The requested revision target does not exist on the named source work."""


class RevisionAmbiguityError(RevisionError):
    """The requested revision target names more than one possible node."""


class UnsupportedRevisionError(RevisionError):
    """The requested revision operation is outside the typed contract of this slice."""
