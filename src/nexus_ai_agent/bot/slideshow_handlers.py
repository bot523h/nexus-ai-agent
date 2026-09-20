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
from nexus_ai_agent.bot.slideshow import (
    BOT_TARGET_DURATION_US,
    MAX_IMAGES,
    SLIDESHOW_JOB_TYPE,
    SlideshowSessionStore,
    photo_extension,
    usage_text,
    validate_prompt,
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

    project_name, error = validate_prompt(" ".join(args))
    if error is not None:
        await _reply(update, error)
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
    await _begin_render(update, context, project_name=project_name, file_ids=file_ids)


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
        project_name, error = validate_prompt(remainder)
        if error is not None:
            await _reply(update, error)
            return
        buffered = _sessions.take_images(key)
        collected = list(dict.fromkeys([*buffered, file_id]))[-MAX_IMAGES:]
        await _begin_render(update, context, project_name=project_name, file_ids=collected)
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
) -> None:
    """Download the buffered photos into a private workspace and enqueue the job."""
    queue = _job_queue(context)
    if queue is None:
        await _reply(update, "❌ صف پردازش داخلی پیکربندی نشده است.")
        return
    user_id = _user_id(update) or 0
    settings = get_settings()
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
    seconds = BOT_TARGET_DURATION_US // 1_000_000
    await _reply(
        update,
        f"⏳ ساخت اسلایدشو در صف داخلی قرار گرفت ({len(image_paths)} تصویر، سقف {seconds} ثانیه).\n"
        f"شناسه: {job_id}",
    )


__all__ = [
    "get_slideshow_sessions",
    "slideshow_cmd",
    "slideshow_photo",
]
