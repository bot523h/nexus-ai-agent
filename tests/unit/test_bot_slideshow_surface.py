"""Pure surface of the Wave 2.5 bot command: limits, sessions, message mapping.

Everything under test here lives in ``bot/slideshow.py`` — deliberately free of
``telegram`` imports so the product's limits (r7) are unit-testable on their own.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.bot.slideshow import (
    MAX_IMAGES,
    SlideshowSessionStore,
    friendly_render_error,
    friendly_success,
    photo_extension,
    usage_text,
    validate_prompt,
)
from nexus_ai_agent.creative.slideshow.worker_adapter import ERROR_CODES


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


KEY = (7, 42)


def test_session_collects_up_to_the_cap_and_refuses_more() -> None:
    store = SlideshowSessionStore(clock=FakeClock())
    store.start(KEY)
    for index in range(MAX_IMAGES):
        assert store.add_image(KEY, f"file-{index}") == index + 1
    assert store.add_image(KEY, "one-too-many") == -1
    assert store.count(KEY) == MAX_IMAGES


def test_duplicate_uploads_are_deduped_not_stacked() -> None:
    store = SlideshowSessionStore(clock=FakeClock())
    store.start(KEY)
    store.add_image(KEY, "same")
    assert store.add_image(KEY, "same") == 1
    assert store.count(KEY) == 1


def test_uploads_without_a_session_are_ignored() -> None:
    store = SlideshowSessionStore(clock=FakeClock())
    assert store.add_image(KEY, "stray") is None
    assert store.take_images(KEY) == []


def test_take_images_closes_the_session() -> None:
    store = SlideshowSessionStore(clock=FakeClock())
    store.start(KEY)
    store.add_image(KEY, "a")
    store.add_image(KEY, "b")
    assert store.take_images(KEY) == ["a", "b"]
    assert store.is_active(KEY) is False
    assert store.take_images(KEY) == []


def test_idle_sessions_expire_and_restart_clears_the_buffer() -> None:
    clock = FakeClock()
    store = SlideshowSessionStore(clock=clock)
    store.start(KEY)
    store.add_image(KEY, "old")
    clock.advance(store._ttl + 1)
    assert store.count(KEY) == 0
    assert store.add_image(KEY, "late") is None

    store.start(KEY)
    store.add_image(KEY, "kept")
    store.start(KEY)  # a restart must not inherit the previous buffer
    assert store.count(KEY) == 0


def test_store_is_bounded_fifo_across_chats() -> None:
    store = SlideshowSessionStore(clock=FakeClock(), max_sessions=2)
    store.start((1, 1))
    store.start((2, 2))
    store.start((3, 3))
    assert store.is_active((1, 1)) is False
    assert store.is_active((3, 3)) is True


def test_prompt_rules() -> None:
    assert validate_prompt("") == (None, None)
    assert validate_prompt("  تعطیل  ") == ("تعطیل", None)
    assert validate_prompt("a" * 60)[1] is None
    long_error = validate_prompt("a" * 61)
    assert long_error[0] is None and "۶۰" in (long_error[1] or "")
    bad = validate_prompt("dangerous/../path!")
    assert bad[0] is None and bad[1].startswith("❌")


def test_photo_extension_only_yields_image_suffixes() -> None:
    assert photo_extension("photos/file_0.jpg") == ".jpg"
    assert photo_extension("x/y.PNG") == ".png"
    assert photo_extension("evil.exe") == ".jpg"
    assert photo_extension(None) == ".jpg"


def test_every_worker_error_code_maps_to_a_plain_message() -> None:
    mapped = {code: friendly_render_error(code) for code in ERROR_CODES}
    assert all(text and text.startswith("❌") for text in mapped.values())
    assert len(set(mapped.values())) == len(ERROR_CODES)
    # Unknown/missing codes degrade to the internal message, never to a traceback.
    for code in ("not-a-code", None, ""):
        assert friendly_render_error(code) == mapped["internal"]  # type: ignore[arg-type]


def test_user_messages_never_leak_paths_or_internals() -> None:
    for code in sorted(ERROR_CODES) + ["???"]:
        text = friendly_render_error(code)
        assert "/" not in text
        assert "Traceback" not in text
        assert "ffmpeg" not in text or "FFmpeg" in text


def test_success_caption_uses_measured_values() -> None:
    caption = friendly_success(
        {
            "duration_us": 30_012_345,
            "shot_count": 4,
            "size_bytes": 3_145_728,
            "width": 1280,
            "height": 720,
        }
    )
    assert "30 ثانیه" in caption
    assert "4 شات" in caption
    assert "1280×720" in caption
    assert "3.0 مگابایت" in caption


def test_success_caption_survives_garbage() -> None:
    caption = friendly_success({"duration_us": "not-a-number", "size_bytes": None})
    assert caption.startswith("✅")
    assert "1280x720" in caption  # falls back to the requested resolution


def test_usage_text_states_the_limits() -> None:
    text = usage_text()
    assert str(MAX_IMAGES) in text
    assert "/slideshow" in text


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_mapper_is_total_over_the_closed_vocabulary(code: str) -> None:
    assert friendly_render_error(code) != friendly_render_error("internal") or code == "internal"
