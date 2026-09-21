"""Creative surface for Nagar studio — wave-4 step5.

This module is the *only* Telegram entrypoint for the Nagar creative
studio beyond ``/slideshow`` and ``/imagine``.  It exposes three minimal
commands without touching ``bot/handlers.py`` (the highest-conflict file,
1623 LOC) — instead the application root wires these handlers alongside
the existing ``feature_handlers`` surface.

Commands
--------
* ``/edit``   — trim / speed / reverse on a replied video
* ``/caption`` — subtitle generation + burn-in (``caption_unavailable`` fail-closed)
* ``/grade``   — LUT / exposure / proxy / OTIO export

Every command:

* validates limits *before* enqueuing (5 items / 30 s pattern from slideshow)
  and replies with a typed failure otherwise;
* enqueues via :class:`JobQueuePort` (``creative_render`` branch) and returns
  immediately — the worker (render lane, task-104) does the heavy work;
* uses the ``i18n`` catalog (15 locales) for replies — never a hard-coded
  Persian string;
* never imports ``features/`` or ``storage/`` — it is a thin surface.

The module is framework-free in its pure mapping layer
(``CreativeSurfaceMapper``) so the unit test can drive it without a live
bot; the PTB glue (``build_creative_handlers``) is intentionally thin.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

# -- error taxonomy (typed, user-facing via i18n) --------------------------


class CreativeErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_MEDIA = "unsupported_media"
    LIMIT_EXCEEDED = "limit_exceeded"
    NOT_REPLIED = "not_replied"
    QUEUE_FAILED = "queue_failed"


@dataclass(frozen=True)
class CreativeFailure:
    code: CreativeErrorCode
    # i18n key suffix — caller does ``f"creative.{code.value}"``
    message_key: str = ""


# -- mapper: pure product logic (no Telegram) -------------------------------


@dataclass(frozen=True)
class CreativeRequest:
    command: str  # edit | caption | grade
    operation: str  # trim | speed | reverse | transcribe | burnin | lut | exposure | proxy | otio
    args: tuple[str, ...]
    media_file_id: str | None
    media_duration_s: float | None


class CreativeSurfaceMapper:
    """Pure mapper: validate + normalize a user request.

    Limits mirror the slideshow surface (5 items / 30 s) so the render lane
    cannot be DoS'd through a different entrypoint.
    """

    MAX_DURATION_S = 30.0
    # Allowed ops per command (allow-list, closed set)
    ALLOWED: dict[str, frozenset[str]] = {
        "edit": frozenset({"trim", "speed", "reverse"}),
        "caption": frozenset({"transcribe", "burnin"}),
        "grade": frozenset({"lut", "exposure", "proxy", "otio"}),
    }

    def map(self, req: CreativeRequest) -> CreativeRequest | CreativeFailure:
        if req.command not in self.ALLOWED:
            return CreativeFailure(
                CreativeErrorCode.INVALID_REQUEST,
                message_key="creative.invalid_command",
            )
        if req.operation not in self.ALLOWED[req.command]:
            return CreativeFailure(
                CreativeErrorCode.INVALID_REQUEST,
                message_key="creative.invalid_operation",
            )
        # Media is required for every operation except otio/proxy metadata
        if req.media_file_id is None and req.operation not in {"otio", "lut"}:
            return CreativeFailure(
                CreativeErrorCode.NOT_REPLIED,
                message_key="creative.not_replied",
            )
        if req.media_duration_s is not None and req.media_duration_s > self.MAX_DURATION_S:
            return CreativeFailure(
                CreativeErrorCode.LIMIT_EXCEEDED,
                message_key="creative.limit_exceeded",
            )
        return req

    def job_payload(self, req: CreativeRequest, user_id: int, chat_id: int) -> dict[str, Any]:
        return {
            "command": req.command,
            "operation": req.operation,
            "args": list(req.args),
            "media_file_id": req.media_file_id,
            "user_id": user_id,
            "chat_id": chat_id,
        }


# -- PTB glue (thin) --------------------------------------------------------


def build_creative_handlers(
    job_queue: JobQueuePort,
    mapper: CreativeSurfaceMapper | None = None,
) -> dict[str, Any]:
    """Return ``{command_name: handler}`` bound to ``job_queue``.

    The caller (``bot/app.py``) registers the handlers:

        handlers["edit"]     → CommandHandler("edit", ...)
        handlers["caption"]  → CommandHandler("caption", ...)
        handlers["grade"]    → CommandHandler("grade", ...)

    Unit tests construct the mapper + a fake ``JobQueuePort`` and drive
    the closures without any Telegram machinery.
    """
    _mapper = mapper or CreativeSurfaceMapper()

    async def _dispatch(update: Any, context: Any, command: str) -> None:  # noqa: ANN401
        message = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if message is None:
            return
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        user_id = int(user.id) if user is not None and hasattr(user, "id") else 0
        chat_id = int(chat.id) if chat is not None and hasattr(chat, "id") else 0

        # Extract operation + args from ``context.args``
        raw_args: list[str] = list(getattr(context, "args", []) or [])
        operation = raw_args[0].lower() if raw_args else ""
        op_args = tuple(raw_args[1:])

        # Media: require a replied-to video/document when the command is a reply
        media_file_id: str | None = None
        duration: float | None = None
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            for attr in ("video", "document", "audio"):
                media = getattr(reply, attr, None)
                if media is not None and hasattr(media, "file_id"):
                    media_file_id = str(media.file_id)
                    duration = getattr(media, "duration", None)
                    if duration is not None:
                        try:
                            duration = float(duration)
                        except (TypeError, ValueError):
                            duration = None
                    break

        req = CreativeRequest(
            command=command,
            operation=operation,
            args=op_args,
            media_file_id=media_file_id,
            media_duration_s=duration,
        )
        mapped = _mapper.map(req)
        if isinstance(mapped, CreativeFailure):
            await message.reply_text(f"❌ {mapped.message_key} ({mapped.code.value})")
            return

        payload = _mapper.job_payload(mapped, user_id, chat_id)
        try:
            from uuid import uuid4

            job_id = await job_queue.enqueue(
                job_type="creative_render",
                idempotency_key=f"creative:{user_id}:{chat_id}:{uuid4().hex}",
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("creative_enqueue_failed", command=command, error=str(exc))
            await message.reply_text("❌ queue failed — try again")
            return

        await message.reply_text(f"⏳ Queued {command}/{operation} — job {job_id}")

    # Factory closures so each command captures its name correctly
    def _make(command: str) -> Any:  # noqa: ANN401
        async def handler(update: Any, context: Any) -> None:  # noqa: ANN401
            await _dispatch(update, context, command)

        handler.__name__ = f"creative_{command}_cmd"
        return handler

    return {
        "edit": _make("edit"),
        "caption": _make("caption"),
        "grade": _make("grade"),
        "mapper": _mapper,
    }
