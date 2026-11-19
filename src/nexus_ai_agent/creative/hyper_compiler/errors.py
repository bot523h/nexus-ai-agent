"""Fail-closed error hierarchy for the hyper-compiler / segment-CAS engine.

Every boundary in this package refuses on the bad path with a *typed* error
instead of degrading: a malformed filter graph, a tampered cache artifact, a
memory-budget breach or a failed subprocess pipe all surface as one of the
classes below, never as a raw ``OSError``/``ValueError`` leaking out.

The classes form a single inheritance tree rooted at :class:`HyperCompilerError`
so a caller can catch the whole family with one ``except`` while still being
able to discriminate the specific refusal by type or by ``code``.
"""

from __future__ import annotations


class HyperCompilerError(Exception):
    """Base class for every typed hyper-compiler failure (fail-closed)."""

    #: Stable machine-readable discriminant; subclasses override it.
    code: str = "hyper_compiler_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class InvalidFilterGraphError(HyperCompilerError):
    """A filter graph (or one of its operands) is unsafe or malformed."""

    code = "invalid_filter_graph"


class SegmentIntegrityError(HyperCompilerError):
    """A cached segment artifact disagrees with its recorded content digest."""

    code = "segment_integrity"


class SegmentTamperError(SegmentIntegrityError):
    """A cached segment manifest is missing, forged or internally inconsistent.

    Raised when a manifest's *provenance* — its renderer version, engine mode,
    pipeline digest or content-addressed key — cannot be re-derived from the
    requested spec, so the artifact cannot be trusted even if its bytes hash
    correctly.  It is a :class:`SegmentIntegrityError` (and therefore a
    :class:`HyperCompilerError`) so existing fail-closed callers keep catching it,
    while a caller that wants to distinguish tampering from a plain digest
    mismatch can.
    """

    code = "segment_tamper"


class MemoryBudgetExceededError(HyperCompilerError):
    """The bounded pipe would exceed its configured memory ceiling."""

    code = "memory_budget_exceeded"


class PipeExecutionError(HyperCompilerError):
    """The streaming pipe could not be built or executed."""

    code = "pipe_execution"


__all__ = [
    "HyperCompilerError",
    "InvalidFilterGraphError",
    "MemoryBudgetExceededError",
    "PipeExecutionError",
    "SegmentIntegrityError",
    "SegmentTamperError",
]
