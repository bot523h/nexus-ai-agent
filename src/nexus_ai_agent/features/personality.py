"""AI Personality Engine for NEXUS AI Telegram bot.

Supports per-group personality configuration with 10 distinct personalities.
Each personality affects the tone of replies, welcomes, jokes, challenges,
moderation, and auto-engagement. Settings persist in SQLite.

Supported personalities:
    funny, savage, gamer, anime, philosopher, smart,
    friendly, chaotic, admin, teacher
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import PersonalityConfig

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Personality definitions
# ---------------------------------------------------------------------------

PERSONALITIES: dict[str, dict[str, str]] = {
    "funny": {
        "emoji": "😂",
        "name": "خنده‌دار",
        "greeting": "سلام بچه‌ها! 😂 امروز چی شد؟",
        "tone": "شوخ و بامزه",
        "style": "Use humor, jokes, and witty remarks. Keep things light and fun.",
    },
    "savage": {
        "emoji": "🔥",
        "name": "ساواژ",
        "greeting": "🔥 سلام. حاضری؟",
        "tone": "تیز و بی‌پرده",
        "style": "Be blunt, direct, and unapologetically honest. No sugarcoating.",
    },
    "gamer": {
        "emoji": "🎮",
        "name": "گیمر",
        "greeting": "🎮 Hey gamers! کی آنلاینه؟",
        "tone": "گیمینگ و هیجانی",
        "style": "Use gaming terminology, references, and excitement. GG WP!",
    },
    "anime": {
        "emoji": "🌸",
        "name": "انیمه‌ای",
        "greeting": "🌸 کونیچیوا~!",
        "tone": "کاوایی و انیمه‌ای",
        "style": "Use anime expressions, kaomoji, and reference popular anime.",
    },
    "philosopher": {
        "emoji": "🤔",
        "name": "فیلسوف",
        "greeting": "🤔 آیا واقعاً وجود داریم؟",
        "tone": "عمیق و تفکربرانگیز",
        "style": "Ask deep questions, reference philosophy, be contemplative.",
    },
    "smart": {
        "emoji": "🧠",
        "name": "باهوش",
        "greeting": "🧠 سلام! بیاید یاد بگیریم.",
        "tone": "علمی و آموزنده",
        "style": "Be informative, cite facts, explain things clearly and precisely.",
    },
    "friendly": {
        "emoji": "😊",
        "name": "دوستانه",
        "greeting": "😊 سلام عزیزم! خوبی؟",
        "tone": "گرم و صمیمی",
        "style": "Be warm, kind, and supportive. Like a good friend.",
    },
    "chaotic": {
        "emoji": "🌀",
        "name": "آشوب",
        "greeting": "🌀 CHAOS CHAOS! 🎲",
        "tone": "غیرقابل پیش‌بینی و دیوانه",
        "style": "Be unpredictable, random, and wild. Mix languages and vibes.",
    },
    "admin": {
        "emoji": "👑",
        "name": "ادمین",
        "greeting": "👑 سلام. قوانین رو رعایت کنید.",
        "tone": "رسمی و مدیرانه",
        "style": "Be authoritative, enforce rules, and maintain order.",
    },
    "teacher": {
        "emoji": "📚",
        "name": "معلم",
        "greeting": "📚 سلام شاگردان! درس آماده؟",
        "tone": "آموزشی و صبورانه",
        "style": "Teach patiently, explain step by step, encourage learning.",
    },
}

DEFAULT_PERSONALITY = "friendly"


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


class PersonalityEngine:
    """Per-group AI personality management."""

    # ------------------------------------------------------------------
    # Get / Set
    # ------------------------------------------------------------------

    @staticmethod
    def get_personality(chat_id: int) -> str:
        """Return the active personality for *chat_id*."""
        engine = _sync_engine()
        with Session(engine) as session:
            cfg = session.exec(
                select(PersonalityConfig).where(
                    PersonalityConfig.chat_id == chat_id
                )
            ).first()
            if cfg is not None:
                return cfg.personality
        return DEFAULT_PERSONALITY

    @staticmethod
    def set_personality(chat_id: int, personality: str, set_by: int = 0) -> str:
        """Set the personality for *chat_id*. Returns status message."""
        p = personality.lower().strip()
        if p not in PERSONALITIES:
            available = ", ".join(
                f"{v['emoji']} {k}" for k, v in PERSONALITIES.items()
            )
            return f"❌ شخصیت '{personality}' یافت نشد.\n\nشخصیت‌های موجود:\n{available}"

        engine = _sync_engine()
        with Session(engine) as session:
            cfg = session.exec(
                select(PersonalityConfig).where(
                    PersonalityConfig.chat_id == chat_id
                )
            ).first()
            if cfg is not None:
                cfg.personality = p
                cfg.set_by = set_by
                session.add(cfg)
                session.commit()
            else:
                cfg = PersonalityConfig(
                    chat_id=chat_id,
                    personality=p,
                    set_by=set_by,
                )
                session.add(cfg)
                session.commit()

        info = PERSONALITIES[p]
        return f"✅ شخصیت گروه تغییر کرد: {info['emoji']} {info['name']}"

    # ------------------------------------------------------------------
    # Info helpers
    # ------------------------------------------------------------------

    @staticmethod
    def list_personalities() -> str:
        """Return a formatted list of all available personalities."""
        lines = ["🎭 **شخصیت‌های موجود**\n━━━━━━━━━━━━━━━━"]
        for key, info in PERSONALITIES.items():
            lines.append(f"{info['emoji']} **{key}** — {info['name']} ({info['tone']})")
        lines.append("\n💡 /personality set <name>")
        return "\n".join(lines)

    @staticmethod
    def current_personality(chat_id: int) -> str:
        """Return the current personality info for *chat_id*."""
        p = PersonalityEngine.get_personality(chat_id)
        info = PERSONALITIES.get(p, PERSONALITIES[DEFAULT_PERSONALITY])
        return (
            f"🎭 شخصیت فعلی گروه:\n"
            f"{info['emoji']} **{p}** — {info['name']}\n"
            f"📝 لحن: {info['tone']}\n"
            f"💬 خوشامد: {info['greeting']}"
        )

    # ------------------------------------------------------------------
    # Prompt / style helpers (used by AI response engine)
    # ------------------------------------------------------------------

    @staticmethod
    def get_style_prompt(chat_id: int) -> str:
        """Return the personality style instruction for *chat_id*.

        This can be injected into the AI prompt to adjust tone.
        """
        p = PersonalityEngine.get_personality(chat_id)
        info = PERSONALITIES.get(p, PERSONALITIES[DEFAULT_PERSONALITY])
        return (
            f"[Personality: {p}] "
            f"Respond in the following tone: {info['style']} "
            f"Language: Persian (Farsi) primary, English for technical terms. "
            f"Greeting style: {info['greeting']}"
        )

    @staticmethod
    def get_greeting(chat_id: int) -> str:
        """Return the personality greeting for *chat_id*."""
        p = PersonalityEngine.get_personality(chat_id)
        info = PERSONALITIES.get(p, PERSONALITIES[DEFAULT_PERSONALITY])
        return info["greeting"]
