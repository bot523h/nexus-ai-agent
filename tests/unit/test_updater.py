"""AutoUpdater contract: approval gate (Phase 0) + capability/result contract (A-07).

The update (``git pull --ff-only`` + ``pip install .``) must not run unless the
owner has approved a ``self_update`` request via the existing /approve flow,
must never block the event loop, and must say *exactly* what happened:
unsupported (nothing ran), already up to date (nothing installed), or updated
(restart required).  The capability probe is exercised against real git
repositories, not mocks.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlmodel import select

import nexus_ai_agent.agent.updater as updater_mod
from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.agent.updater import (
    AutoUpdater,
    UpdateCapability,
    probe_update_capability,
)
from nexus_ai_agent.storage.clock import naive_utcnow
from nexus_ai_agent.storage.db import get_session as real_get_session
from nexus_ai_agent.storage.models import PendingApproval


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point updater/approval ``get_session`` at a fresh sqlite file per test."""
    db_path = str(tmp_path / "app.sqlite")

    @asynccontextmanager
    async def bound_get_session():
        async with real_get_session(db_path) as session:
            yield session

    import nexus_ai_agent.agent.approval as approval_mod

    monkeypatch.setattr(updater_mod, "get_session", bound_get_session)
    monkeypatch.setattr(approval_mod, "get_session", bound_get_session)
    return bound_get_session


@pytest.fixture()
def steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on the mutating steps without touching the (real) capability probe."""
    calls: list[str] = []

    def fake_pull(root: Path) -> None:
        calls.append(f"pull:{root}")

    def fake_pip(root: Path) -> None:
        calls.append(f"pip:{root}")

    monkeypatch.setattr(updater_mod, "_git_pull", fake_pull)
    monkeypatch.setattr(updater_mod, "_pip_install", fake_pip)
    return calls


def _supported(root: Path, head: str = "a" * 40) -> UpdateCapability:
    return UpdateCapability(True, "test checkout", root, head)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def clone(tmp_path: Path) -> tuple[Path, Path]:
    """A real origin repository and a clean clone of it (with an upstream)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "main"], origin)
    _git(["config", "user.email", "ci@example.invalid"], origin)
    _git(["config", "user.name", "ci"], origin)
    (origin / "VERSION").write_text("1\n")
    _git(["add", "VERSION"], origin)
    _git(["commit", "-q", "-m", "one"], origin)
    checkout = tmp_path / "checkout"
    _git(["clone", "-q", str(origin), str(checkout)], tmp_path)
    _git(["config", "user.email", "ci@example.invalid"], checkout)
    _git(["config", "user.name", "ci"], checkout)
    return origin, checkout


async def _first_pending(temp_db: Any) -> PendingApproval | None:
    async with temp_db() as session:
        result = await session.exec(
            select(PendingApproval).where(
                PendingApproval.change_type == "self_update",
                PendingApproval.status == "pending",
            )
        )
        return result.first()


# ── approval gate ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_do_update_refused_without_approval(
    settings_override, temp_db, steps, tmp_path
) -> None:
    updater = AutoUpdater("v3.0.0", capability_probe=lambda: _supported(tmp_path))
    system = ApprovalSystem()
    ok, detail = await updater.do_update(system)

    assert ok is False
    assert "approval" in detail
    assert steps == []  # git pull / pip install must not have run

    row = await _first_pending(temp_db)
    assert row is not None
    assert row.status == "pending"
    assert row.change_type == "self_update"


@pytest.mark.asyncio
async def test_do_update_proceeds_after_owner_approval(
    settings_override, temp_db, steps, tmp_path, monkeypatch
) -> None:
    updater = AutoUpdater("v3.0.0", capability_probe=lambda: _supported(tmp_path, "a" * 40))
    system = ApprovalSystem()
    ok, _ = await updater.do_update(system)
    assert ok is False

    row = await _first_pending(temp_db)
    assert row is not None and row.id is not None
    assert await system.approve(row.id) is True

    monkeypatch.setattr(
        updater_mod,
        "_git",
        lambda args, cwd: subprocess.CompletedProcess(args, 0, stdout="b" * 40 + "\n", stderr=""),
    )
    ok, detail = await updater.do_update(system)
    assert ok is True
    assert detail == f"updated {'a' * 12} -> {'b' * 12}; restart required for the new code to run"
    assert steps == [f"pull:{tmp_path}", f"pip:{tmp_path}"]


@pytest.mark.asyncio
async def test_stale_approval_requires_fresh_approval(
    settings_override, temp_db, steps, tmp_path
) -> None:
    updater = AutoUpdater("v3.0.0", capability_probe=lambda: _supported(tmp_path))
    system = ApprovalSystem()
    ok, _ = await updater.do_update(system)
    assert ok is False

    row = await _first_pending(temp_db)
    assert row is not None and row.id is not None
    assert await system.approve(row.id) is True

    # Age the approval beyond the 30-minute window.
    async with temp_db() as session:
        stale = await session.get(PendingApproval, row.id)
        assert stale is not None
        stale.created_at = naive_utcnow() - timedelta(hours=1)
        session.add(stale)
        await session.commit()

    ok, detail = await updater.do_update(system)
    assert ok is False
    assert "approval" in detail
    assert steps == []


# ── A-07: capability + result contract ────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_is_refused_where_git_pull_cannot_work(
    settings_override, steps, tmp_path
) -> None:
    """The Docker image: package in site-packages, no ``.git`` anywhere above it.

    The old code ran ``git pull`` anyway and reported a generic failure (or,
    with a stray ``.git``, "update completed" for a no-op pull)."""
    site_packages = tmp_path / "site-packages" / "nexus_ai_agent"
    site_packages.mkdir(parents=True)
    capability = probe_update_capability(site_packages)
    assert capability.supported is False
    assert "not inside a git work tree" in capability.reason

    updater = AutoUpdater("v3.0.0", capability_probe=lambda: capability)
    ok, detail = await updater.do_update(None)
    assert ok is False
    assert detail.startswith("self-update unsupported: ")
    assert "rebuild and redeploy" in detail
    assert steps == []  # nothing was executed


def test_probe_requires_a_clean_checkout_with_an_upstream(clone: tuple[Path, Path]) -> None:
    origin, checkout = clone
    inner = checkout / "src" / "pkg"  # the package dir of an editable install
    inner.mkdir(parents=True)
    supported = probe_update_capability(inner)  # any path inside the work tree
    assert supported.supported is True
    assert supported.repo_root == checkout.resolve()
    assert supported.head == _git(["rev-parse", "HEAD"], checkout)
    assert "origin/main" in supported.reason

    (checkout / "VERSION").write_text("dirty\n")
    dirty = probe_update_capability(checkout)
    assert dirty.supported is False
    assert "local modifications" in dirty.reason
    _git(["checkout", "--", "VERSION"], checkout)

    _git(["checkout", "-q", "--detach"], checkout)  # no upstream to pull from
    detached = probe_update_capability(checkout)
    assert detached.supported is False
    assert "no upstream" in detached.reason


@pytest.mark.asyncio
async def test_real_pull_reports_up_to_date_then_updated(
    settings_override, clone: tuple[Path, Path], monkeypatch
) -> None:
    """End to end against real git: only ``pip install`` is stubbed."""
    origin, checkout = clone
    installs: list[Path] = []
    monkeypatch.setattr(updater_mod, "_pip_install", lambda root: installs.append(root))
    updater = AutoUpdater("v3.0.0", capability_probe=lambda: probe_update_capability(checkout))

    before = _git(["rev-parse", "HEAD"], checkout)
    ok, detail = await updater.do_update(None)
    assert ok is True
    assert detail == f"already up to date at {before[:12]}; nothing to install"
    assert installs == []  # a no-op pull installs nothing

    (origin / "VERSION").write_text("2\n")
    _git(["commit", "-q", "-am", "two"], origin)
    ok, detail = await updater.do_update(None)
    after = _git(["rev-parse", "HEAD"], checkout)
    assert after == _git(["rev-parse", "HEAD"], origin) != before
    assert ok is True
    assert (
        detail == f"updated {before[:12]} -> {after[:12]}; restart required for the new code to run"
    )
    assert installs == [checkout.resolve()]


@pytest.mark.asyncio
async def test_do_update_does_not_block_the_event_loop(settings_override, monkeypatch) -> None:
    """Audit A-07 (2026-10-15): ``git pull`` / ``pip install`` used to run
    synchronously inside the coroutine, freezing every other handler for the
    whole update. A slow subprocess must now leave the loop free."""
    import asyncio
    import time

    def slow_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("timeout"), "every subprocess step must be bounded"
        time.sleep(0.3)
        return subprocess.CompletedProcess(args[0], 0, stdout="b" * 40 + "\n", stderr="")

    monkeypatch.setattr("nexus_ai_agent.agent.updater.subprocess.run", slow_run)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        updater = AutoUpdater("v3.0.0", capability_probe=lambda: _supported(Path.cwd(), "a" * 40))
        ok, detail = await updater.do_update(None)
    finally:
        beat.cancel()
    assert ok is True, detail
    # pull + rev-parse + pip = three 0.3 s steps; a blocked loop records ~0 ticks.
    assert ticks >= 10, ticks


# ── A-07: semantic release comparison ─────────────────────────────────────────


def _releases(status: int, tag: str | None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/bot523h/nexus-ai-agent/releases/latest"
        if status != 200:
            return httpx.Response(status, json={"message": "Not Found"})
        return httpx.Response(200, json={"tag_name": tag})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "status", "tag", "expected"),
    [
        ("v3.0.0", 200, "v3.1.0", (True, "v3.1.0")),
        ("3.0.0", 200, "v3.0.0", (False, None)),  # same version, different spelling
        ("v3.0.0", 200, "v2.9.9", (False, None)),  # an *older* release is not an update
        ("v3.0.0", 200, "nightly", (False, None)),  # not a version
        ("v3.0.0", 404, None, (False, None)),  # no releases published
    ],
)
async def test_check_for_update_compares_versions_semantically(
    current: str, status: int, tag: str | None, expected: tuple[bool, str | None]
) -> None:
    updater = AutoUpdater(current, http_transport=_releases(status, tag))
    assert await updater.check_for_update() == expected


def test_pip_step_is_the_dockerfile_install(monkeypatch, tmp_path) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        updater_mod.subprocess, "run", lambda *a, **kw: seen.append({"argv": a[0], **kw})
    )
    updater_mod._pip_install(tmp_path)
    updater_mod._git_pull(tmp_path)
    assert seen[0]["argv"] == [sys.executable, "-m", "pip", "install", "."]
    assert seen[0]["cwd"] == tmp_path and seen[0]["timeout"] == 1200
    assert seen[1]["argv"] == ["git", "pull", "--ff-only"]
    assert seen[1]["cwd"] == tmp_path and seen[1]["timeout"] == 300
