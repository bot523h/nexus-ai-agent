"""Tests for the onboarding callback surface and for the package's import budget.

``bot/handlers.py`` handled every ``^onboarding_`` payload with one fake
confirmation. :mod:`nexus_ai_agent.bot.surface.onboarding` now routes the three
real payloads to the engine's own handler, which answers in the caller's
language with the hint stored in all 15 locale files — and leaves unknown
payloads alone instead of claiming success for them.

The last test in this file guards something the whole package depends on and
that a future "tidy-up" import would silently break: ``bot/surface`` must be
importable when ``telegram`` is not, because the registration tests run in a
bare job.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from surface_fakes import FakeCallback, make_context, make_update

from nexus_ai_agent.bot.surface import onboarding as surface

#: The sentence the removed stub sent for every single callback id.
FAKE_CONFIRMATION = "Onboarding step completed!"


def _update(data: str, *, language: str | None = "en", with_query: bool = True) -> Any:
    callback = FakeCallback(data, user_id=1) if with_query else None
    return make_update(user_id=1, chat_id=10, callback=callback, language_code=language)


# ── routing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "needle_en"),
    [
        ("onboarding_ai", "/ask"),
        ("onboarding_image", "/image"),
        ("onboarding_explore", "All Commands:"),
    ],
)
async def test_each_button_gets_its_own_hint(payload: str, needle_en: str) -> None:
    """One fixed string for three buttons was the bug; three hints is the fix."""
    update = _update(payload)

    await surface.onboarding_callback_cmd(update, make_context())

    query = update.callback_query
    assert query.edits, "the callback message was never edited"
    assert len(query.edits) == 1
    assert needle_en in query.edits[0]
    assert FAKE_CONFIRMATION not in query.edits[0]
    assert query.answers == [None]  # acknowledged, no error toast


async def test_ai_hint_also_carries_the_engine_own_follow_up_line() -> None:
    update = _update("onboarding_ai")

    await surface.onboarding_callback_cmd(update, make_context())

    edited = update.callback_query.edits[0]
    assert "Send any message to start chatting!" in edited


async def test_the_hint_follows_the_callers_language() -> None:
    update = _update("onboarding_ai", language="fa-IR")

    await surface.onboarding_callback_cmd(update, make_context())

    edited = update.callback_query.edits[0]
    assert "نکته" in edited
    assert "Send any message" not in edited


async def test_a_missing_language_falls_back_without_raising() -> None:
    update = _update("onboarding_image", language=None)

    await surface.onboarding_callback_cmd(update, make_context())

    assert surface.DEFAULT_FALLBACK_LANG == "en"
    assert "/image" in update.callback_query.edits[0]


async def test_unknown_payload_is_answered_but_the_message_is_kept() -> None:
    """The old stub edited the message away for *every* id under the pattern."""
    update = _update("onboarding_whatever_comes_next")

    await surface.onboarding_callback_cmd(update, make_context())

    query = update.callback_query
    assert query.edits == [], "an unknown payload must not rewrite the message"
    assert query.answers == [surface.UNKNOWN_TOAST]
    assert FAKE_CONFIRMATION not in str(query.answers)


async def test_an_update_without_a_callback_query_is_a_no_op() -> None:
    update = _update("", with_query=False)

    await surface.onboarding_callback_cmd(update, make_context())  # must not raise

    assert update.replies == []


def test_resolve_lang_maps_telegram_codes_onto_shipped_locales() -> None:
    assert surface.resolve_lang(_update("", language="fa-IR")) == "fa"
    assert surface.resolve_lang(_update("", language="en-US")) == "en"
    assert surface.resolve_lang(_update("", language=None)) == surface.DEFAULT_FALLBACK_LANG
    assert surface.resolve_lang(_update("", language="zz")) in surface_shipped_locales()


def surface_shipped_locales() -> set[str]:
    from nexus_ai_agent.i18n import i18n

    return set(i18n.get_available_languages())


# ── the import budget of the whole package ────────────────────────────────


_BARE_IMPORT_PROBE = """
import sys

sys.modules["telegram"] = None  # `import telegram` now raises ImportError
sys.modules["telegram.error"] = None
sys.modules["telegram.ext"] = None

import nexus_ai_agent.bot.surface as surface

assert "ad_create" in surface.COMMAND_HANDLERS
assert "post" in surface.COMMAND_HANDLERS
assert "welcome" in surface.COMMAND_HANDLERS
assert callable(surface.onboarding_callback_cmd)
print("SURFACE_IMPORTS_WITHOUT_TELEGRAM")
"""


def test_the_surface_package_imports_without_telegram() -> None:
    """``bot/surface`` stays importable in a bare environment.

    ``features.channel_manager`` and ``features.onboarding`` import PTB at module
    level, which is why :func:`manager_for` and the onboarding coroutine import
    them lazily. This test is what stops a future top-level ``from
    nexus_ai_agent.features.channel_manager import …`` tidy-up from breaking the
    cheap registration job — and it runs in a subprocess because
    ``sys.modules`` is shared with every other test in the session.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, this interpreter only
        [sys.executable, "-c", _BARE_IMPORT_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "SURFACE_IMPORTS_WITHOUT_TELEGRAM" in result.stdout
