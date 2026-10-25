"""PhiAgent.moderate fails CLOSED (part of the Gate C fail-closed moderation slice).

An unparseable or malformed safety verdict must never read as ``safe``; a
non-boolean truthy value must not either.  The old behaviour returned
``{"safe": True}`` on a parse error, which let a truncated or injected model
response silently suppress moderation.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.agents.phi_agent import PhiAgent


class _ScriptedLLM:
    """A declared test double for the external model seam only."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def generate(self, prompt: str, system: str = "") -> str:  # noqa: ARG002
        return self._reply


async def _moderate(reply: str) -> dict:
    return await PhiAgent(_ScriptedLLM(reply)).moderate("anything")


@pytest.mark.asyncio
async def test_unparseable_verdict_fails_closed() -> None:
    assert await _moderate("not json at all") == {"safe": False, "reason": "parse_error"}


@pytest.mark.asyncio
async def test_malformed_verdict_fails_closed() -> None:
    assert await _moderate("[1, 2, 3]") == {"safe": False, "reason": "malformed_verdict"}
    assert await _moderate('{"reason": "ok"}') == {"safe": False, "reason": "malformed_verdict"}


@pytest.mark.asyncio
async def test_truthy_non_boolean_does_not_read_as_safe() -> None:
    # A model that answers with the string "false" (truthy in Python) or a
    # stray 1 must not be able to claim safety.
    assert (await _moderate('{"safe": "false", "reason": "x"}'))["safe"] is False
    assert (await _moderate('{"safe": 1, "reason": "x"}'))["safe"] is False


@pytest.mark.asyncio
async def test_explicit_json_true_is_the_only_safe() -> None:
    assert await _moderate('{"safe": true, "reason": "ok"}') == {"safe": True, "reason": "ok"}
    assert await _moderate('{"safe": false, "reason": "bad"}') == {"safe": False, "reason": "bad"}
