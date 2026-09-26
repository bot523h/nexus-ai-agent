from __future__ import annotations

import threading

import pytest

from nexus_ai_agent.presence import (
    InvalidPresenceTTL,
    InvalidPresenceUser,
    PresenceCapacityError,
    PresenceStore,
)


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_presence_mark_online_offline() -> None:
    clock = FakeClock()
    store = PresenceStore(ttl_seconds=10, clock=clock)
    store.mark_online(123)
    assert store.is_online(123) is True
    store.mark_offline(123)
    assert store.is_online(123) is False
    assert store.size == 0


def test_presence_ttl_expiry_is_clock_driven_and_cleans_all_stale_entries() -> None:
    clock = FakeClock()
    store = PresenceStore(ttl_seconds=10, clock=clock)
    store.mark_online(123)
    store.mark_online(456, ttl_seconds=0.5)
    clock.advance(0.5)
    assert store.is_online(456) is False
    assert store.is_online(123) is True
    clock.advance(9.5)
    assert store.is_online(123) is False
    assert store.size == 0


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), float("-inf"), "10"])
def test_invalid_ttls_fail_closed(ttl: float) -> None:
    with pytest.raises(InvalidPresenceTTL):
        PresenceStore(ttl_seconds=ttl)

    store = PresenceStore()
    with pytest.raises(InvalidPresenceTTL):
        store.mark_online(1, ttl_seconds=ttl)
    assert store.size == 0


def test_fractional_and_huge_ttls_have_explicit_contract() -> None:
    clock = FakeClock()
    store = PresenceStore(ttl_seconds=0.25, clock=clock)
    store.mark_online(1)
    clock.advance(0.249)
    assert store.is_online(1) is True
    clock.advance(0.001)
    assert store.is_online(1) is False

    with pytest.raises(InvalidPresenceTTL):
        PresenceStore(ttl_seconds=PresenceStore.MAX_TTL_SECONDS + 1)


def test_repeated_refreshes_do_not_grow_the_store() -> None:
    store = PresenceStore(clock=FakeClock())
    for _ in range(10_000):
        store.mark_online(7)
    assert store.size == 1


def test_store_is_bounded_without_silent_eviction() -> None:
    store = PresenceStore(max_entries=2, clock=FakeClock())
    store.mark_online(1)
    store.mark_online(2)
    with pytest.raises(PresenceCapacityError):
        store.mark_online(3)
    assert store.size == 2
    assert store.is_online(1) is True
    assert store.is_online(2) is True


def test_capacity_is_recovered_after_expiry() -> None:
    clock = FakeClock()
    store = PresenceStore(max_entries=1, ttl_seconds=1, clock=clock)
    store.mark_online(1)
    clock.advance(1)
    store.mark_online(2)
    assert store.is_online(1) is False
    assert store.is_online(2) is True


def test_restart_semantics_are_process_local() -> None:
    clock = FakeClock()
    first = PresenceStore(clock=clock)
    first.mark_online(1)
    second = PresenceStore(clock=clock)
    assert second.is_online(1) is False


def test_invalid_user_ids_are_not_truncated() -> None:
    store = PresenceStore()
    for user_id in (True, False, 1.5, "123"):
        with pytest.raises(InvalidPresenceUser):
            store.mark_online(user_id)  # type: ignore[arg-type]


def test_concurrent_access_is_serialized_and_bounded() -> None:
    store = PresenceStore(max_entries=100, clock=FakeClock())
    errors: list[BaseException] = []

    def worker(offset: int) -> None:
        try:
            for user_id in range(offset, offset + 25):
                store.mark_online(user_id)
                assert store.is_online(user_id) is True
        except BaseException as exc:  # pragma: no cover - only reports a thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i * 25,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert store.size == 100
