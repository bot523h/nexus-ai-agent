"""D4 completion notification for ``slideshow_render`` jobs (Wave 2.5).

The generic completion hook (``bot/app.py``) delegates one job type here:
a successful render is delivered as a document to the originating chat and
*then* its workspace is removed — delivery owns the file (r7 item 4) — while
any failure is rendered as a short plain message from the typed error code.
Telegram is imported lazily inside the send path so this module imports (and
is unit-testable with a stubbed ``telegram`` module) on machines without the
optional client installed.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.bot.slideshow import friendly_render_error, friendly_success
from nexus_ai_agent.config.settings import get_settings

logger = logging.getLogger(__name__)


def _typed_code(error: str | None) -> str | None:
    """Typed code persisted by the queue (``typed_failure:<code>``), if any."""
    from nexus_ai_agent.jobs.failure_semantics import parse_typed_failure_error

    return parse_typed_failure_error(error)


def _contained_workspace(raw: object) -> Path | None:
    """Return the job workspace iff it lives under the configured temp dir.

    The notifier deletes after delivery, so it deletes only what it is
    provably allowed to delete — never a path chosen by a crafted payload.
    """
    if raw is None:
        return None
    candidate = Path(str(raw))
    base = Path(get_settings().creative_temp_dir).resolve()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved.is_dir() and resolved.is_relative_to(base):
        return resolved
    return None


async def notify_slideshow_completion(completion: Any, token: str) -> None:
    """Send one slideshow outcome to its origin chat; clean the workspace.

    Fail-safe by contract: the queue already swallows hook exceptions, but
    cleanup runs in ``finally`` regardless, because a workspace nobody will
    ever see must not outlive the attempt to show it.
    """
    payload = completion.payload or {}
    result = completion.result if isinstance(completion.result, dict) else {}
    raw_chat = payload.get("chat_id")
    workspace = _contained_workspace(payload.get("workspace_dir"))
    try:
        if raw_chat is None:
            logger.info("slideshow job %s finished without an origin chat", completion.job_id)
            return
        chat_id = int(str(raw_chat))
        from telegram import Bot

        bot = Bot(token=token)
        # Source of truth = durable lifecycle state (task-181, §14): only
        # COMPLETED may deliver a success document.  ``result.success is
        # False`` is a second, independent refusal — a lying result can never
        # turn a failure status into a success notification.
        failed = completion.status is not JobStatus.COMPLETED or result.get("success") is False
        if not failed:
            from telegram import InputFile

            artifact = Path(str(result.get("output_path", "")))
            if artifact.is_file():
                # ``InputFile(path)`` opens — and closes — the file itself once
                # the upload completes; the workspace removal below is ours.
                await bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(str(artifact)),
                    caption=friendly_success(result),
                )
                return
            result = {"error_code": "internal"}
        if completion.status not in (
            JobStatus.FAILED_RETRYABLE,
            JobStatus.FAILED_TERMINAL,
            JobStatus.COMPLETED,  # defense: COMPLETED + success=False contradiction
        ):
            # Non-terminal durable state (VERIFYING at delivery time): never a
            # success delivery, and no premature failure copy either.
            logger.info(
                "slideshow job %s in non-terminal state %s — staying silent",
                completion.job_id,
                completion.status,
            )
            return
        code = result.get("error_code") or _typed_code(completion.error)
        if completion.status is JobStatus.FAILED_RETRYABLE:
            head = "⚠️ موقت (قابل تکرار) — "
        else:
            head = "❌ قطعی — "
        await bot.send_message(chat_id=chat_id, text=head + friendly_render_error(code))
    except Exception:  # noqa: BLE001 - the completion hook never re-raises
        logger.exception("slideshow completion notification failed for job %s", completion.job_id)
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)


__all__ = ["notify_slideshow_completion"]
