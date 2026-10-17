"""Behavioural tests for the moderation surface (audit finding A-03, 2026-10-15).

``bot/handlers.py`` used to answer ``/mod_config`` ``/warn`` ``/mute``
``/unmute`` ``/reputation`` with fixed sentences — "⚠️ User warned (1/3)." for
*any* caller and *any* target — and ``/mod_on`` stored a configuration that no
message path consulted. Every assertion below reads something observable: the
database row the engine wrote, the Bot API call the double recorded, or the
denial a non-owner receives.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select
from surface_fakes import FakeBot, FakeMessage, make_context, make_update

from nexus_ai_agent.bot.surface import moderation as surface
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.moderation import ModerationEngine
from nexus_ai_agent.storage import models as _models  # noqa: F401  (registers tables)
from nexus_ai_agent.storage.models import ModerationConfig, UserReputation

OWNER = 4242
INTRUDER = 99
TARGET = 555
GROUP = -100123

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
    ModerationEngine._flood_tracker.clear()
    yield settings
    owner_control._owner_id = None  # type: ignore[attr-defined]


def _rows(settings: Any, model: Any) -> list[Any]:
    engine = create_engine(f"sqlite:///{settings.db_path}")
    try:
        with Session(engine) as session:
            return list(session.exec(select(model)).all())
    finally:
        engine.dispose()


# ── /mod_config ─────────────────────────────────────────────────────────────


async def test_mod_config_is_owner_only(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=GROUP)
    await surface.mod_config_cmd(update, make_context(["anti_spam", "off"]))
    assert update.last_reply == "⛔ Access denied"
    assert _rows(db, ModerationConfig) == []


async def test_mod_config_writes_the_row_and_reports_it(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=GROUP)
    await surface.mod_config_cmd(update, make_context(["link_filter", "on"]))
    (cfg,) = _rows(db, ModerationConfig)
    assert cfg.chat_id == GROUP and cfg.link_filter is True
    assert "link_filter: ✅" in update.last_reply

    await surface.mod_config_cmd(update, make_context(["max_warnings", "5"]))
    (cfg,) = _rows(db, ModerationConfig)
    assert cfg.max_warnings == 5
    assert "max_warnings: 5" in update.last_reply


async def test_mod_config_without_arguments_shows_state_or_absence(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=GROUP)
    await surface.mod_config_cmd(update, make_context([]))
    assert "فعال نیست" in update.last_reply
    assert _rows(db, ModerationConfig) == []


@pytest.mark.parametrize(
    "args",
    [
        ["anti_spam"],
        ["anti_spam", "maybe"],
        ["max_warnings", "0"],
        ["mute_minutes", "x"],
        ["nope", "on"],
    ],
)
async def test_mod_config_rejects_malformed_input(db: Any, args: list[str]) -> None:
    update = make_update(user_id=OWNER, chat_id=GROUP)
    await surface.mod_config_cmd(update, make_context(args))
    assert update.last_reply.startswith("🛡️ استفاده:")
    assert _rows(db, ModerationConfig) == []


# ── /warn /mute /unmute ─────────────────────────────────────────────────────


async def test_warn_is_owner_only_and_writes_nothing_for_intruders(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=GROUP)
    await surface.warn_cmd(update, make_context([str(TARGET)]))
    assert update.last_reply == "⛔ Access denied"
    assert _rows(db, UserReputation) == []


async def test_warn_counts_against_the_configured_ceiling_and_mutes(db: Any) -> None:
    ModerationEngine.set_config(GROUP, max_warnings=2, mute_duration_minutes=7)
    bot = FakeBot()
    update = make_update(user_id=OWNER, chat_id=GROUP)

    await surface.warn_cmd(update, make_context([str(TARGET)], bot=bot))
    assert update.last_reply == f"⚠️ کاربر {TARGET} اخطار گرفت (1/2)."
    assert bot.restricted == []

    await surface.warn_cmd(update, make_context([str(TARGET)], bot=bot))
    assert "(2/2)" in update.last_reply and "بی‌صدا شد" in update.last_reply
    (rep,) = _rows(db, UserReputation)
    assert rep.warnings == 2 and rep.is_muted is True
    assert bot.restricted[0]["user_id"] == TARGET
    assert bot.restricted[0]["permissions"]["can_send_messages"] is False


async def test_warn_takes_the_target_from_the_quoted_message(db: Any) -> None:
    quoted = FakeMessage("spam", author_id=TARGET)
    update = make_update(user_id=OWNER, chat_id=GROUP, reply_to=quoted)
    await surface.warn_cmd(update, make_context([]))
    (rep,) = _rows(db, UserReputation)
    assert rep.user_id == TARGET and rep.warnings == 1


async def test_mute_reports_whether_telegram_enforced_it(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=GROUP)
    admin_bot = FakeBot()
    await surface.mute_cmd(update, make_context([str(TARGET), "15"], bot=admin_bot))
    assert update.last_reply == f"🔇 کاربر {TARGET} به مدت 15 دقیقه بی‌صدا شد."
    assert admin_bot.restricted[0]["until_date"] is not None
    assert ModerationEngine.is_muted(TARGET, GROUP) is True

    powerless = FakeBot(raises=RuntimeError("Not enough rights"))
    await surface.mute_cmd(update, make_context([str(TARGET)], bot=powerless))
    assert "ربات ادمین نیست" in update.last_reply


async def test_unmute_lifts_both_sides(db: Any) -> None:
    ModerationEngine.mute_user(TARGET, GROUP, 30)
    bot = FakeBot()
    update = make_update(user_id=OWNER, chat_id=GROUP)
    await surface.unmute_cmd(update, make_context([str(TARGET)], bot=bot))
    assert update.last_reply == f"🔊 بی‌صدایی کاربر {TARGET} برداشته شد."
    assert ModerationEngine.is_muted(TARGET, GROUP) is False
    assert bot.restricted[0]["permissions"]["can_send_messages"] is True


async def test_mute_rejects_out_of_range_durations(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=GROUP)
    await surface.mute_cmd(update, make_context([str(TARGET), "0"]))
    assert "۱ تا ۱۰۰۸۰" in update.last_reply
    assert _rows(db, UserReputation) == []


# ── /reputation ─────────────────────────────────────────────────────────────


async def test_reputation_shows_the_callers_real_record(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=GROUP)
    await surface.reputation_cmd(update, make_context([]))
    assert "هنوز سابقه‌ای" in update.last_reply

    ModerationEngine.add_warning(INTRUDER, GROUP, "test")
    await surface.reputation_cmd(update, make_context([]))
    assert "اخطارها: 1" in update.last_reply
    assert "85/100" not in update.last_reply


async def test_reputation_of_someone_else_is_owner_only(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=GROUP)
    await surface.reputation_cmd(update, make_context([str(TARGET)]))
    assert update.last_reply == "⛔ Access denied"


# ── ingress ─────────────────────────────────────────────────────────────────


async def test_ingress_ignores_chats_without_configuration(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=GROUP, text="http://spam.example")
    verdict = await surface.evaluate_message(update, make_context(bot=FakeBot()))
    assert verdict["allowed"] is True
    assert update.message.deleted is False
    assert _rows(db, UserReputation) == []


async def test_ingress_removes_a_violation_warns_and_then_mutes(db: Any) -> None:
    ModerationEngine.set_config(GROUP, link_filter=True, max_warnings=2, anti_flood=False)
    bot = FakeBot()
    context = make_context(bot=bot)

    first = make_update(user_id=INTRUDER, chat_id=GROUP, text="buy now http://spam.example")
    verdict = await surface.evaluate_message(first, context)
    assert verdict["allowed"] is False and verdict["action"] == "warn"
    assert first.message.deleted is True
    assert bot.sent[-1][1].startswith("⚠️ اخطار")
    assert bot.restricted == []

    second = make_update(user_id=INTRUDER, chat_id=GROUP, text="again http://spam.example")
    verdict = await surface.evaluate_message(second, context)
    assert verdict["action"] == "mute"
    assert bot.restricted[0]["user_id"] == INTRUDER
    assert ModerationEngine.is_muted(INTRUDER, GROUP) is True

    # A muted user's next message is blocked silently (no new warning).
    third = make_update(user_id=INTRUDER, chat_id=GROUP, text="hello?")
    verdict = await surface.evaluate_message(third, context)
    assert verdict == {"allowed": False, "reasons": ["muted"], "action": "block"}
    assert third.message.deleted is True


async def test_ingress_never_moderates_the_owner_or_private_chats(db: Any) -> None:
    ModerationEngine.set_config(GROUP, link_filter=True)
    owner_update = make_update(user_id=OWNER, chat_id=GROUP, text="http://example.com")
    assert (await surface.evaluate_message(owner_update, make_context()))["allowed"] is True

    ModerationEngine.set_config(INTRUDER, link_filter=True)  # a (positive) private chat id
    private = make_update(user_id=INTRUDER, chat_id=INTRUDER, text="http://example.com")
    assert (await surface.evaluate_message(private, make_context()))["allowed"] is True
    assert _rows(db, UserReputation) == []


# ── engine regression found while wiring the surface ───────────────────────


async def test_is_muted_survives_a_round_trip_through_sqlite(db: Any) -> None:
    """``mute_until`` came back naive from SQLite and was compared with an
    aware ``datetime`` → ``TypeError`` on every muted user (pre-audit)."""
    ModerationEngine.mute_user(TARGET, GROUP, 30)
    assert ModerationEngine.is_muted(TARGET, GROUP) is True

    # An expired mute is lifted lazily on read.
    engine = create_engine(f"sqlite:///{db.db_path}")
    try:
        with Session(engine) as session:
            rep = session.exec(select(UserReputation)).one()
            rep.mute_until = rep.mute_until - timedelta(hours=1)
            session.add(rep)
            session.commit()
    finally:
        engine.dispose()
    assert ModerationEngine.is_muted(TARGET, GROUP) is False


# ── media captions are text too (review finding, 2026-10-17) ───────────────


def _photo() -> tuple[Any, ...]:
    """A one-element ``message.photo`` tuple, as Telegram delivers it."""
    return (type("PhotoSize", (), {"file_id": "p", "file_size": 1_000})(),)


async def test_a_captioned_photo_is_moderated_on_its_caption(db: Any) -> None:
    """A spam link in a photo caption used to be invisible to the ingress."""
    ModerationEngine.set_config(GROUP, link_filter=True, anti_flood=False)
    bot = FakeBot()
    update = make_update(
        user_id=INTRUDER,
        chat_id=GROUP,
        caption="look at this http://spam.example",
        photo=_photo(),
    )
    assert update.message.text is None, "a photo carries no message.text"

    verdict = await surface.evaluate_message(update, make_context(bot=bot))

    assert verdict["allowed"] is False
    assert update.message.deleted is True
    assert bot.sent[-1][1].startswith("⚠️ اخطار")


async def test_a_caption_without_a_violation_is_allowed(db: Any) -> None:
    ModerationEngine.set_config(GROUP, link_filter=True, anti_flood=False)
    update = make_update(user_id=INTRUDER, chat_id=GROUP, caption="عکس قشنگی شد", photo=_photo())
    verdict = await surface.evaluate_message(update, make_context(bot=FakeBot()))
    assert verdict["allowed"] is True
    assert update.message.deleted is False


async def test_bare_media_stays_on_the_early_return_path(db: Any) -> None:
    """No text, no caption → allowed without touching the database."""
    ModerationEngine.set_config(GROUP, link_filter=True, profanity_filter=True)
    update = make_update(user_id=INTRUDER, chat_id=GROUP, photo=_photo())
    verdict = await surface.evaluate_message(update, make_context(bot=FakeBot()))
    assert verdict == {"allowed": True, "reasons": [], "action": "allow"}
    assert update.message.deleted is False
    assert _rows(db, UserReputation) == []


async def test_the_owner_is_never_moderated_on_a_caption_either(db: Any) -> None:
    ModerationEngine.set_config(GROUP, link_filter=True)
    update = make_update(
        user_id=OWNER,
        chat_id=GROUP,
        caption="http://example.com",
        photo=_photo(),
    )
    assert (await surface.evaluate_message(update, make_context()))["allowed"] is True
    assert update.message.deleted is False


# ── the group -1 gate itself: stop or pass (review finding, 2026-10-17) ────


async def test_the_gate_stops_a_violating_message_and_passes_a_clean_one(db: Any) -> None:
    """``_moderation_gate`` is what keeps a denied message from group 0."""
    from telegram.ext import ApplicationHandlerStop

    from nexus_ai_agent.bot.handlers import _moderation_gate

    ModerationEngine.set_config(GROUP, link_filter=True, anti_flood=False)

    denied = make_update(user_id=INTRUDER, chat_id=GROUP, text="http://spam.example")
    with pytest.raises(ApplicationHandlerStop):
        await _moderation_gate(denied, make_context(bot=FakeBot()))

    clean = make_update(user_id=INTRUDER, chat_id=GROUP, text="سلام")
    await _moderation_gate(clean, make_context(bot=FakeBot()))  # must not raise


async def test_the_gate_stops_a_muted_users_message(db: Any) -> None:
    from telegram.ext import ApplicationHandlerStop

    from nexus_ai_agent.bot.handlers import _moderation_gate

    ModerationEngine.set_config(GROUP)
    ModerationEngine.mute_user(INTRUDER, GROUP, 30)
    update = make_update(user_id=INTRUDER, chat_id=GROUP, text="hello?")
    with pytest.raises(ApplicationHandlerStop):
        await _moderation_gate(update, make_context(bot=FakeBot()))


async def test_the_gate_lets_a_command_through_when_it_is_allowed(db: Any) -> None:
    """Commands are now *seen* by the gate — but only a verdict stops them."""
    from nexus_ai_agent.bot.handlers import _moderation_gate

    ModerationEngine.set_config(GROUP, link_filter=True, anti_flood=False)
    update = make_update(user_id=INTRUDER, chat_id=GROUP, text="/start")
    await _moderation_gate(update, make_context(bot=FakeBot()))  # must not raise

    offending = make_update(user_id=INTRUDER, chat_id=GROUP, text="/start http://spam.example")
    from telegram.ext import ApplicationHandlerStop

    with pytest.raises(ApplicationHandlerStop):
        await _moderation_gate(offending, make_context(bot=FakeBot()))
