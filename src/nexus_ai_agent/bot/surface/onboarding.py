"""Telegram surface for the first-run onboarding flow (``features/onboarding.py``).

Callbacks: ``onboarding_ai``, ``onboarding_image``, ``onboarding_explore``.

What this replaces
------------------
``bot/handlers.py`` shipped one catch-all for the whole ``^onboarding_``
namespace::

    async def onboarding_callback_handler(update, context) -> None:
        query = update.callback_query
        if query:
            await query.answer()
            await query.edit_message_text("✅ Onboarding step completed!")

Every button therefore got the same sentence. Nothing was "completed": the
keyboard the engine builds (:func:`nexus_ai_agent.features.onboarding.send_onboarding`)
asks the user to try AI chat, generate an image, or explore — and each of those
hints already exists, translated, in all 15 locale files
(``onboarding.ai_hint`` / ``onboarding.image_hint`` / ``onboarding.explore_hint``).
:func:`handle_onboarding_callback` had zero importers.

Shape of this module
--------------------
The engine stays the single source of truth for *what* each button does; this
module only resolves *which language* to answer in and delegates. Re-implementing
the branch here would recreate the defect this batch exists to remove (two
handlers for one payload, one of them a lie).

Two deliberate limits, both recorded on the board rather than hidden:

* the language comes from the caller's Telegram ``language_code``, not from the
  ``/language`` preference row — reading that row needs the async session
  factory, which lives in the composition root and is not in ``bot_data`` yet;
* ``features.onboarding`` is imported lazily inside the coroutine, because that
  module still imports ``telegram`` at module level (grandfathered in
  ``tests/architecture/legacy_baseline.json``). The surface package must stay
  importable in a bare environment — that invariant is now asserted by
  ``tests/unit/test_surface_onboarding.py``.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.i18n import DEFAULT_LANG, i18n

from ._ptb import callback_data, user_language_code

__all__ = [
    "DEFAULT_FALLBACK_LANG",
    "KNOWN_CALLBACKS",
    "UNKNOWN_TOAST",
    "onboarding_callback_cmd",
    "resolve_lang",
]

#: Language used when the caller's client reports none (some clients do not).
DEFAULT_FALLBACK_LANG = DEFAULT_LANG

#: The payloads :func:`nexus_ai_agent.features.onboarding.handle_onboarding_callback`
#: actually handles. Anything else under ``^onboarding_`` is answered and left
#: untouched — the previous handler claimed success for those too.
KNOWN_CALLBACKS = frozenset({"onboarding_ai", "onboarding_image", "onboarding_explore"})

#: Toast for payloads the engine has no branch for. Deliberately not a success
#: message, and deliberately a toast rather than an edit: the old stub rewrote
#: the onboarding message to claim "step completed" for *every* callback id,
#: destroying the keyboard in the process.
UNKNOWN_TOAST = "این دکمه در نسخهٔ فعلی رفتار دیگری ندارد — /help را ببینید."


def resolve_lang(update: Any) -> str:
    """Map the caller's Telegram language onto a locale this repo ships."""
    return i18n.detect_language(user_language_code(update))


async def onboarding_callback_cmd(update: Any, context: Any) -> None:
    """Route an ``onboarding_*`` callback to the engine's real handler."""
    # Imported here on purpose: the engine module still pulls in ``telegram``,
    # and this package must stay importable without it.
    from nexus_ai_agent.features.onboarding import handle_onboarding_callback

    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if callback_data(update) not in KNOWN_CALLBACKS:
        await query.answer(text=UNKNOWN_TOAST)
        return
    await handle_onboarding_callback(update, context, resolve_lang(update))
