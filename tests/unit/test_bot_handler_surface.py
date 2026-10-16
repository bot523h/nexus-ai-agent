"""Behaviour of the PTB handler modules that had no dedicated test (audit 2026-10-16).

``bot/agent_handlers.py``, ``bot/tool_handlers.py``, ``bot/monitor_handlers.py``
and ``bot/knowledge_handlers.py`` are all registered by
``bot/handlers.build_handlers`` and reachable from ``bot/app.py``.  Until this
file they were exercised only indirectly (registration counts in
``test_handlers.py``), so nothing pinned the two properties that matter:

* **authorization before action** — a non-owner must not be able to construct or
  call the approval system at all, not merely see a refusal message;
* **the reply is the engine's answer** — the text a user sees must come from the
  engine/tool that was called, and the engine must be *closed* even when it
  raises (``knowledge_handlers`` opens a client per command).

The doubles record every Bot API call and every constructor call, so a fixed
string or a skipped ``close()`` fails the test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, Mock

import pytest

from nexus_ai_agent.bot import agent_handlers, knowledge_handlers, monitor_handlers, tool_handlers

OWNER = 4242
INTRUDER = 99


def make_update(user_id: int = OWNER, *, with_message: bool = True) -> Any:
    message = SimpleNamespace(reply_text=AsyncMock()) if with_message else None
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id),
        callback_query=None,
    )


def make_context(args: list[str] | None = None) -> Any:
    return SimpleNamespace(args=args or [], bot=SimpleNamespace())


@pytest.fixture()
def owner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER))
    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.features import owner_control

    settings_module.get_settings.cache_clear()
    owner_control._owner_id = None  # type: ignore[attr-defined]


# ── authorization before action ─────────────────────────────────────────────


async def test_non_owner_cannot_reach_the_approval_system(
    monkeypatch: pytest.MonkeyPatch, owner_env: None
) -> None:
    approval = Mock()
    monkeypatch.setattr(monitor_handlers, "ApprovalSystem", approval)
    update = make_update(INTRUDER)

    await monitor_handlers.approve_cmd(update, make_context(["7"]))
    await monitor_handlers.reject_cmd(update, make_context(["7"]))

    approval.assert_not_called()
    update.message.reply_text.assert_not_awaited()


async def test_missing_message_is_a_no_op_for_every_monitor_command(owner_env: None) -> None:
    update = make_update(OWNER, with_message=False)
    await monitor_handlers.health_cmd(update, make_context())
    await monitor_handlers.approve_cmd(update, make_context(["1"]))
    await monitor_handlers.reject_cmd(update, make_context(["1"]))
    assert update.message is None  # nothing to assert on: no exception, no call


# ── monitor handlers ────────────────────────────────────────────────────────


async def test_health_reports_the_numbers_the_monitor_returned(
    monkeypatch: pytest.MonkeyPatch, owner_env: None
) -> None:
    status = {
        "ram_mb": 512.5,
        "ram_percent": 42.0,
        "disk_free_gb": 7.5,
        "uptime_hours": 3.25,
        "healthy": False,
    }
    monitor = Mock()
    monitor.check_health = AsyncMock(return_value=status)
    monkeypatch.setattr(monitor_handlers, "SelfMonitor", Mock(return_value=monitor))
    update = make_update()

    await monitor_handlers.health_cmd(update, make_context())

    text = update.message.reply_text.await_args.args[0]
    assert "512.5" in text
    assert "42.0" in text
    assert "3.2" in text  # 3.25 formatted with one decimal
    assert "نیازمند بررسی" in text, "unhealthy must not read as healthy"


async def test_approve_requires_a_numeric_id_and_reports_the_outcome(
    monkeypatch: pytest.MonkeyPatch, owner_env: None
) -> None:
    system = Mock()
    system.approve = AsyncMock(return_value=True)
    monkeypatch.setattr(monitor_handlers, "ApprovalSystem", Mock(return_value=system))
    update = make_update()

    await monitor_handlers.approve_cmd(update, make_context(["not-a-number"]))
    assert "شناسه باید عدد باشد" in update.message.reply_text.await_args.args[0]
    system.approve.assert_not_awaited()

    await monitor_handlers.approve_cmd(update, make_context(["7"]))
    system.approve.assert_awaited_once_with(7)
    assert "7" in update.message.reply_text.await_args.args[0]


async def test_approve_reports_a_vanished_request_without_raising(
    monkeypatch: pytest.MonkeyPatch, owner_env: None
) -> None:
    system = Mock()
    system.approve = AsyncMock(return_value=False)
    monkeypatch.setattr(monitor_handlers, "ApprovalSystem", Mock(return_value=system))
    update = make_update()

    await monitor_handlers.approve_cmd(update, make_context(["404"]))

    assert "یافت نشد" in update.message.reply_text.await_args.args[0]


# ── knowledge handlers ──────────────────────────────────────────────────────


class FakeKnowledgeManager:
    """Records ``learn``/``wiki`` calls and whether the client was closed."""

    instances: ClassVar[list[FakeKnowledgeManager]] = []
    learn_error: Exception | None = None
    wiki_content: str | None = "خلاصه ویکی"
    wiki_error: Exception | None = None

    def __init__(self) -> None:
        self.closed = False
        self.wiki = SimpleNamespace(fetch_summary=self._fetch_summary)
        FakeKnowledgeManager.instances.append(self)

    async def learn(self, query: str) -> str:
        if FakeKnowledgeManager.learn_error is not None:
            raise FakeKnowledgeManager.learn_error
        return f"یاد گرفتم: {query}"

    async def _fetch_summary(self, query: str) -> str | None:
        if FakeKnowledgeManager.wiki_error is not None:
            raise FakeKnowledgeManager.wiki_error
        return FakeKnowledgeManager.wiki_content

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def knowledge(monkeypatch: pytest.MonkeyPatch) -> type[FakeKnowledgeManager]:
    FakeKnowledgeManager.instances = []
    FakeKnowledgeManager.learn_error = None
    FakeKnowledgeManager.wiki_content = "خلاصه ویکی"
    FakeKnowledgeManager.wiki_error = None
    monkeypatch.setattr(knowledge_handlers, "KnowledgeManager", FakeKnowledgeManager)
    return FakeKnowledgeManager


async def test_learn_replies_with_the_engine_summary_and_closes(
    knowledge: type[FakeKnowledgeManager],
) -> None:
    update = make_update()
    await knowledge_handlers.learn_cmd(update, make_context(["هوش", "مصنوعی"]))

    first, second = update.message.reply_text.await_args_list
    assert "هوش مصنوعی" in first.args[0], "the acknowledgement names the topic"
    assert second.args[0] == "یاد گرفتم: هوش مصنوعی"
    assert knowledge.instances
    assert knowledge.instances[0].closed is True


async def test_learn_closes_the_client_even_when_the_engine_raises(
    knowledge: type[FakeKnowledgeManager],
) -> None:
    knowledge.learn_error = RuntimeError("fetch failed")
    update = make_update()

    await knowledge_handlers.learn_cmd(update, make_context(["موضوع"]))

    assert "خطا در یادگیری" in update.message.reply_text.await_args.args[0]
    assert knowledge.instances[0].closed is True, "a raised learn() must not leak the client"


async def test_learn_without_topic_asks_for_one_and_never_builds_a_client(
    knowledge: type[FakeKnowledgeManager],
) -> None:
    update = make_update()
    await knowledge_handlers.learn_cmd(update, make_context([]))

    assert "موضوع" in update.message.reply_text.await_args.args[0]
    assert knowledge.instances == []


async def test_wiki_reports_an_empty_answer_instead_of_a_blank_message(
    knowledge: type[FakeKnowledgeManager],
) -> None:
    knowledge.wiki_content = None
    update = make_update()

    await knowledge_handlers.wiki_cmd(update, make_context(["Python"]))

    assert "مطلبی پیدا نشد" in update.message.reply_text.await_args.args[0]
    assert knowledge.instances[0].closed is True


# ── tool handlers ───────────────────────────────────────────────────────────


async def test_weather_renders_the_tool_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = Mock()
    tool.get_weather = AsyncMock(
        return_value={"current_condition": [{"temp_C": "21", "weatherDesc": [{"value": "آفتابی"}]}]}
    )
    monkeypatch.setattr(tool_handlers, "WeatherTool", Mock(return_value=tool))
    update = make_update()

    await tool_handlers.weather_cmd(update, make_context(["Tehran"]))

    tool.get_weather.assert_awaited_once_with("Tehran")
    text = update.message.reply_text.await_args.args[0]
    assert "21" in text
    assert "آفتابی" in text


async def test_weather_without_a_city_asks_instead_of_calling_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = Mock()
    monkeypatch.setattr(tool_handlers, "WeatherTool", Mock(return_value=tool))
    update = make_update()

    await tool_handlers.weather_cmd(update, make_context([]))

    tool.get_weather.assert_not_called()
    assert "نام شهر" in update.message.reply_text.await_args.args[0]


async def test_weather_failure_is_reported_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = Mock()
    tool.get_weather = AsyncMock(return_value=None)
    monkeypatch.setattr(tool_handlers, "WeatherTool", Mock(return_value=tool))
    update = make_update()

    await tool_handlers.weather_cmd(update, make_context(["Nowhere"]))

    assert "خطا" in update.message.reply_text.await_args.args[0]


# ── agent store surface ─────────────────────────────────────────────────────


async def test_agents_menu_offers_every_agent_and_a_stop_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.agents.store.agent_manager import AgentManager

    agents = [
        {"id": "coding", "name": "Coding", "emoji": "👨‍💻", "description": "d", "category": "Tech"},
        {"id": "study", "name": "Study", "emoji": "📚", "description": "d", "category": "Edu"},
        {"id": "tutor", "name": "Tutor", "emoji": "🎓", "description": "d", "category": "Edu"},
    ]
    monkeypatch.setattr(AgentManager, "list_agents", staticmethod(lambda: agents))
    update = make_update()

    await agent_handlers.agents_cmd(update, make_context())

    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks == [
        "agent_select_coding",
        "agent_select_study",
        "agent_select_tutor",
        "agent_stop",
    ]
    assert update.message.reply_text.await_args.kwargs["parse_mode"] == "Markdown"


async def test_agents_menu_is_not_sent_without_a_message() -> None:
    update = make_update(with_message=False)
    await agent_handlers.agents_cmd(update, make_context())
    assert update.message is None
