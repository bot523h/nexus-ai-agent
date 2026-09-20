"""Telegram command consent and payload plumbing without network I/O."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus_ai_agent.bot import slideshow_handlers as handlers


@pytest.mark.parametrize("caption", [False, True])
async def test_fill_options_reach_render_queue(
    monkeypatch: pytest.MonkeyPatch,
    caption: bool,
) -> None:
    sessions = handlers.get_slideshow_sessions()
    sessions.clear()
    sessions.start((7, 42))
    for index in range(3):
        sessions.add_image((7, 42), f"file-{index}")
    begin = AsyncMock()
    monkeypatch.setattr(handlers, "_begin_render", begin)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(
            reply_text=AsyncMock(),
            photo=[SimpleNamespace(file_id="file-2")],
            caption="/slideshow --slides 5 --fill Ocean holiday",
        ),
    )
    context = SimpleNamespace(args=["--slides", "5", "--fill", "Ocean", "holiday"])
    if caption:
        await handlers.slideshow_photo(update, context)
    else:
        await handlers.slideshow_cmd(update, context)
    begin.assert_awaited_once_with(
        update,
        context,
        project_name="Ocean holiday",
        file_ids=["file-0", "file-1", "file-2"],
        target_images=5,
        generate_missing=True,
    )
    assert not sessions.is_active((7, 42))


async def test_missing_consent_preserves_uploaded_photo_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = handlers.get_slideshow_sessions()
    sessions.clear()
    sessions.start((7, 42))
    sessions.add_image((7, 42), "file-0")
    begin = AsyncMock()
    monkeypatch.setattr(handlers, "_begin_render", begin)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    await handlers.slideshow_cmd(update, SimpleNamespace(args=["--slides", "5", "Title"]))
    begin.assert_not_awaited()
    assert sessions.count((7, 42)) == 1
    assert "--fill" in update.message.reply_text.await_args.args[0]
    sessions.clear()
