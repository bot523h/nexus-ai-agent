"""One synchronous engine, resolved like ``get_session`` (audit finding A-02).

Thirteen feature modules used to build their own ``sqlite:///{db_path}``
engine, so a PostgreSQL deployment (``NEXUS_DATABASE_URL``) kept its users and
chats in Postgres while XP, moderation, ads, games, referrals … silently went
to a SQLite file next to the code. ``storage.db.sync_engine`` is now the single
resolution point and every ``_sync_engine`` helper delegates to it.

No database connection is opened for the PostgreSQL case: SQLAlchemy engines
are lazy, so the URL/driver can be asserted without a server.
"""

from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module

#: every feature module that owns a ``_sync_engine`` helper
FEATURE_MODULES = (
    "ads",
    "analytics",
    "anonymous_chat",
    "channel_manager",
    "engagement",
    "force_join",
    "games",
    "gamification",
    "moderation",
    "owner_control",
    "personality",
    "viral_engine",
)

PG_URL = "postgresql://nexus:secret@db.example.internal:5432/nexus"


@pytest.fixture()
def postgres_configured(settings_override: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("NEXUS_DATABASE_URL", PG_URL)
    settings_module.get_settings.cache_clear()
    yield settings_override
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    db_module._sync_engine_url = None  # type: ignore[attr-defined]
    db_module._sync_engine_instance = None  # type: ignore[attr-defined]


def test_sqlite_fallback_follows_nexus_db_path(settings_override: Any) -> None:
    engine = db_module.sync_engine()
    assert engine.url.drivername == "sqlite"
    assert Path(engine.url.database or "") == Path(settings_override.db_path)
    assert db_module.sync_engine() is engine, "cached while the URL is unchanged"


def test_postgres_url_wins_and_uses_the_sync_psycopg_driver(postgres_configured: Any) -> None:
    assert db_module.sync_database_url() == (
        "postgresql+psycopg://nexus:secret@db.example.internal:5432/nexus"
    )
    engine = db_module.sync_engine()
    assert engine.url.drivername == "postgresql+psycopg"
    assert engine.url.host == "db.example.internal"


def test_an_explicit_path_still_pins_sqlite(postgres_configured: Any, tmp_path: Path) -> None:
    pinned = tmp_path / "pinned.sqlite"
    engine = db_module.sync_engine(str(pinned))
    assert engine.url.drivername == "sqlite"
    assert Path(engine.url.database or "") == pinned


def test_switching_the_url_disposes_the_previous_engine(
    settings_override: Any, tmp_path: Path
) -> None:
    first = db_module.sync_engine(str(tmp_path / "a.sqlite"))
    disposed: list[bool] = []
    original = first.dispose
    first.dispose = lambda *a, **k: (disposed.append(True), original(*a, **k))  # type: ignore[method-assign]
    second = db_module.sync_engine(str(tmp_path / "b.sqlite"))
    assert second is not first
    assert disposed == [True]


def test_concurrent_first_use_publishes_exactly_one_engine(
    settings_override: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight workers hitting an empty cache must not orphan seven engines.

    The feature engines call ``sync_engine()`` from ``asyncio.to_thread``
    workers. Before the 2026-10-17 fix the read-check-create-publish sequence
    was unsynchronised: every racing thread saw ``_sync_engine_instance is
    None``, each built its own engine, and only the last one to publish stayed
    in the cache — the others' connection pools were never disposed.

    ``create_engine`` is spied on (``sync_engine`` imports it at call time) and
    the first caller is slowed down, so the race window is deterministic
    instead of a flake that depends on thread scheduling.
    """
    real_create_engine = sqlalchemy.create_engine
    built: list[Any] = []

    def slow_first_create_engine(*args: Any, **kwargs: Any) -> Any:
        if not built:
            time.sleep(0.3)  # widen the window: everyone else sees an empty cache
        engine = real_create_engine(*args, **kwargs)
        built.append(engine)
        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", slow_first_create_engine)
    db_module._sync_engine_url = None  # type: ignore[attr-defined]
    db_module._sync_engine_instance = None  # type: ignore[attr-defined]

    workers = 8
    start = threading.Barrier(workers)
    results: list[Any] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker() -> None:
        try:
            start.wait()
            engine = db_module.sync_engine()
        except BaseException as exc:  # noqa: BLE001 — reported in the assertion below
            with guard:
                errors.append(exc)
            return
        with guard:
            results.append(engine)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], errors
    assert len(built) == 1, f"{len(built)} engines built for a single first use"
    assert len(results) == workers
    assert {id(engine) for engine in results} == {id(db_module._sync_engine_instance)}, (
        "every caller must receive the engine that was published to the cache"
    )


def test_concurrent_first_use_never_returns_an_unpublished_engine(
    settings_override: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stronger form: the returned engine is the cached one *while* racing."""
    real_create_engine = sqlalchemy.create_engine
    built: list[Any] = []

    def spy_create_engine(*args: Any, **kwargs: Any) -> Any:
        # Every caller is slowed down, so without a critical section all
        # twelve observe an empty cache (deterministic, not a scheduling flake).
        time.sleep(0.05)
        engine = real_create_engine(*args, **kwargs)
        built.append(engine)
        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", spy_create_engine)
    db_module._sync_engine_url = None  # type: ignore[attr-defined]
    db_module._sync_engine_instance = None  # type: ignore[attr-defined]

    workers = 12
    start = threading.Barrier(workers)
    returned: list[Any] = []
    guard = threading.Lock()

    def worker() -> None:
        start.wait()
        engine = db_module.sync_engine()
        with guard:
            returned.append(engine)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # One deployment, one engine: every racing caller must be handed the same
    # object, and it must be the object that ended up in the cache.
    assert {id(engine) for engine in returned} == {id(db_module._sync_engine_instance)}, (
        f"{len({id(e) for e in returned})} distinct engines were handed out"
    )
    # A second, synchronised round reuses the published engine: the lock is a
    # first-use guard, not a per-call serialiser.
    built.clear()
    start = threading.Barrier(workers)
    again: list[Any] = []

    def reuse() -> None:
        start.wait()
        again.append(db_module.sync_engine())

    threads = [threading.Thread(target=reuse) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert built == [], "no engine may be built while the cached one matches"
    assert {id(engine) for engine in again} == {id(db_module._sync_engine_instance)}


@pytest.mark.parametrize("name", FEATURE_MODULES)
def test_every_feature_module_delegates_to_the_central_engine(
    postgres_configured: Any, name: str
) -> None:
    module = importlib.import_module(f"nexus_ai_agent.features.{name}")
    engine = module._sync_engine()
    assert engine is db_module.sync_engine(), name
    assert engine.url.drivername == "postgresql+psycopg", name


def test_the_referral_engine_follows_the_configured_backend(postgres_configured: Any) -> None:
    from nexus_ai_agent.features.referral import ReferralEngine

    # Table creation is `checkfirst`, which would need a connection on
    # PostgreSQL — assert the resolution without touching the network.
    engine = ReferralEngine.__new__(ReferralEngine)
    engine._db_path = None
    assert engine._sync_engine().url.drivername == "postgresql+psycopg"


def test_the_referral_engine_creates_its_tables_on_sqlite(settings_override: Any) -> None:
    from sqlalchemy import inspect

    from nexus_ai_agent.features.referral import ReferralEngine

    ReferralEngine()  # db_path=None → NEXUS_DB_PATH
    names = set(inspect(db_module.sync_engine()).get_table_names())
    assert {"referral", "referralcode"} <= names


def test_the_reminder_and_conversation_stores_follow_the_configured_backend(
    postgres_configured: Any,
) -> None:
    from nexus_ai_agent.features.conversation_store import ConversationStore
    from nexus_ai_agent.features.tools import ReminderSystem

    assert ReminderSystem(db_path=None)._engine_ref().url.drivername == "postgresql+psycopg"
    store = ConversationStore.__new__(ConversationStore)  # skip create_all (needs a server)
    store._engine = db_module.sync_engine(None)
    assert store._engine.url.drivername == "postgresql+psycopg"


def test_composition_roots_hand_none_to_engines_when_postgres_is_configured() -> None:
    """``bot/app.py`` and ``bot/feature_handlers.py`` pass ``settings.db_path``
    explicitly; under PostgreSQL that would pin SQLite again."""
    pinned = SimpleNamespace(database_url=None, db_path="data/x.sqlite")
    assert db_module.sync_db_path_for(pinned) == "data/x.sqlite"
    pg = SimpleNamespace(database_url=PG_URL, db_path="data/x.sqlite")
    assert db_module.sync_db_path_for(pg) is None
    blank = SimpleNamespace(database_url="   ", db_path="data/x.sqlite")
    assert db_module.sync_db_path_for(blank) == "data/x.sqlite"


def test_no_module_outside_storage_db_builds_its_own_sqlite_engine() -> None:
    """Structural guard (law R19): the only ``sqlite:///`` engine construction
    in ``src/`` lives in ``storage/db.py``."""
    src = Path(db_module.__file__).resolve().parents[1]
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "db.py" and path.parent.name == "storage":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "``sqlite:///``" in stripped:
                continue
            if "sqlite:///" in stripped or "sqlite+aiosqlite:///" in stripped:
                offenders.append(f"{path.relative_to(src)}:{lineno}: {stripped}")
    assert offenders == [], "\n".join(offenders)
