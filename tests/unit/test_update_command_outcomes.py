"""``/update`` relays the updater's exact outcome to the owner (audit A-07).

Before the closeout every ``success=True`` was announced as "update done,
restart the bot" — including a no-op ``git pull`` — and an *unsupported*
deployment (the container image ships without ``.git``) was shown with the
"pending" hourglass.  The handler is exercised through its real entry point
with the monitor's two calls stubbed at the class boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.agent.self_monitor import SelfMonitor
from nexus_ai_agent.bot import update_handlers
from nexus_ai_agent.bot.update_handlers import update_cmd


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self) -> None:
        self.message = _FakeMessage()
        self.effective_user = SimpleNamespace(id=5_000_000_001)


@pytest.fixture()
def owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_handlers, "is_owner", lambda user_id: user_id == 5_000_000_001)

    async def newer(self: SelfMonitor, current_version: str) -> tuple[bool, str]:
        return True, "v9.9.9"

    monkeypatch.setattr(SelfMonitor, "check_for_update", newer)


_OUTCOMES = [
    ("updated 0632801abcde -> c7a1e5d2b9f3; restart required", True, "✅ updated 0632801abcde"),
    ("already up to date at 0632801abcde; nothing to install", True, "✅ already up to date"),
    ("self-update unsupported: /usr/local/lib is not inside a git work tree", False, "⛔ self-up"),
    ("update requires owner approval (pending id=7)", False, "⏳ update requires owner approval"),
    ("update failed: Command '['git', 'pull', '--ff-only']' timed out", False, "⏳ update failed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_prefix"),
    [((ok, detail), prefix) for detail, ok, prefix in _OUTCOMES],
)
async def test_update_cmd_relays_the_exact_outcome(
    owner: None,
    monkeypatch: pytest.MonkeyPatch,
    outcome: tuple[bool, str],
    expected_prefix: str,
) -> None:
    async def perform(self: SelfMonitor, current_version: str, approval: Any) -> tuple[bool, str]:
        return outcome

    monkeypatch.setattr(SelfMonitor, "perform_update", perform)
    update = _FakeUpdate()
    await update_cmd(update, SimpleNamespace(bot=SimpleNamespace()))  # type: ignore[arg-type]
    final = update.message.replies[-1]
    assert final.startswith(expected_prefix), final
    assert outcome[1] in final  # the detail is never swallowed


@pytest.mark.asyncio
async def test_update_cmd_refuses_non_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_handlers, "is_owner", lambda user_id: False)
    update = _FakeUpdate()
    await update_cmd(update, SimpleNamespace(bot=SimpleNamespace()))  # type: ignore[arg-type]
    assert update.message.replies == ["⛔ Only the bot owner can trigger updates."]
