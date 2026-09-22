"""Behavioural tests for the advertisement surface (``bot/surface/ads.py``).

The point of the file is the difference between a *stub* and a *wiring*.
``bot/handlers.py`` used to answer the six ``/ad_*`` commands with fixed strings
— the same sentence whether the database was written or not — while
:class:`~nexus_ai_agent.features.ads.AdManager` had no importer anywhere in
``src/``. Every test below therefore reads the **row**, not only the reply: if
the handler ever goes back to a literal, these turn red.

They also pin the authorisation the engine does not provide itself
(``pause/resume/delete`` take a bare ``campaign_id``), and the refusal to
render metrics the schema cannot produce.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select
from surface_fakes import make_context, make_update

from nexus_ai_agent.bot.surface import ads as surface
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.ads import AdManager
from nexus_ai_agent.storage import models as _models  # noqa: F401  (registers tables)
from nexus_ai_agent.storage.models import AdCampaign

OWNER = 4242
INTRUDER = 99
CHAT = 10
OTHER_CHAT = 11

#: The literal replies this batch removed. None may come back.
STUB_STRINGS = (
    "Ad campaign created successfully.",
    "Active Ads: 2, Paused: 1.",
    "Ad Stats: 5k impressions, 200 clicks.",
)


@pytest.fixture()
def db(settings_override: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A temp SQLite database with the schema, and a configured owner."""
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER))
    settings_module.get_settings.cache_clear()
    # ``owner_control`` caches the owner id in a module global after first read.
    owner_control._owner_id = None  # type: ignore[attr-defined]

    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    yield settings
    owner_control._owner_id = None  # type: ignore[attr-defined]


def _campaign(chat_id: int = CHAT, **kwargs: Any) -> int:
    """Insert a campaign through the engine, returning its id."""
    return int(
        AdManager.create_campaign(
            chat_id,
            kwargs.pop("text", "📣 فروش ویژه"),
            kwargs.pop("interval_hours", 6),
            kwargs.pop("max_repeats", 0),
            kwargs.pop("created_by", OWNER),
        )
    )


def _rows() -> list[AdCampaign]:
    settings = get_settings()
    with Session(create_engine(f"sqlite:///{settings.db_path}")) as session:
        return list(session.exec(select(AdCampaign)).all())


# ── /ad_create ────────────────────────────────────────────────────────────


async def test_create_writes_a_real_row_and_shows_its_id(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    context = make_context(["📣", "فروش", "ویژه", "--interval", "6", "--repeats", "3"])

    await surface.ad_create_cmd(update, context)

    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.text == "📣 فروش ویژه"
    assert row.interval_hours == 6.0
    assert row.max_repeats == 3
    assert row.status == "active"
    assert row.chat_id == CHAT
    assert row.created_by == OWNER
    assert row.next_run is not None

    text = update.last_reply
    assert f"شناسه: {row.id}" in text
    assert "فاصلهٔ اجرا: 6 ساعت" in text
    assert "وضعیت: active" in text


async def test_create_replies_with_a_real_next_run_not_a_promise(db: Any) -> None:
    """The reply quotes the stored ``next_run``; nothing claims a delivery loop."""
    update = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_create_cmd(update, make_context(["متن", "--interval", "3"]))

    row = _rows()[0]
    assert row.next_run is not None
    assert row.next_run.isoformat()[:16] in update.last_reply
    # ...and the reply promises no delivery loop that does not exist yet.
    for forbidden in ("به‌طور خودکار ارسال می‌شود", "posted automatically"):
        assert forbidden not in update.last_reply


@pytest.mark.parametrize("user", [INTRUDER, 0])
async def test_create_is_owner_only_and_writes_nothing(db: Any, user: int) -> None:
    update = make_update(user_id=user, chat_id=CHAT)

    await surface.ad_create_cmd(update, make_context(["متن"]))

    assert "مالک" in update.last_reply
    assert _rows() == []


async def test_create_without_text_shows_usage_not_a_fake_success(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)

    await surface.ad_create_cmd(update, make_context(["--interval", "6"]))

    assert update.last_reply.startswith("❌")
    assert _rows() == []


@pytest.mark.parametrize("flag,value", [("--interval", "0"), ("--interval", "999999")])
async def test_interval_bounds_are_enforced(db: Any, flag: str, value: str) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)

    await surface.ad_create_cmd(update, make_context(["متن", flag, value]))

    assert "فاصلهٔ زمانی" in update.last_reply
    assert _rows() == []


async def test_repeats_must_be_a_whole_non_negative_number(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)

    await surface.ad_create_cmd(update, make_context(["متن", "--repeats", "-2"]))

    assert "تعداد تکرار" in update.last_reply
    assert _rows() == []


@pytest.mark.parametrize("stub", STUB_STRINGS)
async def test_no_command_replies_with_a_removed_stub(db: Any, stub: str) -> None:
    """Every command is exercised; none may answer with the old literal."""
    update = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_create_cmd(update, make_context(["متن"]))

    for command in (
        surface.ad_create_cmd,
        surface.ad_list_cmd,
        surface.ad_stats_cmd,
        surface.ad_pause_cmd,
        surface.ad_resume_cmd,
        surface.ad_delete_cmd,
    ):
        probe = make_update(user_id=OWNER, chat_id=CHAT)
        await command(probe, make_context(["1"]))
        assert stub not in probe.last_reply


# ── /ad_list ──────────────────────────────────────────────────────────────


async def test_list_reads_the_database_and_scopes_to_the_chat(db: Any) -> None:
    _campaign(CHAT, text="آگهی این گفتگو")
    _campaign(OTHER_CHAT, text="آگهی گفتگوی دیگر")

    mine = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_list_cmd(mine, make_context([]))
    assert "آگهی این گفتگو" in mine.last_reply
    assert "آگهی گفتگوی دیگر" not in mine.last_reply
    assert mine.last_reply.count("•") == 1

    # Even the owner sees only this chat's rows in a group listing.
    other = make_update(user_id=INTRUDER, chat_id=OTHER_CHAT)
    await surface.ad_list_cmd(other, make_context([]))
    assert "آگهی گفتگوی دیگر" in other.last_reply
    assert "آگهی این گفتگو" not in other.last_reply


async def test_list_of_an_empty_chat_says_so(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)

    await surface.ad_list_cmd(update, make_context([]))

    assert "هنوز کمپینی" in update.last_reply
    assert "•" not in update.last_reply


async def test_list_status_filter_validates_its_argument(db: Any) -> None:
    _campaign(CHAT)
    update = make_update(user_id=OWNER, chat_id=CHAT)

    await surface.ad_list_cmd(update, make_context(["bogus"]))

    assert "فیلترهای مجاز" in update.last_reply

    ok = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_list_cmd(ok, make_context(["active"]))
    assert "(active)" in ok.last_reply
    assert "•" in ok.last_reply


# ── /ad_pause · /ad_resume · /ad_delete — the authorisation the engine lacks ─


async def test_pausing_another_chats_campaign_is_refused_and_changes_nothing(
    db: Any,
) -> None:
    """The engine's ``pause_campaign`` takes a bare id; the surface must not.

    Without this check any user who guesses an integer can silence another
    group's campaign — an IDOR the fake handler could never have exposed,
    because it did nothing at all.
    """
    campaign_id = _campaign(CHAT)

    intruder = make_update(user_id=INTRUDER, chat_id=OTHER_CHAT)
    await surface.ad_pause_cmd(intruder, make_context([str(campaign_id)]))

    assert "متعلق به این گفتگو نیست" in intruder.last_reply
    assert _rows()[0].status == "active"


async def test_same_chat_can_pause_and_resume(db: Any) -> None:
    campaign_id = _campaign(CHAT)

    pause = make_update(user_id=INTRUDER, chat_id=CHAT)
    await surface.ad_pause_cmd(pause, make_context([str(campaign_id)]))
    assert _rows()[0].status == "paused"
    assert "وضعیت فعلی: paused" in pause.last_reply

    resume = make_update(user_id=INTRUDER, chat_id=CHAT)
    await surface.ad_resume_cmd(resume, make_context([str(campaign_id)]))
    assert _rows()[0].status == "active"
    assert "وضعیت فعلی: active" in resume.last_reply


async def test_owner_may_act_on_any_chat_and_next_run_is_reset(db: Any) -> None:
    campaign_id = _campaign(OTHER_CHAT)
    before = AdManager.get_campaign(campaign_id)

    update = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_pause_cmd(update, make_context([str(campaign_id)]))

    assert _rows()[0].status == "paused"
    assert before is not None and before["status"] == "active"

    resume = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_resume_cmd(resume, make_context([str(campaign_id)]))
    after = AdManager.get_campaign(campaign_id)
    assert after is not None and after["next_run"] is not None


async def test_missing_id_usage_and_unknown_campaign(db: Any) -> None:
    no_args = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_delete_cmd(no_args, make_context([]))
    assert no_args.last_reply.startswith("❌")

    junk = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_delete_cmd(junk, make_context(["abc"]))
    assert junk.last_reply.startswith("❌")

    ghost = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_delete_cmd(ghost, make_context(["424242"]))
    assert "وجود ندارد" in ghost.last_reply


async def test_delete_removes_the_row(db: Any) -> None:
    campaign_id = _campaign(CHAT)

    update = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_delete_cmd(update, make_context([str(campaign_id)]))

    assert _rows() == []
    assert "انجام شد" in update.last_reply


# ── /ad_stats ─────────────────────────────────────────────────────────────


async def test_stats_are_row_counts_and_say_so(db: Any) -> None:
    _campaign(CHAT)
    paused = _campaign(CHAT)
    AdManager.pause_campaign(paused)
    _campaign(OTHER_CHAT)

    update = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.ad_stats_cmd(update, make_context([]))

    text = update.last_reply
    assert "کل کمپین‌ها: 2" in text
    assert "فعال: 1" in text
    assert "متوقف: 1" in text
    # The removed literal promised numbers that exist in no column.
    assert "5k" not in text
    assert "clicks" not in text
    assert "ثبت نمی‌شود" in text


# ── pure parsing ──────────────────────────────────────────────────────────


def test_flags_may_appear_anywhere_and_words_keep_their_order() -> None:
    parsed = surface.parse_create_args(["a", "--interval", "3", "b"])
    assert isinstance(parsed, surface.CampaignDraft)
    assert (parsed.text, parsed.interval_hours, parsed.max_repeats) == ("a b", 3.0, 0)


def test_defaults_match_the_engines_own_default() -> None:
    parsed = surface.parse_create_args(["متن"])
    assert isinstance(parsed, surface.CampaignDraft)
    assert parsed.interval_hours == 24.0
    assert parsed.max_repeats == 0


def test_missing_flag_value_is_an_error_string_not_a_crash() -> None:
    parsed = surface.parse_create_args(["متن", "--interval"])
    assert isinstance(parsed, str)
    assert "مقداری نیامده" in parsed


def test_non_numeric_flag_is_reported_with_the_offending_token() -> None:
    parsed = surface.parse_create_args(["--repeats", "three"])
    assert isinstance(parsed, str)
    assert "three" in parsed


@pytest.mark.parametrize("argv,expected", [(["7"], 7), (["7", "extra"], 7)])
def test_campaign_id_takes_the_first_argument(argv: list[str], expected: int) -> None:
    assert surface.parse_campaign_id(argv, command="/ad_pause") == expected


@pytest.mark.parametrize("argv", [[], ["0"], ["-1"], ["x"]])
def test_bad_campaign_id_returns_the_usage_line(argv: list[str]) -> None:
    parsed = surface.parse_campaign_id(argv, command="/ad_delete")
    assert isinstance(parsed, str)
    assert "/ad_delete" in parsed


def test_renderers_tolerate_a_vanished_row() -> None:
    """A delete between the read and the render must not raise in the dispatcher."""
    assert "ثبت شد اما بازخوانی آن ممکن نشد" in surface.format_created(None, 5)
    assert "پیدا نشد" in surface.format_state_change(verb="توقف", campaign=None, ok=False)
    assert "دیگر در جدول نیست" in surface.format_state_change(
        verb="حذف", campaign={"id": 5, "status": None}, ok=True
    )
    assert "آگهی" in surface.format_list([], None)
    assert "0" in surface.format_list([{"id": 1, "status": "active", "text": "t"}], "active")
