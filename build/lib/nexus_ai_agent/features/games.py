"""Games & entertainment features for NEXUS AI Telegram bot.

Includes:
- QuizGame: Random quiz questions with inline keyboard (Persian)
- NumberGuess: Guess-the-number game
- WordleFA: Persian 5-letter Wordle clone
- QuickPoll: Quick inline polls
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import QuizScore

logger = get_logger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


# ═══════════════════════════════════════════════════════════════════════
# Quiz Game
# ═══════════════════════════════════════════════════════════════════════

_QUIZ_QUESTIONS: list[dict[str, Any]] = [
    {
        "q": "پایتخت ایران کجاست؟",
        "options": ["اصفهان", "تهران", "شیراز", "مشهد"],
        "answer": 1,
    },
    {
        "q": "بزرگ‌ترین سیاره منظومه شمسی کدام است؟",
        "options": ["زمین", "مشتری", "زحل", "مریخ"],
        "answer": 1,
    },
    {
        "q": "آب به چه زبانی «هیدروژن اکساید» نامیده می‌شود؟",
        "options": ["لاتین", "انگلیسی", "علمی", "فرانسوی"],
        "answer": 2,
    },
    {
        "q": "کدام نویسنده «شاهنامه» را سروده است؟",
        "options": ["حافظ", "فردوسی", "سعدی", "مولوی"],
        "answer": 1,
    },
    {
        "q": "طول طولانی‌ترین رودخانه جهان چقدر است؟",
        "options": ["۶۶۵۰ کیلومتر", "۶۴۰۰ کیلومتر", "۶۲۰۰ کیلومتر", "۶۰۰۰ کیلومتر"],
        "answer": 0,
    },
    {
        "q": "فرمول شیمیایی نمک طعام چیست؟",
        "options": ["NaOH", "NaCl", "KCl", "CaCO₃"],
        "answer": 1,
    },
    {
        "q": "کدام قاره بزرگ‌ترین قاره جهان است؟",
        "options": ["آفریقا", "آسیا", "آمریکا", "اروپا"],
        "answer": 1,
    },
    {
        "q": "نام دیگر سیاره زهره چیست؟",
        "options": ["مشتری", "عطارد", "ناهید", "بهارام"],
        "answer": 2,
    },
    {
        "q": "چند کشور در قاره آمریکای جنوبی وجود دارد؟",
        "options": ["۱۰", "۱۲", "۱۴", "۱۶"],
        "answer": 1,
    },
    {
        "q": "بزرگ‌ترین اقیانوس جهان کدام است؟",
        "options": ["اطلس", "هند", "آرام", "منجمد شمالی"],
        "answer": 2,
    },
]


class QuizGame:
    """Persian quiz game with inline keyboard answers."""

    def __init__(self) -> None:
        self._questions = list(_QUIZ_QUESTIONS)
        self._active: dict[int, dict[str, Any]] = {}

    def get_question(self, user_id: int) -> dict[str, Any] | None:
        """Return a random question dict for *user_id*."""
        if not self._questions:
            return None
        q = random.choice(self._questions)
        self._active[user_id] = q
        return q

    def check_answer(self, user_id: int, choice: int) -> bool:
        """Check if *choice* is correct for *user_id*'s current question."""
        q = self._active.get(user_id)
        if q is None:
            return False
        return q["answer"] == choice

    def clear(self, user_id: int) -> None:
        """Clear active question for *user_id*."""
        self._active.pop(user_id, None)

    def update_score(self, user_id: int, chat_id: int, correct: bool) -> int:
        """Update quiz score in DB. Returns the new total score."""
        engine = _sync_engine()
        with Session(engine) as session:
            existing = session.exec(
                select(QuizScore).where(
                    (QuizScore.user_id == user_id) & (QuizScore.chat_id == chat_id)
                )
            ).first()
            if existing is not None:
                existing.answered += 1
                if correct:
                    existing.score += 1
                from datetime import datetime, timezone

                existing.updated_at = datetime.now(timezone.utc)
                session.commit()
                return existing.score
            else:
                score = QuizScore(
                    user_id=user_id,
                    chat_id=chat_id,
                    score=1 if correct else 0,
                    answered=1,
                )
                session.add(score)
                session.commit()
                return score.score

    def get_leaderboard(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """Return top quiz scores for *chat_id*."""
        engine = _sync_engine()
        with Session(engine) as session:
            results = session.exec(
                select(QuizScore)
                .where(QuizScore.chat_id == chat_id)
                .order_by(col(QuizScore.score).desc())
                .limit(limit)
            ).all()
            return [
                {"user_id": r.user_id, "score": r.score, "answered": r.answered} for r in results
            ]


# ═══════════════════════════════════════════════════════════════════════
# Number Guess
# ═══════════════════════════════════════════════════════════════════════


class NumberGuess:
    """Simple guess-the-number game (1-100)."""

    def __init__(self) -> None:
        self._games: dict[int, dict[str, int]] = {}

    def start(self, user_id: int) -> str:
        """Start a new game. Returns status message."""
        target = random.randint(1, 100)
        self._games[user_id] = {"target": target, "attempts": 0}
        return "🎲 بازی حدس عدد شروع شد! عددی بین ۱ تا ۱۰۰ حدس بزن."

    def guess(self, user_id: int, number: int) -> str:
        """Process a guess. Returns hint or win message."""
        game = self._games.get(user_id)
        if game is None:
            return "⚠️ بازی فعلی ندارید. /guess_start بزنید."
        game["attempts"] += 1
        target = game["target"]
        attempts = game["attempts"]
        if number == target:
            self._games.pop(user_id)
            return f"🎉 آفرین! عدد {target} بود. با {attempts} حدس پیدا شد!"
        if number < target:
            return f"⬆️ بزرگ‌تره! (حدس {attempts})"
        return f"⬇️ کوچک‌تره! (حدس {attempts})"

    def stop(self, user_id: int) -> str:
        """Stop the current game."""
        game = self._games.pop(user_id, None)
        if game is None:
            return "⚠️ بازی فعلی ندارید."
        return f"🛑 بازی تمام شد. عدد {game['target']} بود."

    def is_active(self, user_id: int) -> bool:
        return user_id in self._games


# ═══════════════════════════════════════════════════════════════════════
# Wordle FA — Persian 5-letter Wordle
# ═══════════════════════════════════════════════════════════════════════

_PERSIAN_WORDS: list[str] = [
    "کتاب",
    "مدرس",
    "پنجر",
    "سلامت",
    "عالمی",
    "فکری",
    "حیات",
    "ذکاوت",
    "صداقت",
    "منطق",
    "نورال",
    "خلقت",
    "دقت",
    "سرمد",
    "طبیعت",
    "عفت",
    "فرهن",
    "قدرت",
    "گلست",
    "هنر",
    "ایمان",
    "بزرگ",
    "تاریخ",
    "ثروت",
    "جمال",
    "حقیق",
    "خرد",
    "دلیل",
    "رازق",
    "زبان",
    "سرور",
    "شرف",
    "صبر",
    "ضربان",
    "طلا",
    "ظرفیت",
    "عرفان",
    "فطرت",
    "قناعت",
    "کرامت",
    "گزارش",
    "لفظ",
    "مدرک",
    "ناموس",
    "وطن",
    "یقین",
    "آسما",
    "احتر",
    "بخت",
    "پیمان",
]

_FIVE_LETTER_WORDS = [w for w in _PERSIAN_WORDS if len(w) == 5]
if len(_FIVE_LETTER_WORDS) < 5:
    _FIVE_LETTER_WORDS = [w[:5] for w in _PERSIAN_WORDS if len(w) >= 5]


class WordleFA:
    """Persian 5-letter Wordle game."""

    MAX_ATTEMPTS = 6

    def __init__(self) -> None:
        self._games: dict[int, dict[str, Any]] = {}

    def start(self, user_id: int) -> str:
        """Start a new Wordle game."""
        if not _FIVE_LETTER_WORDS:
            return "❌ کلمه‌ای موجود نیست."
        target = random.choice(_FIVE_LETTER_WORDS)
        self._games[user_id] = {"target": target, "attempts": 0, "history": []}
        return (
            "🟩🟨⬛ وردل فارسی شروع شد!\n"
            "یک کلمه ۵ حرفی حدس بزنید.\n"
            "🟩 = درست  🟨 = جابجا  ⬛ = غلط\n"
            f"تلاش‌ها: {self.MAX_ATTEMPTS}"
        )

    def guess(self, user_id: int, word: str) -> str:
        """Process a Wordle guess. Returns emoji feedback."""
        game = self._games.get(user_id)
        if game is None:
            return "⚠️ بازی فعلی ندارید. /wordle بزنید."
        if len(word) != 5:
            return "❌ کلمه باید ۵ حرف باشد."

        target = game["target"]
        game["attempts"] += 1

        result = ["⬛"] * 5
        target_chars = list(target)
        word_chars = list(word)

        for i in range(5):
            if word_chars[i] == target_chars[i]:
                result[i] = "🟩"
                target_chars[i] = ""
                word_chars[i] = ""

        for i in range(5):
            if word_chars[i] and word_chars[i] in target_chars:
                result[i] = "🟨"
                target_chars[target_chars.index(word_chars[i])] = ""

        feedback = "".join(result)
        game["history"].append(f"{word} → {feedback}")

        if word == target:
            self._games.pop(user_id)
            return f"🎉 آفرین! کلمه «{target}» بود!\n{feedback}"

        if game["attempts"] >= self.MAX_ATTEMPTS:
            self._games.pop(user_id)
            return (
                f"😢 متأسفانه باختید! کلمه «{target}» بود.\n"
                f"{feedback}\n\nتاریخچه:\n" + "\n".join(game["history"])
            )

        return f"{feedback}\nتلاش {game['attempts']}/{self.MAX_ATTEMPTS}\n\n" + "\n".join(
            game["history"]
        )

    def stop(self, user_id: int) -> str:
        """Stop the current Wordle game."""
        game = self._games.pop(user_id, None)
        if game is None:
            return "⚠️ بازی فعلی ندارید."
        return f"🛑 وردل تمام شد. کلمه «{game['target']}» بود."

    def is_active(self, user_id: int) -> bool:
        return user_id in self._games


# ═══════════════════════════════════════════════════════════════════════
# Quick Poll
# ═══════════════════════════════════════════════════════════════════════


class QuickPoll:
    """Quick inline polls with real-time results."""

    def __init__(self) -> None:
        self._polls: dict[str, dict[str, Any]] = {}
        self._voted: dict[int, set[str]] = {}

    def create(self, question: str, options: list[str], poll_id: str | None = None) -> str:
        """Create a new poll. Returns the poll_id."""
        import uuid

        pid = poll_id or str(uuid.uuid4())[:8]
        self._polls[pid] = {
            "question": question,
            "options": options,
            "votes": {i: 0 for i in range(len(options))},
        }
        return pid

    def vote(self, poll_id: str, option_idx: int, user_id: int) -> bool:
        """Cast a vote. Returns True if accepted."""
        poll = self._polls.get(poll_id)
        if poll is None:
            return False
        if option_idx < 0 or option_idx >= len(poll["options"]):
            return False
        user_voted = self._voted.setdefault(user_id, set())
        if poll_id in user_voted:
            return False
        user_voted.add(poll_id)
        poll["votes"][option_idx] = poll["votes"].get(option_idx, 0) + 1
        return True

    def get_results(self, poll_id: str) -> str | None:
        """Return formatted poll results."""
        poll = self._polls.get(poll_id)
        if poll is None:
            return None
        total = sum(poll["votes"].values())
        lines = [f"📊 {poll['question']}"]
        for i, opt in enumerate(poll["options"]):
            count = poll["votes"].get(i, 0)
            pct = (count / total * 100) if total > 0 else 0
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            lines.append(f"  {opt}: {bar} {count} ({pct:.0f}%)")
        lines.append(f"  مجموع آرا: {total}")
        return "\n".join(lines)

    def get_poll(self, poll_id: str) -> dict[str, Any] | None:
        return self._polls.get(poll_id)

    def cleanup(self, poll_id: str) -> None:
        """Remove a finished poll."""
        self._polls.pop(poll_id, None)
