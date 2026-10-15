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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
