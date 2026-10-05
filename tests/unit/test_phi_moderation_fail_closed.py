"""Moderation must fail CLOSED — an unparseable verdict is never "safe".

Regression for a fail-open in ``PhiAgent.moderate``: a handler that returned
``{"safe": True}`` on a JSON parse error would let a truncated or injected model
response silently suppress moderation.  Each test pins one way a verdict could
lie and asserts the observable dict.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus_ai_agent.agents.phi_agent import PhiAgent


class _StubLLM:
    """Returns a fixed raw string from ``generate``; nothing else is used."""

    def __init__(self, raw: object) -> None:
        self._raw = raw
        self.calls = 0

    async def generate(self, prompt: str, system: str = "") -> str:
        self.calls += 1
        if isinstance(self._raw, BaseException):
            raise self._raw
        return self._raw  # type: ignore[return-value]


def _moderate(raw: object) -> dict:
    return asyncio.run(PhiAgent(_StubLLM(raw)).moderate("some content"))


def test_explicit_safe_true() -> None:
    assert _moderate('{"safe": true, "reason": "ok"}') == {"safe": True, "reason": "ok"}


def test_explicit_safe_false() -> None:
    assert _moderate('{"safe": false, "reason": "policy"}') == {"safe": False, "reason": "policy"}


def test_unparseable_verdict_fails_closed() -> None:
    result = _moderate("I think it is probably fine, trust me")
    assert result["safe"] is False
    assert result["reason"] == "parse_error"


def test_empty_string_fails_closed() -> None:
    assert _moderate("")["safe"] is False


def test_non_object_json_fails_closed() -> None:
    assert _moderate("[1, 2, 3]")["safe"] is False
    assert _moderate('"safe"')["safe"] is False


def test_missing_safe_key_fails_closed() -> None:
    result = _moderate('{"reason": "looks ok"}')
    assert result["safe"] is False
    assert result["reason"] == "malformed_verdict"


@pytest.mark.parametrize("value", ["false", "no", 1, 0, None, [], {}])
def test_non_true_safe_value_is_not_safe(value: object) -> None:
    # Only JSON ``true`` counts as safe; a truthy string or a stray 1 cannot
    # read as "safe".
    import json

    result = _moderate(json.dumps({"safe": value, "reason": "x"}))
    assert result["safe"] is False
