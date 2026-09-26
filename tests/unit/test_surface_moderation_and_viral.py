"""Unit contract for the moderation / viral / status Telegram surfaces.

These three modules replaced eleven handlers that answered with constants (or,
for ``/storage`` and ``/model``, with nothing at all). The parsing and
rendering helpers are deliberately pure so they can be tested here without a
Telegram object, a database, or an event loop — the same shape as
``tests/unit/test_surface_ads.py``.

The command coroutines are exercised with minimal fakes through the ``._ptb``
duck-typed accessors, which is the whole reason that indirection exists.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from nexus_ai_agent.bot.surface import moderation as mod
from nexus_ai_agent.bot.surface import status as st
from nexus_ai_agent.bot.surface import viral
from nexus_ai_agent.core.timeutil import utcnow

# ── fakes ───────────────────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, reply_to_user: int | None = None) -> None:
        self.replies: list[str] = []
        self.reply_to_message = (
            type("Quoted", (), {"from_user": type("User", (), {"id": reply_to_user})()})()
            if reply_to_user is not None
            else None
        )

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(
        self, *, user: int | None = 1, chat: int | None = -100, reply_to_user: int | None = None
    ) -> None:
        self.effective_user = type("User", (), {"id": user})() if user is not None else None
        self.effective_chat = type("Chat", (), {"id": chat})() if chat is not None else None
        self.message = FakeMessage(reply_to_user)
        self.effective_message = self.message
        self.callback_query = None

    @property
    def replies(self) -> list[str]:
        return self.message.replies


class FakeContext:
    def __init__(self, *args_: str) -> None:
        self.args = list(args_)
        self.bot_data: dict[str, Any] = {}


def run(coro: Any) -> None:
    asyncio.run(coro)


# ── moderation: parse_switches ──────────────────────────────────────────────


def test_empty_config_args_are_an_error_not_a_silent_success() -> None:
    """The stub answered "Moderation rules updated." for an empty invocation."""
    result = mod.parse_switches([])
    assert isinstance(result, str)
    assert "استفاده" in result


@pytest.mark.parametrize("value", ["on", "true", "1", "yes", "روشن", "فعال"])
def test_truthy_switch_spellings(value: str) -> None:
    assert mod.parse_switches([f"spam={value}"]) == {"anti_spam": True}


@pytest.mark.parametrize("value", ["off", "false", "0", "no", "خاموش", "غیرفعال"])
def test_falsy_switch_spellings(value: str) -> None:
    assert mod.parse_switches([f"spam={value}"]) == {"anti_spam": False}


def test_unknown_key_is_rejected_by_name() -> None:
    result = mod.parse_switches(["nonsense=on"])
    assert isinstance(result, str)
    assert "nonsense" in result


def test_malformed_pair_is_rejected() -> None:
    assert isinstance(mod.parse_switches(["spam"]), str)
    assert isinstance(mod.parse_switches(["spam="]), str)


def test_non_boolean_value_for_a_switch_is_rejected() -> None:
    result = mod.parse_switches(["spam=maybe"])
    assert isinstance(result, str)
    assert "maybe" in result


@pytest.mark.parametrize("bad", ["0", "21", "-3", "abc", "999999"])
def test_max_warnings_is_bounded(bad: str) -> None:
    """An unbounded max_warnings lets one typo disable moderation entirely."""
    assert isinstance(mod.parse_switches([f"warnings={bad}"]), str)


def test_max_warnings_accepts_the_range() -> None:
    assert mod.parse_switches(["warnings=1"]) == {"max_warnings": 1}
    assert mod.parse_switches(["warnings=20"]) == {"max_warnings": 20}


@pytest.mark.parametrize("bad", ["0", "1441", "-1", "x"])
def test_config_mute_duration_is_bounded(bad: str) -> None:
    assert isinstance(mod.parse_switches([f"mute={bad}"]), str)


def test_several_switches_in_one_call() -> None:
    parsed = mod.parse_switches(["spam=on", "links=off", "warnings=5"])
    assert parsed == {"anti_spam": True, "link_filter": False, "max_warnings": 5}


# ── moderation: target & duration ───────────────────────────────────────────


def test_a_quoted_message_names_the_target() -> None:
    update = FakeUpdate(reply_to_user=555)
    assert mod.parse_target_user(update, []) == 555


def test_a_numeric_argument_names_the_target() -> None:
    assert mod.parse_target_user(FakeUpdate(), ["12345"]) == 12345
    assert mod.parse_target_user(FakeUpdate(), ["@12345"]) == 12345


def test_no_target_means_no_target() -> None:
    """A moderation command must never fall back to acting on the caller."""
    assert mod.parse_target_user(FakeUpdate(user=42), []) is None
    assert mod.parse_target_user(FakeUpdate(user=42), ["@someuser"]) is None


def test_reply_based_mute_honours_its_only_number() -> None:
    """Regression: ``/mute 45`` on a reply used to silently mute for 30.

    The first draft of ``parse_duration_minutes`` only read a duration when two
    numbers were present, so the operator's 45 was discarded and the default
    applied — a moderation command disregarding its own argument.
    """
    assert mod.parse_duration_minutes(["45"], target_from_reply=True) == 45


def test_id_based_mute_reads_the_second_number() -> None:
    assert mod.parse_duration_minutes(["12345", "45"], target_from_reply=False) == 45


def test_mute_duration_defaults_when_unspecified() -> None:
    assert mod.parse_duration_minutes([], target_from_reply=True) == mod.DEFAULT_MUTE_MINUTES
    assert (
        mod.parse_duration_minutes(["12345"], target_from_reply=False) == mod.DEFAULT_MUTE_MINUTES
    )


@pytest.mark.parametrize("bad", ["0", "1441", "99999"])
def test_mute_duration_is_bounded(bad: str) -> None:
    """An unbounded duration is a permanent ban wearing a mute's clothes."""
    result = mod.parse_duration_minutes([bad], target_from_reply=True)
    assert isinstance(result, str)


# ── moderation: rendering ───────────────────────────────────────────────────


class FakeConfig:
    anti_spam = True
    anti_flood = False
    link_filter = True
    profanity_filter = True
    max_warnings = 3
    mute_duration_minutes = 30


def test_config_render_reports_every_switch() -> None:
    rendered = mod.format_config(FakeConfig())
    assert "ضداسپم" in rendered
    assert "روشن" in rendered and "خاموش" in rendered
    assert "3" in rendered and "30" in rendered


def test_missing_config_is_reported_as_missing() -> None:
    assert "ذخیره نشده" in mod.format_config(None)


def test_warned_render_uses_the_persisted_count() -> None:
    assert mod.format_warned(7, 2, 3, muted=False) == ("⚠️ اخطار ثبت شد برای کاربر 7: 2/3")
    assert "ساکت شد" in mod.format_warned(7, 3, 3, muted=True)


def test_unmute_render_is_honest_about_a_no_op() -> None:
    assert "ساکت نبود" in mod.format_unmuted(7, was_muted=False)
    assert "برداشته شد" in mod.format_unmuted(7, was_muted=True)


class FakeReputation:
    def __init__(self, *, muted: bool, minutes: int = 10) -> None:
        self.reputation = 12
        self.warnings = 2
        self.is_muted = muted
        self.mute_until = utcnow() + timedelta(minutes=minutes) if muted else None


def test_reputation_render_has_no_invented_scale() -> None:
    """The stub said "85/100 (Good)" — there is no /100 scale in the schema."""
    rendered = mod.format_reputation(7, FakeReputation(muted=False))
    assert "/100" not in rendered
    assert "12" in rendered and "2" in rendered
    assert "آزاد" in rendered


def test_reputation_render_shows_remaining_mute() -> None:
    rendered = mod.format_reputation(7, FakeReputation(muted=True, minutes=10))
    assert "ساکت" in rendered


def test_missing_reputation_is_reported_as_missing() -> None:
    assert "سابقه‌ای ثبت نشده" in mod.format_reputation(7, None)


# ── moderation: command gating ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    ["mod_config_cmd", "mod_warn_cmd", "mod_mute_cmd", "mod_unmute_cmd"],
)
def test_write_commands_refuse_non_owners(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    monkeypatch.setattr(mod, "is_owner", lambda _uid: False)
    update = FakeUpdate(user=999)
    run(getattr(mod, command)(update, FakeContext()))
    assert update.replies == [mod._DENIED]


def test_reputation_is_readable_by_anyone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "is_owner", lambda _uid: False)
    monkeypatch.setattr(
        mod.ModerationEngine, "get_reputation", staticmethod(lambda *_a, **_k: None)
    )
    update = FakeUpdate(user=999, reply_to_user=7)
    run(mod.mod_reputation_cmd(update, FakeContext()))
    assert update.replies and update.replies[0] != mod._DENIED


def test_warn_without_a_target_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "is_owner", lambda _uid: True)
    update = FakeUpdate(user=1)
    run(mod.mod_warn_cmd(update, FakeContext()))
    assert update.replies == [mod._NO_TARGET]


# ── viral ───────────────────────────────────────────────────────────────────


def test_viral_stats_render_counts_only_real_columns() -> None:
    """The stub claimed "450 likes"; no column records reactions."""
    rendered = viral.format_stats({"total": 9, "pending": 4, "posted": 5, "failed": 0})
    assert "9" in rendered and "4" in rendered and "5" in rendered
    assert "450" not in rendered
    assert "واکنش" in rendered  # states plainly that likes are not measured


def test_viral_stats_render_handles_an_empty_chat() -> None:
    assert "هنوز هیچ" in viral.format_stats({"total": 0, "pending": 0, "posted": 0, "failed": 0})


def test_viral_pending_render_lists_real_rows() -> None:
    rendered = viral.format_pending(
        [
            {"id": 3, "text": "aaa bbb", "viral_score": 8.5},
            {"id": 4, "text": "ccc", "viral_score": 7.0},
        ]
    )
    assert "#3" in rendered and "#4" in rendered
    assert "8.5" in rendered


def test_viral_pending_render_handles_an_empty_queue() -> None:
    """The stub always said "3 in queue" — including when the queue was empty."""
    rendered = viral.format_pending([])
    assert "خالی" in rendered
    assert "3" not in rendered


def test_viral_preview_render_says_nothing_was_saved() -> None:
    rendered = viral.format_preview("متن نمونه", 7.5)
    assert "متن نمونه" in rendered
    assert "ذخیره نشده" in rendered


def test_parse_post_id() -> None:
    assert viral.parse_post_id(["12"]) == 12
    assert viral.parse_post_id([]) is None
    assert viral.parse_post_id(["abc"]) is None


def test_viral_post_refuses_a_non_owner_marking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viral, "is_owner", lambda _uid: False)
    update = FakeUpdate(user=999)
    run(viral.viral_post_cmd(update, FakeContext("5")))
    assert update.replies == [viral._DENIED]


def test_viral_post_lists_without_owner_rights(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viral, "is_owner", lambda _uid: False)
    monkeypatch.setattr(viral.ViralEngine, "get_pending_posts", staticmethod(lambda *_a, **_k: []))
    update = FakeUpdate(user=999)
    run(viral.viral_post_cmd(update, FakeContext()))
    assert update.replies == ["📋 صف پست‌های ویرال خالی است. با /viral_now یک پست بسازید."]


# ── status ──────────────────────────────────────────────────────────────────


def test_model_render_never_prints_a_key() -> None:
    rendered = st.format_model_status(
        [("nexus-groq", "groq/llama-3.3-70b"), ("nexus-gemini", "gemini/gemini-2.0-flash")],
        routing_enabled=True,
    )
    assert "nexus-groq" in rendered and "groq/llama-3.3-70b" in rendered
    assert "sk-" not in rendered and "key" not in rendered.lower()


def test_model_render_admits_an_empty_chain() -> None:
    rendered = st.format_model_status([], routing_enabled=True)
    assert "FakeLLM" in rendered


def test_storage_render_marks_configured_providers() -> None:
    rendered = st.format_storage_status(
        db_path="data/nexus.db",
        db_mb=1.5,
        cache_dir="data/cache",
        cache_mb=None,
        clouds=[("Dropbox", True), ("pCloud", False)],
    )
    assert "data/nexus.db" in rendered and "1.5 MB" in rendered
    assert "ساخته نشده" in rendered
    assert "✅ Dropbox" in rendered and "➖ pCloud" in rendered


def test_storage_render_handles_a_purely_local_install() -> None:
    rendered = st.format_storage_status(
        db_path="data/nexus.db",
        db_mb=0.1,
        cache_dir="data/cache",
        cache_mb=0.0,
        clouds=[("Dropbox", False)],
    )
    assert "فقط محلی" in rendered


def test_story_style_admits_the_styles_do_not_exist() -> None:
    """create_story() opens with ``_ = style``: no value changes a pixel."""
    rendered = st.format_story_style()
    assert "Romantic" not in rendered and "Success" not in rendered
    assert "پیاده‌سازی نشده" in rendered


def test_story_style_command_always_answers() -> None:
    update = FakeUpdate()
    run(st.story_style_cmd(update, FakeContext()))
    assert update.replies and update.replies[0].strip()
