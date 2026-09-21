"""Telegram surface for the games (``/wordle``, ``/guess``, ``/poll``, ``/quiz``).

Wires :mod:`nexus_ai_agent.features.games` — whose engines were fully
implemented and tested but never reachable from a command — to real
handlers.  The module keeps one process-wide instance of every engine so the
state created by ``/wordle`` is the same state a later guess is checked
against (the previous ``/quiz`` created a fresh ``QuizGame`` per call and
therefore could never verify an answer).

Polls and quizzes use Telegram's **native poll** objects (``reply_poll``):
no inline keyboard has to be built here, results render live for everyone,
and vote/answer bookkeeping arrives through ``poll_answer`` updates handled
by :func:`poll_answer_handler`.  Plain-text guesses are routed by
:func:`route_game_text`, which the catch-all message handler can call first.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

from nexus_ai_agent.features.games import NumberGuess, QuickPoll, QuizGame, WordleFA
from nexus_ai_agent.features.gamification import XP_PER_QUIZ_CORRECT, GamificationEngine
from nexus_ai_agent.observability.logging import get_logger

from ._ptb import args, chat_id, message_of, message_text, reply, user_id

__all__ = [
    "guess_cmd",
    "guess_start_cmd",
    "guess_stop_cmd",
    "leaderboard_cmd",
    "normalize_persian_word",
    "parse_int",
    "parse_poll_args",
    "poll_answer_handler",
    "poll_cmd",
    "poll_results_cmd",
    "quiz_cmd",
    "reset_games",
    "route_game_text",
    "wordle_cmd",
    "wordle_stop_cmd",
]

logger = get_logger(__name__)

#: Telegram limits for native polls.
MAX_POLL_QUESTION = 300
MAX_POLL_OPTION = 100
MAX_POLL_OPTIONS = 10
#: How many native poll ids are remembered (FIFO) — a bounded in-memory surface.
MAX_TRACKED_POLLS = 500
#: Default answers for ``/poll <question>`` without explicit options.
DEFAULT_POLL_OPTIONS = ("👍 بله", "👎 خیر")

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "ة": "ه", "ۀ": "ه"})
_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0670\u200c\u200d]")
_POLL_SPLIT_RE = re.compile(r"\s*(?:\||؛|;|\n)\s*")

_wordle = WordleFA()
_guess = NumberGuess()
_polls = QuickPoll()
_quiz = QuizGame()
#: telegram poll id → quick-poll id (regular polls)
_native_polls: OrderedDict[str, str] = OrderedDict()
#: telegram poll id → {"chat_id", "answer"} (quiz polls)
_quiz_polls: OrderedDict[str, dict[str, int]] = OrderedDict()
#: (telegram poll id, user id) pairs already scored — quiz polls cannot be re-voted,
#: but the guard keeps a duplicated update from double-counting.
_scored: set[tuple[str, int]] = set()
_latest_poll_by_chat: dict[int, str] = {}


def reset_games() -> None:
    """Forget every in-memory game/poll (tests)."""
    global _wordle, _guess, _polls, _quiz
    _wordle, _guess, _polls, _quiz = WordleFA(), NumberGuess(), QuickPoll(), QuizGame()
    _native_polls.clear()
    _quiz_polls.clear()
    _scored.clear()
    _latest_poll_by_chat.clear()


def _remember(store: OrderedDict[str, Any], key: str, value: Any) -> None:
    store[key] = value
    while len(store) > MAX_TRACKED_POLLS:
        store.popitem(last=False)


# ── helpers ──────────────────────────────────────────────────────────


def parse_int(text: str) -> int | None:
    """``"۴۲"`` / ``"42"`` → ``42``; anything else → ``None``."""
    cleaned = text.strip().translate(_PERSIAN_DIGITS)
    if re.fullmatch(r"[-+]?\d{1,9}", cleaned):
        return int(cleaned)
    return None


def normalize_persian_word(text: str) -> str:
    """Unify Arabic/Persian letter variants and strip diacritics/ZWNJ."""
    return _DIACRITICS_RE.sub("", text.strip()).translate(_ARABIC_TO_PERSIAN)


def parse_poll_args(raw: list[str]) -> tuple[str, list[str]] | None:
    """``question | option | option`` → ``(question, options)``; ``None`` when unusable.

    A bare question gets :data:`DEFAULT_POLL_OPTIONS`.  Options are trimmed,
    de-duplicated and capped at Telegram's limits.
    """
    text = " ".join(raw).strip()
    if not text:
        return None
    parts = [p.strip() for p in _POLL_SPLIT_RE.split(text) if p.strip()]
    if not parts:
        return None
    question, options = parts[0], parts[1:]
    if len(question) > MAX_POLL_QUESTION:
        return None
    if not options:
        options = list(DEFAULT_POLL_OPTIONS)
    deduped: list[str] = []
    for opt in options:
        opt = opt[:MAX_POLL_OPTION]
        if opt not in deduped:
            deduped.append(opt)
    if len(deduped) < 2 or len(deduped) > MAX_POLL_OPTIONS:
        return None
    return question, deduped


# ── Wordle ───────────────────────────────────────────────────────────


async def wordle_cmd(update: Any, context: Any) -> None:
    """``/wordle`` starts a game; ``/wordle <کلمه>`` submits a guess."""
    uid = user_id(update)
    if uid is None:
        return
    raw = args(context)
    if raw:
        await reply(update, _wordle.guess(uid, normalize_persian_word(raw[0])))
        return
    if _wordle.is_active(uid):
        await reply(update, "🟩 بازی وردل فعال دارید. کلمه ۵ حرفی بفرستید یا /wordle_stop.")
        return
    await reply(update, _wordle.start(uid) + "\nکلمه را مستقیم بفرستید یا: /wordle <کلمه>")


async def wordle_stop_cmd(update: Any, context: Any) -> None:
    uid = user_id(update)
    if uid is not None:
        await reply(update, _wordle.stop(uid))


# ── Number guess ─────────────────────────────────────────────────────


async def guess_start_cmd(update: Any, context: Any) -> None:
    uid = user_id(update)
    if uid is None:
        return
    if _guess.is_active(uid):
        await reply(update, "🎲 بازی حدس عدد فعال دارید. عدد بفرستید یا /guess_stop.")
        return
    await reply(update, _guess.start(uid) + "\nعدد را مستقیم بفرستید یا: /guess <عدد>")


async def guess_cmd(update: Any, context: Any) -> None:
    """``/guess <عدد>`` — submit a guess (also accepted as plain text while a game is on)."""
    uid = user_id(update)
    if uid is None:
        return
    raw = args(context)
    number = parse_int(raw[0]) if raw else None
    if number is None:
        await reply(update, "❌ یک عدد بین ۱ تا ۱۰۰ بفرستید. مثال: /guess 50")
        return
    await reply(update, _guess.guess(uid, number))


async def guess_stop_cmd(update: Any, context: Any) -> None:
    uid = user_id(update)
    if uid is not None:
        await reply(update, _guess.stop(uid))


# ── Quick poll (native Telegram poll + QuickPoll tally) ───────────────


async def poll_cmd(update: Any, context: Any) -> None:
    """``/poll سوال | گزینه ۱ | گزینه ۲`` — native poll, tallied by :class:`QuickPoll`."""
    msg = message_of(update)
    cid = chat_id(update)
    if msg is None or cid is None:
        return
    parsed = parse_poll_args(args(context))
    if parsed is None:
        await reply(
            update,
            "📊 نظرسنجی سریع\n\n"
            "استفاده: /poll سوال | گزینه ۱ | گزینه ۲ | ...\n"
            "مثال: /poll بهترین زبان؟ | Python | Rust | Go\n"
            f"بدون گزینه: {' / '.join(DEFAULT_POLL_OPTIONS)} · نتایج: /poll_results",
        )
        return
    question, options = parsed
    pid = _polls.create(question, options)
    sent = await msg.reply_poll(question=question, options=options, is_anonymous=False)
    native_id = getattr(getattr(sent, "poll", None), "id", None)
    if native_id is not None:
        _remember(_native_polls, str(native_id), pid)
    _latest_poll_by_chat[cid] = pid


async def poll_results_cmd(update: Any, context: Any) -> None:
    """``/poll_results [id]`` — tally recorded by the bot for the latest (or given) poll."""
    cid = chat_id(update)
    raw = args(context)
    pid = raw[0] if raw else (_latest_poll_by_chat.get(cid) if cid is not None else None)
    results = _polls.get_results(pid) if pid else None
    if results is None:
        await reply(update, "📊 نظرسنجی فعالی در این گفتگو ثبت نشده است.")
        return
    await reply(update, f"{results}\n🆔 {pid}")


# ── Quiz (native quiz poll + QuizGame scoring) ────────────────────────


async def quiz_cmd(update: Any, context: Any) -> None:
    """``/quiz`` — a Persian trivia question as a native quiz poll; answers are scored."""
    msg = message_of(update)
    uid, cid = user_id(update), chat_id(update)
    if msg is None or uid is None or cid is None:
        return
    question = _quiz.get_question(uid)
    if question is None:
        await reply(update, "❓ فعلاً سؤالی موجود نیست.")
        return
    sent = await msg.reply_poll(
        question=question["q"],
        options=list(question["options"]),
        type="quiz",
        correct_option_id=int(question["answer"]),
        is_anonymous=False,
    )
    native_id = getattr(getattr(sent, "poll", None), "id", None)
    if native_id is not None:
        _remember(_quiz_polls, str(native_id), {"chat_id": cid, "answer": int(question["answer"])})


async def leaderboard_cmd(update: Any, context: Any) -> None:
    """``/leaderboard`` — quiz scores for this chat."""
    cid = chat_id(update)
    if cid is None:
        return
    board = _quiz.get_leaderboard(cid)
    if not board:
        await reply(update, "🏆 هنوز کسی در این گفتگو به کوییز پاسخ نداده است.")
        return
    lines = ["🏆 جدول کوییز", "━━━━━━━━━━━━━━━━"]
    for rank, row in enumerate(board, 1):
        lines.append(f"  {rank}. کاربر {row['user_id']}: {row['score']}/{row['answered']}")
    await reply(update, "\n".join(lines))


async def poll_answer_handler(update: Any, context: Any) -> None:
    """``PollAnswerHandler`` target: records quick-poll votes and scores quiz answers."""
    answer = getattr(update, "poll_answer", None)
    if answer is None:
        return
    poll_id = str(getattr(answer, "poll_id", ""))
    voter = getattr(answer, "user", None)
    voter_id = getattr(voter, "id", None)
    option_ids = list(getattr(answer, "option_ids", None) or [])
    if voter_id is None or not option_ids:
        return  # anonymous/chat voter or a retracted vote — nothing to score
    voter_id = int(voter_id)

    quick_id = _native_polls.get(poll_id)
    if quick_id is not None:
        _polls.vote(quick_id, int(option_ids[0]), voter_id)
        return

    quiz = _quiz_polls.get(poll_id)
    if quiz is None or (poll_id, voter_id) in _scored:
        return
    _scored.add((poll_id, voter_id))
    correct = int(option_ids[0]) == quiz["answer"]
    try:
        _quiz.update_score(voter_id, quiz["chat_id"], correct)
        if correct:
            GamificationEngine.add_xp(voter_id, quiz["chat_id"], XP_PER_QUIZ_CORRECT)
    except Exception:  # noqa: BLE001 - scoring must never break update processing
        logger.exception("quiz_score_failed", user_id=voter_id, chat_id=quiz["chat_id"])


# ── Plain-text routing ───────────────────────────────────────────────


async def route_game_text(update: Any, context: Any) -> bool:
    """Feed a plain text message to the user's active game.

    Returns ``True`` when the text was consumed (the caller should stop
    processing), ``False`` when no game is waiting for input.
    """
    uid = user_id(update)
    text = message_text(update).strip()
    if uid is None or not text or text.startswith("/"):
        return False
    if _guess.is_active(uid):
        number = parse_int(text)
        if number is not None:
            await reply(update, _guess.guess(uid, number))
            return True
    if _wordle.is_active(uid):
        word = normalize_persian_word(text)
        if " " not in word and len(word) == 5:
            await reply(update, _wordle.guess(uid, word))
            return True
    return False
