"""Error hierarchy and public-surface tests for the hyper-compiler package."""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.hyper_compiler import (
    HyperCompilerError,
    InvalidFilterGraphError,
    MemoryBudgetExceededError,
    PipeExecutionError,
    SegmentIntegrityError,
)


def test_base_error_carries_message_and_code() -> None:
    err = HyperCompilerError("boom")
    assert str(err) == "boom"
    assert err.message == "boom"
    assert err.code == "hyper_compiler_error"


def test_base_error_accepts_code_override() -> None:
    err = HyperCompilerError("boom", code="custom")
    assert err.code == "custom"


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (InvalidFilterGraphError, "invalid_filter_graph"),
        (SegmentIntegrityError, "segment_integrity"),
        (MemoryBudgetExceededError, "memory_budget_exceeded"),
        (PipeExecutionError, "pipe_execution"),
    ],
)
def test_subclass_codes_and_inheritance(cls: type[HyperCompilerError], code: str) -> None:
    err = cls("x")
    assert isinstance(err, HyperCompilerError)
    assert err.code == code


def test_all_errors_are_catchable_as_one_family() -> None:
    with pytest.raises(HyperCompilerError):
        raise SegmentIntegrityError("bad")


def test_public_surface_exports() -> None:
    import nexus_ai_agent.creative.hyper_compiler as pkg

    for name in pkg.__all__:
        assert hasattr(pkg, name), name
