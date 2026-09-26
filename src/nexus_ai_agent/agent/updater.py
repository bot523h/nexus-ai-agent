"""Self-update tooling (git pull + reinstall) behind owner approval.

Hardening notes (read before touching ``_run``)
-----------------------------------------------
``do_update`` is an ``async def`` that used to call ``subprocess.run`` twice::

    subprocess.run(["git", "pull"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "."], check=True)

Four separate defects were stacked in those two lines, and they compound:

1. **The event loop was blocked.** ``subprocess.run`` is synchronous. A
   ``pip install .`` routinely takes 30-120 seconds, and for that entire window
   the bot answered *nobody* — not the owner who typed ``/update``, not the
   Telegram keep-alive, not the webhook. From the outside the bot was simply
   down.
2. **There was no timeout.** ``subprocess.run`` without ``timeout=`` waits
   forever.
3. **stdin was inherited.** If the remote asked for credentials, ``git`` blocks
   on a terminal prompt that can never be answered in a daemon. Combined with
   (1) and (2) that is a permanent, unrecoverable freeze of the whole bot — no
   crash, no restart, no log line.
4. **Output was discarded.** With ``check=True`` and no capture, the owner got
   ``Command '['git', 'pull']' returned non-zero exit status 1`` and nothing
   else: no "merge conflict", no "detached HEAD", no "permission denied".

The replacement runs each command through :func:`_run`, which uses
``asyncio.create_subprocess_exec`` (never a shell), pins ``cwd`` to the
repository root instead of whatever directory the process happened to start in,
closes stdin, sets ``GIT_TERMINAL_PROMPT=0`` so git fails fast instead of
prompting, enforces a per-command timeout, and returns the captured output so a
failure is reportable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import literal_column
from sqlmodel import select

from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.core.timeutil import utcnow
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import PendingApproval

logger = logging.getLogger(__name__)

SELF_UPDATE_TYPE = "self_update"
_APPROVAL_WINDOW = timedelta(minutes=30)

#: Per-command wall-clock ceiling. ``git pull`` is network-bound and ``pip
#: install .`` compiles wheels, so the budget is generous — but finite.
GIT_TIMEOUT_SECONDS = 120
PIP_TIMEOUT_SECONDS = 900

#: Bytes of captured output kept for the failure report. A broken ``pip
#: install`` can emit megabytes; the tail is where the error is.
_OUTPUT_TAIL = 2000


def repo_root() -> Path:
    """Directory the update commands run in.

    ``git pull`` and ``pip install .`` are both relative to the working
    directory, and a long-lived bot's CWD is whatever systemd, Docker or the
    operator's shell happened to set. Anchoring to the package location makes
    the update deterministic — and makes ``pip install .`` install *this*
    checkout rather than whatever happens to sit in ``$PWD``.
    """
    return Path(__file__).resolve().parents[3]


async def _run(command: list[str], *, cwd: Path, timeout: int) -> tuple[int, str]:
    """Run *command* off the event loop. Returns ``(returncode, output)``.

    Never raises for a non-zero exit — the caller decides what a failure means.
    A timeout kills the process group and is reported as a distinct exit code.
    """
    env = dict(os.environ)
    # Fail fast instead of blocking on an interactive credential prompt that
    # no daemon can ever answer.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "")
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PIP_INPUT", "0")

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError) as exc:
        return -1, f"could not start {command[0]!r}: {exc}"

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return -2, f"timed out after {timeout}s"

    output = stdout.decode("utf-8", errors="replace").strip()
    return process.returncode or 0, output[-_OUTPUT_TAIL:]


def _normalise_version(value: str | None) -> str:
    """Compare ``3.13.0`` and ``v3.13.0`` as the same version.

    The running version comes from ``importlib.metadata`` (``3.13.0``, no
    prefix) while GitHub release tags conventionally carry one (``v3.13.0``).
    A bare ``!=`` therefore reported "update available" on every single check,
    forever, for a bot that was already current.
    """
    return (value or "").strip().lstrip("vV")


class AutoUpdater:
    def __init__(self, current_version: str, repo: str = "bot523h/nexus-ai-agent") -> None:
        self.current_version = current_version
        self.repo = repo

    async def check_for_update(self) -> tuple[bool, str | None]:
        """Check GitHub for the latest release.

        Goes through :func:`~nexus_ai_agent.core.http_client.get_http_client`
        rather than a bare ``httpx.AsyncClient()`` so the call inherits the
        project's timeout, retry policy, per-host circuit breaker and SSRF
        guard — the same treatment every other outbound call in this codebase
        gets. A raw client here meant one unreachable GitHub could be retried
        on every poll with no backoff.
        """
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        try:
            from nexus_ai_agent.core.http_client import get_http_client

            payload = await get_http_client().get_json(url)
        except Exception as exc:  # noqa: BLE001 - network boundary
            # Deliberately distinguished in the log: a failed *check* is not
            # the same fact as "you are up to date", even though the tuple
            # shape cannot express the difference for legacy callers.
            logger.warning("update_check_failed url=%s error=%s", url, exc)
            return False, None

        latest = payload.get("tag_name") if isinstance(payload, dict) else None
        if not latest:
            logger.warning("update_check_no_tag_name url=%s", url)
            return False, None
        if _normalise_version(latest) != _normalise_version(self.current_version):
            return True, str(latest)
        return False, None

    async def do_update(self, approval: ApprovalSystem | None = None) -> tuple[bool, str]:
        """Perform git pull and reinstall.

        If *approval* (an :class:`ApprovalSystem`) is provided, an *approved*
        ``PendingApproval`` with ``change_type="self_update"`` must exist and
        be newer than :data:`_APPROVAL_WINDOW`; otherwise a pending request is
        created (the owner is notified and can approve via ``/approve <id>``)
        and the update is refused.

        Passing ``approval=None`` skips the gate (dev/CLI use only — the bot
        command handler always passes a live ApprovalSystem).

        Returns a ``(success, detail)`` tuple. On failure *detail* now carries
        the captured output of the command that failed, so the owner can act
        on it instead of receiving a bare exit status.
        """
        if approval is not None:
            approved, detail = await self._ensure_self_update_approval(approval)
            if not approved:
                return False, detail

        cwd = repo_root()
        steps: tuple[tuple[str, list[str], int], ...] = (
            ("git pull", ["git", "pull", "--ff-only"], GIT_TIMEOUT_SECONDS),
            # The project has no requirements.txt — install from pyproject,
            # exactly like the Dockerfile does.
            ("pip install", [sys.executable, "-m", "pip", "install", "."], PIP_TIMEOUT_SECONDS),
        )

        for label, command, timeout in steps:
            code, output = await _run(command, cwd=cwd, timeout=timeout)
            if code != 0:
                logger.error("update_step_failed step=%s code=%s output=%s", label, code, output)
                return False, f"update failed at `{label}` (exit {code}):\n{output}"
            logger.info("update_step_ok step=%s", label)

        logger.info("Update completed. Please restart the bot.")
        return True, "update completed"

    async def _ensure_self_update_approval(self, approval: ApprovalSystem) -> tuple[bool, str]:
        """Return (approved, detail) for the self-update approval gate."""
        now = utcnow()  # naive UTC — matches PendingApproval.created_at
        async with get_session() as session:
            result = await session.execute(
                select(PendingApproval)
                .where(
                    PendingApproval.change_type == SELF_UPDATE_TYPE,
                    PendingApproval.status == "approved",
                )
                .order_by(literal_column("created_at").desc())
            )
            latest = result.scalars().first()

        if latest is not None and latest.created_at >= now - _APPROVAL_WINDOW:
            return True, "approved"

        approval_id = await approval.request_approval(
            SELF_UPDATE_TYPE,
            f"Auto-update {self.current_version} -> latest (git pull + pip install .). "
            f"Approve with /approve <id>, then run /update again.",
        )
        return False, f"update requires owner approval (pending id={approval_id})"
