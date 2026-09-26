"""Approval-gate and subprocess-hardening tests for AutoUpdater.

The update (git pull + pip install .) must not run unless the owner has
approved a ``self_update`` request via the existing /approve flow.

The command runner is patched at ``updater._run`` rather than at
``subprocess.run``: ``do_update`` is an ``async def`` and now shells out with
``asyncio.create_subprocess_exec`` so it does not block the event loop for the
30-120 seconds a real ``pip install .`` takes. See ``test_run_*`` at the bottom
for the properties that replaced the old bare ``subprocess.run`` call.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlmodel import select

from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.agent.updater import AutoUpdater
from nexus_ai_agent.core.timeutil import utcnow
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
    import nexus_ai_agent.agent.updater as updater_mod

    monkeypatch.setattr(updater_mod, "get_session", bound_get_session)
    monkeypatch.setattr(approval_mod, "get_session", bound_get_session)
    return bound_get_session


async def _first_pending(temp_db: Any) -> PendingApproval | None:
    async with temp_db() as session:
        result = await session.execute(
            select(PendingApproval).where(
                PendingApproval.change_type == "self_update",
                PendingApproval.status == "pending",
            )
        )
        return result.scalars().first()


@pytest.mark.asyncio
async def test_do_update_refused_without_approval(settings_override, temp_db, monkeypatch) -> None:
    calls: list[list[str]] = []

    async def _fake_run(command, *, cwd, timeout):
        calls.append(command)
        return 0, ""

    monkeypatch.setattr("nexus_ai_agent.agent.updater._run", _fake_run)

    updater = AutoUpdater("v3.0.0")
    system = ApprovalSystem()
    ok, detail = await updater.do_update(system)

    assert ok is False
    assert "approval" in detail
    assert calls == []  # git pull / pip install must not have run

    row = await _first_pending(temp_db)
    assert row is not None
    assert row.status == "pending"
    assert row.change_type == "self_update"


@pytest.mark.asyncio
async def test_do_update_proceeds_after_owner_approval(
    settings_override, temp_db, monkeypatch
) -> None:
    calls: list[list[str]] = []

    async def _fake_run(command, *, cwd, timeout):
        calls.append(command)
        return 0, ""

    monkeypatch.setattr("nexus_ai_agent.agent.updater._run", _fake_run)

    updater = AutoUpdater("v3.0.0")
    system = ApprovalSystem()
    ok, _ = await updater.do_update(system)
    assert ok is False

    row = await _first_pending(temp_db)
    assert row is not None and row.id is not None
    assert await system.approve(row.id) is True

    ok, detail = await updater.do_update(system)
    assert ok is True
    assert detail == "update completed"
    assert calls == [
        ["git", "pull", "--ff-only"],
        [sys.executable, "-m", "pip", "install", "."],
    ]


@pytest.mark.asyncio
async def test_do_update_without_approval_system_runs_directly(
    settings_override, temp_db, monkeypatch
) -> None:
    """approval=None is a dev/CLI path; the bot handler always passes one."""
    calls: list[list[str]] = []

    async def _fake_run(command, *, cwd, timeout):
        calls.append(command)
        return 0, ""

    monkeypatch.setattr("nexus_ai_agent.agent.updater._run", _fake_run)

    updater = AutoUpdater("v3.0.0")
    ok, _ = await updater.do_update(None)
    assert ok is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_stale_approval_requires_fresh_approval(
    settings_override, temp_db, monkeypatch
) -> None:
    calls: list[list[str]] = []

    async def _fake_run(command, *, cwd, timeout):
        calls.append(command)
        return 0, ""

    monkeypatch.setattr("nexus_ai_agent.agent.updater._run", _fake_run)

    updater = AutoUpdater("v3.0.0")
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
        stale.created_at = utcnow() - timedelta(hours=1)
        session.add(stale)
        await session.commit()

    ok, detail = await updater.do_update(system)
    assert ok is False
    assert "approval" in detail
    assert calls == []


# ── subprocess hardening ────────────────────────────────────────────────────
#
# These pin the four defects the old two-line `subprocess.run` pair carried.
# Each one was capable of freezing the entire bot, not just the /update call.


@pytest.mark.asyncio
async def test_run_does_not_raise_on_a_non_zero_exit() -> None:
    """`check=True` turned a normal git failure into an exception with no output."""
    from nexus_ai_agent.agent.updater import _run, repo_root

    code, output = await _run(
        [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"],
        cwd=repo_root(),
        timeout=30,
    )
    assert code == 3
    assert "boom" in output


@pytest.mark.asyncio
async def test_run_captures_output_for_the_failure_report() -> None:
    """The owner used to get an exit status and nothing else."""
    from nexus_ai_agent.agent.updater import _run, repo_root

    code, output = await _run(
        [sys.executable, "-c", "import sys; sys.stderr.write('stderr detail\\n'); sys.exit(1)"],
        cwd=repo_root(),
        timeout=30,
    )
    assert code == 1
    assert "stderr detail" in output, "stderr must be folded into the report"


@pytest.mark.asyncio
async def test_run_enforces_a_timeout() -> None:
    """Without a timeout a hung `git pull` freezes the bot forever."""
    from nexus_ai_agent.agent.updater import _run, repo_root

    code, output = await _run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=repo_root(),
        timeout=1,
    )
    assert code == -2
    assert "timed out" in output


@pytest.mark.asyncio
async def test_run_closes_stdin() -> None:
    """A subprocess that reads stdin must get EOF, not block on a prompt.

    This is the freeze that had no recovery path: `git` asking for credentials
    on an inherited terminal, inside an un-timed synchronous call, inside the
    event loop.
    """
    from nexus_ai_agent.agent.updater import _run, repo_root

    code, output = await _run(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
        cwd=repo_root(),
        timeout=10,
    )
    assert code == 0
    assert output == "''", f"stdin was not closed: {output!r}"


@pytest.mark.asyncio
async def test_run_disables_git_credential_prompts() -> None:
    from nexus_ai_agent.agent.updater import _run, repo_root

    code, output = await _run(
        [sys.executable, "-c", "import os; print(os.environ['GIT_TERMINAL_PROMPT'])"],
        cwd=repo_root(),
        timeout=10,
    )
    assert code == 0
    assert output == "0"


@pytest.mark.asyncio
async def test_run_does_not_block_the_event_loop() -> None:
    """The whole point: other coroutines keep running during an update.

    A synchronous `subprocess.run` inside `async def do_update` stopped every
    other task — every user's message — until `pip install .` finished.
    """
    import asyncio

    from nexus_ai_agent.agent.updater import _run, repo_root

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        code, _ = await _run(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            cwd=repo_root(),
            timeout=30,
        )
    finally:
        beat.cancel()

    assert code == 0
    assert ticks > 5, f"event loop was starved during the subprocess ({ticks} ticks)"


@pytest.mark.asyncio
async def test_run_reports_a_missing_binary_instead_of_raising() -> None:
    from nexus_ai_agent.agent.updater import _run, repo_root

    code, output = await _run(["definitely-not-a-real-binary-xyz"], cwd=repo_root(), timeout=5)
    assert code == -1
    assert "could not start" in output


@pytest.mark.asyncio
async def test_do_update_reports_the_failing_step(settings_override, temp_db, monkeypatch) -> None:
    """A failed update must name the step and carry the captured output."""

    async def _failing_run(command, *, cwd, timeout):
        return 1, "fatal: refusing to merge unrelated histories"

    monkeypatch.setattr("nexus_ai_agent.agent.updater._run", _failing_run)

    ok, detail = await AutoUpdater("v3.0.0").do_update(None)
    assert ok is False
    assert "git pull" in detail
    assert "unrelated histories" in detail


@pytest.mark.asyncio
async def test_do_update_stops_at_the_first_failure(
    settings_override, temp_db, monkeypatch
) -> None:
    """`pip install .` must not run against a tree `git pull` failed to update."""
    calls: list[list[str]] = []

    async def _failing_run(command, *, cwd, timeout):
        calls.append(command)
        return 1, "boom"

    monkeypatch.setattr("nexus_ai_agent.agent.updater._run", _failing_run)

    ok, _ = await AutoUpdater("v3.0.0").do_update(None)
    assert ok is False
    assert len(calls) == 1, f"kept going after a failed step: {calls}"


@pytest.mark.asyncio
async def test_update_commands_run_in_the_repository_root(
    settings_override, temp_db, monkeypatch
) -> None:
    """CWD is whatever started the daemon; `pip install .` must not trust it."""
    from nexus_ai_agent.agent.updater import repo_root

    seen: list = []

    async def _recording_run(command, *, cwd, timeout):
        seen.append(cwd)
        return 0, ""

    monkeypatch.setattr("nexus_ai_agent.agent.updater._run", _recording_run)

    ok, _ = await AutoUpdater("v3.0.0").do_update(None)
    assert ok is True
    assert seen and all(path == repo_root() for path in seen)
    assert (repo_root() / "pyproject.toml").is_file(), "repo_root() must point at the project"


@pytest.mark.asyncio
async def test_every_update_command_has_a_timeout(settings_override, temp_db, monkeypatch) -> None:
    timeouts: list[int] = []

    async def _recording_run(command, *, cwd, timeout):
        timeouts.append(timeout)
        return 0, ""

    monkeypatch.setattr("nexus_ai_agent.agent.updater._run", _recording_run)

    await AutoUpdater("v3.0.0").do_update(None)
    assert timeouts and all(isinstance(t, int) and t > 0 for t in timeouts)


# ── version comparison ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "tag", "expected"),
    [
        ("v3.13.0", "v3.13.0", False),
        ("3.13.0", "v3.13.0", False),  # the permanent false positive
        ("v3.13.0", "3.13.0", False),
        ("v3.13.0", "v3.14.0", True),
        ("3.13.0", "v3.14.0", True),
    ],
)
@pytest.mark.asyncio
async def test_version_comparison_ignores_the_tag_prefix(
    monkeypatch, current: str, tag: str, expected: bool
) -> None:
    """`running_version()` yields `3.13.0`; GitHub tags carry a `v`.

    A bare `!=` therefore announced an available update on every poll, forever,
    for a bot that was already on the newest release.
    """

    class _FakeClient:
        async def get_json(self, url: str, **kwargs):
            return {"tag_name": tag}

    monkeypatch.setattr("nexus_ai_agent.core.http_client.get_http_client", lambda: _FakeClient())

    needed, latest = await AutoUpdater(current).check_for_update()
    assert needed is expected
    assert (latest is not None) is expected


@pytest.mark.asyncio
async def test_check_for_update_survives_a_network_failure(monkeypatch) -> None:
    class _BrokenClient:
        async def get_json(self, url: str, **kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setattr("nexus_ai_agent.core.http_client.get_http_client", lambda: _BrokenClient())

    needed, latest = await AutoUpdater("v3.13.0").check_for_update()
    assert needed is False and latest is None


@pytest.mark.asyncio
async def test_check_for_update_uses_the_resilient_client(monkeypatch) -> None:
    """A bare `httpx.AsyncClient()` skips the retry policy, the per-host
    circuit breaker and the SSRF guard every other outbound call gets."""
    used: list[str] = []

    class _FakeClient:
        async def get_json(self, url: str, **kwargs):
            used.append(url)
            return {"tag_name": "v3.13.0"}

    monkeypatch.setattr("nexus_ai_agent.core.http_client.get_http_client", lambda: _FakeClient())

    await AutoUpdater("v3.13.0", repo="owner/repo").check_for_update()
    assert used == ["https://api.github.com/repos/owner/repo/releases/latest"]
