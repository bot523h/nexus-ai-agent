"""Unit tests for PostgresCheckpointer (C1) using a fake saver factory.

No real Postgres is required: the checkpointer receives an injectable
``saver_factory`` returning scriptable fakes whose failures we can schedule.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from nexus_ai_agent.adapters.langgraph.lifecycle_recording import (
    LifecycleRecordingSaver,
)
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage.langgraph_checkpoint import (
    AsyncCompatibleSqliteSaver,
    PostgresCheckpointer,
    get_checkpointer,
)

# These tests script psycopg's own exception classes, so the optional [postgres]
# extra is required even though no server is contacted (task-107).
psycopg = pytest.importorskip("psycopg", reason="requires the [postgres] extra")

URL = "postgres://nexus:secret@db.example.com:5432/nexusdb"
NORMALIZED_URL = "postgresql://nexus:secret@db.example.com:5432/nexusdb"


@pytest.fixture(autouse=True)
def _clean_db_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


class FakePool:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSaver:
    def __init__(self, factory: FakeSaverFactory) -> None:
        self._factory = factory
        self.setup_calls = 0

    def _next_saver(self) -> FakeSaver:
        return self._factory.savers[-1]

    async def setup(self) -> None:
        self.setup_calls += 1
        self._factory.before_setup()

    async def aget_tuple(self, config: Any) -> Any:
        self._factory.count("aget_tuple")
        self._factory.before_op()
        return ("aget_tuple", config)

    async def aget(self, config: Any) -> Any:
        self._factory.count("aget")
        self._factory.before_op()
        return ("aget", config)

    async def aput(self, *args: Any, **kwargs: Any) -> Any:
        self._factory.count("aput")
        self._factory.before_op()
        return ("aput", self._factory.op_count)

    async def aput_writes(self, *args: Any, **kwargs: Any) -> Any:
        self._factory.count("aput_writes")
        self._factory.before_op()
        return ("aput_writes", self._factory.op_count)

    async def adelete_thread(self, *args: Any, **kwargs: Any) -> None:
        self._factory.count("adelete_thread")
        self._factory.before_op()

    async def aprune(self, *args: Any, **kwargs: Any) -> None:
        self._factory.count("aprune")
        self._factory.before_op()

    async def aget_delta_channel_history(self, *args: Any, **kwargs: Any) -> Any:
        self._factory.count("aget_delta_channel_history")
        self._factory.before_op()
        return ("history", self._factory.op_count)

    def alist(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self._factory.count("alist")
        self._factory.before_op()

        async def _gen() -> AsyncIterator[Any]:
            yield "item-1"
            yield "item-2"

        return _gen()


class FakeSaverFactory:
    """Scriptable (saver, pool) factory counting every construction attempt."""

    def __init__(
        self,
        *,
        setup_failures: int = 0,
        setup_error: Exception | None = None,
        op_failures: int = 0,
        op_error: Exception | None = None,
    ) -> None:
        self.setup_failures = setup_failures
        self.setup_error = (
            setup_error
            if setup_error is not None
            else psycopg.OperationalError("connection refused")
        )
        self.op_failures = op_failures
        self.op_error = (
            op_error if op_error is not None else psycopg.OperationalError("connection lost")
        )
        self.build_calls = 0
        self.op_count = 0
        self.pools: list[FakePool] = []
        self.savers: list[FakeSaver] = []
        self.loops: list[asyncio.AbstractEventLoop] = []

    async def __call__(self) -> tuple[FakeSaver, FakePool]:
        self.build_calls += 1
        self.loops.append(asyncio.get_running_loop())
        pool = FakePool()
        saver = FakeSaver(self)
        self.pools.append(pool)
        self.savers.append(saver)
        return saver, pool

    def count(self, name: str) -> None:
        self.op_count += 1

    def before_setup(self) -> None:
        if self.setup_failures:
            self.setup_failures -= 1
            raise self.setup_error

    def before_op(self) -> None:
        if self.op_failures:
            self.op_failures -= 1
            raise self.op_error


def make_checkpointer(factory: FakeSaverFactory) -> PostgresCheckpointer:
    return PostgresCheckpointer(URL, saver_factory=factory, max_attempts=5, base_delay=0.0)


class TestLazyConstruction:
    @pytest.mark.asyncio
    async def test_constructor_is_lazy(self) -> None:
        factory = FakeSaverFactory()
        make_checkpointer(factory)
        assert factory.build_calls == 0

    @pytest.mark.asyncio
    async def test_first_operation_builds_inside_running_loop(self) -> None:
        factory = FakeSaverFactory()
        checkpointer = make_checkpointer(factory)
        result = await checkpointer.aput("state")
        assert factory.build_calls == 1
        assert factory.loops[0] is asyncio.get_running_loop()
        assert result == ("aput", 1)

    @pytest.mark.asyncio
    async def test_saver_and_setup_created_once(self) -> None:
        factory = FakeSaverFactory()
        checkpointer = make_checkpointer(factory)
        for _ in range(3):
            await checkpointer.aput()
            await checkpointer.aget_tuple({"configurable": {"thread_id": "t"}})
        assert factory.build_calls == 1
        assert factory.savers[0].setup_calls == 1
        assert factory.op_count == 6

    @pytest.mark.asyncio
    async def test_warmup_builds_eagerly_and_operations_reuse_it(self) -> None:
        factory = FakeSaverFactory()
        checkpointer = make_checkpointer(factory)
        await checkpointer.warmup()
        assert factory.build_calls == 1
        assert factory.savers[0].setup_calls == 1
        await checkpointer.aput()
        assert factory.build_calls == 1


class TestConstructionRetry:
    @pytest.mark.asyncio
    async def test_retries_connection_errors_with_backoff_slots(self) -> None:
        factory = FakeSaverFactory(setup_failures=2)
        checkpointer = make_checkpointer(factory)
        await checkpointer.aput()
        assert factory.build_calls == 3
        # Pools from failed attempts are closed; the live one stays open.
        assert factory.pools[0].closed and factory.pools[1].closed
        assert not factory.pools[2].closed
        assert factory.savers[2].setup_calls == 1

    @pytest.mark.asyncio
    async def test_gives_up_after_five_attempts(self) -> None:
        factory = FakeSaverFactory(setup_failures=10)
        checkpointer = make_checkpointer(factory)
        with pytest.raises(RuntimeError, match="5 attempts"):
            await checkpointer.aput()
        assert factory.build_calls == 5
        assert all(pool.closed for pool in factory.pools)

    @pytest.mark.asyncio
    async def test_does_not_retry_non_connection_errors(self) -> None:
        factory = FakeSaverFactory(setup_failures=5, setup_error=ValueError("bad config"))
        checkpointer = make_checkpointer(factory)
        with pytest.raises(ValueError, match="bad config"):
            await checkpointer.aput()
        assert factory.build_calls == 1
        assert factory.pools[0].closed


class TestOperationReconnect:
    @pytest.mark.asyncio
    async def test_reconnects_once_after_connection_lost(self) -> None:
        factory = FakeSaverFactory(op_failures=1)
        checkpointer = make_checkpointer(factory)
        result = await checkpointer.aput()
        assert result == ("aput", 2)
        # Old pool closed by reset(); a fresh pool/saver was built.
        assert factory.pools[0].closed
        assert not factory.pools[1].closed
        assert factory.build_calls == 2

    @pytest.mark.asyncio
    async def test_retries_exactly_once(self) -> None:
        factory = FakeSaverFactory(op_failures=2)
        checkpointer = make_checkpointer(factory)
        with pytest.raises(psycopg.OperationalError, match="connection lost"):
            await checkpointer.aput()
        # Initial build + one rebuild; the second failure propagates.
        assert factory.build_calls == 2
        assert factory.pools[0].closed  # closed by reset() before the single retry
        assert not factory.pools[1].closed  # the rebuilt pool stays live

    @pytest.mark.asyncio
    async def test_does_not_retry_non_connection_operation_errors(self) -> None:
        factory = FakeSaverFactory(op_failures=1, op_error=KeyError("bad channel"))
        checkpointer = make_checkpointer(factory)
        with pytest.raises(KeyError, match="bad channel"):
            await checkpointer.aput()
        assert factory.build_calls == 1
        assert not factory.pools[0].closed

    @pytest.mark.asyncio
    async def test_alist_reconnects_on_call_time_connection_error(self) -> None:
        factory = FakeSaverFactory(op_failures=1)
        checkpointer = make_checkpointer(factory)
        items = [item async for item in checkpointer.alist()]
        assert items == ["item-1", "item-2"]
        assert factory.pools[0].closed
        assert factory.build_calls == 2

    @pytest.mark.asyncio
    async def test_reset_closes_pool_and_next_operation_rebuilds(self) -> None:
        factory = FakeSaverFactory()
        checkpointer = make_checkpointer(factory)
        await checkpointer.aput()
        await checkpointer.reset()
        assert factory.pools[0].closed
        await checkpointer.aput()
        assert factory.build_calls == 2
        assert not factory.pools[1].closed


class TestGetCheckpointer:
    def test_returns_lifecycle_wrapped_sqlite_saver_without_url(self) -> None:
        from nexus_ai_agent.adapters.langgraph.lifecycle_recording import (
            LifecycleRecordingSaver,
        )

        checkpointer = get_checkpointer(":memory:")
        # C1 wiring: the runtime passes through the lifecycle wrapper.
        assert isinstance(checkpointer, LifecycleRecordingSaver)
        assert isinstance(checkpointer._saver, AsyncCompatibleSqliteSaver)

    def test_kill_switch_returns_unwrapped_sqlite_saver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_LIFECYCLE_HOOKS_ENABLED", "false")
        settings_module.get_settings.cache_clear()
        checkpointer = get_checkpointer(":memory:")
        assert isinstance(checkpointer, AsyncCompatibleSqliteSaver)

    def test_returns_wrapped_postgres_checkpointer_with_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PR3 four-way matrix: PG + kill-switch on → wrapped (the wrapper is
        # the only place the lifecycle is applied).
        monkeypatch.setenv("NEXUS_DATABASE_URL", URL)
        settings_module.get_settings.cache_clear()
        checkpointer = get_checkpointer("data/langgraph.sqlite")
        assert isinstance(checkpointer, LifecycleRecordingSaver)
        assert isinstance(checkpointer._saver, PostgresCheckpointer)
        assert checkpointer._saver.database_url == NORMALIZED_URL

    def test_postgres_url_ignores_local_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", URL)
        settings_module.get_settings.cache_clear()
        checkpointer = get_checkpointer(":memory:")
        assert isinstance(checkpointer, LifecycleRecordingSaver)
        assert checkpointer._saver.database_url == NORMALIZED_URL

    def test_kill_switch_returns_bare_postgres_checkpointer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", URL)
        monkeypatch.setenv("NEXUS_LIFECYCLE_HOOKS_ENABLED", "false")
        settings_module.get_settings.cache_clear()
        checkpointer = get_checkpointer(":memory:")
        assert isinstance(checkpointer, PostgresCheckpointer)
        assert not isinstance(checkpointer, LifecycleRecordingSaver)
        assert checkpointer.database_url == NORMALIZED_URL
