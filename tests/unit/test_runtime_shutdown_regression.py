"""W1 recovery — shutdown/cancellation regression contracts.

Proves ``RuntimeContext.aclose()`` is idempotent, progress-preserving, and
cancellation-safe (mission §8/§13):

* normal close runs request_queue → job_queue → storage in order;
* one component raising ``Exception`` never aborts the rest;
* ``asyncio.CancelledError`` (a ``BaseException`` since 3.8) never strands
  resources: remaining steps still run, the cancellation is re-raised (never
  swallowed), and ``_closed`` stays ``False`` so a second ``aclose()`` retries;
* double close after success is a no-op;
* partial initialization (``job_queue=None``) is handled.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.application.runtime import RuntimeContext


def _make_runtime(
    *,
    queue_close: Any = None,
    job_shutdown: Any = None,
    store_dispose: Any = None,
    job_queue_present: bool = True,
) -> tuple[RuntimeContext, dict[str, Any]]:
    """Build a RuntimeContext from fakes.  Returns (runtime, calls recorder)."""
    calls: dict[str, Any] = {"order": [], "queue": 0, "job": 0, "store": 0}

    class _FakeQueue:
        async def close(self) -> None:
            calls["queue"] += 1
            calls["order"].append("request_queue")
            if queue_close is not None:
                await queue_close()

    class _FakeJobQueue:
        async def shutdown(self) -> None:
            calls["job"] += 1
            calls["order"].append("job_queue")
            if job_shutdown is not None:
                await job_shutdown()

    class _FakeEngine:
        def dispose(self) -> None:
            calls["store"] += 1
            calls["order"].append("storage")
            if store_dispose is not None:
                store_dispose()

    fake_store = SimpleNamespace(_engine=_FakeEngine())
    rt = RuntimeContext(
        settings=SimpleNamespace(),
        db=SimpleNamespace(backend="sqlite", source="default"),
        request_queue=_FakeQueue(),  # type: ignore[arg-type]
        conversation_store=fake_store,  # type: ignore[arg-type]
        gemini_engine=None,
        llm_provider=None,
        summarizer_engine=None,
    )
    rt.job_queue = _FakeJobQueue() if job_queue_present else None
    return rt, calls


async def _raise_exc() -> None:
    raise RuntimeError("boom")


async def _raise_cancelled() -> None:
    raise asyncio.CancelledError("cancelled by fake")


def _raise_store_exc() -> None:
    raise RuntimeError("store boom")


# ── normal + ordering + idempotency ─────────────────────────────────────


async def test_normal_close_runs_in_order_and_is_idempotent() -> None:
    rt, calls = _make_runtime()
    await rt.aclose()
    assert calls["order"] == ["request_queue", "job_queue", "storage"]
    assert rt.is_closed is True
    await rt.aclose()
    assert calls["order"] == ["request_queue", "job_queue", "storage"], "second close no-op"
    assert calls["queue"] == 1 and calls["job"] == 1 and calls["store"] == 1


async def test_partial_initialization_without_job_queue_closes() -> None:
    rt, calls = _make_runtime(job_queue_present=False)
    await rt.aclose()
    assert calls["order"] == ["request_queue", "storage"]
    assert rt.is_closed is True


async def test_job_queue_without_shutdown_attr_is_skipped() -> None:
    rt, calls = _make_runtime()
    rt.job_queue = SimpleNamespace()  # no .shutdown
    await rt.aclose()
    assert calls["order"] == ["request_queue", "storage"]
    assert rt.is_closed is True


# ── Exception in one component never aborts the rest ───────────────────


async def test_exception_in_queue_close_still_runs_job_and_storage() -> None:
    rt, calls = _make_runtime(queue_close=_raise_exc)
    await rt.aclose()  # must not raise
    assert calls["order"] == ["request_queue", "job_queue", "storage"]
    assert rt.is_closed is True


async def test_exception_in_job_shutdown_still_runs_queue_and_storage() -> None:
    rt, calls = _make_runtime(job_shutdown=_raise_exc)
    await rt.aclose()
    assert calls["order"] == ["request_queue", "job_queue", "storage"]
    assert rt.is_closed is True


async def test_exception_in_storage_dispose_still_marks_closed() -> None:
    rt, calls = _make_runtime(store_dispose=_raise_store_exc)
    await rt.aclose()
    assert calls["order"] == ["request_queue", "job_queue", "storage"]
    assert rt.is_closed is True


async def test_exceptions_in_all_components_still_completes() -> None:
    rt, calls = _make_runtime(
        queue_close=_raise_exc, job_shutdown=_raise_exc, store_dispose=_raise_store_exc
    )
    await rt.aclose()
    assert calls["order"] == ["request_queue", "job_queue", "storage"]
    assert rt.is_closed is True


# ── Cancellation: progress + re-raise + retry ──────────────────────────


async def test_queue_close_cancelled_still_runs_rest_then_reraises() -> None:
    rt, calls = _make_runtime(queue_close=_raise_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await rt.aclose()
    # Progress preserved despite the cancellation.
    assert calls["order"] == ["request_queue", "job_queue", "storage"]
    # Not stranded: retry is allowed.
    assert rt.is_closed is False


async def test_job_shutdown_cancelled_still_runs_storage_then_reraises() -> None:
    rt, calls = _make_runtime(job_shutdown=_raise_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await rt.aclose()
    assert calls["order"] == ["request_queue", "job_queue", "storage"]
    assert rt.is_closed is False


async def test_retry_after_cancellation_completes_and_marks_closed() -> None:
    attempts = {"n": 0}

    async def _fail_once() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise asyncio.CancelledError("first attempt cancelled")

    rt, calls = _make_runtime(queue_close=_fail_once)
    with pytest.raises(asyncio.CancelledError):
        await rt.aclose()
    assert rt.is_closed is False
    await rt.aclose()
    assert rt.is_closed is True
    assert calls["queue"] == 2
    assert calls["order"].count("request_queue") == 2


async def test_caller_cancelling_aclose_cannot_strand_resources() -> None:
    """Case 6: the task running ``aclose()`` is cancelled externally.  The
    runtime must still attempt every step, surface CancelledError, and allow
    a retry — never swallow the cancellation and never strand with
    ``_closed=True`` while cleanup is incomplete."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def _blocking_close() -> None:
        started.set()
        await release.wait()

    rt, calls = _make_runtime(queue_close=_blocking_close)
    task = asyncio.create_task(rt.aclose())
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    release.set()  # let the fake finish if it resumes
    with pytest.raises(asyncio.CancelledError):
        await task
    # The queue step was attempted; job + storage still ran (progress).
    assert calls["order"][0] == "request_queue"
    assert "job_queue" in calls["order"] and "storage" in calls["order"]
    assert rt.is_closed is False
    # Retry with a cooperative queue now completes.
    rt2, calls2 = _make_runtime()
    # Rebind the same doubt: a fresh runtime closes cleanly (sanity).
    await rt2.aclose()
    assert rt2.is_closed is True
    assert calls2["order"] == ["request_queue", "job_queue", "storage"]


async def test_cancellation_is_never_swallowed_as_success() -> None:
    """A cancelled shutdown must never look like a clean shutdown."""
    rt, _ = _make_runtime(queue_close=_raise_cancelled)
    cancelled = False
    try:
        await rt.aclose()
    except asyncio.CancelledError:
        cancelled = True
    assert cancelled is True, "aclose() swallowed CancelledError"
    assert rt.is_closed is False, "cancelled shutdown must not mark closed"
