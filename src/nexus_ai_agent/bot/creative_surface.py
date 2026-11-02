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
  durable queue, distinct commands stay distinct;
* the execution trace (task-215) is minted **here**, where the user request
  arrives, and never downstream: its identity is derived from the durable
  idempotency key the surface already owns, so a redelivered message keeps
  one causal identity instead of forking one per attempt. The worker only
  *verifies* that binding; it never mints.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.application.execution_trace import ExecutionTrace, mint_trace
from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.bot.surface._ptb import bot_of, user_language_code
from nexus_ai_agent.creative.render_jobs import (
    CreativeRenderError,
    refuse_symlinked_node,
)
from nexus_ai_agent.i18n import i18n
from nexus_ai_agent.jobs.creative_passport import CreativeRequestConflictError
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
    operation: str  # trim | speed | reverse | transcribe | exposure | proxy | otio
    args: tuple[str, ...]
    media_file_id: str | None
    media_duration_s: float | None
    media_file_size: int | None = None


class CreativeSurfaceMapper:
    """Pure mapper: validate + normalize a user request.

    The allowed set is the *executable* matrix (task-166): every entry maps
    to a canonical pack operation the worker can actually run. Operations
    with no honest execution path today (``lut`` — no .cube assets and no
    lane op; ``burnin`` — no subtitles op in the lane IR) are refused here,
    at the surface, instead of being accepted and faked later.
    """

    # Allowed ops per command (allow-list, closed set)
    ALLOWED: dict[str, frozenset[str]] = {
        "edit": frozenset({"trim", "speed", "reverse"}),
        "caption": frozenset({"transcribe"}),
        "grade": frozenset({"exposure", "proxy", "otio"}),
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
        trace_id: str,
        input_sha256: str | None = None,
    ) -> dict[str, Any]:
        return {
            "command": req.command,
            "operation": req.operation,
            "args": list(req.args),
            "workspace_dir": workspace_dir,
            "input_path": input_path,
            # P0 redelivery (task-nagar-workspace-redelivery-01): the content identity
            # of the staged bytes. It participates in the queue's canonical payload
            # fingerprint, so a redelivery with different media is a *changed
            # request* -- refused by the existing creative conflict guard instead of
            # silently reusing the original job. It is evidence, never authority: the
            # worker re-measures the file and refuses a mismatch.
            "input_sha256": input_sha256,
            "media_duration_us": (
                int(req.media_duration_s * 1_000_000) if req.media_duration_s is not None else None
            ),
            "user_id": user_id,
            "chat_id": chat_id,
            "lang": lang,
            "idempotency_key": idempotency_key,
            # Proof-carrying execution spine (task-215): the identity minted at
            # this surface. The worker re-derives it from the idempotency key
            # and refuses a row that does not match, so this value carries no
            # authority of its own.
            "trace_id": trace_id,
            # No lifecycle opt-in travels in the job row: the worker derives
            # the EXPERIMENTAL-pack opt-in from the canonical operation
            # (render_jobs.EXPERIMENTAL_OPT_IN_OPERATIONS, task-183).
        }


#: Surface identity for the proof-carrying execution spine (task-215). Closed
#: vocabulary: ``ExecutionTrace`` refuses an unknown surface rather than
#: silently forking the trace namespace. Kept identical to
#: ``creative/render_jobs.py::CREATIVE_SURFACE`` on purpose -- the worker
#: re-derives the identity under this exact name.
CREATIVE_SURFACE = "telegram.creative"


def execution_trace(*, user_id: int, chat_id: int, message_id: int | None) -> ExecutionTrace:
    """Mint the trace for one creative request at the surface that received it.

    The identity is derived from the durable idempotency key this surface
    already owns, so a redelivered Telegram message keeps ONE causal identity
    instead of minting a new one per delivery. ``actor_ref`` is a scope
    (``tg:<user_id>``), never a name, a handle, or a token.
    """
    return mint_trace(
        surface=CREATIVE_SURFACE,
        actor_ref=f"tg:{user_id}",
        idempotency_key=idempotency_key(user_id, chat_id, message_id),
        intent_ref=None,
    )


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
    """Job-scoped workspace name, derived from the idempotency key.

    The workspace is the *directory* that belongs to one logical request; it is
    created idempotently and never recursively deleted by the surface. The input
    files inside it are additionally content-addressed, so two deliveries with
    different bytes coexist instead of replacing each other.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return Path(temp_root) / f"{WORKSPACE_PREFIX}{digest}"


#: Prefix for this delivery's private staging directory. Deliberately does NOT
#: start with ``WORKSPACE_PREFIX``: the worker refuses any workspace name without
#: that prefix, so staging residue can never be mistaken for a job workspace.
STAGING_PREFIX = "staging_"

#: Suffixes are only used to pick a sensible filename; nothing is executed by name.
_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


class _StagingError(Exception):
    """Typed, user-facing staging failure local to this module."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}".strip(": "))
        self.code = code
        self.detail = detail


def _safe_suffix(raw: object) -> str:
    """Derive a conservative file suffix from Telegram's reported path."""
    suffix = Path(str(raw or "")).suffix
    if suffix and _SAFE_SUFFIX_RE.match(suffix):
        return suffix
    return ".bin"


def _lstat_regular(path: Path, label: str) -> os.stat_result:
    """``lstat`` and insist on a regular file — never a symlink, never a directory."""
    try:
        stat_result = os.lstat(path)
    except OSError as exc:
        raise _StagingError("media_missing", f"{label} missing: {path}") from exc
    if stat.S_ISLNK(stat_result.st_mode):
        raise _StagingError("media_missing", f"{label} is a symlink: {path}")
    if not stat.S_ISREG(stat_result.st_mode):
        raise _StagingError("media_missing", f"{label} is not a regular file: {path}")
    return stat_result


def _size_of(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        return os.fstat(descriptor).st_size
    finally:
        os.close(descriptor)


def _sha256_of_file(path: Path) -> str:
    """Measure the actual bytes, refusing to follow a symlink into another object."""
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        pass
    return digest.hexdigest()


def _refuse_symlinked_directory(path: Path, label: str) -> None:
    """Refuse a directory component that is a symlink (pre-planted redirect).

    The rule itself now lives with the worker (``render_jobs.refuse_symlinked_node``)
    so that the two ends of the queue cannot drift into two different definitions of
    a trusted path. This wrapper only translates the refusal into the surface's own
    typed error, keeping the user-facing behaviour unchanged.
    """
    try:
        refuse_symlinked_node(path, label)
    except CreativeRenderError as exc:
        raise _StagingError("media_missing", exc.detail) from exc


def _trusted_root(configured: str | os.PathLike[str]) -> Path:
    """Return the canonical trusted root, refusing a symlinked *configured* path.

    ``Path.resolve()`` is the wrong first step. It follows the link and returns the
    target, so every later containment check compares resolved-against-resolved and
    reports success while the bytes go somewhere else entirely. The configured path
    is therefore inspected **as written, before any resolution**.

    The trust boundary is deliberately drawn at the configured node itself, matching
    ``_refuse_symlinked_directory``: an ancestor is admin-owned path prefix, and
    refusing ancestors would break legitimate deployments whose root sits under a
    symlinked mount (macOS ``/tmp`` -> ``/private/tmp`` is the everyday case). An
    attacker able to swap an *ancestor* of the configured root already owns the
    host, and the resolved-to-resolved containment stays internally consistent for
    that case anyway.

    A missing root is *not* an error: the caller creates it (see
    ``_ensure_private_directory``, which keeps ``parents=True`` for exactly this
    reason, because ``get_settings()`` is ``lru_cache``d and only creates the
    directory once per process).
    """
    raw = Path(configured)
    _refuse_symlinked_directory(raw, "creative root")
    return raw.resolve(strict=False)


def _ensure_private_directory(path: Path, root: Path, label: str) -> Path:
    """Create a directory under *root* with owner-only permissions, atomically.

    The trusted root is created on demand (``parents=True``) before the child, and
    that ordering is load-bearing in both directions:

    * the child is created with ``parents=False`` so it can never silently
      materialise a missing ancestor chain — every component between it and the
      trusted root has already been proved to be a real, non-symlink directory;
    * the root itself MUST be created, because ``get_settings()`` is ``lru_cache``d
      and creates it only once per process. A temp reaper, an emptied volume, or an
      operator clearing scratch space would otherwise make every later creative
      request fail for the life of the process. The pre-change code got this
      resilience from ``workspace.mkdir(parents=True, exist_ok=True)``.
    """
    _refuse_symlinked_directory(root, f"{label} root")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise _StagingError("media_missing", f"{label} root cannot be created: {root}") from exc
    _refuse_symlinked_directory(root, f"{label} root")
    _harden_private_directory(root, label="root")
    _refuse_symlinked_directory(path.parent, f"{label} parent")
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    except OSError as exc:
        raise _StagingError("media_missing", f"{label} cannot be created: {path}") from exc
    _refuse_symlinked_directory(path, label)
    _harden_private_directory(path, label=label)
    resolved = path.resolve(strict=False)
    if not _is_within(resolved, root):
        raise _StagingError("media_missing", f"{label} escapes the creative root: {resolved}")
    return resolved


def _harden_private_directory(path: Path, *, label: str) -> None:
    """Enforce owner-only ``0o700`` on a private app-owned directory we just created.

    ``mkdir(mode=0o700, exist_ok=True)`` applies ``0o700`` (minus umask) only when
    it *creates* the directory: a directory that already exists — a leftover from an
    older deployment, a wider umask at first creation, a volume restored from a
    backup, or a deliberately loosened scratch root — keeps whatever mode it has.
    The private staging/workspace contract would then be silently unenforced, and
    another local principal could read or tamper with in-flight media.

    We repair the mode only when the node is a real directory we own; a node that is
    not ours, or a symlink, is refused rather than chmod'd (never follow a link while
    repairing). The result is re-``stat``ed so we act on the mode actually applied,
    not the mode we requested. This mirrors the sibling write path, which already
    uses ``os.fchmod`` to force permissions regardless of umask (IBPC ``session.py``).
    """
    try:
        stat_result = os.lstat(path)
    except OSError as exc:
        raise _StagingError("media_missing", f"{label} is unreadable: {path}") from exc
    if stat.S_ISLNK(stat_result.st_mode):
        raise _StagingError("media_missing", f"{label} is a symlink: {path}")
    if not stat.S_ISDIR(stat_result.st_mode):
        raise _StagingError("media_missing", f"{label} is not a directory: {path}")
    if stat_result.st_uid != os.geteuid():
        raise _StagingError("media_missing", f"{label} is not owned by this process: {path}")
    if stat.S_IMODE(stat_result.st_mode) != 0o700:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise _StagingError(
                "media_missing", f"{label} permissions cannot be tightened: {path}"
            ) from exc
        if stat.S_IMODE(os.lstat(path).st_mode) != 0o700:
            raise _StagingError("media_missing", f"{label} permissions not enforceable: {path}")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _publish_immutable(source: Path, workspace: Path, digest: str) -> Path:
    """Hard-link *source* to a content-addressed name with strict no-clobber.

    ``os.link`` fails with ``FileExistsError`` when the destination exists, which is
    precisely the atomic no-clobber primitive we need — ``os.replace`` would silently
    overwrite a live input and is therefore never used here. A pre-existing
    destination is accepted only when it is a regular, non-symlink file whose bytes
    hash to the same digest; anything else fails closed.
    """
    destination = workspace / f"input-{digest}{source.suffix}"
    _refuse_symlinked_directory(workspace, "workspace")
    try:
        os.link(source, destination, follow_symlinks=False)
        return destination
    except FileExistsError:
        pass
    except OSError as exc:
        raise _StagingError("media_missing", f"publication failed: {destination}") from exc

    # The immutable destination already exists: reuse it only if it is provably ours.
    _lstat_regular(destination, "existing input")
    if _sha256_of_file(destination) != digest:
        raise _StagingError("media_missing", f"existing input is corrupt: {destination}")
    return destination


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

    async def _stage_media(  # noqa: ANN401
        req: CreativeRequest, context: Any, workspace: Path, staging: Path
    ) -> tuple[str, str]:
        """Download into *private staging* and atomically publish an immutable input.

        Returns ``(published_path, sha256_hex)``. The bytes that are measured are the
        bytes that are published: the digest is computed from the staged file, then
        the file is hard-linked into its deterministic, content-addressed destination
        with **no clobber**. Nothing is ever written over an existing input.

        The SHA-256 covers the actual downloaded object, not Telegram's metadata, and
        the publication path is derived from the measured digest, so two deliveries
        with different bytes can never resolve to the same final input.
        """
        bot = bot_of(context)
        if bot is None or req.media_file_id is None:
            raise _StagingError("media_missing")
        tg_file = await bot.get_file(req.media_file_id)
        suffix = _safe_suffix(getattr(tg_file, "file_path", ""))

        staged = staging / f"download{suffix}"
        await tg_file.download_to_drive(staged)

        # The download is the only untrusted window. Measure it as a regular file.
        stat_result = _lstat_regular(staged, "staged download")
        digest = _sha256_of_file(staged)
        if stat_result.st_size > MAX_MEDIA_BYTES:
            raise _StagingError("media_too_large")
        if stat_result.st_size != _size_of(staged):
            raise _StagingError("media_missing", "staged download changed while measured")

        published = _publish_immutable(source=staged, workspace=workspace, digest=digest)
        return str(published), digest

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
        # Proof-carrying execution spine (task-215): minted here, verified by
        # the worker, never minted downstream.
        trace = execution_trace(
            user_id=user_id, chat_id=chat_id, message_id=getattr(message, "message_id", None)
        )
        logger.info(
            "creative_trace_minted",
            trace_id=trace.trace_id,
            command=command,
            idempotency_key=key,
        )

        from nexus_ai_agent.config.settings import get_settings

        # Trusted root resolution (P0 closeout 2026-11-02). The ORDER matters and
        # is load-bearing: the CONFIGURED path is inspected for symlinks *before*
        # resolve(), because resolve() canonicalizes a symlink away and the guard
        # downstream could then only ever see the real target. Reproduced before
        # this fix: a symlinked creative_temp_dir silently redirected every
        # creative download into an unrelated directory.
        #: Private staging owned EXCLUSIVELY by this delivery. Nothing outside this
        #: directory is ever deleted here — see the cleanup contract below.
        staging: Path | None = None
        try:
            try:
                temp_root = _trusted_root(get_settings().creative_temp_dir)
            except _StagingError as exc:
                # A refused trusted root is a typed, user-visible refusal: no job
                # row, no reply that claims a queued job, and no byte written.
                logger.warning("creative_root_refused", command=command, detail=exc.detail)
                await _reply(update, i18n.t("creative.media_download_failed", lang=lang))
                return
            workspace = _workspace_for(key, str(temp_root))
            input_path: str | None = None
            input_sha256: str | None = None
            try:
                workspace = _ensure_private_directory(workspace, temp_root, "workspace")
            except _StagingError as exc:
                logger.warning("creative_workspace_refused", command=command, detail=exc.detail)
                await _reply(update, i18n.t("creative.media_download_failed", lang=lang))
                return

            if mapped.operation not in CreativeSurfaceMapper.MEDIALESS_OPS:
                try:
                    staging = Path(
                        tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=str(temp_root))
                    ).resolve(strict=False)
                    if not _is_within(staging, temp_root):
                        raise _StagingError("media_missing", "staging escapes the creative root")
                    input_path, input_sha256 = await _stage_media(
                        mapped, context, workspace, staging
                    )
                except Exception:  # noqa: BLE001 - download errors are user-visible
                    logger.warning("creative_media_stage_failed", command=command)
                    await _reply(update, i18n.t("creative.media_download_failed", lang=lang))
                    return

            payload = _mapper.job_payload(
                mapped,
                user_id=user_id,
                chat_id=chat_id,
                lang=lang,
                idempotency_key=key,
                workspace_dir=str(workspace),
                input_path=input_path,
                input_sha256=input_sha256,
                trace_id=trace.trace_id,
            )
            try:
                job_id = await job_queue.enqueue(
                    job_type=CREATIVE_RENDER_JOB_TYPE,
                    idempotency_key=key,
                    payload=payload,
                )
            except CreativeRequestConflictError:
                # A redelivery of this message with DIFFERENT bytes. The admitted row
                # keeps its payload and its immutable input; this delivery is refused
                # explicitly rather than silently reported as the original job.
                logger.warning("creative_request_conflict", command=command, idempotency_key=key)
                await _reply(
                    update, i18n.t("creative.request_conflict", lang=lang, command=command)
                )
                return
            except Exception as exc:  # noqa: BLE001 - surface queue failure to the user
                logger.warning("creative_enqueue_failed", command=command, error=str(exc))
                await _reply(update, i18n.t("creative.queue_failed", lang=lang))
                return
        finally:
            # Cleanup ownership: this delivery deletes ONLY its own staging directory,
            # and only after the input has been hard-linked into its immutable
            # destination. An admitted job's input is never inside staging, so it can
            # never be removed here; a crash leaves at worst one orphaned
            # ``staging_*`` directory, which is strictly preferable to deleting
            # another job's input.
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

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
