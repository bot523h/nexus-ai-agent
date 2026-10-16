"""The agent store registry and its public listing (audit 2026-10-16).

``agents/store/specialized_agents.py`` is imported by ``bot/agent_handlers.py``
(reachable from ``bot/app.py``) and had no dedicated test.  The module is
data-shaped, so the behaviour worth pinning is the *registry contract* the rest
of the store depends on: unique ids, complete presentation metadata, a
non-trivial system prompt, and a listing that is stable, JSON-safe and does not
expose the prompt or any credential to the handler layer.
"""

from __future__ import annotations

import json
import re

from nexus_ai_agent.agents.store.agent_manager import AgentManager
from nexus_ai_agent.agents.store.base_agent import StoreAgent
from nexus_ai_agent.agents.store.specialized_agents import AGENTS

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def test_registry_is_not_empty_and_ids_are_slugs() -> None:
    assert len(AGENTS) >= 5, "the store ships a menu, not a single agent"
    for agent_id in AGENTS:
        assert ID_PATTERN.fullmatch(agent_id), f"{agent_id!r} is not a stable slug id"


def test_every_agent_carries_its_own_presentation_metadata() -> None:
    for agent_id, agent in AGENTS.items():
        assert isinstance(agent, type) and issubclass(agent, StoreAgent), (
            f"{agent_id} is not a StoreAgent subclass"
        )
        assert agent.name.strip(), f"{agent_id} has no display name"
        assert agent.emoji.strip(), f"{agent_id} has no emoji"
        assert agent.description.strip(), f"{agent_id} has no description"
        assert agent.category.strip(), f"{agent_id} has no category"
        assert len(agent.system_prompt.strip()) >= 20, f"{agent_id} has a placeholder prompt"
        assert agent.__name__.endswith("Agent"), f"{agent_id} uses an unexpected class name"


def test_agent_names_are_distinct_so_a_menu_cannot_show_twins() -> None:
    names = [agent.name for agent in AGENTS.values()]
    assert len(set(names)) == len(names)
    assert len({agent.__name__ for agent in AGENTS.values()}) == len(AGENTS)


def test_listing_is_json_safe_ordered_and_hides_the_prompt() -> None:
    listed = AgentManager.list_agents()

    assert [entry["id"] for entry in listed] == list(AGENTS)
    assert json.loads(json.dumps(listed)) == listed, "the listing crosses the JSON boundary"
    for entry in listed:
        assert set(entry) == {"id", "name", "emoji", "description", "category"}
        assert "system_prompt" not in entry
        assert all(isinstance(value, str) for value in entry.values())


def test_listing_ids_are_the_registry_keys_not_the_display_names() -> None:
    by_id = {entry["id"]: entry for entry in AgentManager.list_agents()}
    for agent_id, agent in AGENTS.items():
        assert by_id[agent_id]["name"] == agent.name
        assert by_id[agent_id]["emoji"] == agent.emoji
