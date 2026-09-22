"""Contract tests for advertisement campaigns (task-159).

The replies must come from the real ``AdManager``, not from the simulated
strings still sitting in the leased ``handlers.py`` stubs.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from nexus_ai_agent.bot.feature_handlers import (
    build_feature_command_handlers,
    build_feature_engines,
)
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.ads import AdManager, AdValidationError
from nexus_ai_agent.storage import models as models_module  # noqa: F401
from nexus_ai_agent.storage.models import AdCampaign

OWNER = 100
CHAT = -1001


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        del kwargs
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


def make_update(user_id: int = OWNER, chat_id: int = CHAT) -> SimpleNamespace:
    message = SimpleNamespace(reply_text=AsyncMock(), reply_to_message=None)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
        edited_message=None,
        callback_query=None,
    )


def make_context(args: list[str] | None = None, bot: Any = None) -> SimpleNamespace:
    return SimpleNamespace(args=args or [], bot=bot)


def reply_text(update: SimpleNamespace) -> str:
    return str(update.message.reply_text.call_args.args[0])


@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    db_file = str(tmp_path / "ads.sqlite")
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("NEXUS_DB_PATH", db_file)
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER))
    settings_module.get_settings.cache_clear()
    owner_control._owner_id = None
    settings = Settings().model_copy(update={"db_path": db_file, "owner_telegram_id": OWNER})
    engines = build_feature_engines(settings)
    try:
        yield SimpleNamespace(
            db=db_file,
            settings=settings,
            engines=engines,
            cmds=build_feature_command_handlers(engines, settings),
        )
    finally:
        engines.ads.close()
        engines.channel.close()
        engines.onboarding.close()
        owner_control._owner_id = None
        settings_module.get_settings.cache_clear()


def _count(db: str) -> int:
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as session:
        count = len(session.query(AdCampaign).all())
    engine.dispose()
    return count


async def test_owner_create_persists_and_lists(env: SimpleNamespace) -> None:
    update = make_update()
    await env.cmds["ad_create"](update, make_context(["6", "3", "سلام", "دنیا"]))
    assert "کمپین #" in reply_text(update)
    assert "impression" not in reply_text(update).lower()
    assert _count(env.db) == 1
    listed = make_update()
    await env.cmds["ad_list"](listed, make_context())
    text = reply_text(listed)
    assert "سلام دنیا" in text
    assert "[active]" in text


async def test_bad_input_writes_no_row(env: SimpleNamespace) -> None:
    cases = [
        ["0", "1", "متن"],
        ["200", "1", "متن"],
        ["6", "-1", "متن"],
        ["6", "1", "   "],
        ["abc", "1", "متن"],
        ["6"],
    ]
    for args in cases:
        update = make_update()
        await env.cmds["ad_create"](update, make_context(args))
        assert reply_text(update).startswith("❌")
    assert _count(env.db) == 0


async def test_non_owner_cannot_create_or_delete(env: SimpleNamespace) -> None:
    bot = FakeBot()
    env.engines.ads.bind(bot)
    update = make_update(user_id=999)
    await env.cmds["ad_create"](update, make_context(["6", "1", "نباید ذخیره شود"], bot=bot))
    assert reply_text(update) == "⛔ Access denied"
    assert _count(env.db) == 0
    assert bot.sent == []
    campaign_id = env.engines.ads.create_campaign(CHAT, "واقعی", 6, 1, OWNER)
    denied = make_update(user_id=999)
    await env.cmds["ad_delete"](denied, make_context([str(campaign_id)]))
    assert reply_text(denied) == "⛔ Access denied"
    assert env.engines.ads.get_campaign(campaign_id) is not None


async def test_unset_owner_id_denies_everyone(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEXUS_OWNER_TELEGRAM_ID", raising=False)
    settings_module.get_settings.cache_clear()
    owner_control._owner_id = None
    for user_id in (0, OWNER):
        update = make_update(user_id=user_id)
        await env.cmds["ad_stats"](update, make_context())
        assert reply_text(update) == "⛔ Access denied"
    assert _count(env.db) == 0


async def test_claim_due_is_at_most_once_including_threads(env: SimpleNamespace) -> None:
    manager: AdManager = env.engines.ads
    campaign_id = manager.create_campaign(CHAT, "یک‌بار", 6, 0, OWNER)
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(lambda _: manager.claim_due(), range(4)))
    claimed = [item for batch in batches for item in batch]
    assert len(claimed) == 1
    assert claimed[0]["id"] == campaign_id
    assert manager.claim_due() == []
    stored = manager.get_campaign(campaign_id)
    assert stored is not None
    assert stored["repeat_count"] == 1
    assert stored["status"] == "active"


async def test_max_repeats_completes_and_naive_past_is_due(env: SimpleNamespace) -> None:
    manager: AdManager = env.engines.ads
    campaign_id = manager.create_campaign(CHAT, "تمام", 2, 1, OWNER)
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        row = session.get(AdCampaign, campaign_id)
        assert row is not None
        row.next_run = datetime(2020, 1, 1)  # naive, as SQLite often returns
        session.add(row)
        session.commit()
    engine.dispose()
    claimed = manager.claim_due()
    assert len(claimed) == 1
    stored = manager.get_campaign(campaign_id)
    assert stored is not None
    assert stored["status"] == "completed"
    assert stored["next_run"] is None
    assert manager.get_due_campaigns() == []


async def test_pause_skips_delivery_and_resume_is_due(env: SimpleNamespace) -> None:
    manager: AdManager = env.engines.ads
    campaign_id = manager.create_campaign(CHAT, "توقف", 4, 0, OWNER)
    assert manager.pause_campaign(campaign_id) is True
    assert manager.claim_due() == []
    assert manager.resume_campaign(campaign_id) is True
    assert len(manager.claim_due()) == 1


async def test_unbound_delivery_does_not_claim(env: SimpleNamespace) -> None:
    manager: AdManager = env.engines.ads
    campaign_id = manager.create_campaign(CHAT, "بی‌ربات", 4, 0, OWNER)
    assert await manager.deliver_due() == 0
    stored = manager.get_campaign(campaign_id)
    assert stored is not None
    assert stored["repeat_count"] == 0


async def test_deliver_due_sends_once_even_if_send_fails(env: SimpleNamespace) -> None:
    manager: AdManager = env.engines.ads
    bot = FakeBot()
    manager.bind(bot)
    manager.create_campaign(CHAT, "ارسال واقعی", 8, 0, OWNER)
    assert await manager.deliver_due() == 1
    assert bot.sent == [(CHAT, "ارسال واقعی")]
    assert await manager.deliver_due() == 0
    assert len(bot.sent) == 1

    failing = FakeBot(fail=True)
    manager.bind(failing)
    manager.create_campaign(CHAT, "شکست", 8, 0, OWNER)
    assert await manager.deliver_due() == 0
    assert failing.sent == []
    assert await manager.deliver_due() == 0


async def test_delete_accepts_persian_digits(env: SimpleNamespace) -> None:
    campaign_id = env.engines.ads.create_campaign(CHAT, "حذف", 6, 0, OWNER)
    persian = str(campaign_id).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    update = make_update()
    await env.cmds["ad_delete"](update, make_context([persian]))
    assert "حذف شد" in reply_text(update)
    assert env.engines.ads.get_campaign(campaign_id) is None


async def test_campaign_cap_and_stats_have_no_fake_impressions(env: SimpleNamespace) -> None:
    manager: AdManager = env.engines.ads
    for index in range(20):
        manager.create_campaign(CHAT, f"کمپین {index}", 6, 0, OWNER)
    with pytest.raises(AdValidationError):
        manager.create_campaign(CHAT, "اضافه", 6, 0, OWNER)
    update = make_update()
    await env.cmds["ad_stats"](update, make_context())
    text = reply_text(update)
    assert "کل 20" in text
    assert "فعال 20" in text
    assert "impression" not in text.lower()
    assert "5k" not in text


async def test_loop_start_stop_is_idempotent(env: SimpleNamespace) -> None:
    manager: AdManager = env.engines.ads
    manager.bind(FakeBot())
    manager.poll_seconds = 30
    await manager.start()
    await manager.start()
    assert manager.snapshot()["running"] is True
    await manager.stop()
    await manager.stop()
    assert manager.snapshot()["running"] is False


def test_future_campaign_is_not_due(env: SimpleNamespace) -> None:
    manager: AdManager = env.engines.ads
    campaign_id = manager.create_campaign(CHAT, "آینده", 6, 0, OWNER)
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        row = session.get(AdCampaign, campaign_id)
        assert row is not None
        row.next_run = datetime.now(timezone.utc) + timedelta(hours=3)
        session.add(row)
        session.commit()
    engine.dispose()
    assert manager.claim_due() == []
