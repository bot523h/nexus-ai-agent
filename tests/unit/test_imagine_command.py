"""The real /imagine registration delivers bytes and protects paid-provider access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from nexus_ai_agent.bot import handlers
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.creative.image_gen import GeneratedImage, ImageRequest, PaidTierRequiredError


@pytest.fixture()
def imagine(monkeypatch: pytest.MonkeyPatch):
    for factory in ("ImageGenEngine", "SpeechEngine", "UnifiedCloudStorage", "ReferralEngine"):
        monkeypatch.setattr(handlers, factory, Mock())
    registered = handlers.build_handlers(
        graph=None,
        db_session_factory=Mock(),
        settings=Settings().model_copy(update={"allowed_user_ids": [42], "owner_telegram_id": 0}),
        presence=Mock(),
        storage=None,
    )
    return next(
        handler.callback for handler in registered if "imagine" in getattr(handler, "commands", ())
    )


def update_for(user_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        edited_message=None,
        message=SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock()),
    )


async def test_imagine_calls_provider_and_sends_validated_bytes(imagine, monkeypatch) -> None:
    provider = SimpleNamespace(
        generate=AsyncMock(return_value=GeneratedImage(b"image", "image/png"))
    )
    monkeypatch.setattr(handlers, "get_image_gen_provider", lambda: provider)
    update = update_for()
    await imagine(update, SimpleNamespace(args=["blue", "ocean"]))
    provider.generate.assert_awaited_once_with(ImageRequest("blue ocean"))
    update.message.reply_photo.assert_awaited_once_with(photo=b"image", caption="🎨 blue ocean")


async def test_imagine_denies_unlisted_user_before_provider_creation(imagine, monkeypatch) -> None:
    factory = Mock()
    monkeypatch.setattr(handlers, "get_image_gen_provider", factory)
    update = update_for(999)
    await imagine(update, SimpleNamespace(args=["ocean"]))
    factory.assert_not_called()
    update.message.reply_photo.assert_not_awaited()
    assert "مجاز نیست" in update.message.reply_text.await_args.args[0]


async def test_imagine_shows_usage_without_egress(imagine, monkeypatch) -> None:
    factory = Mock()
    monkeypatch.setattr(handlers, "get_image_gen_provider", factory)
    update = update_for()
    await imagine(update, SimpleNamespace(args=[]))
    factory.assert_not_called()
    assert "/imagine" in update.message.reply_text.await_args.args[0]


async def test_imagine_does_not_expose_provider_errors(imagine, monkeypatch) -> None:
    provider = SimpleNamespace(
        generate=AsyncMock(side_effect=PaidTierRequiredError("private detail"))
    )
    monkeypatch.setattr(handlers, "get_image_gen_provider", lambda: provider)
    update = update_for()
    await imagine(update, SimpleNamespace(args=["ocean"]))
    update.message.reply_photo.assert_not_awaited()
    assert "private detail" not in update.message.reply_text.await_args.args[0]
