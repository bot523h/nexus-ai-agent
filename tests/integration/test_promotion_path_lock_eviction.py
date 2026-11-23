"""Reference-counted eviction behavior for queue destination locks."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue


def test_promotion_path_locks_evict_after_last_reference(tmp_path: Path) -> None:
    queue = InProcessJobQueue(tmp_path / "queue.sqlite3")
    for index in range(100):
        with queue._acquire_promotion_path_lock(tmp_path / f"artifact-{index}.mp4"):
            assert len(queue._promotion_path_locks) == 1
            assert len(queue._promotion_path_lock_refs) == 1
        assert not queue._promotion_path_locks
        assert not queue._promotion_path_lock_refs


def test_waiting_thread_keeps_shared_lock_until_it_releases(tmp_path: Path) -> None:
    queue = InProcessJobQueue(tmp_path / "queue.sqlite3")
    artifact = tmp_path / "same-artifact.mp4"
    context = queue._acquire_promotion_path_lock(artifact)
    context.__enter__()
    key = str(tmp_path / ".same-artifact.mp4.promote.lock")
    shared_lock = queue._promotion_path_locks[key]
    second_started = threading.Event()
    second_entered = threading.Event()
    second_release = threading.Event()
    failures: list[BaseException] = []

    def second_thread() -> None:
        try:
            second_started.set()
            with queue._acquire_promotion_path_lock(artifact):
                second_entered.set()
                if not second_release.wait(timeout=5):
                    raise AssertionError("test did not release the second lock holder")
        except BaseException as exc:  # capture worker-thread assertions for the test thread
            failures.append(exc)

    worker = threading.Thread(target=second_thread)
    worker.start()
    assert second_started.wait(timeout=2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with queue._promotion_lock:
            refs = queue._promotion_path_lock_refs.get(key, 0)
        if refs == 2:
            break
        time.sleep(0.001)
    assert refs == 2, "waiting thread must acquire a reference before blocking on the path lock"
    assert not second_entered.is_set()
    assert queue._promotion_path_locks[key] is shared_lock

    context.__exit__(None, None, None)
    assert second_entered.wait(timeout=2)
    with queue._promotion_lock:
        assert queue._promotion_path_locks.get(key) is shared_lock
        assert queue._promotion_path_lock_refs[key] == 1
    second_release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not failures
    assert queue._promotion_path_locks == {}
    assert queue._promotion_path_lock_refs == {}
