"""Telegram surface for a natural-language trim of a replied-to video.

The handler only stages and measures user media, then sends a typed host context
through the normal LangGraph graph. Meaning, strategy, compilation, queueing,
CommandBus execution, and artifact verification remain in their owning layers.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from nexus_ai_agent.bot.creative_surface import (
    MAX_DURATION_US,
    MAX_MEDIA_BYTES,
    _workspace_for,
    idempotency_key,
)
from nexus_ai_agent.bot.surface._ptb import user_language_code
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.i18n import i18n
from nexus_ai_agent.orchestration.agent_intelligence import AgentInputContext

logger = logging.getLogger(__name__)


def _remove_staging(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("agent_staging_cleanup_failed", extra={"path": str(path)})


def build_agent_intelligence_handler(graph: Any, settings: Settings) -> Any:
    """Build ``/agent_edit <natural language>`` for a replied-to video message."""

    async def _handler(update: Any, context: Any) -> None:
        message = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if message is None:
            return
        reply = getattr(message, "reply_to_message", None)
        media = getattr(reply, "video", None) if reply is not None else None
        if media is None and reply is not None:
            candidate = getattr(reply, "document", None)
            if candidate is not None and str(getattr(candidate, "mime_type", "")).startswith(
                "video/"
            ):
                media = candidate
        lang = i18n.detect_language(user_language_code(update))
        text = " ".join(str(arg) for arg in (getattr(context, "args", []) or [])).strip()
        if media is None:
            await message.reply_text(i18n.t("creative.not_replied", lang=lang))
            return
        if not text:
            await message.reply_text(i18n.t("creative.agent_edit.request_required", lang=lang))
            return
        if len(text) > 4000:
            await message.reply_text(i18n.t("creative.agent_edit.request_too_long", lang=lang))
            return

        size = getattr(media, "file_size", None)
        if isinstance(size, int) and size > MAX_MEDIA_BYTES:
            await message.reply_text(
                i18n.t(
                    "creative.media_too_large", lang=lang, max_mb=MAX_MEDIA_BYTES // (1024 * 1024)
                )
            )
            return

        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        try:
            user_id = int(user.id) if user is not None and hasattr(user, "id") else 0
            chat_id = int(chat.id) if chat is not None and hasattr(chat, "id") else 0
        except (TypeError, ValueError):
            user_id, chat_id = 0, 0
        if chat_id == 0:
            await message.reply_text(i18n.t("creative.agent_edit.processing_failed", lang=lang))
            return
        request_id = idempotency_key(
            user_id,
            chat_id,
            getattr(message, "message_id", None),
        )
        workspace = _workspace_for(request_id, settings.creative_temp_dir)
        try:
            root = Path(settings.creative_temp_dir).resolve(strict=False)
            workspace.resolve(strict=False).relative_to(root)
            workspace_preexisting = workspace.exists()
            workspace.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, ValueError):
            logger.exception("agent_workspace_prepare_failed")
            await message.reply_text(i18n.t("creative.media_download_failed", lang=lang))
            return
        target = workspace / "input.mp4"
        staging = workspace / f".input-{uuid4().hex}.part.mp4"
        try:
            bot = getattr(context, "bot", None)
            if bot is None:
                raise RuntimeError("Telegram bot file API is unavailable")
            tg_file = await bot.get_file(str(media.file_id))
            await tg_file.download_to_drive(staging)

            from nexus_ai_agent.creative.slideshow.ffmpeg import (
                probe_video,
                resolve_ffmpeg_bin,
                sha256_file,
            )

            staged_size = staging.stat().st_size
            if staged_size <= 0 or staged_size > MAX_MEDIA_BYTES:
                raise ValueError("staged video is empty or exceeds the upload limit")
            staged_digest = sha256_file(staging)
            if target.exists():
                if not target.is_file() or sha256_file(target) != staged_digest:
                    raise ValueError("this Telegram request id is already bound to different media")
                _remove_staging(staging)
            else:
                staging.replace(target)
            measured = probe_video(target, binary=resolve_ffmpeg_bin())
            if measured.duration_us <= 0 or measured.duration_us > MAX_DURATION_US:
                _remove_staging(staging)
                if not workspace_preexisting:
                    shutil.rmtree(workspace, ignore_errors=True)
                await message.reply_text(
                    i18n.t(
                        "creative.limit_exceeded",
                        lang=lang,
                        max_seconds=MAX_DURATION_US // 1_000_000,
                    )
                )
                return
            digest = sha256_file(target)
            source_id = (
                "asset_" + hashlib.sha256(f"{media.file_id}:{digest}".encode()).hexdigest()[:24]
            )
            from nexus_ai_agent.creative.spine.compiler import request_idempotency_key

            project_id = f"shot-{request_idempotency_key(request_id)}"
            agent_context = AgentInputContext(
                project_id=project_id,
                source={
                    "asset_id": source_id,
                    "media_kind": "video",
                    "duration_us": measured.duration_us,
                    "content_sha256": digest,
                    "width_px": measured.width or 0,
                    "height_px": measured.height or 0,
                },
                workspace_dir=str(workspace),
                input_path=str(target),
                user_id=user_id,
                chat_id=chat_id,
                lang=lang,
            )
        except Exception:
            _remove_staging(staging)
            if not workspace_preexisting:
                shutil.rmtree(workspace, ignore_errors=True)
            logger.exception("agent_video_stage_failed")
            await message.reply_text(i18n.t("creative.media_download_failed", lang=lang))
            return

        state = {
            "thread_id": f"tg:{chat_id}",
            "chat_id": chat_id,
            "user_id": user_id,
            "correlation_id": request_id,
            "messages": [{"role": "user", "content": text}],
            "intent": "unknown",
            "active_persona": "",
            "current_task": None,
            "tool_results": [],
            "memory_context": "",
            "response": "",
            "error": None,
            "turn_count": 0,
            "moderation_passed": True,
            "creative_asset": agent_context.model_dump(mode="json"),
        }
        try:
            result = await graph.ainvoke(
                state,
                config={"configurable": {"thread_id": f"tg:{chat_id}"}},
            )
        except Exception:
            # A graph exception may occur after queue insertion. Preserve this
            # request's workspace; startup recovery or bounded temp retention
            # owns cleanup when enqueue status is uncertain.
            logger.exception("agent_intelligence_graph_failed")
            await message.reply_text(i18n.t("creative.agent_edit.processing_failed", lang=lang))
            return

        outcome = result.get("agent_intelligence") or {}
        known_pre_enqueue_result = (
            bool(outcome)
            and not outcome.get("job_id")
            and outcome.get("error_code") != "queue_enqueue_failed"
        )
        if known_pre_enqueue_result and not workspace_preexisting:
            # Clarification, unsupported intent, and compiler refusal are
            # explicit no-enqueue results. A queue error is ambiguous: commit
            # may have succeeded before acknowledgement failed, so retain media.
            shutil.rmtree(workspace, ignore_errors=True)
        await message.reply_text(
            str(result.get("response") or i18n.t("creative.agent_edit.no_result", lang=lang))
        )

    return _handler


__all__ = ["build_agent_intelligence_handler"]
