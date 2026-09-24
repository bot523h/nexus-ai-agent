"""Completion delivery for ``creative_render`` jobs.

The generic completion hook in ``bot/app.py`` delegates one job type here.
Contract (owner directive §6 and §8):

* a **verified** artifact is delivered as a document to the originating chat and
  its workspace is removed *after* the upload — delivery owns the file;
* a durable ``FAILED`` row (the only way the worker reports a failure) is
  rendered from the typed code in the row into the caller's language;
* no raw key, filesystem path, stack trace or internal exception ever reaches
  the chat: ``creative.failed.<code>`` is a closed vocabulary, and an unknown
  code degrades to ``creative.failed.internal``.

This module is **framework-free**: the transport arrives as a
:class:`CreativeSender`, and the Telegram implementation lives in the
composition root (``bot/app.py``, the one place already allowed to import the
client).  That keeps the delivery decision unit-testable without PTB *and*
keeps this file out of the frozen ``legacy_baseline.json`` debt ledger
(``tests/architecture/test_import_boundaries.py``).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Protocol

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.i18n import I18n
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

#: ``CreativeRenderError`` stores ``"[<code>] detail"``; only the code travels.
_CODE_RE = re.compile(r"^\[(?P<code>[a-z_]+)\]")

#: Every code the notifier can render (superset of the worker's vocabulary —
#: ``internal`` covers an untyped crash and an unknown future code).
KNOWN_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "unsupported_operation",
        "media_missing",
        "media_unusable",
        "render_failed",
        "ffmpeg_unavailable",
        "artifact_verification_failed",
        "timeout",
        "internal",
    }
)


class CreativeSender(Protocol):
    """Transport the notifier needs; implemented by the composition root."""

    async def send_document(self, chat_id: int, document: Path, caption: str) -> None: ...

    async def send_message(self, chat_id: int, text: str) -> None: ...


def failure_code(completion: Any, result: dict[str, object]) -> str:  # noqa: ANN401 - queue type
    """Extract the typed code from a FAILED row's error text."""
    for candidate in (completion.error, result.get("error_code"), result.get("error")):
        if not candidate:
            continue
        match = _CODE_RE.match(str(candidate).strip())
        code = match.group("code") if match else str(candidate).strip()
        if code in KNOWN_FAILURE_CODES:
            return code
    return "internal"


def _contained_workspace(raw: object) -> Path | None:
    """Return the job workspace iff it lives under the configured temp dir.

    The notifier deletes after delivery, so it deletes only what it is provably
    allowed to delete — never a path chosen by a crafted payload.
    """
    if raw is None:
        return None
    try:
        resolved = Path(str(raw)).resolve()
    except (OSError, RuntimeError):
        return None
    if not resolved.is_dir():
        return None
    if not resolved.is_relative_to(Path(get_settings().creative_temp_dir).resolve()):
        return None
    return resolved


def _short_digest(value: object) -> str:
    text = str(value or "")
    text = text.removeprefix("sha256:")
    return text[:12] if text else "-"


async def notify_creative_completion(completion: Any, sender: CreativeSender) -> None:  # noqa: ANN401
    """Deliver one creative outcome to its origin chat; clean the workspace.

    Never raises: the queue already persists the terminal state before calling
    this hook, and a broken notifier must not turn a durable ``FAILED`` into a
    success (or the other way round).
    """
    payload = dict(completion.payload or {})
    result = completion.result if isinstance(completion.result, dict) else {}
    i18n = I18n()
    lang = str(payload.get("lang") or "en")
    workspace = _contained_workspace(payload.get("workspace_dir"))
    job_id = str(getattr(completion, "job_id", "-"))
    try:
        raw_chat = payload.get("chat_id")
        if raw_chat is None:
            logger.info("creative job %s finished without an origin chat", job_id)
            return
        chat_id = int(str(raw_chat))
        failed = completion.status is JobStatus.FAILED or not result.get("success")
        if not failed:
            artifact = Path(str(result.get("output_path", "")))
            if artifact.is_file() and artifact.is_relative_to(workspace or artifact.parent):
                await sender.send_document(
                    chat_id,
                    artifact,
                    i18n.t(
                        "creative.completed",
                        lang=lang,
                        command=str(result.get("command", "")),
                        operation=str(result.get("operation", "")),
                        job_id=job_id,
                        sha256=_short_digest(result.get("output_sha256")),
                    ),
                )
                return
            logger.error("creative job %s completed without a deliverable file", job_id)
            failed_code = "internal"
        else:
            failed_code = failure_code(completion, result)
        await sender.send_message(
            chat_id,
            i18n.t(f"creative.failed.{failed_code}", lang=lang, job_id=job_id),
        )
    except Exception:  # noqa: BLE001 - the completion hook never re-raises
        logger.exception("creative completion notification failed for job %s", job_id)
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)


__all__ = ["KNOWN_FAILURE_CODES", "CreativeSender", "failure_code", "notify_creative_completion"]
