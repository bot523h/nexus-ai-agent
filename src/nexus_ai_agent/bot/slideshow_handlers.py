"""Telegram glue for ``/slideshow`` (Wave 2.5).

This module owns the PTB surface only: parse the command, collect photo
uploads through the pure session store, download images to a private job
workspace, and hand the whole thing to ``JobQueuePort``.  It never renders,
never shells out, and never blocks the event loop with encode work — the
queue is the sole execution path (Wave 2.5 decision, items 1–3).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from telegram import Update
from telegram.ext import ContextTypes

from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.bot.middleware import AuthMiddleware
from nexus_ai_agent.bot.slideshow import (
    BOT_TARGET_DURATION_US,
    MAX_IMAGES,
    SLIDESHOW_JOB_TYPE,
    SlideshowSessionStore,
    parse_slideshow_options,
    photo_extension,
    usage_text,
)
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.creative.slideshow.worker_adapter import (
    DEFAULT_RESOLUTION,
    WORKSPACE_PREFIX,
)

logger = logging.getLogger(__name__)

#: One shared collection surface for all chats (bounded, TTL-expiring).
_sessions = SlideshowSessionStore()


def get_slideshow_sessions() -> SlideshowSessionStore:
    """The process-wide session store (exposed for tests and shutdown)."""
    return _sessions


def _chat_id(update: Update) -> int | None:
    return update.effective_chat.id if update.effective_chat else None


def _user_id(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


def _session_key(update: Update) -> tuple[int, int]:
    return (_chat_id(update) or 0, _user_id(update) or 0)


def _job_queue(context: ContextTypes.DEFAULT_TYPE) -> JobQueuePort | None:
    application = getattr(context, "application", None)
    bot_data = getattr(application, "bot_data", {})
    return cast(JobQueuePort | None, bot_data.get("job_queue"))


async def _reply(update: Update, text: str) -> None:
    if update.message is not None:
        await update.message.reply_text(text)


async def slideshow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/slideshow`` starts collecting; ``/slideshow <عنوان>`` queues the render."""
    if update.message is None:
        return
    key = _session_key(update)
    args = list(context.args or [])
    if not args:
        _sessions.start(key)
        await _reply(update, usage_text())
        return

    try:
        options = parse_slideshow_options(args)
        if _sessions.count(key):
            options.validate_count(_sessions.count(key))
    except ValueError as exc:
        await _reply(update, str(exc))
        return
    file_ids = _sessions.take_images(key)
    if not file_ids:
        _sessions.start(key)
        await _reply(
            update,
            f"📸 هنوز تصویری در این نشست نیست. تصویر بفرستید (تا {MAX_IMAGES} عدد)، "
            "سپس دوباره /slideshow <عنوان>.",
        )
        return
    await _begin_render(
        update,
        context,
        project_name=options.project_name,
        file_ids=file_ids,
        target_images=options.target_images,
        generate_missing=options.generate_missing,
        upscale_factor=options.upscale_factor,
    )


async def slideshow_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buffer photo uploads into the open session; a caption queues the render.

    Users with no open session are left completely alone — the bot answers a
    thousand photos a day for other features and must not nag about slideshows.
    """
    if update.message is None or not update.message.photo:
        return
    key = _session_key(update)
    file_id = update.message.photo[-1].file_id
    caption = (update.message.caption or "").strip()
    if caption.startswith("/slideshow"):
        remainder = caption[len("/slideshow") :].strip()
        try:
            options = parse_slideshow_options(remainder.split())
        except ValueError as exc:
            await _reply(update, str(exc))
            return
        if not _sessions.is_active(key):
            _sessions.start(key)
        count = _sessions.add_image(key, file_id)
        if count is None or count < 0:
            await _reply(update, f"❌ سقف {MAX_IMAGES} تصویر پر شده است.")
            return
        try:
            options.validate_count(count)
        except ValueError as exc:
            await _reply(update, str(exc))
            return
        collected = _sessions.take_images(key)
        await _begin_render(
            update,
            context,
            project_name=options.project_name,
            file_ids=collected,
            target_images=options.target_images,
            generate_missing=options.generate_missing,
            upscale_factor=options.upscale_factor,
        )
        return

    count = _sessions.add_image(key, file_id)
    if count is None:
        return
    if count < 0:
        await _reply(
            update,
            f"🖼 سقف {MAX_IMAGES} تصویر پر شده است — /slideshow <عنوان> را بفرستید.",
        )
        return
    if count >= MAX_IMAGES:
        await _reply(update, f"🖼 {count}/{MAX_IMAGES} — سقف پر شد؛ حالا /slideshow <عنوان>.")
    else:
        await _reply(
            update,
            f"🖼 {count}/{MAX_IMAGES} دریافت شد — تصویر دیگر بفرستید یا /slideshow <عنوان>.",
        )


async def _begin_render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    project_name: str | None,
    file_ids: list[str],
    target_images: int | None = None,
    generate_missing: bool = False,
    upscale_factor: int | None = None,
) -> None:
    """Download the buffered photos into a private workspace and enqueue the job."""
    queue = _job_queue(context)
    if queue is None:
        await _reply(update, "❌ صف پردازش داخلی پیکربندی نشده است.")
        return
    user_id = _user_id(update) or 0
    settings = get_settings()
    if generate_missing and not AuthMiddleware(
        settings.allowed_user_ids, settings.owner_telegram_id
    ).is_allowed(user_id):
        await _reply(update, "❌ دسترسی به تولید تصویر مجاز نیست.")
        return
    workspace = Path(settings.creative_temp_dir) / f"{WORKSPACE_PREFIX}{user_id}_{uuid4().hex[:12]}"
    image_paths: list[Path] = []
    try:
        workspace.mkdir(parents=True)
        for index, file_id in enumerate(file_ids):
            file = await context.bot.get_file(file_id)
            data = await file.download_as_bytearray()
            target = workspace / f"img_{index:02d}{photo_extension(file.file_path)}"
            target.write_bytes(bytes(data))
            image_paths.append(target)
    except Exception:  # noqa: BLE001 - an undeliverable upload is not a render
        logger.exception("slideshow upload download failed for user %s", user_id)
        shutil.rmtree(workspace, ignore_errors=True)
        await _reply(update, "❌ دریافت تصویرها از تلگرام ناموفق بود؛ دوباره تلاش کنید.")
        return

    payload: dict[str, Any] = {
        "chat_id": _chat_id(update),
        "user_id": user_id,
        "image_paths": [str(path) for path in image_paths],
        "workspace_dir": str(workspace),
        "output_path": str(workspace / "master.mp4"),
        "target_duration_us": BOT_TARGET_DURATION_US,
        "resolution": DEFAULT_RESOLUTION,
    }
    if target_images is not None:
        payload["target_images"] = target_images
    if upscale_factor is not None:
        payload["upscale_factor"] = upscale_factor
    if generate_missing:
        payload["generate_missing"] = True
        payload["generation_prompt"] = project_name
    if project_name:
        payload["project_name"] = project_name
    try:
        job_id = await queue.enqueue(
            job_type=SLIDESHOW_JOB_TYPE,
            idempotency_key=f"telegram-slideshow:{user_id}:{uuid4().hex}",
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 - surface queue failure to the user
        shutil.rmtree(workspace, ignore_errors=True)
        await _reply(update, f"❌ صف رندر در دسترس نیست: {exc}")
        return
    missing = max(0, (target_images or len(image_paths)) - len(image_paths))
    upscale_notice = (
        f"\n🔍 تصاویر پیش از رندر با Lanczos و ضریب {upscale_factor} بزرگ می‌شوند."
        if upscale_factor is not None
        else ""
    )
    generation_notice = (
        f"\n🎨 {missing} تصویر تکمیلی با سرویس تولید تصویر ساخته می‌شود." if missing else ""
    )
    seconds = BOT_TARGET_DURATION_US // 1_000_000
    await _reply(
        update,
        f"⏳ ساخت اسلایدشو در صف داخلی قرار گرفت ({len(image_paths)} تصویر، سقف {seconds} ثانیه).\n"
        f"شناسه: {job_id}{upscale_notice}{generation_notice}",
    )


__all__ = [
    "get_slideshow_sessions",
    "slideshow_cmd",
    "slideshow_photo",
]
