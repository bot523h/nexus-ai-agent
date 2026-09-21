"""B4 — ``bot`` injection for ForceJoinManager / AnonymousChatManager + gate fixes."""

from __future__ import annotations

import pytest
from surface_fakes import FakeBot, FakeCallbackQuery, make_context, make_update

from nexus_ai_agent.bot.surface import community as surface
from nexus_ai_agent.features import force_join as fj
from nexus_ai_agent.features.anonymous_chat import AnonymousChatManager
from nexus_ai_agent.features.force_join import ForceJoinManager


@pytest.fixture(autouse=True)
def _fresh() -> None:
    surface.reset_community()
    fj._membership_cache.clear()


# ── Anonymous chat (feature) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_anon_pairing_relay_and_leave(feature_db) -> None:
    bot = FakeBot()
    mgr = AnonymousChatManager()
    mgr.bind_bot(bot)

    assert "صف انتظار" in await mgr.join_queue(1)
    assert "همچنان" in await mgr.join_queue(1)  # no duplicate queue entry
    assert mgr.is_waiting(1)

    await mgr.join_queue(2)
    assert mgr.partner_of(1) == 2 and mgr.partner_of(2) == 1
    assert not mgr.is_waiting(1)
    # both sides were notified about the match
    assert {m["chat_id"] for m in bot.sent} == {1, 2}

    bot.sent.clear()
    assert await mgr.send_anon_message(1, "سلام") is True
    assert bot.sent[-1]["chat_id"] == 2 and "سلام" in bot.sent[-1]["text"]

    await mgr.leave_chat(1)
    assert mgr.partner_of(2) is None
    assert await mgr.send_anon_message(2, "x") is False


@pytest.mark.asyncio
async def test_anon_leave_queue_and_stale_entries(feature_db) -> None:
    mgr = AnonymousChatManager(FakeBot())
    await mgr.join_queue(1)
    assert "صف انتظار خارج" in await mgr.leave_chat(1)
    assert not mgr.is_waiting(1)
    assert "هیچ چت" in await mgr.leave_chat(1)


@pytest.mark.asyncio
async def test_anon_requires_bot_for_relay(feature_db) -> None:
    mgr = AnonymousChatManager()
    with pytest.raises(RuntimeError):
        mgr._require_bot()


# ── Anonymous chat (surface) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_anon_surface_binds_bot_from_context_and_routes_text(feature_db) -> None:
    bot = FakeBot()
    await surface.anon_start_cmd(make_update(user_id=1), make_context(bot=bot))
    await surface.anon_start_cmd(make_update(user_id=2), make_context(bot=bot))
    assert surface.get_anon_manager().bot is bot
    assert surface.get_anon_manager().partner_of(1) == 2

    bot.sent.clear()
    update = make_update(user_id=1, text="hi there")
    assert await surface.route_anon_text(update, make_context(bot=bot)) is True
    assert bot.sent[-1]["chat_id"] == 2

    # commands / group chats / unpaired users are never relayed
    assert (
        await surface.route_anon_text(make_update(user_id=1, text="/help"), make_context(bot=bot))
        is False
    )
    assert (
        await surface.route_anon_text(
            make_update(user_id=1, chat_id=-5, text="x"), make_context(bot=bot)
        )
        is False
    )
    assert (
        await surface.route_anon_text(make_update(user_id=3, text="x"), make_context(bot=bot))
        is False
    )

    update = make_update(user_id=2)
    await surface.anon_stop_cmd(update, make_context(bot=bot))
    assert update.message.last.startswith("🚪") or "پایان" in update.message.last


@pytest.mark.asyncio
async def test_anon_start_only_in_private_chat(feature_db) -> None:
    update = make_update(user_id=1, chat_id=-100)
    await surface.anon_start_cmd(update, make_context(bot=FakeBot()))
    assert "خصوصی" in update.message.last


# ── Force join (feature) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_membership_fails_closed_without_bot(feature_db) -> None:
    mgr = ForceJoinManager()
    assert await mgr.check_membership(1, "@chan") is False


@pytest.mark.asyncio
async def test_check_membership_caches_only_positive_results(feature_db) -> None:
    bot = FakeBot(members={("@chan", 1): "member"})
    mgr = ForceJoinManager(bot)
    assert await mgr.check_membership(2, "@chan") is False
    bot.members[("@chan", 2)] = "member"
    assert await mgr.check_membership(2, "@chan") is True  # negative result was not cached
    del bot.members[("@chan", 2)]
    assert await mgr.check_membership(2, "@chan") is True  # positive result is cached
    mgr.invalidate_cache(2)
    assert await mgr.check_membership(2, "@chan") is False
    assert await mgr.check_membership(1, "@chan") is True


@pytest.mark.asyncio
async def test_should_block_engages_when_enabled(feature_db) -> None:
    """Regression: ``ForceJoinConfig.enabled is True`` never matched a row."""
    mgr = ForceJoinManager(FakeBot(members={("@club", 1): "member"}))
    assert await mgr.should_block(2, chat_id=-1) is False  # nothing configured
    assert ForceJoinManager.required_channel(-1) is None

    ForceJoinManager.set_config(-1, enabled=True, channel_username="@club")
    assert ForceJoinManager.required_channel(-1) == "@club"
    assert ForceJoinManager.required_channel(-999) == "@club"  # bot-wide fallback
    assert await mgr.should_block(2, chat_id=-1) is True
    assert await mgr.should_block(1, chat_id=-1) is False
    assert await mgr.should_block(2, "start", chat_id=-1) is False  # public command

    ForceJoinManager.set_config(-2, enabled=False, channel_username="@club")
    assert ForceJoinManager.required_channel(-2) is None  # per-chat off wins
    ForceJoinManager.set_config(-1, enabled=False)
    assert await mgr.should_block(2, chat_id=-1) is False


# ── Force join (surface) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forcejoin_gate_blocks_non_members_and_passes_members(feature_db) -> None:
    ForceJoinManager.set_config(-1, enabled=True, channel_username="@club")
    bot = FakeBot(members={("@club", 1): "member"})

    update = make_update(user_id=2, chat_id=-1, text="hello")
    assert await surface.forcejoin_gate(update, make_context(bot=bot)) is True
    text, kwargs = update.message.replies[-1]
    assert "@club" in text and kwargs.get("reply_markup") is not None

    update = make_update(user_id=1, chat_id=-1, text="hello")
    assert await surface.forcejoin_gate(update, make_context(bot=bot)) is False
    assert update.message.replies == []


@pytest.mark.asyncio
async def test_forcejoin_verify_callback(feature_db) -> None:
    ForceJoinManager.set_config(-1, enabled=True, channel_username="@club")
    bot = FakeBot()
    query = FakeCallbackQuery("forcejoin_verify", user_id=5)
    update = make_update(user_id=5, chat_id=-1, callback=query, message=False)
    await surface.forcejoin_verify_callback(update, make_context(bot=bot))
    assert query.edits[-1].startswith("❌")

    bot.members[("@club", 5)] = "member"
    await surface.forcejoin_verify_callback(update, make_context(bot=bot))
    assert query.edits[-1].startswith("✅")
