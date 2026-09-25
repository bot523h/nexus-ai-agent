"""Flood-isolation and thread-offload contracts for ``ModerationEngine``.

Closes the two mutation gaps recorded by FORENSIC_REPORT_task122 (baseline
``e405e07``):

* **Mutation C** — flood key collapsed to ``user_id`` only.  The tracker must
  be keyed by ``(chat_id, user_id)``: flooding in one chat must never
  contaminate another chat of the same user, and two users in one chat must
  never contaminate each other.  Both single-field mutations fail at least
  one test here; only the composite key passes all of them.
* **Thread offload** — ``ModerationEngine.*`` performs synchronous SQLAlchemy
  work and builds its engine *inside* every call (``_sync_engine()``), so no
  engine/session object may be retained on the class.  That is exactly the
  property that makes the ``asyncio.to_thread`` offload in the bot layer safe:
  nothing loop-bound or thread-bound crosses the boundary.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.moderation import ModerationEngine
from nexus_ai_agent.storage import models as _models  # noqa: F401

OWNER_ID = 1001
USER_A = 2001
USER_B = 2002
CHAT_1 = -1001
CHAT_2 = -1002


@pytest.fixture()
def db(settings_override: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Temporary SQLite database and owner identity (mirrors the wiring suite)."""
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER_ID))
    settings_module.get_settings.cache_clear()
    owner_control._owner_id = None
    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture()
def flood_tracker() -> Any:
    """Clear the class-level flood tracker around each test (it is shared state)."""
    ModerationEngine._flood_tracker.clear()
    yield ModerationEngine._flood_tracker
    ModerationEngine._flood_tracker.clear()


# ═══════════════════════════════════════════════════════════════════════
# Mutation C: flood isolation keyed by (chat_id, user_id)
# ═══════════════════════════════════════════════════════════════════════


def test_flood_same_user_does_not_cross_chats(db: Any, flood_tracker: Any) -> None:
    """User A flooding Chat 1 must not be flagged in Chat 2 (kills key=user_id)."""
    for _ in range(ModerationEngine.FLOOD_MAX_MESSAGES):
        assert ModerationEngine.is_flooding(USER_A, CHAT_1) is False
    # 6th message inside the window in Chat 1 floods.
    assert ModerationEngine.is_flooding(USER_A, CHAT_1) is True

    # Same user, different chat ⇒ fresh window, NOT flooded.
    assert ModerationEngine.is_flooding(USER_A, CHAT_2) is False
    assert ModerationEngine.is_flooding(USER_A, CHAT_2) is False

    # The other chat's traffic must not have perturbed Chat 1's own window.
    assert ModerationEngine.is_flooding(USER_A, CHAT_1) is True


def test_flood_same_chat_does_not_cross_users(db: Any, flood_tracker: Any) -> None:
    """User A flooding Chat 1 must not flag User B in the same chat (kills key=chat_id)."""
    for _ in range(ModerationEngine.FLOOD_MAX_MESSAGES):
        assert ModerationEngine.is_flooding(USER_A, CHAT_1) is False
    assert ModerationEngine.is_flooding(USER_A, CHAT_1) is True

    # Different user, same chat ⇒ independent window, NOT flooded.
    assert ModerationEngine.is_flooding(USER_B, CHAT_1) is False
    assert ModerationEngine.is_flooding(USER_B, CHAT_1) is False

    assert ModerationEngine.is_flooding(USER_A, CHAT_1) is True


def test_check_message_flood_is_chat_scoped(db: Any, flood_tracker: Any) -> None:
    """End-to-end: check_message flood verdict must be scoped to one chat."""
    ModerationEngine.set_config(CHAT_1, anti_flood=True)
    ModerationEngine.set_config(CHAT_2, anti_flood=True)

    results = [
        ModerationEngine.check_message(USER_A, CHAT_1, "سلام")
        for _ in range(ModerationEngine.FLOOD_MAX_MESSAGES + 1)
    ]
    flooded = results[-1]
    assert flooded["allowed"] is False
    assert "flood" in flooded["reasons"]

    # Same user, next chat: no flood carry-over through the ingress path.
    fresh = ModerationEngine.check_message(USER_A, CHAT_2, "سلام")
    assert fresh["allowed"] is True
    assert fresh["reasons"] == []


def test_flood_tracker_stores_composite_keys(db: Any, flood_tracker: Any) -> None:
    """The in-memory tracker itself must hold (chat_id, user_id) keys."""
    ModerationEngine.is_flooding(USER_A, CHAT_1)
    ModerationEngine.is_flooding(USER_B, CHAT_1)
    ModerationEngine.is_flooding(USER_A, CHAT_2)
    assert set(flood_tracker.keys()) == {(CHAT_1, USER_A), (CHAT_1, USER_B), (CHAT_2, USER_A)}


# ═══════════════════════════════════════════════════════════════════════
# Thread offload: nothing loop-bound/thread-bound is retained or shared
# ═══════════════════════════════════════════════════════════════════════


def _sqlalchemy_class_attrs() -> list[str]:
    """Names of ModerationEngine class attributes that are SQLAlchemy objects."""
    return [
        name
        for name, value in vars(ModerationEngine).items()
        if type(value).__module__.startswith("sqlalchemy")
    ]


def test_engine_class_holds_no_shared_db_state(db: Any) -> None:
    """No engine/session may be cached on the class (only plain data + methods)."""
    ModerationEngine.set_config(CHAT_1, anti_spam=True)
    ModerationEngine.mute_user(USER_A, CHAT_1, duration_minutes=5)
    assert _sqlalchemy_class_attrs() == []


def test_check_message_executes_on_a_worker_thread(db: Any, flood_tracker: Any) -> None:
    """Simulate the production offload: the whole engine call runs in a thread.

    The worker thread performs its own engine/session lifecycle (created inside
    the call), proving the to_thread pattern needs nothing from the caller's
    loop and leaves nothing behind on the class.
    """
    ModerationEngine.set_config(CHAT_1, anti_spam=True, anti_flood=True)

    observed: dict[str, Any] = {}

    def worker() -> None:
        observed["thread_id"] = threading.get_ident()
        observed["result"] = ModerationEngine.check_message(USER_B, CHAT_1, "پیام معمولی")
        observed["sqlalchemy_attrs"] = _sqlalchemy_class_attrs()

    thread = threading.Thread(target=worker, name="moderation-offload-sim")
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "moderation worker thread did not finish"

    # It genuinely ran on a different thread than the (loop) caller…
    assert observed["thread_id"] != threading.get_ident()
    # …completed a full DB round-trip successfully there…
    assert observed["result"] == {"allowed": True, "reasons": [], "action": "allow"}
    # …and retained no SQLAlchemy object on the shared class.
    assert observed["sqlalchemy_attrs"] == []

    # Only plain (chat_id, user_id) keys cross the thread boundary.
    assert set(flood_tracker.keys()) == {(CHAT_1, USER_B)}
