"""Typed errors for the NAGAR ATLAS episodic-causality lab.

Every failure mode the lab can report is a distinct, catchable type. The lab is
an advisory analysis layer, so a *refusal* (malformed input, unsupported schema,
a resource limit) is a first-class typed outcome rather than a silent default.
"""

from __future__ import annotations

__all__ = [
    "AtlasError",
    "StoryValidationError",
    "DuplicateIdentifierError",
    "UnknownReferenceError",
    "LimitExceededError",
    "UnsupportedSchemaError",
    "DegenerateGeometryError",
]


class AtlasError(Exception):
    """Base class for every error the episode-causality lab raises."""


class StoryValidationError(AtlasError):
    """The story document is structurally malformed (missing/invalid field)."""


class DuplicateIdentifierError(StoryValidationError):
    """Two entities share an identifier inside one namespace."""


class UnknownReferenceError(StoryValidationError):
    """An event or constraint references an identifier that was never declared."""


class LimitExceededError(StoryValidationError):
    """A declared bound (count, size, nesting, numeric range) was exceeded."""


class UnsupportedSchemaError(StoryValidationError):
    """The document declares a schema version this lab does not implement."""


class DegenerateGeometryError(AtlasError):
    """A geometric transform is degenerate (e.g. a singular homography)."""
