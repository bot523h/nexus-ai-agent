"""Creative surface for the Nagar studio — canonical Telegram entrypoint.

This module is the *only* Telegram entrypoint for one-shot media edits beyond
``/slideshow`` and ``/imagine``. The full canonical chain is:

    Telegram /edit|/caption|/grade
      → creative_surface (validation + workspace staging)
      → JobQueuePort.enqueue(creative_render)        (durable, idempotent)
      → worker handler creative/render_jobs.py       (packs runtime registry)
      → CommandBus dispatch (typed pack operation)
      → render lane (FFmpeg, measured artifact)
      → completion notify (translated, artifact attached)

Rules this module keeps (the same ones ``AGENTS.md``/``CREATIVE_STUDIO.md``
enforce elsewhere):

* the pure mapping layer (``CreativeSurfaceMapper``) stays framework-free so
  unit tests drive it without a live bot; the PTB glue is duck-typed through
  ``bot/surface/_ptb.py`` accessors and never imports ``telegram``;
* the handler map contains EXACTLY the three command names — never a helper
  object under a fake key (the ``mapper`` regression of wave-4 step5);
* every user-visible string comes from the ``i18n`` catalog — never a raw
  key and never a hard-coded sentence;
* the idempotency key is anchored to the Telegram message identity
  (``user_id/chat_id/message_id``): redelivered updates dedupe at the
  durable queue, distinct commands stay distinct.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.bot.surface._ptb import bot_of, user_language_code
from nexus_ai_agent.i18n import i18n
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

#: Job type consumed by the worker lane (creative/render_jobs.py).
CREATIVE_RENDER_JOB_TYPE = "creative_render"

#: Workspace prefix under ``settings.creative_temp_dir`` (payload trust
#: boundary re-validated by the worker; mirrored from the slideshow pattern).
WORKSPACE_PREFIX = "creative_"

#: Product limits (mirrored to i18n params): a ≤30 s source keeps renders
#: bounded; 50 MiB keeps staging bounded. The worker re-asserts both.
MAX_DURATION_US = 30_000_000
MAX_MEDIA_BYTES = 50 * 1024 * 1024


# -- error taxonomy (typed, user-facing via i18n) --------------------------


class CreativeErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_MEDIA = "unsupported_media"
    LIMIT_EXCEEDED = "limit_exceeded"
    MEDIA_TOO_LARGE = "media_too_large"
    NOT_REPLIED = "not_replied"
    QUEUE_FAILED = "queue_failed"


@dataclass(frozen=True)
class CreativeFailure:
    code: CreativeErrorCode
    # i18n key — the surface ALWAYS resolves it through ``i18n.t`` before
    # replying; a key must never appear in a user-visible string.
    message_key: str = ""


# -- mapper: pure product logic (no Telegram) -------------------------------


@dataclass(frozen=True)
class CreativeRequest:
    command: str  # edit | caption | grade
    operation: str  # trim | speed | reverse | transcribe | burnin | exposure | lut | proxy | otio
    args: tuple[str, ...]
    media_file_id: str | None
    media_duration_s: float | None
    media_file_size: int | None = None


class CreativeSurfaceMapper:
    """Pure mapper: validate + normalize a user request.

    The allowed set is the *executable* matrix (task-166): every entry maps
    to a canonical pack operation the worker can actually run.  Session 3
    adds ``lut`` (shipped ``.cube`` assets + the ``lut3d`` lane instrument)
    and ``burnin`` (staged SRT + the ``subtitles`` lane instrument) — both
    refused here before those honest paths existed, accepted now that they do.
    """

    # Allowed ops per command (allow-list, closed set)
    ALLOWED: dict[str, frozenset[str]] = {
        "edit": frozenset({"trim", "speed", "reverse"}),
        "caption": frozenset({"transcribe", "burnin"}),
        "grade": frozenset({"exposure", "lut", "proxy", "otio"}),
    }
    # Operations that can run without a staged media file.
    MEDIALESS_OPS: frozenset[str] = frozenset({"otio"})

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
        if req.operation not in self.MEDIALESS_OPS and req.media_file_id is None:
            return CreativeFailure(
                CreativeErrorCode.NOT_REPLIED,
                message_key="creative.not_replied",
            )
        if req.media_duration_s is not None and req.media_duration_s * 1_000_000 > MAX_DURATION_US:
            return CreativeFailure(
                CreativeErrorCode.LIMIT_EXCEEDED,
                message_key="creative.limit_exceeded",
            )
        if req.media_file_size is not None and req.media_file_size > MAX_MEDIA_BYTES:
            return CreativeFailure(
                CreativeErrorCode.MEDIA_TOO_LARGE,
                message_key="creative.media_too_large",
            )
        return req

    def job_payload(
        self,
        req: CreativeRequest,
        *,
        user_id: int,
        chat_id: int,
        lang: str,
        idempotency_key: str,
        workspace_dir: str,
        input_path: str | None,
    ) -> dict[str, Any]:
        return {
            "command": req.command,
            "operation": req.operation,
            "args": list(req.args),
            "workspace_dir": workspace_dir,
            "input_path": input_path,
            "media_duration_us": (
                int(req.media_duration_s * 1_000_000) if req.media_duration_s is not None else None
            ),
            "user_id": user_id,
            "chat_id": chat_id,
            "lang": lang,
            "idempotency_key": idempotency_key,
            # Session 3: grade/* runs on the EXPERIMENTAL delivery pack, so
            # the surface opts those jobs in explicitly (the bus gate refuses
            # them otherwise).  caption/edit packs are AVAILABLE — no opt-in.
            "allow_experimental": req.command == "grade",
        }


def idempotency_key(user_id: int, chat_id: int, message_id: int | None) -> str:
    """Deterministic identity for one user request.

    ``message_id`` is Telegram's own delivery identity: webhook/polling
    redelivery of the SAME message repeats the same id, and the durable
    queue's UNIQUE key therefore collapses retries into one job. Two
    different commands are two different messages — never a collision.
    A message-less synthetic update (only possible in tests) receives an
    explicit random suffix, since there is nothing to dedupe against.
    """
    if message_id is None:
        from uuid import uuid4

        return f"creative:{user_id}:{chat_id}:ad-hoc:{uuid4().hex}"
    return f"creative:{user_id}:{chat_id}:{message_id}"


def _workspace_for(key: str, temp_root: str) -> Path:
    """Job-scoped workspace name, derived from the idempotency key so a
    redelivered message stages into byte-identical replacements of its own
    files instead of multiplying directories."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return Path(temp_root) / f"{WORKSPACE_PREFIX}{digest}"


# -- PTB glue (thin, duck-typed; never imports telegram) ---------------------


def build_creative_handlers(
    job_queue: JobQueuePort,
    mapper: CreativeSurfaceMapper | None = None,
) -> dict[str, Any]:
    """Return ``{command_name: handler}`` for EXACTLY edit/caption/grade.

    No helper objects in this map (the wave-4 ``mapper`` regression): the
    caller registers every value as a command handler, so the keys are the
    public command surface.
    """
    _mapper = mapper or CreativeSurfaceMapper()

    async def _reply(update: Any, text: str) -> None:  # noqa: ANN401
        message = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if message is not None:
            await message.reply_text(text)

    def _failure_text(failure: CreativeFailure, lang: str, command: str) -> str:
        if failure.message_key == "creative.invalid_operation":
            operations = " ".join(sorted(_mapper.ALLOWED.get(command, ())))
            return i18n.t(
                failure.message_key, lang=lang, command=command, operations=operations or "—"
            )
        if failure.message_key == "creative.limit_exceeded":
            return i18n.t(failure.message_key, lang=lang, max_seconds=MAX_DURATION_US // 1_000_000)
        if failure.message_key == "creative.media_too_large":
            return i18n.t(failure.message_key, lang=lang, max_mb=MAX_MEDIA_BYTES // (1024 * 1024))
        return i18n.t(failure.message_key, lang=lang)

    async def _stage_media(req: CreativeRequest, context: Any, workspace: Path) -> str | None:  # noqa: ANN401
        """Download the replied media into the job workspace (PTB glue)."""
        bot = bot_of(context)
        if bot is None or req.media_file_id is None:
            return None
        tg_file = await bot.get_file(req.media_file_id)
        suffix = Path(str(getattr(tg_file, "file_path", "") or "")).suffix or ".bin"
        target = workspace / f"input{suffix}"
        await tg_file.download_to_drive(target)
        return str(target)

    async def _dispatch(update: Any, context: Any, command: str) -> None:  # noqa: ANN401
        message = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if message is None:
            return
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        user_id = int(user.id) if user is not None and hasattr(user, "id") else 0
        chat_id = int(chat.id) if chat is not None and hasattr(chat, "id") else 0
        lang = i18n.detect_language(user_language_code(update))

        raw_args: list[str] = list(getattr(context, "args", []) or [])
        operation = raw_args[0].lower() if raw_args else ""
        op_args = tuple(raw_args[1:])

        media_file_id: str | None = None
        duration_s: float | None = None
        file_size: int | None = None
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            for attr in ("video", "document", "audio"):
                media = getattr(reply, attr, None)
                if media is not None and hasattr(media, "file_id"):
                    media_file_id = str(media.file_id)
                    duration_raw = getattr(media, "duration", None)
                    if duration_raw is not None:
                        try:
                            duration_s = float(duration_raw)
                        except (TypeError, ValueError):
                            duration_s = None
                    size_raw = getattr(media, "file_size", None)
                    if isinstance(size_raw, int) and size_raw > 0:
                        file_size = size_raw
                    break

        req = CreativeRequest(
            command=command,
            operation=operation,
            args=op_args,
            media_file_id=media_file_id,
            media_duration_s=duration_s,
            media_file_size=file_size,
        )
        mapped = _mapper.map(req)
        if isinstance(mapped, CreativeFailure):
            logger.info("creative_request_rejected", command=command, code=mapped.code.value)
            await _reply(update, _failure_text(mapped, lang, command))
            return

        key = idempotency_key(user_id, chat_id, getattr(message, "message_id", None))

        from nexus_ai_agent.config.settings import get_settings

        workspace = _workspace_for(key, get_settings().creative_temp_dir)
        input_path: str | None = None
        if mapped.operation not in CreativeSurfaceMapper.MEDIALESS_OPS:
            try:
                shutil.rmtree(workspace, ignore_errors=True)
                workspace.mkdir(parents=True, exist_ok=True)
                input_path = await _stage_media(mapped, context, workspace)
            except Exception:  # noqa: BLE001 - download errors are user-visible
                logger.warning("creative_media_stage_failed", command=command)
                shutil.rmtree(workspace, ignore_errors=True)
                await _reply(update, i18n.t("creative.media_download_failed", lang=lang))
                return
            if input_path is None:
                await _reply(update, i18n.t("creative.media_download_failed", lang=lang))
                return
        else:
            workspace.mkdir(parents=True, exist_ok=True)

        payload = _mapper.job_payload(
            mapped,
            user_id=user_id,
            chat_id=chat_id,
            lang=lang,
            idempotency_key=key,
            workspace_dir=str(workspace),
            input_path=input_path,
        )
        try:
            job_id = await job_queue.enqueue(
                job_type=CREATIVE_RENDER_JOB_TYPE,
                idempotency_key=key,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - surface queue failure to the user
            logger.warning("creative_enqueue_failed", command=command, error=str(exc))
            await _reply(update, i18n.t("creative.queue_failed", lang=lang))
            return

        queued_text = i18n.t(
            "creative.queued",
            lang=lang,
            command=command,
            operation=operation,
            job_id=job_id,
        )
        await _reply(update, queued_text)

    def _make(command: str) -> Any:  # noqa: ANN401
        async def handler(update: Any, context: Any) -> None:  # noqa: ANN401
            await _dispatch(update, context, command)

        handler.__name__ = f"creative_{command}_cmd"
        return handler

    return {
        "edit": _make("edit"),
        "caption": _make("caption"),
        "grade": _make("grade"),
    }
