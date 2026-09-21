"""B2 — Wordle / NumberGuess / QuickPoll / Quiz wired to ``features/games.py``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from surface_fakes import make_context, make_update

from nexus_ai_agent.bot.surface import games as surface
from nexus_ai_agent.features.gamification import XP_PER_QUIZ_CORRECT, GamificationEngine


@pytest.fixture(autouse=True)
def _fresh_games() -> None:
    surface.reset_games()


# ── helpers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [("42", 42), ("۴۲", 42), (" 7 ", 7), ("-3", -3), ("abc", None), ("1e3", None), ("", None)],
)
def test_parse_int(text: str, expected: int | None) -> None:
    assert surface.parse_int(text) == expected


def test_normalize_persian_word() -> None:
    assert surface.normalize_persian_word("كتاب") == "کتاب"
    assert surface.normalize_persian_word("ميز") == "میز"
    assert surface.normalize_persian_word("مَدرسه") == "مدرسه"
    assert surface.normalize_persian_word("می\u200cرود") == "میرود"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["بهترین", "زبان؟", "|", "Python", "|", "Rust"], ("بهترین زبان؟", ["Python", "Rust"])),
        (["q?"], ("q?", list(surface.DEFAULT_POLL_OPTIONS))),
        (["q", "؛", "a", "؛", "b", "؛", "a"], ("q", ["a", "b"])),
        (["q;a;b;c"], ("q", ["a", "b", "c"])),
        (["q", "|", "only"], None),
        ([], None),
        (["q"] + ["|", "o"] * 11, None),
    ],
)
def test_parse_poll_args(raw: list[str], expected: tuple[str, list[str]] | None) -> None:
    assert surface.parse_poll_args(raw) == expected


def test_parse_poll_args_dedupes_more_than_ten_options() -> None:
    parsed = surface.parse_poll_args(["q", "|", "|".join(f"o{i}" for i in range(12))])
    assert parsed is None
    parsed = surface.parse_poll_args(["q", "|", "|".join(f"o{i}" for i in range(10))])
    assert parsed is not None and len(parsed[1]) == 10


# ── Number guess ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_number_guess_flow_is_shared_between_command_and_text() -> None:
    update = make_update(user_id=1)
    await surface.guess_start_cmd(update, make_context())
    assert "شروع شد" in update.message.last
    target = surface._guess._games[1]["target"]

    # wrong guess by command
    wrong = 1 if target != 1 else 2
    update = make_update(user_id=1)
    await surface.guess_cmd(update, make_context([str(wrong)]))
    assert "حدس 1" in update.message.last

    # plain text is routed to the same game
    update = make_update(user_id=1, text=str(target))
    assert await surface.route_game_text(update, make_context()) is True
    assert "آفرین" in update.message.last
    assert surface._guess.is_active(1) is False

    # no game → text is not consumed
    update = make_update(user_id=1, text="55")
    assert await surface.route_game_text(update, make_context()) is False
    assert update.message.replies == []


@pytest.mark.asyncio
async def test_guess_cmd_validates_input_and_stop() -> None:
    update = make_update(user_id=2)
    await surface.guess_cmd(update, make_context(["abc"]))
    assert update.message.last.startswith("❌")
    update = make_update(user_id=2)
    await surface.guess_cmd(update, make_context(["5"]))
    assert "بازی فعلی ندارید" in update.message.last

    await surface.guess_start_cmd(make_update(user_id=2), make_context())
    update = make_update(user_id=2)
    await surface.guess_start_cmd(update, make_context())
    assert "فعال دارید" in update.message.last
    update = make_update(user_id=2)
    await surface.guess_stop_cmd(update, make_context())
    assert update.message.last.startswith("🛑")


# ── Wordle ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wordle_flow() -> None:
    update = make_update(user_id=3)
    await surface.wordle_cmd(update, make_context())
    assert "وردل" in update.message.last
    target = surface._wordle._games[3]["target"]

    update = make_update(user_id=3)
    await surface.wordle_cmd(update, make_context(["ab"]))
    assert "۵ حرف" in update.message.last

    # plain-text guess with Arabic yeh/kaf is normalised and accepted
    variant = target.replace("ی", "ي").replace("ک", "ك")
    update = make_update(user_id=3, text=variant)
    assert await surface.route_game_text(update, make_context()) is True
    assert "🟩🟩🟩🟩🟩" in update.message.last
    assert surface._wordle.is_active(3) is False

    update = make_update(user_id=3)
    await surface.wordle_stop_cmd(update, make_context())
    assert "بازی فعلی ندارید" in update.message.last


@pytest.mark.asyncio
async def test_wordle_route_ignores_non_words_and_commands() -> None:
    await surface.wordle_cmd(make_update(user_id=4), make_context())
    for text in ["/help", "سلام دنیا خوبی", "abc"]:
        update = make_update(user_id=4, text=text)
        assert await surface.route_game_text(update, make_context()) is False
    assert surface._wordle.is_active(4)


# ── Polls ────────────────────────────────────────────────────────────


def _poll_answer(poll_id: str, user_id: int, option_ids: list[int]) -> SimpleNamespace:
    return SimpleNamespace(poll_id=poll_id, user=SimpleNamespace(id=user_id), option_ids=option_ids)


@pytest.mark.asyncio
async def test_poll_cmd_sends_native_poll_and_tallies_votes() -> None:
    update = make_update(user_id=1, chat_id=-9)
    await surface.poll_cmd(update, make_context(["بهترین؟", "|", "Python", "|", "Rust"]))
    assert update.message.polls == [
        {"question": "بهترین؟", "options": ["Python", "Rust"], "is_anonymous": False}
    ]
    native_id = "native-1"
    assert native_id in surface._native_polls

    ctx = make_context()
    await surface.poll_answer_handler(
        make_update(poll_answer=_poll_answer(native_id, 11, [0])), ctx
    )
    await surface.poll_answer_handler(
        make_update(poll_answer=_poll_answer(native_id, 12, [1])), ctx
    )
    await surface.poll_answer_handler(
        make_update(poll_answer=_poll_answer(native_id, 12, [0])), ctx
    )
    # retracted vote / unknown poll are ignored
    await surface.poll_answer_handler(make_update(poll_answer=_poll_answer(native_id, 13, [])), ctx)
    await surface.poll_answer_handler(make_update(poll_answer=_poll_answer("zzz", 14, [0])), ctx)

    update = make_update(user_id=1, chat_id=-9)
    await surface.poll_results_cmd(update, make_context())
    text = update.message.last
    assert "Python" in text and "Rust" in text
    assert "مجموع آرا: 2" in text

    other = make_update(user_id=1, chat_id=-10)
    await surface.poll_results_cmd(other, make_context())
    assert "ثبت نشده" in other.message.last


@pytest.mark.asyncio
async def test_poll_cmd_usage_and_default_options() -> None:
    update = make_update(user_id=1, chat_id=-9)
    await surface.poll_cmd(update, make_context([]))
    assert "استفاده" in update.message.last and update.message.polls == []

    update = make_update(user_id=1, chat_id=-9)
    await surface.poll_cmd(update, make_context(["ok?"]))
    assert update.message.polls[0]["options"] == list(surface.DEFAULT_POLL_OPTIONS)


def test_tracked_polls_are_bounded() -> None:
    for i in range(surface.MAX_TRACKED_POLLS + 5):
        surface._remember(surface._native_polls, f"p{i}", "q")
    assert len(surface._native_polls) == surface.MAX_TRACKED_POLLS
    assert "p0" not in surface._native_polls


# ── Quiz ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quiz_scores_only_once_and_awards_xp(feature_db) -> None:
    update = make_update(user_id=1, chat_id=-9)
    await surface.quiz_cmd(update, make_context())
    poll = update.message.polls[0]
    assert poll["type"] == "quiz" and poll["is_anonymous"] is False
    answer = poll["correct_option_id"]
    wrong = (answer + 1) % len(poll["options"])
    native_id = "native-1"

    ctx = make_context()
    await surface.poll_answer_handler(
        make_update(poll_answer=_poll_answer(native_id, 21, [answer])), ctx
    )
    await surface.poll_answer_handler(
        make_update(poll_answer=_poll_answer(native_id, 21, [answer])), ctx
    )
    await surface.poll_answer_handler(
        make_update(poll_answer=_poll_answer(native_id, 22, [wrong])), ctx
    )

    board = surface._quiz.get_leaderboard(-9)
    by_user = {row["user_id"]: row for row in board}
    assert by_user[21]["score"] == 1 and by_user[21]["answered"] == 1
    assert by_user[22]["score"] == 0 and by_user[22]["answered"] == 1
    assert GamificationEngine.get_profile(21, -9)["xp"] == XP_PER_QUIZ_CORRECT
    assert GamificationEngine.get_profile(22, -9)["xp"] == 0

    update = make_update(user_id=1, chat_id=-9)
    await surface.leaderboard_cmd(update, make_context())
    assert "کاربر 21: 1/1" in update.message.last

    update = make_update(user_id=1, chat_id=-77)
    await surface.leaderboard_cmd(update, make_context())
    assert "هنوز کسی" in update.message.last
