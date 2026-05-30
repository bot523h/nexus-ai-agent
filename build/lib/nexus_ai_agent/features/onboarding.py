"""User Onboarding Flow — interactive first-time experience.

When a new user starts the bot for the first time, instead of just showing
the main menu, we guide them through a brief onboarding:

1. Welcome message with bot capabilities
2. Auto-detect their language
3. Show a quick feature highlight
4. Offer a /ai demo prompt

This makes the first impression memorable and reduces the "what can this bot do?"
confusion that leads to user drop-off.
"""

from __future__ import annotations

from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from nexus_ai_agent.i18n import i18n

logger = structlog.get_logger(__name__)

# Onboarding steps as i18n keys
_ONBOARDING_STEPS = [
    "onboarding.welcome",
    "onboarding.language",
    "onboarding.features",
    "onboarding.try_ai",
]


async def is_first_time_user(user_id: int, db_session_factory: Any) -> bool:
    """Check if this is the user's first interaction with the bot."""
    try:
        from sqlmodel import select as _sel

        from nexus_ai_agent.storage.models import UserLanguage

        async with db_session_factory() as session:
            existing = (
                await session.exec(_sel(UserLanguage).where(UserLanguage.user_id == user_id))
            ).first()
            return existing is None
    except Exception:
        # If we can't check, assume first time (better to onboard than not)
        return True


async def send_onboarding(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
) -> None:
    """Send the interactive onboarding flow as a single rich message."""

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

    message = update.effective_message
    if message is not None:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def handle_onboarding_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
) -> None:
    """Handle onboarding inline button presses."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()

    data = query.data
    if data == "onboarding_ai":
        await query.edit_message_text(
            i18n.t("onboarding.ai_hint", lang=lang)
            + "\n\n"
            + i18n.t("onboarding.go_chat", lang=lang),
            parse_mode="Markdown",
        )
    elif data == "onboarding_image":
        await query.edit_message_text(
            i18n.t("onboarding.image_hint", lang=lang)
            + "\n\n"
            + i18n.t("onboarding.go_chat", lang=lang),
            parse_mode="Markdown",
        )
    elif data == "onboarding_explore":
        await query.edit_message_text(
            i18n.t("onboarding.explore_hint", lang=lang),
            parse_mode="Markdown",
        )
