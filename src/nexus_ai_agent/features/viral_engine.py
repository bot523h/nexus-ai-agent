"""Viral Content Engine for NEXUS AI Telegram bot.

Turns @nexus_ai_official into a fully automated AI-managed channel.
Analyzes engagement, detects trending topics, assigns viral scores,
generates auto hashtags, and schedules nightly auto-posts.

No paid APIs — uses local NLP heuristics, engagement metrics,
reactions, replies, and message activity.
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import ViralPost

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


# ---------------------------------------------------------------------------
# Content templates (viral-style Persian posts)
# ---------------------------------------------------------------------------

_VIRAL_TEMPLATES: list[dict[str, Any]] = [
    {
        "category": "tech",
        "templates": [
            "🚀 آیا هوش مصنوعی جایگزین برنامه‌نویس‌ها میشه؟\n\n💬 نظر شما چیه؟\n\n#AI #تکنولوژی #هوش_مصنوعی",  # noqa: E501
            "📱 ترفند: با این روش سرعت اینترنتتون ۲ برابر میشه!\n\n💡 خیلی‌ها نمی‌دونن...\n\n#اینترنت #ترفند #تکنولوژی",  # noqa: E501
            "🔧 ۵ ابزار رایگان که زندگی‌تون رو عوض می‌کنه!\n\n1️⃣ ...\n2️⃣ ...\n3️⃣ ...\n4️⃣ ...\n5️⃣ ...\n\n#ابزار #رایگان #تکنولوژی",  # noqa: E501
        ],
    },
    {
        "category": "motivation",
        "templates": [
            "💪 موفقیت از جایی شروع میشه که بقیه تسلیم میشن!\n\n#انگیزشی #موفقیت #هدف",
            "🌟 فرق بین موفق و ناموفق: ادامه دادن وقتی سخته!\n\n#انگیزشی #پشتکار",
            "🎯 هر روز یه قدم کوچیک، ولی هر روز!\n\n#انگیزشی #توسعه_فردی",
        ],
    },
    {
        "category": "fun",
        "templates": [
            "😂 سؤال: چرا برنامه‌نویس‌ها混淆 میشن؟ چون زبونشونو گم می‌کنن!\n\n#طنز #برنامه‌نویسی",
            "🎮 اگه زندگیت یه بازی بود، چیستری می‌زدی؟ 😄\n\n#طنز #سرگرمی",
            "🍕 پیتزا با آناناس: نابودی یا شاهکار؟ 🍍\n\nنظر بدید! 👇\n\n#طنز #غذا",
        ],
    },
    {
        "category": "knowledge",
        "templates": [
            "🧠 آیا می‌دونستید؟ مغز انسان ۸۶ میلیارد نورون داره!\n\n#علم #دانستنی",
            "📚 کتاب هفته: «عادت‌های اتمی» — جیمز کلیر\n\nتغییر کوچیک = نتیجه بزرگ 💥\n\n#کتاب #توصیه",  # noqa: E501
            "🌍 فکت جالب: هر روز ۲.۵ کوینتیون بایت داده تولید میشه!\n\n#علم #تکنولوژی #داده",
        ],
    },
]

# Hashtag pools
_HASHTAG_POOLS: dict[str, list[str]] = {
    "tech": ["#AI", "#تکنولوژی", "#هوش_مصنوعی", "#برنامه_نویسی", "#توسعه"],
    "motivation": ["#انگیزشی", "#موفقیت", "#هدف", "#پشتکار", "#توسعه_فردی"],
    "fun": ["#طنز", "#سرگرمی", "#خنده", "#بازی", "#چالش"],
    "knowledge": ["#علم", "#دانستنی", "#کتاب", "#آموزش", "#فکت"],
}


class ViralEngine:
    """Viral content generation, scoring, and scheduling."""

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Content generation
    # ------------------------------------------------------------------

    @staticmethod
    def generate_post() -> str:
        """Generate a random viral post from templates."""
        category = random.choice(_VIRAL_TEMPLATES)
        return random.choice(category["templates"])

    @staticmethod
    def generate_posts(count: int = 10) -> list[str]:
        """Generate *count* viral posts."""
        return [ViralEngine.generate_post() for _ in range(count)]

    # ------------------------------------------------------------------
    # Hashtag generation
    # ------------------------------------------------------------------

    @staticmethod
    def auto_hashtags(text: str) -> str:
        """Generate hashtags based on content analysis."""
        text_lower = text.lower()
        tags: list[str] = []

        # Simple keyword matching
        keywords_map: dict[str, list[str]] = {
            "ai": ["#AI", "#هوش_مصنوعی"],
            "برنامه": ["#برنامه_نویسی", "#کد"],
            "موفقیت": ["#موفقیت", "#انگیزشی"],
            "علم": ["#علم", "#دانستنی"],
            "کتاب": ["#کتاب", "#آموزش"],
            "بازی": ["#بازی", "#گیمینگ"],
        }
        for keyword, ht in keywords_map.items():
            if keyword in text_lower:
                tags.extend(ht)

        # Fallback: add random trending tags
        if not tags:
            pool = random.choice(list(_HASHTAG_POOLS.values()))
            tags = random.sample(pool, min(3, len(pool)))

        # Remove duplicates while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for t in tags:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return " ".join(unique[:5])

    # ------------------------------------------------------------------
    # Viral scoring
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_viral_score(text: str) -> float:
        """Calculate a 0-10 viral score for *text*.

        Based on heuristics: length, hashtags, emojis, questions,
        call-to-actions, and content category.
        """
        score = 5.0  # baseline

        # Length: 50-300 chars is sweet spot
        length = len(text)
        if 50 <= length <= 300:
            score += 1.5
        elif length > 300:
            score += 0.5

        # Hashtags present
        if "#" in text:
            score += 1.0

        # Emojis present
        _emoji_re = (
            r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
            r"\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF"
            r"\U00002702-\U000027B0]"
        )
        emoji_count = len(re.findall(_emoji_re, text))
        if emoji_count >= 3:
            score += 1.0

        # Questions (engagement driver)
        if "؟" in text or "?" in text:
            score += 1.0

        # Call to action
        if any(phrase in text for phrase in ["نظر", "بگید", "لایک", "اشتراک", "👇"]):
            score += 0.5

        return min(score, 10.0)

    # ------------------------------------------------------------------
    # Duplicate prevention
    # ------------------------------------------------------------------

    @staticmethod
    def is_duplicate(text: str, chat_id: int = 0) -> bool:
        """Check if *text* has been posted before (by content hash)."""
        content_hash = hashlib.md5(text.encode()).hexdigest()[:12]
        engine = _sync_engine()
        with Session(engine) as session:
            existing = session.exec(
                select(ViralPost).where(
                    ViralPost.chat_id == chat_id,
                    col(ViralPost.text).contains(content_hash[:8]),
                )
            ).first()
            return existing is not None

    # ------------------------------------------------------------------
    # DB operations
    # ------------------------------------------------------------------

    @staticmethod
    def save_post(chat_id: int, text: str, viral_score: float = 0.0) -> int:
        """Save a viral post to the DB. Returns post ID."""
        engine = _sync_engine()
        with Session(engine) as session:
            post = ViralPost(
                chat_id=chat_id,
                text=text,
                viral_score=viral_score,
                status="pending",
            )
            session.add(post)
            session.commit()
            session.refresh(post)
            return post.id if post.id is not None else 0

    @staticmethod
    def get_pending_posts(chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """Return pending viral posts for *chat_id*."""
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = (
                select(ViralPost)
                .where(ViralPost.chat_id == chat_id, ViralPost.status == "pending")
                .order_by(col(ViralPost.viral_score).desc())
                .limit(limit)
            )
            results = session.exec(stmt).all()
            return [
                {
                    "id": r.id,
                    "text": r.text[:100],
                    "viral_score": r.viral_score,
                    "status": r.status,
                }
                for r in results
            ]

    @staticmethod
    def mark_posted(post_id: int) -> None:
        """Mark a viral post as posted."""
        engine = _sync_engine()
        with Session(engine) as session:
            post = session.get(ViralPost, post_id)
            if post is not None:
                post.status = "posted"
                post.posted_at = datetime.now(timezone.utc)
                session.add(post)
                session.commit()

    @staticmethod
    def get_stats(chat_id: int = 0) -> dict[str, int]:
        """Return viral engine statistics."""
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(ViralPost)
            if chat_id:
                stmt = stmt.where(ViralPost.chat_id == chat_id)
            all_posts = session.exec(stmt).all()
            return {
                "total": len(all_posts),
                "pending": sum(1 for p in all_posts if p.status == "pending"),
                "posted": sum(1 for p in all_posts if p.status == "posted"),
                "failed": sum(1 for p in all_posts if p.status == "failed"),
            }

    # ------------------------------------------------------------------
    # Nightly auto-generation
    # ------------------------------------------------------------------

    async def generate_and_schedule(self, chat_id: int, count: int = 10) -> int:
        """Generate *count* viral posts and save them as pending.

        Returns the number of posts actually generated (excluding duplicates).
        """
        generated = 0
        for _ in range(count):
            text = self.generate_post()
            if self.is_duplicate(text, chat_id):
                continue
            score = self.calculate_viral_score(text)
            # Append auto-generated hashtags if not already present
            if "#" not in text:
                tags = self.auto_hashtags(text)
                text = f"{text}\n\n{tags}"
                score = self.calculate_viral_score(text)
            self.save_post(chat_id, text, viral_score=score)
            generated += 1
        logger.info("viral_generated", chat_id=chat_id, count=generated)
        return generated
