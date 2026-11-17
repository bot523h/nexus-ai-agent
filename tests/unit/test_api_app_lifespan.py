"""The API lifespan owns the durable job queue (P0, task-283 parity).

Two layers are covered:

* contract tests with a recording queue double — they pin the call order
  (acquire -> resume -> serve -> shutdown) and every failure branch;
* REAL-DISK tests with real ``InProcessJobQueue`` sidecars, where ownership is
  the actual ``flock`` on the rendezvous file.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import nexus_ai_agent.api.app as api_app_module
from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    QueueOwnershipError,
)


class _RecordingQueue:
    """Stands in for ``InProcessJobQueue`` and records lifecycle calls."""

    def __init__(
        self,
        *,
        acquire_error: BaseException | None = None,
        resume_error: BaseException | None = None,
        shutdown_error: BaseException | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._acquire_error = acquire_error
        self._resume_error = resume_error
        self._shutdown_error = shutdown_error

    async def acquire_ownership(self) -> None:
        self.calls.append("acquire")
        if self._acquire_error is not None:
            raise self._acquire_error

    async def resume_pending(self, **_kwargs: object) -> list[str]:
        self.calls.append("resume")
        if self._resume_error is not None:
            raise self._resume_error
        return []

    async def shutdown(self) -> None:
        self.calls.append("shutdown")
        if self._shutdown_error is not None:
            raise self._shutdown_error


def _use_queue(monkeypatch: pytest.MonkeyPatch, queue: object) -> None:
    monkeypatch.setattr(
        api_app_module,
        "get_creative_registry",
        lambda: SimpleNamespace(queue=queue),
    )


async def test_lifespan_acquires_resumes_then_shuts_down_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _RecordingQueue()
    _use_queue(monkeypatch, queue)

    async with api_app_module._api_lifespan(api_app_module.app):
        assert queue.calls == ["acquire", "resume"]

    assert queue.calls == ["acquire", "resume", "shutdown"]


async def test_ownership_conflict_is_fatal_and_recovers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _RecordingQueue(acquire_error=QueueOwnershipError("held by pid 1"))
    _use_queue(monkeypatch, queue)

    with pytest.raises(RuntimeError, match="refusing to start the API"):
        async with api_app_module._api_lifespan(api_app_module.app):
            pytest.fail("the app must not start without queue ownership")

    # Never requeued another live process's rows, and never "shut down" a
    # queue this process does not own.
    assert queue.calls == ["acquire"]


async def test_recovery_failure_still_releases_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _RecordingQueue(resume_error=RuntimeError("sqlite busy"))
    _use_queue(monkeypatch, queue)

    with pytest.raises(RuntimeError, match="mandatory queue recovery failed"):
        async with api_app_module._api_lifespan(api_app_module.app):
            pytest.fail("the app must not serve after a failed recovery")

    assert queue.calls == ["acquire", "resume", "shutdown"]


async def test_shutdown_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _RecordingQueue(shutdown_error=RuntimeError("worker stuck"))
    _use_queue(monkeypatch, queue)

    async with api_app_module._api_lifespan(api_app_module.app):
        pass

    assert queue.calls == ["acquire", "resume", "shutdown"]


async def test_real_sidecar_is_owned_while_serving_and_released_after(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    _use_queue(monkeypatch, queue)

    async with api_app_module._api_lifespan(api_app_module.app):
        assert queue.owns_process_lock is True

    assert queue.owns_process_lock is False


async def test_real_sidecar_second_owner_in_same_host_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    first = InProcessJobQueue(db_path)
    second = InProcessJobQueue(db_path)
    await first.acquire_ownership()
    try:
        _use_queue(monkeypatch, second)
        with pytest.raises(RuntimeError, match="refusing to start the API"):
            async with api_app_module._api_lifespan(api_app_module.app):
                pytest.fail("a second owner must not start serving")
        assert second.owns_process_lock is False
        assert first.owns_process_lock is True
    finally:
        await first.shutdown()


def test_app_is_built_with_the_owning_lifespan() -> None:
    assert api_app_module.app.router.lifespan_context is api_app_module._api_lifespan
