"""Behavioural tests for the viral-queue surface (audit finding A-03, 2026-10-15).

``/viral_preview`` ``/viral_stats`` ``/viral_post`` used to answer fixed strings
("12 posts sent, 450 likes total", "3 in queue") regardless of what the
``ViralPost`` table held. Every assertion below reads the table or the Bot API
double.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select
from surface_fakes import FakeBot, make_context, make_update

from nexus_ai_agent.bot.surface import viral as surface
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.viral_engine import ViralEngine
from nexus_ai_agent.storage import models as _models  # noqa: F401  (registers tables)
from nexus_ai_agent.storage.models import ViralPost

OWNER = 4242
INTRUDER = 99
GROUP = -100777

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def db(settings_override: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER))
    settings_module.get_settings.cache_clear()
    owner_control._owner_id = None  # type: ignore[attr-defined]
    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    yield settings
    owner_control._owner_id = None  # type: ignore[attr-defined]


def _posts(settings: Any) -> list[ViralPost]:
    engine = create_engine(f"sqlite:///{settings.db_path}")
    try:
        with Session(engine) as session:
            return list(session.exec(select(ViralPost)).all())
    finally:
        engine.dispose()


async def test_preview_and_queue_report_an_empty_queue_honestly(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=GROUP)
    await surface.viral_preview_cmd(update, make_context())
    assert "خالی است" in update.last_reply
    await surface.viral_post_cmd(update, make_context())
    assert "خالی است" in update.last_reply
    await surface.viral_stats_cmd(update, make_context())
    assert "کل: 0" in update.last_reply and "450" not in update.last_reply


async def test_preview_is_the_highest_scored_pending_post_of_this_chat(db: Any) -> None:
    low = ViralEngine.save_post(GROUP, "low " * 10, 1.0)
    high = ViralEngine.save_post(GROUP, "high " * 10, 9.5)
    ViralEngine.save_post(GROUP + 1, "other chat", 99.0)
    update = make_update(user_id=INTRUDER, chat_id=GROUP)
    await surface.viral_preview_cmd(update, make_context())
    assert f"id={high}" in update.last_reply and f"id={low}" not in update.last_reply
    assert "other chat" not in update.last_reply


async def test_stats_count_real_rows(db: Any) -> None:
    posted = ViralEngine.save_post(GROUP, "a", 1.0)
    ViralEngine.save_post(GROUP, "b", 2.0)
    ViralEngine.mark_posted(posted)
    update = make_update(user_id=INTRUDER, chat_id=GROUP)
    await surface.viral_stats_cmd(update, make_context())
    assert "کل: 2" in update.last_reply
    assert "در صف: 1" in update.last_reply
    assert "ارسال‌شده: 1" in update.last_reply


async def test_publish_is_owner_only(db: Any) -> None:
    ViralEngine.save_post(GROUP, "pending", 3.0)
    bot = FakeBot()
    update = make_update(user_id=INTRUDER, chat_id=GROUP)
    await surface.viral_post_cmd(update, make_context(["publish"], bot=bot))
    assert update.last_reply == "⛔ Access denied"
    assert bot.sent == []
    assert all(post.status == "pending" for post in _posts(db))


async def test_publish_sends_the_full_text_and_marks_the_row(db: Any) -> None:
    long_text = "x" * 150  # the queue listing truncates to 100 chars
    post_id = ViralEngine.save_post(GROUP, long_text, 3.0)
    bot = FakeBot()
    update = make_update(user_id=OWNER, chat_id=GROUP)
    await surface.viral_post_cmd(update, make_context(["publish", str(post_id)], bot=bot))
    assert bot.sent == [(GROUP, long_text)]
    (row,) = _posts(db)
    assert row.status == "posted" and row.posted_at is not None
    assert f"#{post_id}" in update.last_reply


async def test_publish_failure_is_reported_and_leaves_the_queue_intact(db: Any) -> None:
    ViralEngine.save_post(GROUP, "pending", 3.0)
    bot = FakeBot(raises=RuntimeError("chat not found"))
    update = make_update(user_id=OWNER, chat_id=GROUP)
    await surface.viral_post_cmd(update, make_context(["publish"], bot=bot))
    assert update.last_reply.startswith("❌ ارسال ناموفق بود")
    assert all(post.status == "pending" for post in _posts(db))


async def test_publish_of_an_unknown_id_is_refused(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=GROUP)
    await surface.viral_post_cmd(update, make_context(["publish", "424242"], bot=FakeBot()))
    assert "در صف این گفتگو نیست" in update.last_reply
