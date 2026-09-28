"""W2 — the registry's singleton promise under concurrency (LAW 1, LAW 6).

:mod:`registry` documents "exactly one authority per process" and "a split brain
must be visible, not hidden". The accessors are process globals that *synchronous*
constructors call (``SummarizerEngine.__init__`` resolves its gateway inline), and
this repository runs synchronous work in threads (``asyncio.to_thread`` in the job
queue, in whisper, in the ffmpeg/render pipeline, in RAG) and starts fresh loops
with ``asyncio.run`` (CLI, maintenance), so that promise has to hold when two
threads reach *first use* at the same moment — not only when one caller asks twice
in a row. No LLM request is executed inside a worker thread today; the hazard is
the shared global, and the invariant is unconditional either way.

Every test here widens the check-then-build window with a deliberately slow
build and releases all threads from a barrier at once. Against a lock-free
registry that produces several authorities in one process — the captured log
literally prints ``llm_gateway_installed`` once per thread — which means two sets
of concurrency bounds, rate windows, breaker state and metrics, plus a loser that
keeps serving its caller while nobody owns its adapter's HTTP pool any more.

The deadlock test matters as much as the race tests: the fix introduces locks,
and a lock held while calling into the other accessor would hang the process
instead of splitting it. Both failure modes are asserted here.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from nexus_ai_agent.llm.gateway import registry
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.policy import default_policy
from nexus_ai_agent.llm.gateway.registry import (
    _CREDENTIAL_GATEWAYS,
    gateway_for_credentials,
    get_llm_gateway,
    reset_llm_gateway,
)

#: Wide enough that every thread is inside the check-then-build window at once,
#: short enough that eight serialized builds still finish in well under a second.
BUILD_SECONDS = 0.05
THREADS = 8

#: Synthetic fixtures — these assertions are about identity, never about a key.
KEY = "AIzaSyTEST-ONLY-KEY-VALUE-0000000042"
MODEL = "gemini-2.0-flash"


def _routeless_gateway() -> LLMGateway:
    """A real authority with no routes: these tests assert on identity only."""

    return LLMGateway(policy=default_policy(routes=()))


async def _close_all(gateways: Sequence[LLMGateway]) -> None:
    """Close everything a burst built; an unclosed authority leaks its HTTP pool."""

    for gateway in gateways:
        if not gateway.closed:
            await gateway.aclose()


def _burst(work: Callable[[int], Any], threads: int = THREADS) -> list[Any]:
    """Run *work* on *threads* threads released at the same instant (blocking).

    A join timeout is an assertion, not a convenience: a registry that deadlocks
    must fail the test rather than hang the suite until CI kills the job.
    """

    barrier = threading.Barrier(threads)
    results: list[Any] = [None] * threads
    failures: list[BaseException] = []

    def runner(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = work(index)
        except BaseException as exc:  # noqa: BLE001 — reported on the main thread
            failures.append(exc)

    workers = [threading.Thread(target=runner, args=(i,), name=f"race-{i}") for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    alive = [worker.name for worker in workers if worker.is_alive()]
    assert not alive, f"thread(s) never returned — the registry deadlocked: {alive}"
    assert not failures, f"a concurrent caller raised: {failures!r}"
    return results


async def _race(work: Callable[[int], Any], threads: int = THREADS) -> list[Any]:
    """Drive :func:`_burst` off the loop: the race is between threads, not tasks."""

    return await asyncio.to_thread(_burst, work, threads)


@pytest.fixture()
async def built() -> Sequence[LLMGateway]:
    """Track every gateway a burst constructs, and close them all afterwards."""

    created: list[LLMGateway] = []
    try:
        yield created
    finally:
        reset_llm_gateway()
        for stale in list(_CREDENTIAL_GATEWAYS.values()):
            if all(stale is not known for known in created):
                created.append(stale)
        _CREDENTIAL_GATEWAYS.clear()
        await _close_all(created)


def _slow_factory(created: list[LLMGateway]) -> Callable[..., LLMGateway]:
    """A build slow enough for every waiting thread to be inside it at once."""

    def build(settings: Any = None, **kwargs: Any) -> LLMGateway:
        time.sleep(BUILD_SECONDS)
        gateway = _routeless_gateway()
        created.append(gateway)
        return gateway

    return build


async def test_concurrent_first_use_builds_exactly_one_process_authority(
    monkeypatch: pytest.MonkeyPatch,
    built: list[LLMGateway],
) -> None:
    """Eight threads, one first use: one authority, one build, no split brain."""

    monkeypatch.setattr(registry, "build_gateway_from_settings", _slow_factory(built))

    handed_out = await _race(lambda _: get_llm_gateway())

    assert len(built) == 1, f"the authority was built {len(built)} times, not once"
    assert len({id(gateway) for gateway in handed_out}) == 1, (
        "concurrent callers received different authorities — two sets of bounds, "
        "rate windows, breaker state and metrics in one process"
    )
    assert handed_out[0] is built[0]
    assert all(not gateway.closed for gateway in handed_out)


async def test_a_closed_authority_is_rebuilt_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
    built: list[LLMGateway],
) -> None:
    """The rebuild path is a second check-then-build site, so it is raced too."""

    monkeypatch.setattr(registry, "build_gateway_from_settings", _slow_factory(built))
    stale = get_llm_gateway()
    await _close_all([stale])
    assert stale.closed

    handed_out = await _race(lambda _: get_llm_gateway())

    assert len(built) == 2, "a closed authority must be rebuilt exactly once"
    assert len({id(gateway) for gateway in handed_out}) == 1
    assert handed_out[0] is built[1]
    assert handed_out[0] is not stale
    assert not handed_out[0].closed


async def test_concurrent_scoped_credential_resolution_builds_exactly_one_gateway(
    monkeypatch: pytest.MonkeyPatch,
    built: list[LLMGateway],
) -> None:
    """The credential cache has the same window, and the same split-brain cost."""

    class SlowGateway(LLMGateway):
        """Sleeps inside construction, which is exactly where the race lives."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            time.sleep(BUILD_SECONDS)
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(registry, "LLMGateway", SlowGateway)

    handed_out = await _race(lambda _: gateway_for_credentials(KEY, MODEL))

    assert len(built) == 1, f"the scoped gateway was built {len(built)} times, not once"
    assert len({id(gateway) for gateway in handed_out}) == 1, (
        "one credential set resolved to several gateways, so its rate window and "
        "its concurrency bound were silently split"
    )
    assert len(_CREDENTIAL_GATEWAYS) == 1
    # Nothing was constructed and then abandoned: an unowned gateway is an
    # adapter whose HTTP pool nobody will ever close.
    assert all(gateway is handed_out[0] or gateway.closed for gateway in built)


async def test_mixed_authority_and_credential_bursts_never_deadlock(
    monkeypatch: pytest.MonkeyPatch,
    built: list[LLMGateway],
) -> None:
    """Both accessors at once from different threads must complete, not hang."""

    monkeypatch.setattr(registry, "build_gateway_from_settings", _slow_factory(built))

    def work(index: int) -> Any:
        if index % 2:
            return gateway_for_credentials(KEY, MODEL)
        return get_llm_gateway()

    handed_out = await _race(work)

    authorities = {id(result) for index, result in enumerate(handed_out) if index % 2 == 0}
    scoped = {id(result) for index, result in enumerate(handed_out) if index % 2}
    assert len(authorities) == 1, "the process authority split under a mixed burst"
    assert len(scoped) == 1, "the credential cache split under a mixed burst"
    assert authorities != scoped, "a scoped credential gateway replaced the authority"


async def test_locking_does_not_change_when_the_authority_is_built(
    monkeypatch: pytest.MonkeyPatch,
    built: list[LLMGateway],
) -> None:
    """The lock changes *how many* builds happen, never *when* the first one does."""

    calls: list[int] = []

    def counting_build(settings: Any = None, **kwargs: Any) -> LLMGateway:
        calls.append(1)
        gateway = _routeless_gateway()
        built.append(gateway)
        return gateway

    monkeypatch.setattr(registry, "build_gateway_from_settings", counting_build)

    assert calls == [], "resetting the registry must not build anything"
    first = get_llm_gateway()
    assert len(calls) == 1
    assert get_llm_gateway() is first
    assert len(calls) == 1, "a warm authority must not be rebuilt"


async def test_a_warm_authority_never_touches_the_lock_under_load(
    monkeypatch: pytest.MonkeyPatch,
    built: list[LLMGateway],
) -> None:
    """Steady state stays cheap: the fast path returns before acquiring anything.

    Asserted by making acquisition impossible — a lock that cannot be taken would
    deadlock any caller that tried, so a burst that completes proves the warm path
    never asks for it.
    """

    monkeypatch.setattr(registry, "build_gateway_from_settings", _slow_factory(built))
    warm = get_llm_gateway()

    class Unacquirable:
        def __enter__(self) -> None:
            raise AssertionError("the warm path acquired the authority lock")

        def __exit__(self, *exc: object) -> None:  # pragma: no cover - never reached
            return None

    # Restore before fixture teardown: reset is now correctly a locked writer.
    with monkeypatch.context() as patch:
        patch.setattr(registry, "_AUTHORITY_LOCK", Unacquirable())
        handed_out = await _race(lambda _: get_llm_gateway(), threads=4)

    assert all(gateway is warm for gateway in handed_out)
    assert len(built) == 1


def test_the_registry_publishes_its_locks_as_an_invariant() -> None:
    """The guarantee is structural, so the primitives it rests on are named here."""

    assert isinstance(registry._AUTHORITY_LOCK, type(threading.Lock()))
    assert isinstance(registry._CREDENTIAL_LOCK, type(threading.Lock()))
    assert registry._AUTHORITY_LOCK is not registry._CREDENTIAL_LOCK, (
        "one shared lock would couple credential scoping to the process authority"
    )
