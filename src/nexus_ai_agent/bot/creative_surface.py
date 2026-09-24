"""Creative surface for Nagar studio — the Telegram entrypoint of the canonical chain.

This module is the only Telegram entrypoint for the creative studio beyond
``/slideshow`` and ``/imagine``.  It exposes three commands without touching
``bot/handlers.py`` (the highest-conflict file); the application root
(``bot/app.py``) registers the returned handlers.

Commands
--------
* ``/edit``    — trim / speed / reverse on a replied video
* ``/caption`` — subtitles (no honest execution path yet → refused typed)
* ``/grade``   — exposure (LUT / proxy / OTIO have no lane primitive yet → refused)

Every command:

* validates and normalizes *before* enqueuing, and replies in the caller's
  language through the ``i18n`` catalog — **a raw key such as
  ``creative.not_replied`` is never sent to a user** (owner directive §6;
  ``tests/unit/test_creative_surface.py`` pins it);
* stages the replied media into a workspace under ``settings.creative_temp_dir``
  named ``creative_<id>`` — the worker re-validates that containment, so the
  queue row alone can never point the renderer at an arbitrary path;
* enqueues ``creative_render`` through :class:`JobQueuePort` with an
  idempotency key anchored to the Telegram message id (a redelivered update
  re-uses the job instead of rendering twice) and returns immediately;
* reports *honestly*: an operation without a real lane primitive is refused
  with ``creative.not_available`` instead of being queued for a fake success.

The pure mapping layer (:class:`CreativeSurfaceMapper`) stays framework-free so
the unit tests drive it without a live bot; :func:`build_creative_handlers` is
the thin PTB glue.
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from nexus_ai_agent.adapters.creative_render_job import (
    CREATIVE_RENDER_JOB_TYPE,
    EXECUTABLE_OPERATIONS,
)
from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.i18n import I18n
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

#: The only three commands the studio surface owns.
COMMANDS: tuple[str, ...] = ("edit", "caption", "grade")

#: Advertised operations per command (the full product vocabulary).  An
#: advertised operation without an executable twin is answered with
#: ``creative.not_available`` — never queued.
ADVERTISED_OPERATIONS: dict[str, frozenset[str]] = {
    "edit": frozenset({"trim", "speed", "reverse"}),
    "caption": frozenset({"transcribe", "burnin"}),
    "grade": frozenset({"lut", "exposure", "proxy", "otio"}),
}


class CreativeErrorCode(str, Enum):
    """Typed surface failures.  Each maps to a localized message key."""

    INVALID_REQUEST = "invalid_request"
    INVALID_OPERATION = "invalid_operation"
    NOT_AVAILABLE = "not_available"
    NOT_REPLIED = "not_replied"
    LIMIT_EXCEEDED = "limit_exceeded"
    MEDIA_FAILED = "media_failed"
    QUEUE_FAILED = "queue_failed"


#: Failure code → i18n key.  Every key exists in all 15 locales
#: (``tests/unit/test_creative_i18n.py``).
MESSAGE_KEYS: dict[CreativeErrorCode, str] = {
    CreativeErrorCode.INVALID_REQUEST: "creative.invalid_request",
    CreativeErrorCode.INVALID_OPERATION: "creative.invalid_operation",
    CreativeErrorCode.NOT_AVAILABLE: "creative.not_available",
    CreativeErrorCode.NOT_REPLIED: "creative.not_replied",
    CreativeErrorCode.LIMIT_EXCEEDED: "creative.limit_exceeded",
    CreativeErrorCode.MEDIA_FAILED: "creative.media_failed",
    CreativeErrorCode.QUEUE_FAILED: "creative.queue_failed",
}


@dataclass(frozen=True)
class CreativeFailure:
    """A refusal plus everything needed to render it in the caller's language."""

    code: CreativeErrorCode
    context: dict[str, str] = field(default_factory=dict)

    @property
    def message_key(self) -> str:
        return MESSAGE_KEYS[self.code]


@dataclass(frozen=True)
class CreativeRequest:
    """A normalized surface request (pure data)."""

    command: str
    operation: str
    args: tuple[str, ...]
    media_file_id: str | None
    media_duration_s: float | None
    media_suffix: str = ".mp4"
    message_id: int | None = None


class CreativeSurfaceMapper:
    """Pure mapper: validate + normalize a user request.

    Limits mirror the slideshow surface (30 s) so the render lane cannot be
    DoS'd through a different entrypoint.
    """

    MAX_DURATION_S = 30.0

    def __init__(self, executable_operations: dict[tuple[str, str], str] | None = None):
        self._executable = (
            EXECUTABLE_OPERATIONS if executable_operations is None else executable_operations
        )

    def runnable_operations(self, command: str) -> frozenset[str]:
        """Operations of ``command`` that really render something today."""
        return frozenset(op for (cmd, op) in self._executable if cmd == command)

    def map(self, req: CreativeRequest) -> CreativeRequest | CreativeFailure:
        if req.command not in ADVERTISED_OPERATIONS:
            return CreativeFailure(CreativeErrorCode.INVALID_REQUEST, {"command": req.command})
        if req.operation not in ADVERTISED_OPERATIONS[req.command]:
            return CreativeFailure(
                CreativeErrorCode.INVALID_OPERATION,
                {"command": req.command, "operation": req.operation},
            )
        if req.operation not in self.runnable_operations(req.command):
            return CreativeFailure(
                CreativeErrorCode.NOT_AVAILABLE,
                {"command": req.command, "operation": req.operation},
            )
        if req.media_file_id is None:
            return CreativeFailure(
                CreativeErrorCode.NOT_REPLIED,
                {"command": req.command, "operation": req.operation},
            )
        if req.media_duration_s is not None and req.media_duration_s > self.MAX_DURATION_S:
            return CreativeFailure(
                CreativeErrorCode.LIMIT_EXCEEDED,
                {"seconds": str(int(self.MAX_DURATION_S))},
            )

        normalized_args = self._normalize_args(req)
        if isinstance(normalized_args, CreativeFailure):
            return normalized_args
        return CreativeRequest(
            command=req.command,
            operation=req.operation,
            args=normalized_args,
            media_file_id=req.media_file_id,
            media_duration_s=req.media_duration_s,
            media_suffix=req.media_suffix,
            message_id=req.message_id,
        )

    def _normalize_args(self, req: CreativeRequest) -> tuple[str, ...] | CreativeFailure:
        """Reject non-numeric operands here, where the user can be told plainly."""
        numeric_slots = {("edit", "trim"): 2, ("edit", "speed"): 1, ("grade", "exposure"): 1}
        expected = numeric_slots.get((req.command, req.operation))
        if expected is None:
            return ()
        if len(req.args) > expected:
            return CreativeFailure(
                CreativeErrorCode.INVALID_REQUEST,
                {"command": req.command, "operation": req.operation},
            )
        for raw in req.args:
            try:
                float(raw)
            except ValueError:
                return CreativeFailure(
                    CreativeErrorCode.INVALID_REQUEST,
                    {"command": req.command, "operation": req.operation},
                )
        return tuple(req.args)

    def job_payload(
        self,
        req: CreativeRequest,
        *,
        user_id: int,
        chat_id: int,
        workspace_dir: Path,
        input_path: Path,
        lang: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Build the ``creative_render`` queue envelope (matches the worker model)."""
        return {
            "command": req.command,
            "operation": req.operation,
            "args": list(req.args),
            "workspace_dir": str(workspace_dir),
            "input_path": str(input_path),
            "media_duration_us": (
                int(req.media_duration_s * 1_000_000) if req.media_duration_s is not None else None
            ),
            "user_id": user_id,
            "chat_id": chat_id,
            "lang": lang,
            "idempotency_key": idempotency_key,
        }

    @staticmethod
    def idempotency_key(req: CreativeRequest, *, chat_id: int, user_id: int) -> str:
        """Stable per Telegram message: a redelivered update never renders twice."""
        anchor = req.message_id if req.message_id is not None else req.media_file_id
        return f"creative:{user_id}:{chat_id}:{anchor}:{req.command}:{req.operation}"

    @staticmethod
    def workspace_name() -> str:
        """Reserved workspace name the worker's containment check expects."""
        return f"creative_{uuid4().hex[:12]}"


# -- PTB glue (thin) ----------------------------------------------------------


def _reply_language(update: Any) -> str:  # noqa: ANN401 - framework object
    """Resolve the caller's language from Telegram metadata (public i18n API)."""
    user = getattr(update, "effective_user", None)
    return I18n().detect_language(getattr(user, "language_code", None))


def _reply_media(message: Any) -> Any | None:  # noqa: ANN401 - framework object
    """Return the replied media object (video/document/audio) or ``None``."""
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None
    for attr in ("video", "document", "audio"):
        media = getattr(reply, attr, None)
        if media is not None and getattr(media, "file_id", None):
            return media
    return None


async def _stage_media(bot: Any, file_id: str, destination: Path) -> None:  # noqa: ANN401
    """Download replied media into the job workspace via the bot's own client."""
    if bot is None:
        raise RuntimeError("no bot client available to fetch the replied media")
    telegram_file = await bot.get_file(file_id)
    await telegram_file.download_to_drive(str(destination))


def build_creative_handlers(
    job_queue: JobQueuePort,
    mapper: CreativeSurfaceMapper | None = None,
    *,
    i18n: I18n | None = None,
    stage_media: Callable[[Any, str, Path], Awaitable[None]] | None = None,
) -> dict[str, Callable[[Any, Any], Awaitable[None]]]:
    """Return ``{command: handler}`` bound to ``job_queue``.

    ``stage_media`` is injectable so tests (and deployments without a live bot
    client) do not need Telegram at all; the default is :func:`_stage_media`.
    """
    _mapper = mapper or CreativeSurfaceMapper()
    _i18n = i18n or I18n()
    _stage = stage_media or _stage_media

    def _say(message: Any, key: str, lang: str, **context: str) -> Awaitable[Any]:
        return message.reply_text(_i18n.t(key, lang=lang, **context))

    async def _dispatch(update: Any, context: Any, command: str) -> None:  # noqa: ANN401
        message = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if message is None:
            return
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        user_id = int(user.id) if user is not None and hasattr(user, "id") else 0
        chat_id = int(chat.id) if chat is not None and hasattr(chat, "id") else 0
        bot = getattr(context, "bot", None)

        raw_args: list[str] = list(getattr(context, "args", []) or [])
        operation = raw_args[0].lower() if raw_args else ""
        media = _reply_media(message)
        duration: float | None = None
        suffix = ".mp4"
        if media is not None:
            raw_duration = getattr(media, "duration", None)
            if raw_duration is not None:
                try:
                    duration = float(raw_duration)
                except (TypeError, ValueError):
                    duration = None
            file_name = getattr(media, "file_name", None)
            if file_name:
                parsed = Path(str(file_name)).suffix
                if parsed:
                    suffix = parsed.lower()

        req = CreativeRequest(
            command=command,
            operation=operation,
            args=tuple(raw_args[1:]),
            media_file_id=str(media.file_id) if media is not None else None,
            media_duration_s=duration,
            media_suffix=suffix,
            message_id=getattr(message, "message_id", None),
        )
        lang = _reply_language(update)
        mapped = _mapper.map(req)
        if isinstance(mapped, CreativeFailure):
            await _say(message, mapped.message_key, lang, **_failure_context(mapped, lang, _i18n))
            return

        workspace = Path(get_settings().creative_temp_dir) / _mapper.workspace_name()
        workspace.mkdir(parents=True, exist_ok=True)
        input_path = workspace / f"source{req.media_suffix}"
        try:
            await _stage(bot, str(media.file_id) if media is not None else "", input_path)
        except Exception as exc:  # noqa: BLE001 - any fetch failure is user-visible
            logger.warning("creative_media_stage_failed", error=type(exc).__name__)
            shutil.rmtree(workspace, ignore_errors=True)
            await _say(
                message,
                MESSAGE_KEYS[CreativeErrorCode.MEDIA_FAILED],
                lang,
            )
            return

        idempotency_key = _mapper.idempotency_key(mapped, chat_id=chat_id, user_id=user_id)
        payload = _mapper.job_payload(
            mapped,
            user_id=user_id,
            chat_id=chat_id,
            workspace_dir=workspace,
            input_path=input_path,
            lang=lang,
            idempotency_key=idempotency_key,
        )
        try:
            job_id = await job_queue.enqueue(
                job_type=CREATIVE_RENDER_JOB_TYPE,
                idempotency_key=idempotency_key,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - the queue is the trust boundary
            logger.warning("creative_enqueue_failed", command=command, error=type(exc).__name__)
            shutil.rmtree(workspace, ignore_errors=True)
            await _say(message, MESSAGE_KEYS[CreativeErrorCode.QUEUE_FAILED], lang)
            return

        await _say(
            message,
            "creative.queued",
            lang,
            command=mapped.command,
            operation=mapped.operation,
            job_id=job_id,
        )

    def _make(command: str) -> Callable[[Any, Any], Awaitable[None]]:
        async def handler(update: Any, context: Any) -> None:  # noqa: ANN401
            await _dispatch(update, context, command)

        handler.__name__ = f"creative_{command}_cmd"
        return handler

    return {command: _make(command) for command in COMMANDS}


def _failure_context(failure: CreativeFailure, lang: str, i18n: I18n) -> dict[str, str]:
    """Fill every placeholder a localized failure message may carry.

    ``str.format`` raises on a *missing* placeholder, so unfilled slots get a
    neutral default instead of ever surfacing the raw key.
    """
    context = dict(failure.context)
    return {
        "command": context.get("command") or "-",
        "operation": context.get("operation") or "-",
        "seconds": context.get("seconds", "30"),
        "job_id": context.get("job_id", "-"),
    }


__all__ = [
    "ADVERTISED_OPERATIONS",
    "COMMANDS",
    "MESSAGE_KEYS",
    "CreativeErrorCode",
    "CreativeFailure",
    "CreativeRequest",
    "CreativeSurfaceMapper",
    "build_creative_handlers",
]
