"""User Onboarding Flow — interactive first-time experience.

When a new user starts the bot for the first time, instead of just showing
the main menu, we guide them through a brief onboarding:

1. Welcome message with bot capabilities
2. Auto-detect their language
3. Show a quick feature highlight
4. Offer a /ai demo prompt

A user is first-time when no ``UserLanguage`` row exists. The row is written
only after the onboarding message is sent, so a failed send can be retried.
An existing row — including one written by ``/language`` — is never overwritten.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.i18n import SUPPORTED_LANGUAGES, i18n

logger = structlog.get_logger(__name__)

# Keys actually rendered. ``onboarding.language`` is not in the locale files
# and must not be required — language is detected, not asked.
_ONBOARDING_STEPS = [
    "onboarding.welcome",
    "onboarding.features",
    "onboarding.try_ai",
]


def _row_from(session: Any, stmt: Any) -> Any:
    """Read one row from a SQLModel session (``exec``) or SQLAlchemy (``execute``).

    PR #33 showed that a SQLAlchemy ``AsyncSession`` has ``execute`` but not
    ``exec``. Swallowing that ``AttributeError`` treated every caller as a
    first-time user. Callers of this helper fail closed when it raises.
    """
    if hasattr(session, "exec"):
        result = session.exec(stmt)
        return result.first() if hasattr(result, "first") else None
    result = session.execute(stmt)
    scalars = result.scalars() if hasattr(result, "scalars") else result
    return scalars.first()


class OnboardingStore:
    """Sync SQLite store for the language recorded on a user's first ``/start``."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._engine: Any | None = None
        self._lock = threading.Lock()

    def _engine_ref(self) -> Any:
        if self._engine is None:
            from sqlalchemy import create_engine

            path = self._db_path or get_settings().db_path
            self._engine = create_engine(
                f"sqlite:///{path}",
                echo=False,
                connect_args={"check_same_thread": False},
            )
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def is_first_time(self, user_id: int) -> bool:
        try:
            from sqlmodel import Session
            from sqlmodel import select as _sel

            from nexus_ai_agent.storage.models import UserLanguage

            with self._lock, Session(self._engine_ref()) as session:
                stmt = _sel(UserLanguage).where(UserLanguage.user_id == user_id)
                return _row_from(session, stmt) is None
        except Exception:  # noqa: BLE001
            logger.exception("onboarding_first_time_check_failed", user_id=user_id)
            return False

    def language_for(self, user_id: int, fallback: str = "en") -> str:
        try:
            from sqlmodel import Session
            from sqlmodel import select as _sel

            from nexus_ai_agent.storage.models import UserLanguage

            with Session(self._engine_ref()) as session:
                stmt = _sel(UserLanguage).where(UserLanguage.user_id == user_id)
                row = _row_from(session, stmt)
                if row is not None and row.language in SUPPORTED_LANGUAGES:
                    return str(row.language)
        except Exception:  # noqa: BLE001
            logger.exception("onboarding_language_lookup_failed", user_id=user_id)
        return fallback if fallback in SUPPORTED_LANGUAGES else "en"

    def remember(self, user_id: int, language: str) -> str:
        """Insert the language if absent. Returns the language that is stored."""
        stored = language if language in SUPPORTED_LANGUAGES else "en"
        from sqlmodel import Session
        from sqlmodel import select as _sel

        from nexus_ai_agent.storage.models import UserLanguage

        with self._lock, Session(self._engine_ref()) as session:
            stmt = _sel(UserLanguage).where(UserLanguage.user_id == user_id)
            existing = _row_from(session, stmt)
            if existing is not None:
                return str(existing.language)
            session.add(UserLanguage(user_id=user_id, language=stored))
            session.commit()
        return stored


async def is_first_time_user(user_id: int, db_session_factory: Any) -> bool:
    """Return True if the user has no stored language.

    Supports a SQLModel session (``exec``) and a SQLAlchemy async session
    (``execute``). Operational failures return False so a broken session cannot
    onboard every subsequent message.
    """
    try:
        from sqlmodel import select as _sel

        from nexus_ai_agent.storage.models import UserLanguage

        async with db_session_factory() as session:
            stmt = _sel(UserLanguage).where(UserLanguage.user_id == user_id)
            if hasattr(session, "exec"):
                existing = (await session.exec(stmt)).first()
            else:
                result = await session.execute(stmt)
                existing = result.scalars().first()
            return existing is None
    except Exception:  # noqa: BLE001
        logger.exception("onboarding_first_time_check_failed", user_id=user_id)
        return False


def _message_of(update: Any) -> Any:
    message = getattr(update, "effective_message", None)
    if message is None:
        message = getattr(update, "message", None)
    return message


async def send_onboarding(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None,
    lang: str,
) -> bool:
    """Send the interactive onboarding flow. True only when the reply succeeded.

    ``context`` is unused; it stays so existing callers that pass the PTB
    context keep working. Markdown is attempted first and falls back to plain
    text, because a locale string is not a guaranteed Markdown document.
    """
    del context
    message = _message_of(update)
    if message is None:
        return False

    welcome = i18n.t("onboarding.welcome", lang=lang)
    features = i18n.t("onboarding.features", lang=lang)
    try_ai = i18n.t("onboarding.try_ai", lang=lang)
    text = f"{welcome}\n\n{features}\n\n{try_ai}"

    keyboard = [
        [
            InlineKeyboardButton(
                i18n.t("onboarding.btn_ai", lang=lang),
                callback_data="onboarding_ai",
            ),
            InlineKeyboardButton(
                i18n.t("onboarding.btn_image", lang=lang),
                callback_data="onboarding_image",
            ),
        ],
        [
            InlineKeyboardButton(
                i18n.t("onboarding.btn_explore", lang=lang),
                callback_data="onboarding_explore",
            ),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:  # noqa: BLE001
        logger.exception("onboarding_markdown_failed")
        try:
            await message.reply_text(text, reply_markup=reply_markup)
        except Exception:  # noqa: BLE001
            logger.exception("onboarding_send_failed")
            return False
    return True


async def maybe_onboard(update: Any, context: Any, store: OnboardingStore) -> bool:
    """Send onboarding on the first successful ``/start`` and persist the language.

    Missing ``language_code`` is stored as ``en``. A send failure does not
    persist, so the next ``/start`` retries. A store failure is logged and does
    not raise — ``/start`` must still complete its other work.
    """
    user = getattr(update, "effective_user", None)
    if user is None:
        return False
    try:
        user_id = int(user.id)
        if not store.is_first_time(user_id):
            return False
        language = i18n.detect_language(getattr(user, "language_code", None))
        sent = await send_onboarding(update, context, language)
        if sent:
            store.remember(user_id, language)
        return sent
    except Exception:  # noqa: BLE001
        logger.exception("onboarding_failed")
        return False


async def handle_onboarding_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
) -> None:
    """Handle onboarding inline button presses. Unknown data is acknowledged only."""
    del context
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except Exception:  # noqa: BLE001
        logger.exception("onboarding_callback_answer_failed")

    data = query.data or ""
    if data == "onboarding_ai":
        text = (
            i18n.t("onboarding.ai_hint", lang=lang)
            + "\n\n"
            + i18n.t("onboarding.go_chat", lang=lang)
        )
    elif data == "onboarding_image":
        text = (
            i18n.t("onboarding.image_hint", lang=lang)
            + "\n\n"
            + i18n.t("onboarding.go_chat", lang=lang)
        )
    elif data == "onboarding_explore":
        text = i18n.t("onboarding.explore_hint", lang=lang)
    else:
        return
    try:
        await query.edit_message_text(text, parse_mode="Markdown")
    except Exception:  # noqa: BLE001
        logger.exception("onboarding_callback_edit_failed")
        try:
            await query.edit_message_text(text)
        except Exception:  # noqa: BLE001
            logger.exception("onboarding_callback_edit_plain_failed")
