"""AI Community Engagement engine for NEXUS AI Telegram bot.

Detects inactive groups and triggers auto-engagement through ice breakers,
meme prompts, trivia, daily questions, roast battles, quiz battles,
AI storytelling, random challenges, Persian jokes, and group events.

Rate-limited, anti-spam, smart timing, configurable frequency.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import EngagementConfig

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


# ---------------------------------------------------------------------------
# Content banks (Persian)
# ---------------------------------------------------------------------------

_ICE_BREAKERS: list[str] = [
    "💬 سلام بچه‌ها! کی آنلاینه؟ 👋",
    "🎲 بیاید یه بازی شروع کنیم! کی آماده‌ست؟",
    "🤔 سؤال روز: اگه می‌تونستید یه قدرت ابرقهرمانی داشته باشید، چی بود؟",
    "🎭 نظرسنجی: قهوه یا چای؟ ☕🍵",
    "😂 شوخی روز: چرا برنامه‌نویس‌ها بلندی دریا رو نمی‌فهمن؟ چون عمق نداره! 🌊",
]

_DAILY_QUESTIONS: list[str] = [
    "🤔 سؤال امروز: بزرگترین رویای شما چیه؟",
    "💭 اگه می‌تونستید به گذشته سفر کنید، کجا می‌رفتید؟",
    "🌍 بهترین سفری که رفتید کجا بود؟",
    "📚 آخرین کتابی که خوندید چی بود؟",
    "🎵 موسیقی مورد علاقه‌تون چیه؟",
    "🍕 پیتزا با آناناس: موافق یا مخالف؟ 😄",
    "🏆 بزرگترین دستاوردتون چیه؟",
    "💡 اگه یه اپلیکیشن می‌ساختید، چی بود؟",
]

_JOKES: list[str] = [
    "😂 یه نفر پرسید: AI جایگزین ما میشه؟ گفتم: نه، ولی خسته شدن ما رو جایگزین می‌کنه!",
    "😄 وای‌فای قطع شد... مردم فهمیدن همسایه‌هاشون چیه!",
    "🤣 برنامه‌نویس: این باگ نیست، فیچره!",
    "😂 چرا ربات خسته شد؟ چون زیاد تایپ کرد! ⌨️",
    "😄 سؤال: چرا کامپیوتر سرد شد؟ جواب: چون ویندوزش باز بود! 🪟",
    "🤣 AI: من هوش مصنوعی‌ام. انسان: اثبات کن. AI: ...من بیکارم!",
]

_CHALLENGES: list[str] = [
    "🏆 چالش: یه کلمه فارسی بگید که آخرش «ـه» باشه!",
    "🎯 چالش: یه جمله بگید که توش هیچ «الف» نباشه!",
    "⚡ چالش: یه عدد بگید، من میگم زوج یا فرد!",
    "🔥 چالش: سرچ کنید: «چرا آسمان آبی‌ست؟» جوابش بدون گوگل!",
    "🧩 چالش: یه اسم بگید که هر حرفش یه معنی بده!",
]

_ROASTS: list[str] = [
    "🔥 رُست: کی می‌خواد رُست بشه؟ اول کسی که لایک می‌کنه! 😈",
    "😈 رُست بَتِل! کی شجاعه؟ دو نفر بیان، بقیه داورن!",
    "🔥 توجه: رُست فقط برای تفریحه! با احترام 😊",
]

_STORIES: list[str] = [
    "📖 داستان: یه روز یه ربات کوچیک بود که آرزو داشت انسان بشه...",
    "📚 قصه: در سرزمینی دور، هوش مصنوعی زنده شد...",
    "📖 ادامه داستان: کی می‌خواد جمله بعدی رو بگه؟",
]


class EngagementEngine:
    """AI community engagement with smart timing and rate limiting."""

    # Anti-spam: minimum seconds between engagements per chat
    MIN_INTERVAL_SECONDS = 300  # 5 minutes

    # In-memory last engagement time per chat
    _last_engagement: dict[int, datetime] = {}

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def get_config(chat_id: int) -> EngagementConfig | None:
        """Return the engagement config for *chat_id*, or None."""
        engine = _sync_engine()
        with Session(engine) as session:
            return session.exec(
                select(EngagementConfig).where(EngagementConfig.chat_id == chat_id)
            ).first()

    @staticmethod
    def set_config(
        chat_id: int,
        *,
        enabled: bool,
        frequency_minutes: int = 60,
    ) -> EngagementConfig:
        """Create or update engagement config for *chat_id*."""
        engine = _sync_engine()
        with Session(engine) as session:
            existing = session.exec(
                select(EngagementConfig).where(EngagementConfig.chat_id == chat_id)
            ).first()
            if existing is not None:
                existing.enabled = enabled
                existing.frequency_minutes = frequency_minutes
                session.add(existing)
                session.commit()
                session.refresh(existing)
                return existing
            cfg = EngagementConfig(
                chat_id=chat_id,
                enabled=enabled,
                frequency_minutes=frequency_minutes,
            )
            session.add(cfg)
            session.commit()
            session.refresh(cfg)
            return cfg

    # ------------------------------------------------------------------
    # Engagement actions
    # ------------------------------------------------------------------

    @staticmethod
    def get_ice_breaker() -> str:
        """Return a random ice breaker message."""
        return random.choice(_ICE_BREAKERS)

    @staticmethod
    def get_daily_question() -> str:
        """Return a random daily question."""
        return random.choice(_DAILY_QUESTIONS)

    @staticmethod
    def get_joke() -> str:
        """Return a random Persian joke."""
        return random.choice(_JOKES)

    @staticmethod
    def get_challenge() -> str:
        """Return a random challenge."""
        return random.choice(_CHALLENGES)

    @staticmethod
    def get_roast() -> str:
        """Return a random roast prompt."""
        return random.choice(_ROASTS)

    @staticmethod
    def get_story() -> str:
        """Return a random story prompt."""
        return random.choice(_STORIES)

    @staticmethod
    def get_event() -> str:
        """Return a random group event prompt."""
        events = [
            "🎉 رویداد: مسابقه سرعت تایپ! کی سریع‌تره؟",
            "🎊 رویداد: تریویا نایت! آماده باشید!",
            "🏆 رویداد: رُست بَتِل! شجاع‌ترین کی‌ست؟",
            "🎮 رویداد: حدس عدد! از ۱ تا ۱۰۰",
            "🧠 رویداد: فکت یا فیکشن! کی دروغ‌گوست؟",
        ]
        return random.choice(events)

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def can_engage(self, chat_id: int) -> bool:
        """Check if enough time has passed since last engagement."""
        last = self._last_engagement.get(chat_id)
        if last is None:
            return True
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed >= self.MIN_INTERVAL_SECONDS

    def mark_engaged(self, chat_id: int) -> None:
        """Record that we just engaged in *chat_id*."""
        self._last_engagement[chat_id] = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Auto-engagement checker
    # ------------------------------------------------------------------

    async def check_and_engage(self, chat_id: int) -> str | None:
        """If engagement is enabled and enough time passed, return a message.

        Returns None if no engagement should happen.
        """
        cfg = self.get_config(chat_id)
        if cfg is None or not cfg.enabled:
            return None

        if not self.can_engage(chat_id):
            return None

        self.mark_engaged(chat_id)

        # Update DB timestamp
        engine = _sync_engine()
        with Session(engine) as session:
            db_cfg = session.exec(
                select(EngagementConfig).where(EngagementConfig.chat_id == chat_id)
            ).first()
            if db_cfg is not None:
                db_cfg.last_engagement = datetime.now(timezone.utc)
                session.add(db_cfg)
                session.commit()

        # Pick a random engagement type
        action = random.choice(["icebreaker", "joke", "challenge", "question", "story", "event"])
        if action == "icebreaker":
            return self.get_ice_breaker()
        if action == "joke":
            return self.get_joke()
        if action == "challenge":
            return self.get_challenge()
        if action == "question":
            return self.get_daily_question()
        if action == "story":
            return self.get_story()
        return self.get_event()
