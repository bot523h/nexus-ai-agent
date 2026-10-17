"""Self-update tooling (git pull + reinstall) behind owner approval.

Contract (audit A-07, closeout 2026-10-15)
-----------------------------------------
* The update runs **off the event loop** and every subprocess step is bounded.
* Before anything runs, :func:`probe_update_capability` decides whether this
  deployment *can* self-update at all.  The Docker image is built with
  ``.dockerignore`` excluding ``.git`` and installs the package into
  site-packages, so ``git pull`` there can only fail; the old code reported
  that as a generic "update failed" and, worse, reported ``git pull`` printing
  "Already up to date" as "update completed".  Now the result is explicit:
  ``self-update unsupported: <reason>`` (nothing is executed), ``already up to
  date at <sha>`` (nothing installed) or ``updated <old> -> <new>; restart
  required``.
* ``git pull --ff-only`` — a self-update never creates merge commits.
* Release comparison is semantic (``packaging.version``): a release that is
  *older* than the running version, or a tag that is not a version, is not an
  update.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version
from sqlalchemy import literal_column
from sqlmodel import select

import nexus_ai_agent
from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.storage.clock import naive_utcnow
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import PendingApproval

logger = logging.getLogger(__name__)

SELF_UPDATE_TYPE = "self_update"
_APPROVAL_WINDOW = timedelta(minutes=30)
_GIT_PROBE_TIMEOUT_S = 30
_GIT_TIMEOUT_S = 300
_PIP_TIMEOUT_S = 1200
_RELEASE_CHECK_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class UpdateCapability:
    """Whether — and from where — this process can update itself."""

    supported: bool
    reason: str
    repo_root: Path | None = None
    head: str | None = None


def _package_root() -> Path:
    return Path(nexus_ai_agent.__file__).resolve().parent


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_PROBE_TIMEOUT_S,
    )


def probe_update_capability(repo_root: Path | None = None) -> UpdateCapability:
    """Decide whether ``git pull`` is a meaningful operation here.

    Supported only when the running package lives inside a *clean* git work
    tree whose branch has an upstream.  Everything else (site-packages
    installs, container images built without ``.git``, detached or dirty
    checkouts) is reported as unsupported *before* any mutation.
    """
    root = repo_root or _package_root()
    if shutil.which("git") is None:
        return UpdateCapability(False, "git is not installed in this environment")
    try:
        top = _git(["rev-parse", "--show-toplevel"], root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return UpdateCapability(False, f"git probe failed: {exc}")
    if top.returncode != 0:
        return UpdateCapability(
            False,
            f"{root} is not inside a git work tree (container images ship without .git; "
            "rebuild and redeploy the image instead)",
        )
    toplevel = Path(top.stdout.strip())
    upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], toplevel)
    if upstream.returncode != 0:
        return UpdateCapability(
            False, "checkout has no upstream branch to pull from", repo_root=toplevel
        )
    status = _git(["status", "--porcelain", "--untracked-files=no"], toplevel)
    if status.stdout.strip():
        return UpdateCapability(
            False,
            "working tree has local modifications; refusing to pull over them",
            repo_root=toplevel,
        )
    head = _git(["rev-parse", "HEAD"], toplevel).stdout.strip()
    return UpdateCapability(
        True, f"git checkout at {toplevel} tracking {upstream.stdout.strip()}", toplevel, head
    )


def _git_pull(root: Path) -> None:
    subprocess.run(["git", "pull", "--ff-only"], check=True, timeout=_GIT_TIMEOUT_S, cwd=root)


def _pip_install(root: Path) -> None:
    # The project has no requirements.txt — install from pyproject, exactly
    # like the Dockerfile does.
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "."],
        check=True,
        timeout=_PIP_TIMEOUT_S,
        cwd=root,
    )


def _parse_version(tag: str | None) -> Version | None:
    if not tag:
        return None
    try:
        return Version(tag.strip().lstrip("vV"))
    except InvalidVersion:
        return None


class AutoUpdater:
    def __init__(
        self,
        current_version: str,
        repo: str = "bot523h/nexus-ai-agent",
        *,
        capability_probe: Callable[[], UpdateCapability] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.current_version = current_version
        self.repo = repo
        self._probe = capability_probe or probe_update_capability
        self._http_transport = http_transport

    async def check_for_update(self) -> tuple[bool, str | None]:
        """Check GitHub for the latest release; ``(True, tag)`` only if it is newer."""
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient(
            timeout=_RELEASE_CHECK_TIMEOUT_S, transport=self._http_transport
        ) as client:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                logger.error("Update check error: %s", exc)
                return False, None
        if resp.status_code != 200:
            logger.info("Update check: no release information (HTTP %s)", resp.status_code)
            return False, None
        tag = resp.json().get("tag_name")
        latest = _parse_version(tag)
        current = _parse_version(self.current_version)
        if latest is None or current is None:
            logger.warning(
                "Update check: cannot compare versions (latest=%r, current=%r)",
                tag,
                self.current_version,
            )
            return False, None
        if latest > current:
            return True, str(tag)
        return False, None

    async def do_update(self, approval: ApprovalSystem | None = None) -> tuple[bool, str]:
        """Perform ``git pull --ff-only`` and reinstall.

        If *approval* (an :class:`ApprovalSystem`) is provided, an *approved*
        ``PendingApproval`` with ``change_type="self_update"`` must exist and
        be newer than :data:`_APPROVAL_WINDOW`; otherwise a pending request is
        created (the owner is notified and can approve via ``/approve <id>``)
        and the update is refused.

        Passing ``approval=None`` skips the gate (dev/CLI use only — the bot
        command handler always passes a live ApprovalSystem).

        Returns a ``(success, detail)`` tuple; *detail* is one of
        ``update requires owner approval …``, ``self-update unsupported: …``,
        ``already up to date at <sha>``, ``updated <old> -> <new>; restart
        required …`` or ``update failed: …``.
        """
        if approval is not None:
            approved, detail = await self._ensure_self_update_approval(approval)
            if not approved:
                return False, detail

        capability = await asyncio.to_thread(self._probe)
        if not capability.supported or capability.repo_root is None:
            logger.warning("Self-update unsupported: %s", capability.reason)
            return False, f"self-update unsupported: {capability.reason}"
        root = capability.repo_root
        before = capability.head or ""

        try:
            # Off the event loop (audit A-07): `git pull` + `pip install .` take
            # minutes and used to freeze every other handler meanwhile. Each
            # step is bounded so a hung network call cannot wedge the bot.
            await asyncio.to_thread(_git_pull, root)
            after = (await asyncio.to_thread(_git, ["rev-parse", "HEAD"], root)).stdout.strip()
            if after == before:
                logger.info("Self-update: already up to date at %s", after)
                return True, f"already up to date at {after[:12]}; nothing to install"
            await asyncio.to_thread(_pip_install, root)
        except Exception as e:  # noqa: BLE001 — every failure mode is reported to the owner
            logger.error("Update failed: %s", e)
            return False, f"update failed: {e}"
        logger.info("Update completed (%s -> %s). Please restart the bot.", before, after)
        return (
            True,
            f"updated {before[:12]} -> {after[:12]}; restart required for the new code to run",
        )

    async def _ensure_self_update_approval(self, approval: ApprovalSystem) -> tuple[bool, str]:
        """Return (approved, detail) for the self-update approval gate."""
        now = naive_utcnow()  # stored representation — matches PendingApproval.created_at
        async with get_session() as session:
            result = await session.exec(
                select(PendingApproval)
                .where(
                    PendingApproval.change_type == SELF_UPDATE_TYPE,
                    PendingApproval.status == "approved",
                )
                .order_by(literal_column("created_at").desc())
            )
            latest = result.first()

        if latest is not None and latest.created_at >= now - _APPROVAL_WINDOW:
            return True, "approved"

        approval_id = await approval.request_approval(
            SELF_UPDATE_TYPE,
            f"Auto-update {self.current_version} -> latest (git pull + pip install .). "
            f"Approve with /approve <id>, then run /update again.",
        )
        return False, f"update requires owner approval (pending id={approval_id})"
